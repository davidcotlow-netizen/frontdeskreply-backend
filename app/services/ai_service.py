"""
AI Service — Frontdesk AI
All methods SYNCHRONOUS — Celery workers are sync; no event loop needed.
Manual retry on malformed JSON (3 attempts) with safe fallback.

MOCK MODE: Activated when ANTHROPIC_API_KEY is missing or equals "test-anthropic-key".
In mock mode, no Anthropic calls are made. Returns deterministic classifications
based on keyword matching. Return shape is identical to live mode.
"""

import json
import logging
import os
import time
from typing import Optional

from app.models.schemas import ClassificationResult, Intent, RecommendedAction, Sentiment

logger = logging.getLogger(__name__)

# ── Mock mode detection ───────────────────────────────────────────────────────
# Use pydantic_settings (which reads .env) rather than os.getenv (which does not).
# This ensures the .env file is loaded before the mock mode flag is set.

def _get_api_key() -> str:
    try:
        from app.core.config import get_settings
        return get_settings().anthropic_api_key
    except Exception:
        return os.getenv("ANTHROPIC_API_KEY", "")

_raw_key = _get_api_key()
MOCK_MODE: bool = (not _raw_key) or (_raw_key in ("test-anthropic-key", ""))

if MOCK_MODE:
    logger.warning(
        "⚠️  MOCK AI MODE ACTIVE — Anthropic API calls are disabled. "
        "Set ANTHROPIC_API_KEY to a real key for live mode."
    )
else:
    import anthropic  # only imported when we actually need it


CLASSIFY_SYSTEM_PROMPT = """You are a message classification engine for a home service business AI system.
Your job is to classify inbound customer messages and return a strict JSON object.
Return ONLY valid JSON. No markdown, no explanation, no preamble.
IMPORTANT: urgency_score MUST be an integer between 1 and 5 (1=low, 5=critical). Never use values outside this range."""

CLASSIFY_SCHEMA_HINT = """{
  "intent": "booking_request|quote_request|faq|complaint|emergency|billing|cancellation|speak_to_owner|unknown",
  "urgency_score": 3,
  "sentiment": "positive|neutral|frustrated|urgent|distressed",
  "confidence": 0.0,
  "requires_human_review": true,
  "recommended_action": "auto_send|approval_queue|escalate_immediately",
  "safe_to_auto_send": false,
  "reasoning_short": "one sentence max",
  "escalation_reason": null
}"""

DRAFT_SYSTEM_PROMPT = """You are the AI assistant for a home service business.
Draft professional, helpful responses to customer messages.

STRICT SOURCE-OF-TRUTH RULES:
1. NEVER invent appointment times, technician names, or specific pricing
2. ONLY use facts present in the business profile and FAQ data provided
3. If you don't know a specific fact, offer to follow up instead of guessing
4. Keep responses under 90 words
5. End with a single, clear next step for the customer
6. Match the business's stated tone exactly
7. Do NOT mention that you are an AI

Write the response text only. No subject line, no preamble."""


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _mock_classify(message_text: str, sender_name: Optional[str] = None) -> ClassificationResult:
    """
    Keyword-based deterministic classification. No API call.
    Return shape is identical to the live ClassificationResult.
    """
    body = message_text.lower()

    # Emergency check first — highest priority
    if any(w in body for w in ["emergency", "flood", "fire", "gas leak", "no heat", "burst pipe",
                                "95 degrees", "no ac", "infant", "baby", "dangerous"]):
        return ClassificationResult(
            intent=Intent.emergency,
            urgency_score=5,
            sentiment=Sentiment.distressed,
            confidence=0.91,
            requires_human_review=True,
            recommended_action=RecommendedAction.escalate_immediately,
            safe_to_auto_send=False,
            reasoning_short="[MOCK] Emergency keywords detected — escalating immediately.",
            escalation_reason="emergency_detected",
        )

    if any(w in body for w in ["stopped working", "not working", "broke", "broken",
                                "leak", "no hot water", "ac out", "heat out", "come today",
                                "come out", "need someone", "repair", "fix"]):
        return ClassificationResult(
            intent=Intent.booking_request,
            urgency_score=4,
            sentiment=Sentiment.urgent,
            confidence=0.88,
            requires_human_review=True,
            recommended_action=RecommendedAction.approval_queue,
            safe_to_auto_send=False,
            reasoning_short="[MOCK] Service request with high urgency detected.",
        )

    if any(w in body for w in ["quote", "price", "cost", "how much", "estimate", "rate",
                                "charge", "fee", "pricing"]):
        return ClassificationResult(
            intent=Intent.quote_request,
            urgency_score=2,
            sentiment=Sentiment.neutral,
            confidence=0.85,
            requires_human_review=True,
            recommended_action=RecommendedAction.approval_queue,
            safe_to_auto_send=False,
            reasoning_short="[MOCK] Pricing inquiry detected.",
        )

    if any(w in body for w in ["hours", "open", "available", "schedule", "appointment",
                                "when", "service area", "do you service", "do you cover"]):
        return ClassificationResult(
            intent=Intent.faq,
            urgency_score=1,
            sentiment=Sentiment.neutral,
            confidence=0.90,
            requires_human_review=False,
            recommended_action=RecommendedAction.auto_send,
            safe_to_auto_send=True,
            reasoning_short="[MOCK] FAQ-type question — safe to auto-respond.",
        )

    if any(w in body for w in ["complaint", "unhappy", "terrible", "awful", "refund",
                                "disappointed", "unacceptable", "manager", "owner"]):
        return ClassificationResult(
            intent=Intent.complaint,
            urgency_score=3,
            sentiment=Sentiment.frustrated,
            confidence=0.86,
            requires_human_review=True,
            recommended_action=RecommendedAction.approval_queue,
            safe_to_auto_send=False,
            reasoning_short="[MOCK] Complaint detected — requires human review.",
        )

    # Default fallback
    return ClassificationResult(
        intent=Intent.unknown,
        urgency_score=2,
        sentiment=Sentiment.neutral,
        confidence=0.60,
        requires_human_review=True,
        recommended_action=RecommendedAction.approval_queue,
        safe_to_auto_send=False,
        reasoning_short="[MOCK] No strong signal — routed to approval queue.",
    )


