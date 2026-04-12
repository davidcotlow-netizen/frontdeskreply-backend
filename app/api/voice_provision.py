"""
Voice AI Provisioning — Frontdesk AI
Self-service endpoint that provisions Voice AI for a Pro plan business.
Chains: Build prompt → Create Retell LLM → Create Agent → Buy Twilio number → SIP trunk → Import to Retell → Save to Supabase
"""

import logging
import httpx
from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.core.database import get_db
from app.services.chat_service import get_business_chat_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice-provision"])


def build_voice_prompt(config: dict, plan_tier: str = "pro") -> str:
    """Build the Vela voice system prompt from business config + FAQs."""
    faqs = config.get("faqs", [])
    faq_text = "\n".join([f"Q: {f['question']}\nA: {f['answer']}" for f in faqs])
    business_name = config.get("name", "our business")
    phone = config.get("phone", "")
    city = config.get("city", "")
    tone = config.get("tone", "friendly and casual")

    multi_lang = ""
    if plan_tier in ("pro", "enterprise"):
        multi_lang = "\n12. MULTI-LANGUAGE: If the caller speaks any language other than English, respond entirely in that language for the rest of the call."

    return f"""You are Vela, the phone assistant for {business_name}{f' in {city}' if city else ''}.

## YOUR FAQ KNOWLEDGE BASE — THIS IS YOUR ONLY SOURCE OF TRUTH

You MUST answer caller questions using ONLY the FAQ answers below. When a caller asks a question, find the matching FAQ and use the facts from that answer. Use the specific dates, prices, and details written in the FAQ. Do not make up or invent any information.

{faq_text}

## RULES

1. ONLY answer from the FAQ above. If the FAQ has a specific date, say that date. If the FAQ has a specific price, say that price. NEVER say "we're finalizing", "coming soon", "I don't have that yet", or "we'll announce" when the FAQ already contains the answer.
2. If a question is NOT covered by any FAQ above, say you'll find out and get back to them.
3. Keep responses SHORT — under 30 words when possible. This is a phone call, not a text chat.
4. Be energetic, enthusiastic, warm, and {tone}. Use contractions and casual phrasing.
5. Do NOT mention you are an AI or chatbot. You are Vela, a chat assistant for {business_name}.
6. Never use emojis, bullet points, markdown, or URLs. This is spoken aloud.
7. NEVER tell the caller to call a phone number — they are ALREADY on the phone with you.
8. After answering, ask "Is there anything else I can help you with?"
9. If they say goodbye, say a warm goodbye naturally.
10. NAME PERSONALIZATION: Your opening asks for the caller's name. When they give it, say "Nice to meet you, [name]! Quick question — how did you hear about us?" After they answer, say "Thanks for letting me know! How can I help you today?" Use their name occasionally throughout. HOWEVER, if the CALLER HISTORY section below shows this is a returning caller, skip the name/source questions entirely — greet them warmly by name and ask how you can help.
11. TRANSITION VARIETY: Rotate through different transitions before answering. Never use the same transition twice per call. Sometimes skip the transition entirely and just answer directly.{multi_lang}

## CALLER HISTORY (if available)
{{{{caller_history}}}}

BUSINESS INFO: {business_name}{f', {city}' if city else ''}{f', {phone}' if phone else ''}

BOOKING: If the caller wants to book, tell them you will text them the booking link after the call."""


