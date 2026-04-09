"""
Voice Webhook — Frontdesk AI
Handles inbound Twilio voice calls using Gather + Say (no ConversationRelay needed).
Works on any Twilio account without special feature access.
"""

import logging
import asyncio
from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from app.services.voice_service import (
    get_business_by_twilio_number,
    check_business_voice_eligible,
    create_call_session,
    end_call_session,
    add_call_transcript,
    get_call_transcripts,
)
from app.services.chat_service import get_business_chat_config
from app.services.chat_ai_service import get_chat_ai_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


def escape_xml(text: str) -> str:
    """Escape text for safe inclusion in TwiML XML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@router.post("/inbound")
async def inbound_call(request: Request):
    """
    Twilio hits this when a call comes in.
    Greets the caller and starts listening with <Gather>.
    """
    form = await request.form()
    to_number = form.get("To", "")
    from_number = form.get("From", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"Inbound call: from={from_number} to={to_number} sid={call_sid}")

    # Look up business
    business = get_business_by_twilio_number(to_number)
    if not business:
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Google.en-US-Neural2-F">Sorry, this number is not configured. Goodbye.</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    business_id = business["business_id"]

    if not check_business_voice_eligible(business_id):
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Google.en-US-Neural2-F">Thank you for calling. Please visit our website for more information. Goodbye.</Say><Hangup/></Response>'
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

    # Respond with greeting + listen for speech
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="/api/v1/voice/respond?session_id={session_id}&amp;business_id={business_id}" method="POST" speechTimeout="auto" language="en-US" enhanced="true">
        <Say voice="Google.en-US-Neural2-F">{greeting}</Say>
    </Gather>
    <Say voice="Google.en-US-Neural2-F">I didn't catch that. Goodbye!</Say>
    <Hangup/>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


@router.post("/respond")
async def respond_to_speech(request: Request):
    """
    Called by Twilio after <Gather> captures caller speech.
    Sends speech to Claude, speaks the response, then listens again.
    """
    form = await request.form()
    speech_result = form.get("SpeechResult", "").strip()
    session_id = request.query_params.get("session_id", "")
    business_id = request.query_params.get("business_id", "")

    logger.info(f"Caller said: '{speech_result}' session={session_id}")

    if not speech_result:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="/api/v1/voice/respond?session_id={session_id}&amp;business_id={business_id}" method="POST" speechTimeout="auto" language="en-US" enhanced="true">
        <Say voice="Google.en-US-Neural2-F">I'm sorry, I didn't catch that. Could you say that again?</Say>
    </Gather>
    <Say voice="Google.en-US-Neural2-F">Goodbye!</Say>
    <Hangup/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Save caller transcript
    add_call_transcript(session_id=session_id, role="caller", content=speech_result)

    # Check for goodbye/hangup intent
    goodbye_phrases = ["goodbye", "bye", "that's all", "nothing else", "no thanks", "i'm good", "hang up", "end call"]
    if any(phrase in speech_result.lower() for phrase in goodbye_phrases):
        farewell = "Thanks for calling! Have a great day. Goodbye!"
        add_call_transcript(session_id=session_id, role="milo", content=farewell)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Google.en-US-Neural2-F">{escape_xml(farewell)}</Say>
    <Hangup/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Check for transfer request
    transfer_phrases = ["real person", "human", "someone else", "transfer", "speak to someone", "talk to someone", "representative", "operator"]
    if any(phrase in speech_result.lower() for phrase in transfer_phrases):
        transfer_msg = "Of course! Let me transfer you now. One moment please."
        add_call_transcript(session_id=session_id, role="milo", content=transfer_msg)
        config = get_business_chat_config(business_id)
        biz_phone = config.get("phone", "") if config else ""
        if biz_phone:
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Google.en-US-Neural2-F">{escape_xml(transfer_msg)}</Say>
    <Dial>{escape_xml(biz_phone)}</Dial>
</Response>"""
        else:
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Google.en-US-Neural2-F">I'm sorry, I don't have a direct number to transfer you to. Please try calling back during business hours. Goodbye!</Say>
    <Hangup/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Get AI response from Claude
    config = get_business_chat_config(business_id)
    ai_service = get_chat_ai_service()

    # Build conversation history
    transcripts = get_call_transcripts(session_id)
    history = [{"role": "visitor" if t["role"] == "caller" else "ai", "content": t["content"]} for t in transcripts[:-1]]

    # Generate response (collect full response, not streaming for TwiML)
    full_response = ""
    try:
        async for chunk in ai_service.stream_chat_response(
            business_config=config,
            message_history=history,
            visitor_message=speech_result,
            voice_mode=True,
        ):
            full_response += chunk
    except Exception as e:
        logger.error(f"Voice AI error: {e}")
        phone = config.get("phone", "") if config else ""
        full_response = f"I'm sorry, I'm having trouble right now. Please call us at {phone} for help."

    # Clean up response for speech — strip markdown and emojis
    import re
    full_response = full_response.replace("**", "").replace("*", "").replace("#", "").replace("_", "")
    # Remove all emojis and unicode symbols
    full_response = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27BF\u2B50\u2764\u2705\u274C\u26A0\u2728\u2615\u270B\u270C\u261D\u2934\u2935\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2600-\u26FF\u2702-\u27B0\u2934-\u2935\u3030\u303D\u3297\u3299\uFE0F\u200D]', '', full_response)
    full_response = full_response.strip()

    logger.info(f"Milo says: '{full_response[:100]}' session={session_id}")

    # Save Milo transcript
    add_call_transcript(session_id=session_id, role="milo", content=full_response)

    # Speak response and listen for next question
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="/api/v1/voice/respond?session_id={session_id}&amp;business_id={business_id}" method="POST" speechTimeout="auto" language="en-US" enhanced="true">
        <Say voice="Google.en-US-Neural2-F">{escape_xml(full_response)}</Say>
    </Gather>
    <Say voice="Google.en-US-Neural2-F">I didn't hear anything. If you need more help, just call back anytime. Goodbye!</Say>
    <Hangup/>
</Response>"""

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
    db = get_db()
    res = db.table("call_sessions").select("id").eq("call_sid", call_sid).maybe_single().execute()

    if res and res.data:
        db.table("call_sessions").update({
            "status": "ended" if call_status == "completed" else call_status,
            "duration_seconds": int(duration),
            "ended_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }).eq("id", res.data["id"]).execute()

    return PlainTextResponse("OK")
