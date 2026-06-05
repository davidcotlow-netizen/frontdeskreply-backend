"""
Voice Webhook — Frontdesk AI
Handles inbound Twilio voice calls using Gather + Say.
Uses Google Chirp3-HD-Leda for natural-sounding voice.
"""

import logging
import re
from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from app.core.database import get_db
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

VOICE = "Google.en-US-Chirp3-HD-Leda"


def escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def strip_emojis(text: str) -> str:
    text = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27BF\u2B50\u2764\u2705\u274C\u26A0\u2728\u2615\u270B\u270C\u261D\u2934\u2935\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2600-\u26FF\u2702-\u27B0\u3030\u303D\u3297\u3299\uFE0F\u200D]', '', text)
    return text.replace("**", "").replace("*", "").replace("#", "").replace("_", "").strip()


# \u2500\u2500 Spam-call rejection \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Block list lives in `blocked_numbers` (see migration 014). Numbers stored
# E.164. Per-call cost is one indexed Supabase select; rejection happens
# before any AI / Gather is fired, so blocked callers cost effectively
# zero (Twilio drops the call at the carrier handshake).

def normalize_phone_e164(phone: str) -> str:
    """Normalize a US phone number to E.164 (+1XXXXXXXXXX).

    Twilio's `From` arrives in E.164 most of the time, but local-format
    fallbacks happen on some carriers; manually-seeded blocklist rows may
    also be loose. We pass-through anything already prefixed `+`, and
    apply `+1` to 10-digit US numbers.
    """
    if not phone:
        return ""
    if phone.startswith("+"):
        return phone
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return phone


def is_caller_blocked(business_id: str, from_number: str) -> bool:
    """Check `blocked_numbers` for an exact OR prefix match.

    Falls open (returns False) on any DB error so a transient outage
    doesn't take legitimate calls offline \u2014 worst case is a few spam
    calls slip through until the DB recovers.
    """
    if not from_number or not business_id:
        return False
    e164 = normalize_phone_e164(from_number)
    if not e164:
        return False
    try:
        db = get_db()
        res = (
            db.table("blocked_numbers")
              .select("phone_number, match_type, reason")
              .eq("business_id", business_id)
              .execute()
        )
        for row in (res.data or []):
            stored = row.get("phone_number") or ""
            mtype = row.get("match_type") or "exact"
            if mtype == "exact" and stored == e164:
                logger.warning(
                    f"voice_blocked_exact: caller={e164} business={business_id} "
                    f"reason={row.get('reason') or ''}"
                )
                return True
            if mtype == "prefix" and stored and e164.startswith(stored):
                logger.warning(
                    f"voice_blocked_prefix: caller={e164} matched={stored} "
                    f"business={business_id} reason={row.get('reason') or ''}"
                )
                return True
    except Exception:
        logger.exception("blocked_numbers_lookup_failed")
        return False
    return False


def _get_retell_voice_agent(business_id: str) -> str | None:
    """Return the business's Retell voice agent_id if it uses Retell for voice.

    When present, inbound calls (that survive the spam blocklist) are bridged
    straight to Retell over SIP — so the call passes through our blocklist
    FIRST, then reaches the exact same Vela agent as before. Falls open
    (returns None -> legacy Gather flow) on any DB error.
    """
    try:
        db = get_db()
        res = db.table("channels").select("config").eq(
            "business_id", business_id
        ).eq("channel_type", "voice").execute()
        for ch in (res.data or []):
            cfg = ch.get("config") or {}
            if cfg.get("retell_agent_id"):
                return cfg["retell_agent_id"]
    except Exception:
        logger.exception("retell_voice_agent_lookup_failed")
    return None


