"""
Chat AI Service — Frontdesk AI
Async streaming Claude integration for live chat.

Separate from ai_service.py (which is sync, for Celery workers).
This module uses AsyncAnthropic for WebSocket-compatible async streaming.
"""

import logging
import os
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)


# ── Mock mode detection (same pattern as ai_service.py) ──────────────────────

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
        "Chat AI Service: MOCK MODE ACTIVE — no Anthropic calls will be made."
    )
else:
    import anthropic


# ── System prompt for live chat ──────────────────────────────────────────────

CHAT_SYSTEM_PROMPT = """You are Milo, the friendly live chat assistant for {business_name}, a {business_type} in {city}.

Your name is Milo. If anyone asks your name, say "I'm Milo!" If they ask what you are, say you're the chat assistant for {business_name}.

RULES:
1. Be conversational, concise, and helpful — this is a real-time chat, not an email.
2. Keep responses under 60 words unless the question requires a detailed answer.
3. ONLY use facts from the FAQ and business profile below — never invent information.
4. If you don't know something or aren't confident in the answer, direct the visitor to call {phone} for the most accurate answer. Example: "That's a great question! For the best answer, give us a call at {phone} and we'll get you taken care of."
5. Match the business tone: {tone}
6. Do NOT mention that you are an AI, chatbot, or language model. You are Milo, a chat assistant.
7. Always include the phone number {phone} when you can't fully answer a question or when the visitor needs to take action (booking, scheduling, etc.).
8. Be warm and personable — use the visitor's name if provided.

BUSINESS INFO:
Name: {business_name}
Type: {business_type}
City: {city}
Phone: {phone}
Hours: {hours}
Service Areas: {service_areas}

FAQ KNOWLEDGE BASE:
{faq_block}
"""


# ── Chat AI class ────────────────────────────────────────────────────────────

class ChatAIService:
    def __init__(self):
        if MOCK_MODE:
            self.client = None
            self.model = "mock"
            logger.info("ChatAIService initialized in MOCK MODE")
        else:
            from app.core.config import get_settings
            settings = get_settings()
            self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            self.model = settings.claude_model
            logger.info(f"ChatAIService initialized in LIVE MODE (model={self.model})")

    def _build_system_prompt(self, config: dict) -> str:
        """Build the system prompt from business config."""
        faqs = config.get("faqs", [])
        faq_block = "\n".join(
            [f"Q: {f['question']}\nA: {f['answer']}" for f in faqs]
        ) or "(No FAQ data configured)"

        return CHAT_SYSTEM_PROMPT.format(
            business_name=config.get("name", "our business"),
            business_type=config.get("type", "service business"),
            city=config.get("city", ""),
            phone=config.get("phone", ""),
            hours=config.get("hours", ""),
            service_areas=config.get("service_areas", ""),
            tone=config.get("tone", "professional but warm"),
            faq_block=faq_block,
        )

    def _build_messages(self, message_history: list, visitor_message: str) -> list:
        """Convert chat history + new message into Claude messages format."""
        messages = []

        # Include recent history for context
        for msg in message_history[-20:]:  # last 20 messages max
            role = "user" if msg.get("role") == "visitor" else "assistant"
            messages.append({"role": role, "content": msg["content"]})

        # Add the new visitor message
        messages.append({"role": "user", "content": visitor_message})

        return messages

    async def stream_chat_response(
        self,
        business_config: dict,
        message_history: list,
        visitor_message: str,
        visitor_name: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream Claude's response token by token.
        Yields individual text chunks as they arrive.

        In mock mode, yields a canned response word by word.
        """
        if MOCK_MODE:
            async for chunk in self._mock_stream(visitor_message, business_config, visitor_name):
                yield chunk
            return

        system_prompt = self._build_system_prompt(business_config)
        if visitor_name:
            system_prompt += f"\n\nThe visitor's name is: {visitor_name}"

        messages = self._build_messages(message_history, visitor_message)

        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=250,
                system=system_prompt,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text

        except Exception as e:
            logger.error(f"Chat AI streaming error: {e}", exc_info=True)
            yield f"I apologize, I'm having a moment. Please call us at {business_config.get('phone', 'our office')} for immediate help."

    async def get_confidence_score(
        self,
        response_text: str,
        business_config: dict,
    ) -> float:
        """
        Estimate confidence in the AI response.
        Uses heuristic hedging detection (fast, no extra API call).

        Priority order:
        1. Check if response uses FAQ content (high confidence)
        2. Check for strong uncertainty phrases (low confidence)
        3. Default moderate confidence
        """
        text_lower = response_text.lower()

        # ── First: check if response references FAQ content (high confidence) ──
        faq_answers = [f.get("answer", "").lower() for f in business_config.get("faqs", [])]
        faq_match = False
        for answer in faq_answers:
            if len(answer) > 20:
                # Check if multiple key words from the FAQ answer appear in the response
                answer_words = [w for w in answer.split() if len(w) > 4][:8]
                matches = sum(1 for w in answer_words if w in text_lower)
                if matches >= 2:
                    faq_match = True
                    break

        if faq_match:
            return 0.92  # High confidence — answer grounded in FAQ

        # ── Second: check for strong uncertainty / deflection phrases ──
        # These indicate the AI genuinely doesn't know the answer
        low_confidence_phrases = [
            "let me connect you with",
            "i'm not sure about that",
            "i don't have that information",
            "i don't have specific details",
            "great question! let me connect",
            "for the most accurate answer",
            "i'd need to check on that",
            "i'm unable to confirm",
        ]
        for phrase in low_confidence_phrases:
            if phrase in text_lower:
                return 0.4  # Low confidence — AI punted to human

        return 0.80  # Default moderate-high confidence

    async def _mock_stream(
        self,
        visitor_message: str,
        business_config: dict,
        visitor_name: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Mock streaming for development — yields words one at a time."""
        import asyncio

        name = business_config.get("name", "our team")
        phone = business_config.get("phone", "our office")
        greeting = f"Hi{' ' + visitor_name if visitor_name else ''}! "

        body_lower = visitor_message.lower()
        if any(w in body_lower for w in ["price", "cost", "how much"]):
            response = f"{greeting}Great question! I'd love to help with pricing. The best way to get an accurate quote is to give us a call at {phone}. We'll get you taken care of! [MOCK]"
        elif any(w in body_lower for w in ["book", "schedule", "appointment", "session"]):
            response = f"{greeting}We'd love to get you booked! Check our website for availability or call us at {phone} to reserve your spot. [MOCK]"
        elif any(w in body_lower for w in ["hours", "open", "when"]):
            hours = business_config.get("hours", "standard business hours")
            response = f"{greeting}Our hours are {hours}. Feel free to reach out anytime! [MOCK]"
        else:
            response = f"{greeting}Thanks for reaching out to {name}! How can I help you today? If you need anything specific, just let me know or call us at {phone}. [MOCK]"

        # Yield word by word with small delays to simulate streaming
        words = response.split(" ")
        for i, word in enumerate(words):
            if i > 0:
                yield " "
            yield word
            await asyncio.sleep(0.05)


# ── Singleton accessor ────────────────────────────────────────────��──────────

_chat_ai_service: Optional[ChatAIService] = None


def get_chat_ai_service() -> ChatAIService:
    global _chat_ai_service
    if _chat_ai_service is None:
        _chat_ai_service = ChatAIService()
    return _chat_ai_service
