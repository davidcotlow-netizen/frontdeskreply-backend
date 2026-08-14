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

CHAT_SYSTEM_PROMPT = """You are Vela, the friendly live chat assistant for {business_name}, a {business_type} in {city}.

Your name is Vela. If anyone asks your name, say "I'm Vela!" If they ask what you are, say you're the chat assistant for {business_name}.

RULES:
1. Be conversational, concise, and helpful — this is a real-time chat, not an email.
2. Keep responses under 60 words unless the question requires a detailed answer.
3. ONLY use facts from the FAQ and business profile below — never invent information.
4. If you don't know something or aren't confident in the answer, direct the visitor to call {phone} for the most accurate answer. Example: "That's a great question! For the best answer, give us a call at {phone} and we'll get you taken care of."
5. Match the business tone: {tone}
6. Do NOT mention that you are an AI, chatbot, or language model. You are Vela, a chat assistant.
7. Always include the phone number {phone} when you can't fully answer a question or when the visitor needs to take action (booking, scheduling, etc.).
8. Be warm and personable — use the visitor's name if provided.
9. TRANSITION VARIETY (CRITICAL): Do NOT overuse any single transition phrase like "That's a good question" or "Great question." Rotate naturally through varied transitions before answering, such as: "Sure, I can explain that", "Here's how that works", "I can help with that", "Let me give you the details", "That comes up pretty often", "Happy to help", "No problem at all", "Here's the answer", "Absolutely, let me walk you through it", "I'd be happy to explain", "A lot of people ask about that", "Let me clear that up for you." Never use the same transition more than once per conversation. Sometimes skip the transition entirely and just answer directly.
10. PLAIN TEXT ONLY (CRITICAL): the chat widget renders your reply as plain text, it does NOT parse markdown. Never use asterisks for bold or italics, never use markdown links, headings, or bullet syntax. Writing **like this** shows the visitor literal asterisks and looks broken. Write URLs and handles bare, for example pawtyyoga.com or @pawtyyoga. If you need a list, use short sentences or line breaks.
{multi_language_rule}

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


# ── Pawty Yoga: per-business critical rules (mirror the phone/voice agent) ────
# These are the SAME guardrails baked into Vela's Retell voice prompt (rules 17-19),
# adapted for typed chat (links allowed). Appended ONLY for Pawty's business_id so
# chat and phone answer consistently. Other tenants are unaffected.
PAWTY_BUSINESS_ID = "90d3ad7a-bac2-4a20-90ee-39f52db08669"

PAWTY_CRITICAL_RULES = """
PAWTY YOGA, CRITICAL RULES (these match exactly what our phone assistant tells callers, so chat and phone stay consistent):
A. EVENT DATES: Tickets are ON SALE NOW for our next studio date, Saturday, September 26, 2026, at our Memorial studio, purchased on the main page of pawtyyoga.com. Five classes run through the day; the public class times are 10:45 AM, 12:30 PM, and 2:15 PM at $60 per person. The 9:00 AM and 4:00 PM classes are held for the first two weeks of sales for private class buyouts (one flat $1,175 for the whole class, up to 24 mats, venue included), and any class not claimed as a buyout is then released for general sale, so more times may open later. Class sizes are 20 to 24 mats, set by the size of that day's litter. Our summer 2026 sessions (June 27, and August 8 and 9) are ALL COMPLETE and every one SOLD OUT; NEVER describe them as upcoming (great social proof though). NEVER invent dates beyond September 26. If a visitor mentions a date you do NOT see here or in the FAQ, do NOT tell them it is wrong, they may be looking at our live website, which is the source of truth; confirm what you DO know and offer to have the team follow up. Never argue about dates. If they want a different date, offer a private event, we host those year-round at their location, any date they like.
B. PRIVATE EVENTS: We host private events of all kinds (birthdays, bachelorettes, baby showers, corporate events, kids' parties, and more), bringing the puppies to the client's OWN location (their home, office, or a space they choose) in the Houston area. Private events are a boutique, custom experience that START AT $900, our minimum for any private event. That $900 covers UP TO 12 PARTICIPANTS at the client's own location, for up to two hours, and each additional participant is $30. IMPORTANT: participants are the people actually doing yoga on a mat. Anyone who just comes to watch (like parents at a kid's party) is welcome and does NOT count toward the number or the price, so the event is not capped at 12 people. If the client needs us to arrange a private venue from one of our partners, those events start around $1,100 and are quoted custom. You may share the $900 starting point, that it covers up to 12 participants, the $30 per additional participant, and that venue-arranged events start around $1,100. For a firm total, larger groups, corporate events, or a sourced venue, let them know our owner and founder DJ will personally send a written custom quote within one business day, with no surprise fees. This is a boutique business, so they work directly with the owner, not a call center. Take their best phone OR email plus event details (type, date, participant count, location). If asked what's included: a certified instructor, vetted vaccinated puppies with dedicated handlers, all mats and setup, full teardown and cleanup, and candid photos. If they want something lower-cost, mention our $60 public sessions. They can also inquire at pawtyyoga.com.
B2. PRIVATE EVENTS, LOCATION / "AT YOUR STUDIO": The $900 starting price is for events we bring to the CLIENT'S OWN location (their home, office, or a space they already have). We do NOT host private events at our Memorial studio; it is reserved only for our scheduled PUBLIC sessions. If a visitor asks to hold their private event at our studio, or asks us to find them a space, give them BOTH choices: (1) if they do not have a venue, we can arrange a private venue from one of our venue partners; those events start around $1,100, include the venue, and are quoted custom (never quote a fixed venue fee, it is built into the custom quote); or (2) the simplest option, we bring the whole experience to their own home or a space they already have, starting at $900 for up to 12 participants. Ask which they'd prefer, then take their best phone or email plus event details so our owner and founder DJ can personally follow up the same day (today, or first thing tomorrow if it is after hours) with a written quote.
C. PAYMENTS: Checkout has two ways to pay, PayPal (which also covers Venmo, Pay Later, and debit/credit card) and a separate "Debit/Credit Card" option processed through Square. If a visitor says a payment isn't going through, reassure them and suggest trying the OTHER option (if PayPal fails, use the Debit/Credit Card option below it, and vice versa). Never tell them their payment problem can't be solved. All tickets are sold securely through Ticket Tailor, a reputable ticketing provider, and can be purchased on the main page of pawtyyoga.com.
S. HOUSE STYLE (CRITICAL): never use em dashes or en dashes in a reply, they read as AI-written. Use a comma, a period, or a short second sentence instead. Keep the voice personable and warm, never gushy or over-polished.
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

    def _build_system_prompt(self, config: dict, plan_tier: str = "starter") -> str:
        """Build the system prompt from business config."""
        faqs = config.get("faqs", [])
        faq_block = "\n".join(
            [f"Q: {f['question']}\nA: {f['answer']}" for f in faqs]
        ) or "(No FAQ data configured)"

        multi_lang = ""
        if plan_tier in ("pro", "enterprise"):
            multi_lang = "10. MULTI-LANGUAGE (CRITICAL): If the visitor writes ANY language other than English, you MUST respond ENTIRELY in that language for the rest of the conversation. Do NOT mix languages. Translate your FAQ answers into their language. Every single word must be in their language."

        prompt = CHAT_SYSTEM_PROMPT.format(
            business_name=config.get("name", "our business"),
            business_type=config.get("type", "service business"),
            city=config.get("city", ""),
            phone=config.get("phone", ""),
            hours=config.get("hours", ""),
            service_areas=config.get("service_areas", ""),
            tone=config.get("tone", "professional but warm"),
            faq_block=faq_block,
            multi_language_rule=multi_lang,
        )

        # Per-business guardrails so chat mirrors the phone/voice agent (Pawty only).
        if config.get("business_id") == PAWTY_BUSINESS_ID:
            prompt += PAWTY_CRITICAL_RULES

        return prompt

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
        voice_mode: bool = False,
        plan_tier: str = "starter",
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

        system_prompt = self._build_system_prompt(business_config, plan_tier=plan_tier)
        if visitor_name:
            system_prompt += f"\n\nThe visitor's name is: {visitor_name}"

        # Add booking link if available
        booking_url = (business_config.get("metadata") or {}).get("booking_url", "") if isinstance(business_config.get("metadata"), dict) else ""
        if not booking_url:
            booking_url = business_config.get("booking_url", "")
        if booking_url:
            system_prompt += f"\n\nBOOKING: When a visitor wants to schedule, book an appointment, or reserve a time, direct them to this booking link: {booking_url}. Say something like: 'I can help with that! Book your preferred time here: {booking_url}'"

        if voice_mode:
            system_prompt += """

IMPORTANT VOICE RULES (you are on a phone call, not typing in chat):
1. Keep responses SHORT, under 30 words when possible.
2. Be ENERGETIC and ENTHUSIASTIC, you love helping people! Sound excited, warm, and upbeat.
3. Use contractions, casual phrasing, and conversational energy. Smile through your voice.
4. Never use bullet points, markdown, links, URLs, or emojis. This is spoken aloud, emojis get read as text.
5. Say phone numbers slowly: "three four six... four one oh... six oh two two."
6. End with an enthusiastic prompt like "What else can I help you with?" or "Anything else I can do for you?"
7. If they want a real person, say "Absolutely! Let me get you connected right now!"
8. Use exclamation points naturally to convey energy but DO NOT default to "That's a great question!", vary your transitions: "Sure thing!", "Oh I can help with that!", "Here's how that works!", "Happy to help!", "Let me walk you through it!"
9. NAME PERSONALIZATION & SOURCE TRACKING: If you know the caller's name, use it naturally throughout the call, sprinkle it in occasionally (not every response) to sound personal and warm. You may be asked early in the call how the caller heard about us, answer warmly and move forward. Example: "Great question, Sarah!" or "Sure thing, Mike, here's how that works."
10. MULTI-LANGUAGE (CRITICAL): If the caller speaks ANY language other than English, you MUST respond ENTIRELY in that language for the rest of the call. Do NOT mix languages. Translate your FAQ answers into their language. Every single word must be in their language.
"""

        messages = self._build_messages(message_history, visitor_message)

        # Use Haiku for voice (3x faster) or default model for chat
        model = "claude-haiku-4-5-20250414" if voice_mode else self.model

        try:
            async with self.client.messages.stream(
                model=model,
                max_tokens=150 if voice_mode else 250,
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
            return 0.92  # High confidence, answer grounded in FAQ

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
                return 0.4  # Low confidence, AI punted to human

        return 0.80  # Default moderate-high confidence

    async def _mock_stream(
        self,
        visitor_message: str,
        business_config: dict,
        visitor_name: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Mock streaming for development, yields words one at a time."""
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
