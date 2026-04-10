"""
SMS Chat — Frontdesk AI
Handles inbound SMS texts with Vela AI responses.
Same FAQ-powered AI as voice and chat, but via text message.
"""

import logging
from fastapi import APIRouter, Request, Response

from app.services.voice_service import get_business_by_twilio_number
from app.services.chat_service import get_business_chat_config, _find_or_create_contact
from app.services.chat_ai_service import get_chat_ai_service
from app.services.sms_service import send_sms
from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sms-chat", tags=["sms-chat"])


@router.post("/inbound")
async def sms_inbound(request: Request):
    """
    Twilio hits this when an SMS arrives.
    Vela responds using the same FAQ knowledge base as voice and chat.
    """
    form = await request.form()
    from_number = form.get("From", "")
    to_number = form.get("To", "")
    body = form.get("Body", "").strip()

    logger.info(f"SMS received: from={from_number} to={to_number} body={body[:100]}")

    if not body:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml"
        )

    # Look up business by the Twilio number
    business = get_business_by_twilio_number(to_number)
    if not business:
        logger.warning(f"No business found for SMS number: {to_number}")
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml"
        )

    business_id = business["business_id"]
    config = get_business_chat_config(business_id)
    if not config:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml"
        )

    # Save sender as a lead
    db = get_db()
    _find_or_create_contact(db, business_id, phone=from_number)

    # Get conversation history for this phone number (from previous SMS exchanges)
    # For now, treat each SMS as standalone (no history)
    history = []

    # Generate Vela response
    ai_service = get_chat_ai_service()
    full_response = ""
    try:
        async for chunk in ai_service.stream_chat_response(
            business_config=config,
            message_history=history,
            visitor_message=body,
            voice_mode=False,
        ):
            full_response += chunk
    except Exception as e:
        logger.error(f"SMS AI error: {e}")
        phone = config.get("phone", "")
        full_response = f"Thanks for texting! For the fastest help, give us a call at {phone}."

    # Clean up for SMS
    full_response = full_response.replace("**", "").replace("*", "").replace("#", "").replace("_", "").strip()

    # Truncate to SMS-friendly length (160 chars per segment, keep under 320)
    if len(full_response) > 320:
        full_response = full_response[:317] + "..."

    logger.info(f"Vela SMS reply: {full_response[:100]}")

    # Respond via TwiML <Message>
    escaped = full_response.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>'
    return Response(content=twiml, media_type="application/xml")