def _mock_draft(
    message_text: str,
    classification: ClassificationResult,
    business_profile: dict,
) -> str:
    """
    Generates a plausible draft reply without calling the API.
    Uses business name and phone from the profile if available.
    """
    name = business_profile.get("name", "our team")
    phone = business_profile.get("phone", "our office")
    intent = classification.intent

    templates = {
        Intent.emergency: (
            f"We received your urgent message and are prioritizing your request right now. "
            f"A technician will contact you within the next 30 minutes. "
            f"If you need immediate assistance, please call {phone} directly. "
            f"— {name} [MOCK DRAFT]"
        ),
        Intent.booking_request: (
            f"Thank you for reaching out to {name}! We received your service request and "
            f"will have a technician contact you shortly to schedule a visit. "
            f"For faster service, you can also call us at {phone}. [MOCK DRAFT]"
        ),
        Intent.quote_request: (
            f"Thanks for your interest in {name}! We'd be happy to provide a quote. "
            f"A team member will follow up within 1 business hour with pricing details. "
            f"Questions in the meantime? Call us at {phone}. [MOCK DRAFT]"
        ),
        Intent.faq: (
            f"Great question! We're happy to help. A member of the {name} team will "
            f"follow up shortly with the information you need. "
            f"You can also reach us directly at {phone}. [MOCK DRAFT]"
        ),
        Intent.complaint: (
            f"We sincerely apologize for your experience and take this very seriously. "
            f"A manager from {name} will contact you personally within the hour. "
            f"Please call {phone} if you'd like to speak with someone immediately. [MOCK DRAFT]"
        ),
    }

    return templates.get(
        intent,
        (
            f"Thank you for contacting {name}. We received your message and will "
            f"follow up shortly. For urgent matters, please call {phone}. [MOCK DRAFT]"
        ),
    )


# ── Main service class ────────────────────────────────────────────────────────

