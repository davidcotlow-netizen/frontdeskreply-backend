"""
Voice Webhook — Frontdesk AI
Handles inbound Twilio voice calls and routes to ConversationRelay.
"""

import logging
from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from app.services.voice_service import (
    get_business_by_twilio_number,
    check_business_voice_eligible,
    create_call_session,
    end_call_session,
)
from app.services.chat_service import get_business_chat_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/inbound")
async def inbound_call(request: Request):
    """
    Twilio hits this webhook when an inbound call arrives.
    Returns TwiML that starts a ConversationRelay session
    pointing to our WebSocket endpoint.
    """
    form = await request.form()
    to_number = form.get("To", "")
    from_number = form.get("From", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"Inbound call: from={from_number} to={to_number} sid={call_sid}")

    # Look up which business owns this number
    business = get_business_by_twilio_number(to_number)
    if not business:
        logger.warning(f"No business found for number: {to_number}")
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Amy">I'm sorry, this number is not configured. Please try again later. Goodbye.</Say>
    <Hangup/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    business_id = business["business_id"]

    # Check Pro plan eligibility
    if not check_business_voice_eligible(business_id):
        logger.warning(f"Business {business_id} not eligible for voice (not Pro)")
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Amy">Thank you for calling. Please visit our website for more information. Goodbye.</Say>
    <Hangup/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Load business config for the greeting
    config = get_business_chat_config(business_id)
    business_name = config.get("name", "our business") if config else "our business"

    # Create call session
    session = create_call_session(
        business_id=business_id,
        caller_phone=from_number,
        call_sid=call_sid,
    )
    session_id = session["id"]

    # Build the WebSocket URL for ConversationRelay
    # Railway/production uses wss://, local uses ws://
    ws_host = request.headers.get("host", "api.frontdeskreply.com")
    ws_protocol = "wss" if "frontdeskreply.com" in ws_host else "ws"
    ws_url = f"{ws_protocol}://{ws_host}/ws/voice/{business_id}?session_id={session_id}&caller={from_number}"

    # Return TwiML with ConversationRelay
    # ConversationRelay handles STT + TTS natively
    # It connects to our WebSocket and sends/receives text
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <ConversationRelay
            url="{ws_url}"
            voice="Polly.Amy"
            language="en-US"
            transcriptionProvider="google"
            ttsProvider="amazon"
            welcomeGreeting="Hi! I'm Milo from {business_name}. How can I help you today?"
            interruptible="true"
            dtmfDetection="true"
        />
    </Connect>
</Response>"""

    logger.info(f"ConversationRelay started: session={session_id} ws={ws_url}")
    return Response(content=twiml, media_type="application/xml")


@router.post("/status")
async def call_status(request: Request):
    """
    Twilio status callback — fires when call ends.
    Updates call session with duration and final status.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    duration = form.get("CallDuration", "0")
    recording_url = form.get("RecordingUrl", "")

    logger.info(f"Call status update: sid={call_sid} status={call_status} duration={duration}s")

    # Find the session by call_sid
    from app.core.database import get_db
    db = get_db()
    res = db.table("call_sessions").select("id").eq(
        "call_sid", call_sid
    ).maybe_single().execute()

    if res and res.data:
        session_id = res.data["id"]
        updates = {
            "status": "ended" if call_status == "completed" else call_status,
            "duration_seconds": int(duration),
        }
        if recording_url:
            updates["recording_url"] = recording_url
        db.table("call_sessions").update(updates).eq("id", session_id).execute()
        logger.info(f"Call session updated: {session_id}")

    return PlainTextResponse("OK")
