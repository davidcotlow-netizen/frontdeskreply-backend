"""
WhatsApp Chat — Frontdesk AI
Handles inbound WhatsApp messages with Vela AI responses.
Same FAQ-powered AI as voice, chat, and SMS — but via WhatsApp.

Twilio WhatsApp sends messages to this webhook.
We respond with Vela's AI answer via TwiML.
"""

import logging
import re
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Response

from app.core.database import get_db
from app.services.chat_service import get_business_chat_config, _find_or_create_contact
from app.services.chat_ai_service import get_chat_ai_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_for_message(text: str) -> str:
    """Clean up AI response for WhatsApp — keep it clean but allow some formatting."""
    text = text.replace("**", "*")  # Convert markdown bold to WhatsApp bold
    text = re.sub(r'[\U00010000-\U0010ffff]', '', text)  # Remove complex emojis
    return text.strip()


def get_business_by_whatsapp_number(phone_number: str) -> dict | None:
    """Look up which business owns a WhatsApp number."""
    db = get_db()
    # Check channels table for whatsapp type
    res = db.table("channels").select("business_id").eq(
        "channel_type", "whatsapp"
    ).eq("external_identifier", phone_number).maybe_single().execute()
    if res and res.data:
        return res.data

    # Also check voice channels (same number may serve both)
    res = db.table("channels").select("business_id").eq(
        "channel_type", "voice"
    ).eq("external_identifier", phone_number).maybe_single().execute()
    if res and res.data:
        return res.data

    return None


@router.post("/inbound")
async def whatsapp_inbound(request: Request):
    """
    Twilio hits this when a WhatsApp message arrives.
    Vela responds using the same FAQ knowledge base as chat and voice.
    """
    form = await request.form()
    from_number = form.get("From", "")  # "whatsapp:+1234567890"
    to_number = form.get("To", "")      # "whatsapp:+1234567890"
    body = form.get("Body", "").strip()
    profile_name = form.get("ProfileName", "")

    # Strip "whatsapp:" prefix for lookups
    clean_from = from_number.replace("whatsapp:", "")
    clean_to = to_number.replace("whatsapp:", "")

    logger.info(f"WhatsApp message: from={clean_from} to={clean_to} body={body[:100]}")

    if not body:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml"
        )

    # Look up business
    business = get_business_by_whatsapp_number(clean_to)
    if not business:
        logger.warning(f"No business found for WhatsApp number: {clean_to}")
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml"
        )

    business_id = business["business_id"]

    # Check Pro plan
    db = get_db()
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").maybe_single().execute()
    if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") not in ("pro", "enterprise"):
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml"
        )

    config = get_business_chat_config(business_id)
    if not config:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml"
        )

    # Save sender as a lead
    contact_name = profile_name or "WhatsApp User"
    _find_or_create_contact(db, business_id, phone=clean_from, name=contact_name)

    # Save inbound message to chat_messages via a WhatsApp session
    session_id = _get_or_create_whatsapp_session(db, business_id, clean_from, contact_name)
    _add_whatsapp_message(db, session_id, "visitor", body)

    # Get conversation history for context
    history = _get_whatsapp_history(db, session_id)

    # Generate Vela response
    ai_service = get_chat_ai_service()
    full_response = ""
    try:
        async for chunk in ai_service.stream_chat_response(
            business_config=config,
            message_history=history[:-1],
            visitor_message=body,
            voice_mode=False,
        ):
            full_response += chunk
    except Exception as e:
        logger.error(f"WhatsApp AI error: {e}")
        phone = config.get("phone", "")
        full_response = f"Thanks for reaching out! For the fastest help, give us a call at {phone}."

    full_response = strip_for_message(full_response)

    # Limit to WhatsApp-friendly length (1600 chars max)
    if len(full_response) > 1600:
        full_response = full_response[:1597] + "..."

    # Save Vela's response
    _add_whatsapp_message(db, session_id, "ai", full_response)

    logger.info(f"Vela WhatsApp reply: {full_response[:100]}")

    # Respond via TwiML
    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape_xml(full_response)}</Message></Response>'
    return Response(content=twiml, media_type="application/xml")


# ── WhatsApp Session Management ──────────────────────────────────────────────

def _get_or_create_whatsapp_session(db, business_id: str, phone: str, name: str) -> str:
    """Get existing active WhatsApp session or create new one."""
    # Look for active session with this phone number (within last 24 hours)
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    res = db.table("chat_sessions").select("id").eq(
        "business_id", business_id
    ).eq("visitor_email", f"whatsapp:{phone}").eq(
        "status", "active"
    ).gte("started_at", cutoff).maybe_single().execute()

    if res and res.data:
        return res.data["id"]

    # Create new session
    session = db.table("chat_sessions").insert({
        "business_id": business_id,
        "visitor_name": name,
        "visitor_email": f"whatsapp:{phone}",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "metadata": {"channel": "whatsapp", "phone": phone},
    }).execute()
    return session.data[0]["id"]


def _add_whatsapp_message(db, session_id: str, role: str, content: str):
    """Add a message to the WhatsApp conversation."""
    db.table("chat_messages").insert({
        "session_id": session_id,
        "role": role,
        "content": content,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def _get_whatsapp_history(db, session_id: str) -> list:
    """Get recent messages for context."""
    res = db.table("chat_messages").select("role, content").eq(
        "session_id", session_id
    ).order("sent_at", desc=False).limit(20).execute()
    return [{"role": "visitor" if m["role"] == "visitor" else "ai", "content": m["content"]} for m in (res.data or [])]