@router.post("/provision")
async def provision_voice_ai(business_id: str):
    """
    One-click Voice AI provisioning for Pro plan businesses.
    Creates Retell LLM + Agent, buys Twilio number, configures SIP trunk.
    """
    settings = get_settings()
    db = get_db()

    # ── 1. Validate business exists and is Pro ───────────────────
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").maybe_single().execute()

    if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") not in ("pro", "enterprise"):
        raise HTTPException(status_code=403, detail="Voice AI requires Pro or Enterprise plan")

    # Check if already provisioned
    existing = db.table("channels").select("id, external_identifier").eq(
        "business_id", business_id
    ).eq("channel_type", "voice").maybe_single().execute()

    if existing and existing.data and existing.data.get("external_identifier"):
        return {
            "status": "already_provisioned",
            "phone_number": existing.data["external_identifier"],
            "message": "Voice AI is already enabled for this business.",
        }

    # ── 2. Load business config + FAQs ───────────────────────────
    config = get_business_chat_config(business_id)
    if not config:
        raise HTTPException(status_code=404, detail="Business not found")

    business_name = config.get("name", "Business")
    city = config.get("city", "")

    # ── 3. Build voice prompt ────────────────────────────────────
    prompt = build_voice_prompt(config)
    logger.info(f"Built voice prompt for {business_name}: {len(prompt)} chars")

    # ── 4. Create Retell LLM ────────────────────────────────────
    retell_headers = {
        "Authorization": f"Bearer {settings.retell_api_key}",
        "Content-Type": "application/json",
    }

    try:
        llm_res = httpx.post("https://api.retellai.com/create-retell-llm", headers=retell_headers, json={
            "model": "claude-4.5-haiku",
            "general_prompt": prompt,
        }, timeout=30)

        if llm_res.status_code != 201:
            logger.error(f"Retell LLM creation failed: {llm_res.text}")
            raise HTTPException(status_code=500, detail="Failed to create voice AI model")

        llm_id = llm_res.json().get("llm_id")
        logger.info(f"Created Retell LLM: {llm_id}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Voice AI model creation timed out")

    # ── 5. Create Retell Agent ───────────────────────────────────
    try:
        agent_res = httpx.post("https://api.retellai.com/create-agent", headers=retell_headers, json={
            "response_engine": {"type": "retell-llm", "llm_id": llm_id},
            "voice_id": "11labs-Adrian",
            "agent_name": f"Vela - {business_name}",
            "language": "en-US",
            "interruption_sensitivity": 0.8,
            "responsiveness": 1.0,
            "enable_backchannel": True,
            "enable_recording": True,
            "begin_message": f"Thanks for calling {business_name}! I'm Vela, who do I have the pleasure of speaking with today?",
            "max_call_duration_ms": 300000,
            "end_call_after_silence_ms": 15000,
        }, timeout=30)

        if agent_res.status_code != 201:
            logger.error(f"Retell Agent creation failed: {agent_res.text}")
            raise HTTPException(status_code=500, detail="Failed to create voice AI agent")

        agent_id = agent_res.json().get("agent_id")
        logger.info(f"Created Retell Agent: {agent_id}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Voice AI agent creation timed out")

    # ── 6. Buy a Twilio number ───────────────────────────────────
    try:
        from twilio.rest import Client
        twilio_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        # Try to find a number in the business's area code / city
        area_code = None
        if config.get("phone"):
            digits = "".join(c for c in config["phone"] if c.isdigit())
            if len(digits) >= 10:
                area_code = digits[-10:-7]  # Extract area code

        search_params = {"voice_enabled": True, "sms_enabled": True, "country": "US"}
        if area_code:
            search_params["area_code"] = area_code

        available = twilio_client.available_phone_numbers("US").local.list(**search_params, limit=1)

        if not available:
            # Fallback to any US number
            available = twilio_client.available_phone_numbers("US").local.list(voice_enabled=True, limit=1)

        if not available:
            raise HTTPException(status_code=500, detail="No phone numbers available")

        # Buy the number
        purchased = twilio_client.incoming_phone_numbers.create(phone_number=available[0].phone_number)
        phone_number = purchased.phone_number
        phone_sid = purchased.sid
        logger.info(f"Bought Twilio number: {phone_number} (SID: {phone_sid})")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Twilio number purchase failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to purchase phone number: {str(e)}")

    # ── 7. Add number to SIP trunk ───────────────────────────────
    try:
        twilio_client.trunking.v1.trunks(settings.twilio_sip_trunk_sid).phone_numbers.create(
            phone_number_sid=phone_sid
        )
        logger.info(f"Added {phone_number} to SIP trunk")
    except Exception as e:
        logger.error(f"SIP trunk assignment failed: {e}")
        # Non-fatal — number is bought, we can fix SIP later

    # ── 8. Import number to Retell ───────────────────────────────
    try:
        import_res = httpx.post("https://api.retellai.com/import-phone-number", headers=retell_headers, json={
            "phone_number": phone_number,
            "termination_uri": "frontdeskreply.pstn.twilio.com",
            "inbound_agent_id": agent_id,
        }, timeout=15)

        if import_res.status_code != 201:
            logger.error(f"Retell phone import failed: {import_res.text}")
        else:
            logger.info(f"Imported {phone_number} to Retell with agent {agent_id}")
    except Exception as e:
        logger.error(f"Retell phone import error: {e}")

    # ── 9. Save to Supabase ──────────────────────────────────────
    # Store Retell IDs in the voice channel's config column.
    # This is the single source of truth for _sync_retell_prompt().
    db.table("channels").insert({
        "business_id": business_id,
        "channel_type": "voice",
        "external_identifier": f"retell:{agent_id}",
        "provider": "retell",
        "active": True,
        "config": {
            "retell_agent_id": agent_id,
            "retell_llm_id": llm_id,
            "voice_phone_number": phone_number,
            "voice_phone_sid": phone_sid,
        },
    }).execute()

    logger.info(f"Voice AI provisioned for {business_name}: {phone_number}")

    return {
        "status": "provisioned",
        "phone_number": phone_number,
        "agent_id": agent_id,
        "llm_id": llm_id,
        "message": f"Voice AI is live! Vela will answer calls at {phone_number}.",
    }


@router.get("/status")
async def voice_status(business_id: str):
    """Check if Voice AI is enabled for a business."""
    db = get_db()

    channel = db.table("channels").select("external_identifier").eq(
        "business_id", business_id
    ).eq("channel_type", "voice").maybe_single().execute()

    if channel and channel.data and channel.data.get("external_identifier"):
        return {
            "enabled": True,
            "phone_number": channel.data["external_identifier"],
        }

    return {"enabled": False, "phone_number": None}