class AIService:
    def __init__(self):
        if MOCK_MODE:
            # Skip settings and SDK init entirely in mock mode
            self.client = None
            self.model = "mock"
            self.confidence_threshold = 0.75
            self.max_classify_tokens = 0
            self.max_draft_tokens = 0
            logger.info("AIService initialized in MOCK MODE")
        else:
            from app.core.config import get_settings
            settings = get_settings()
            self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            self.model = settings.claude_model
            self.confidence_threshold = settings.confidence_threshold
            self.max_classify_tokens = settings.max_classify_tokens
            self.max_draft_tokens = settings.max_draft_tokens
            logger.info(f"AIService initialized in LIVE MODE (model={self.model})")

    def classify_message(
        self,
        message_text: str,
        business_context: dict,
        sender_name: Optional[str] = None,
        _attempt: int = 0,
    ) -> ClassificationResult:

        if MOCK_MODE:
            logger.info(f"[MOCK] Classifying: {message_text[:80]}...")
            result = _mock_classify(message_text, sender_name)
            result = result.apply_safety_gates(self.confidence_threshold)
            logger.info(
                f"[MOCK] Classified: intent={result.intent} "
                f"conf={result.confidence:.2f} action={result.recommended_action}"
            )
            return result

        # ── Live path (unchanged from original) ──────────────────────────
        biz = business_context
        sender_label = sender_name or "Customer"

        user_prompt = (
            f"Business: {biz.get('name')} ({biz.get('type')}) in {biz.get('city')}\n"
            f"Hours: {biz.get('hours')}\n"
            f"Emergency policy: {biz.get('emergency_policy')}\n"
            f"Service areas: {biz.get('service_areas')}\n\n"
            f"Message from {sender_label}:\n\"{message_text}\"\n\n"
            f"Return classification as JSON matching this schema:\n{CLASSIFY_SCHEMA_HINT}"
        )

        logger.info(f"Classifying (attempt {_attempt+1}): {message_text[:80]}...")

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_classify_tokens,
                system=CLASSIFY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw_text = response.content[0].text.strip()
            cleaned = raw_text.replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            # Clamp urgency_score to valid range in case model returns out-of-bounds value
            if "urgency_score" in data:
                data["urgency_score"] = max(1, min(5, int(data["urgency_score"])))
            result = ClassificationResult(**data)

        except Exception as e:
            if _attempt < 2:
                logger.warning(f"Classification attempt {_attempt+1} failed ({e}), retrying...")
                time.sleep(1.5)
                return self.classify_message(message_text, business_context, sender_name, _attempt + 1)
            logger.error(f"Classification failed after 3 attempts: {e}")
            return ClassificationResult(
                intent=Intent.unknown,
                urgency_score=3,
                confidence=0.0,
                requires_human_review=True,
                safe_to_auto_send=False,
                recommended_action=RecommendedAction.approval_queue,
                reasoning_short="Classification failed — routed to human review",
            )

        result = result.apply_safety_gates(self.confidence_threshold)
        logger.info(
            f"Classified: intent={result.intent} conf={result.confidence:.2f} "
            f"action={result.recommended_action}"
        )
        return result

    def generate_draft(
        self,
        message_text: str,
        classification: ClassificationResult,
        business_profile: dict,
        faq_context: list,
        conversation_history: Optional[list] = None,
        _attempt: int = 0,
    ) -> str:

        if MOCK_MODE:
            logger.info(f"[MOCK] Generating draft for intent={classification.intent.value}")
            draft = _mock_draft(message_text, classification, business_profile)
            logger.info(f"[MOCK] Draft: {len(draft)} chars")
            return draft

        # ── Live path (unchanged from original) ──────────────────────────
        faq_block = "\n".join(
            [f"Q: {f['question']}\nA: {f['answer']}" for f in (faq_context or [])]
        ) or "(No FAQ data configured)"

        history_block = ""
        if conversation_history:
            history_block = "\n\nPrevious messages:\n"
            for msg in conversation_history[-5:]:
                label = "Customer" if msg.get("direction") == "inbound" else "Us"
                history_block += f"{label}: {msg.get('body', '')}\n"

        user_prompt = (
            f"Business: {business_profile.get('name')} | {business_profile.get('type')} | "
            f"{business_profile.get('city')}\n"
            f"Phone: {business_profile.get('phone')}\n"
            f"Hours: {business_profile.get('hours')}\n"
            f"Emergency: {business_profile.get('emergency_policy')}\n"
            f"Areas: {business_profile.get('service_areas')}\n"
            f"Tone: {business_profile.get('tone', 'professional but warm')}\n\n"
            f"FAQ Knowledge Base:\n{faq_block}"
            f"{history_block}\n\n"
            f"Message (intent: {classification.intent.value}, urgency: {classification.urgency_score}/5):\n"
            f"\"{message_text}\"\n\n"
            f"Draft a response following all source-of-truth rules."
        )

        logger.info(f"Generating draft for intent={classification.intent.value}")

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_draft_tokens,
                system=DRAFT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            draft = response.content[0].text.strip()
            logger.info(f"Draft: {len(draft)} chars")
            return draft

        except Exception as e:
            if _attempt < 1:
                logger.warning(f"Draft generation failed ({e}), retrying...")
                time.sleep(2)
                return self.generate_draft(
                    message_text, classification, business_profile,
                    faq_context, conversation_history, _attempt + 1
                )
            logger.error(f"Draft generation failed: {e}")
            name = business_profile.get("name", "us")
            phone = business_profile.get("phone", "our main number")
            return (
                f"Thank you for reaching out to {name}. We received your message and will "
                f"follow up shortly. If this is urgent, please call us at {phone}."
            )


# ── Singleton accessor ────────────────────────────────────────────────────────

_ai_service: Optional[AIService] = None


def get_ai_service() -> AIService:
    global _ai_service
    if _ai_service is None:
        _ai_service = AIService()
    return _ai_service