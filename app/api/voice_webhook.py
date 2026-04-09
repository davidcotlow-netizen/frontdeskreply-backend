"""
Voice Webhook — Frontdesk AI
Handles inbound Twilio voice calls using ConversationRelay for low-latency streaming.
"""

import logging
from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from app.services.voice_service import (
    get_business_by_twilio_number,
    check_business_voice_eligible,
    create_call_session,
    add_call_transcript,
)
from app.services.chat_service import get_business_chat_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


def escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@router.post("/inbound")
async def inbound_call(request: Request):
    """
    Twilio hits this when a call comes in.
    Returns TwiML with ConversationRelay for low-latency streaming voice AI.
    """
    form = await request.form()
    to_number = form.get("To", "")
    from_number = form.get("From", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"Inbound call: from={from_number} to={to_number} sid={call_sid}")

    # Look up business
    business = get_business_by_twilio_number(to_number)
    if not business:
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Google.en-US-Chirp3-HD-Leda">Sorry, this number is not configured. Goodbye.</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    business_id = business["business_id"]

    if not check_business_voice_eligible(business_id):
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Google.en-US-Chirp3-HD-Leda">Thank you for calling. Please visit our website for more information. Goodbye.</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    config = get_business_chat_config(business_id)
    business_name = escape_xml(config.get("name", "our business")) if config else "our business"

    # Create call session
    session = create_call_session(
        business_id=business_id,
        caller_phone=from_number,
        call_sid=call_sid,
    )
    session_id = session["id"]

    greeting = f"Hi! I'm Milo from {business_name}. How can I help you today?"
    add_call_transcript(session_id=session_id, role="milo", content=greeting)

    # Build WebSocket URL for ConversationRelay
    ws_host = request.headers.get("host", "api.frontdeskreply.com")
    ws_protocol = "wss" if "frontdeskreply.com" in ws_host else "ws"
    ws_url = f"{ws_protocol}://{ws_host}/ws/voice/{business_id}?session_id={session_id}&caller={from_number}"

    # Return TwiML with ConversationRelay — streams text bidirectionally
    # Twilio handles STT + TTS, we just send/receive text via WebSocket
    # Build action URL for when Connect ends
    action_url = f"https://{request.headers.get('host', 'api.frontdeskreply.com')}/api/v1/voice/status"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect action="{action_url}">
        <ConversationRelay url="{ws_url}" welcomeGreeting="{escape_xml(greeting)}" ttsProvider="ElevenLabs" transcriptionProvider="Deepgram" language="en-US" interruptible="true" dtmfDetection="true" />
    </Connect>
</Response>"""

    logger.info(f"ConversationRelay started: session={session_id}")
    return Response(content=twiml, media_type="application/xml")


@router.post("/status")
async def call_status(request: Request):
    """Twilio status callback — fires when call ends."""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    duration = form.get("CallDuration", "0")

    logger.info(f"Call status: sid={call_sid} status={call_status} duration={duration}s")

    from app.core.database import get_db
    from datetime import datetime, timezone
    db = get_db()
    res = db.table("call_sessions").select("id").eq("call_sid", call_sid).maybe_single().execute()

    if res and res.data:
        db.table("call_sessions").update({
            "status": "ended" if call_status == "completed" else call_status,
            "duration_seconds": int(duration),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", res.data["id"]).execute()

    return PlainTextResponse("OK")