@router.post("/inbound")
async def inbound_call(request: Request):
    form = await request.form()
    to_number = form.get("To", "")
    from_number = form.get("From", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"Inbound call: from={from_number} to={to_number} sid={call_sid}")

    business = get_business_by_twilio_number(to_number)
    if not business:
        twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{VOICE}">Sorry, this number is not configured. Goodbye.</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    business_id = business["business_id"]

    # Spam-call rejection. <Reject/> drops the call at the Twilio carrier
    # layer — no Gather, no Say, no AI prompt fires. The caller hears a
    # standard rejection signal and we pay $0 in Anthropic/TTS/per-second
    # voice charges. See migration 014 for the blocked_numbers table.
    if is_caller_blocked(business_id, from_number):
        logger.warning(
            f"call_rejected_blocked: from={from_number} to={to_number} "
            f"sid={call_sid} business={business_id}"
        )
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="rejected"/></Response>'
        return Response(content=twiml, media_type="application/xml")

    if not check_business_voice_eligible(business_id):
        twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{VOICE}">Thank you for calling. Please visit our website for more information. Goodbye.</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    # Retell-powered businesses: the call already cleared the spam blocklist
    # above, so bridge it to the same Retell agent over SIP. answerOnBridge
    # keeps the caller hearing ringback until Retell picks up; callerId passes
    # the ORIGINAL caller number through so caller-ID / notifications stay intact.
    retell_agent = _get_retell_voice_agent(business_id)
    if retell_agent:
        sip_uri = f"sip:{to_number}@sip.retellai.com"
        logger.info(f"voice_bridge_retell: from={from_number} to={to_number} agent={retell_agent} sip={sip_uri}")
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Dial answerOnBridge="true" callerId="{escape_xml(from_number)}" timeout="20">'
            f'<Sip>{escape_xml(sip_uri)}</Sip></Dial></Response>'
        )
        return Response(content=twiml, media_type="application/xml")

    config = get_business_chat_config(business_id)
    business_name = escape_xml(config.get("name", "our business")) if config else "our business"

    session = create_call_session(business_id=business_id, caller_phone=from_number, call_sid=call_sid)
    session_id = session["id"]

    greeting = f"Hi! I'm Vela from {business_name}. How can I help you today?"
    add_call_transcript(session_id=session_id, role="milo", content=greeting)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="/api/v1/voice/respond?session_id={session_id}&amp;business_id={business_id}" method="POST" speechTimeout="auto" language="en-US" enhanced="true">
        <Say voice="{VOICE}">{greeting}</Say>
    </Gather>
    <Say voice="{VOICE}">I didn't catch that. Goodbye!</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.post("/respond")
async def respond_to_speech(request: Request):
    form = await request.form()
    speech_result = form.get("SpeechResult", "").strip()
    session_id = request.query_params.get("session_id", "")
    business_id = request.query_params.get("business_id", "")

    logger.info(f"Caller said: '{speech_result}' session={session_id}")

    if not speech_result:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="/api/v1/voice/respond?session_id={session_id}&amp;business_id={business_id}" method="POST" speechTimeout="auto" language="en-US" enhanced="true">
        <Say voice="{VOICE}">I'm sorry, I didn't catch that. Could you say that again?</Say>
    </Gather>
    <Say voice="{VOICE}">Goodbye!</Say>
    <Hangup/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    add_call_transcript(session_id=session_id, role="caller", content=speech_result)

    # Goodbye detection
    goodbye_phrases = ["goodbye", "bye", "that's all", "nothing else", "no thanks", "i'm good", "hang up", "end call"]
    if any(phrase in speech_result.lower() for phrase in goodbye_phrases):
        farewell = "Thanks for calling! Have a great day. Goodbye!"
        add_call_transcript(session_id=session_id, role="milo", content=farewell)
        twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{VOICE}">{escape_xml(farewell)}</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    # Transfer detection
    transfer_phrases = ["real person", "human", "someone else", "transfer", "speak to someone", "talk to someone", "representative", "operator"]
    if any(phrase in speech_result.lower() for phrase in transfer_phrases):
        transfer_msg = "Absolutely! Let me get you connected right now!"
        add_call_transcript(session_id=session_id, role="milo", content=transfer_msg)
        config = get_business_chat_config(business_id)
        biz_phone = config.get("phone", "") if config else ""
        if biz_phone:
            twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{VOICE}">{escape_xml(transfer_msg)}</Say><Dial>{escape_xml(biz_phone)}</Dial></Response>'
        else:
            twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{VOICE}">I\'m sorry, I don\'t have a direct number to transfer you to. Please try calling back during business hours. Goodbye!</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    # AI response
    config = get_business_chat_config(business_id)
    ai_service = get_chat_ai_service()

    transcripts = get_call_transcripts(session_id)
    history = [{"role": "visitor" if t["role"] == "caller" else "ai", "content": t["content"]} for t in transcripts[:-1]]

    full_response = ""
    try:
        async for chunk in ai_service.stream_chat_response(
            business_config=config, message_history=history,
            visitor_message=speech_result, voice_mode=True,
        ):
            full_response += chunk
    except Exception as e:
        logger.error(f"Voice AI error: {e}")
        phone = config.get("phone", "") if config else ""
        full_response = f"I'm sorry, I'm having trouble right now. Please call us at {phone} for help."

    full_response = strip_emojis(full_response)
    logger.info(f"Vela says: '{full_response[:100]}'")
    add_call_transcript(session_id=session_id, role="milo", content=full_response)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="/api/v1/voice/respond?session_id={session_id}&amp;business_id={business_id}" method="POST" speechTimeout="auto" language="en-US" enhanced="true">
        <Say voice="{VOICE}">{escape_xml(full_response)}</Say>
    </Gather>
    <Say voice="{VOICE}">I didn't hear anything. Call back anytime. Goodbye!</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.post("/status")
async def call_status(request: Request):
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
