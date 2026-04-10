"""
Voice WebSocket — Frontdesk AI
Handles Twilio ConversationRelay WebSocket for streaming voice AI.

ConversationRelay protocol:
- Twilio sends: {"type": "prompt", "voicePrompt": "caller speech text"}
- Twilio sends: {"type": "interrupt"} when caller talks over Vela
- Twilio sends: {"type": "setup", ...} on connection
- Twilio sends: {"type": "dtmf", "digit": "1"} for keypad
- We send: {"type": "text", "token": "word"} for streaming
- We send: {"type": "text", "token": "", "last": true} to end response
"""

import asyncio
import json
import logging
import re
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.voice_service import (
    add_call_transcript,
    get_call_transcripts,
    end_call_session,
)
from app.services.chat_service import get_business_chat_config
from app.services.chat_ai_service import get_chat_ai_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


def strip_emojis(text: str) -> str:
    """Remove emojis and unicode symbols for clean speech."""
    text = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27BF\u2B50\u2764\u2705\u274C\u26A0\u2728\u2615\u270B\u270C\u261D\u2934\u2935\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2600-\u26FF\u2702-\u27B0\u2934-\u2935\u3030\u303D\u3297\u3299\uFE0F\u200D]', '', text)
    return text.replace("**", "").replace("*", "").replace("#", "").replace("_", "").strip()


@router.websocket("/ws/voice/{business_id}")
async def voice_websocket(websocket: WebSocket, business_id: str):
    """
    WebSocket endpoint for Twilio ConversationRelay.
    Receives transcribed speech, sends Claude responses token-by-token.
    """
    await websocket.accept()

    query = dict(websocket.query_params)
    session_id = query.get("session_id", "")
    caller_phone = query.get("caller", "unknown")

    config = get_business_chat_config(business_id)
    if not config:
        await websocket.close(code=4404, reason="Business not found")
        return

    ai_service = get_chat_ai_service()
    conversation_history = []
    call_start = time.time()

    # Add greeting to history
    greeting = f"Hi! I'm Vela from {config.get('name', 'our business')}. How can I help you today?"
    conversation_history.append({"role": "ai", "content": greeting})

    logger.info(f"Voice WS connected: business={business_id} session={session_id}")

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type", "")

            # ── Setup ────────────────────────────────────────────
            if msg_type == "setup":
                logger.info(f"ConversationRelay setup received")
                continue

            # ── Caller speech ────────────────────────────────────
            if msg_type == "prompt":
                caller_text = data.get("voicePrompt", "").strip()
                if not caller_text:
                    continue

                logger.info(f"Caller: {caller_text[:100]}")

                # Save transcript
                add_call_transcript(session_id=session_id, role="caller", content=caller_text)
                conversation_history.append({"role": "visitor", "content": caller_text})

                # Check for goodbye
                goodbye_phrases = ["goodbye", "bye", "that's all", "nothing else", "no thanks", "i'm good", "hang up"]
                if any(phrase in caller_text.lower() for phrase in goodbye_phrases):
                    farewell = "Thanks for calling! Have a great day!"
                    await websocket.send_text(json.dumps({"type": "text", "token": farewell, "last": True}))
                    add_call_transcript(session_id=session_id, role="milo", content=farewell)
                    await websocket.send_text(json.dumps({"type": "end"}))
                    break

                # Check for transfer
                transfer_phrases = ["real person", "human", "transfer", "speak to someone", "talk to someone", "operator"]
                if any(phrase in caller_text.lower() for phrase in transfer_phrases):
                    transfer_msg = "Absolutely! Let me get you connected right now!"
                    await websocket.send_text(json.dumps({"type": "text", "token": transfer_msg, "last": True}))
                    add_call_transcript(session_id=session_id, role="milo", content=transfer_msg)
                    await websocket.send_text(json.dumps({"type": "end"}))
                    break

                # Stream Claude response token by token
                full_response = ""
                try:
                    async for chunk in ai_service.stream_chat_response(
                        business_config=config,
                        message_history=conversation_history[:-1],
                        visitor_message=caller_text,
                        voice_mode=True,
                    ):
                        clean_chunk = strip_emojis(chunk)
                        if clean_chunk:
                            full_response += clean_chunk
                            # Send each token immediately — Twilio speaks as it receives
                            await websocket.send_text(json.dumps({
                                "type": "text",
                                "token": clean_chunk,
                            }))

                    # Signal end of response
                    await websocket.send_text(json.dumps({
                        "type": "text",
                        "token": "",
                        "last": True,
                    }))

                except Exception as e:
                    logger.error(f"Voice AI error: {e}", exc_info=True)
                    fallback = f"I'm sorry, I'm having trouble right now. You can reach us at {config.get('phone', 'our office')}."
                    await websocket.send_text(json.dumps({"type": "text", "token": fallback, "last": True}))
                    full_response = fallback

                # Save Vela response
                if full_response:
                    add_call_transcript(session_id=session_id, role="milo", content=full_response)
                    conversation_history.append({"role": "ai", "content": full_response})

            # ── Interrupt ────────────────────────────────────────
            if msg_type == "interrupt":
                logger.info(f"Caller interrupted")
                continue

            # ── DTMF ─────────────────────────────────────────────
            if msg_type == "dtmf":
                digit = data.get("digit", "")
                logger.info(f"DTMF: {digit}")
                if digit == "0":
                    await websocket.send_text(json.dumps({"type": "text", "token": "Transferring you now!", "last": True}))
                    await websocket.send_text(json.dumps({"type": "end"}))
                    break
                continue

    except WebSocketDisconnect:
        logger.info(f"Voice WS disconnected: session={session_id}")
    except Exception as e:
        logger.error(f"Voice WS error: {e}", exc_info=True)
    finally:
        duration = int(time.time() - call_start)
        if session_id:
            end_call_session(session_id, duration_seconds=duration)
        logger.info(f"Call ended: session={session_id} duration={duration}s")


# ── Dashboard endpoints ──────────────────────────────────────────────────────

@router.get("/api/v1/voice/calls")
async def list_calls(business_id: str, period: str = "month"):
    from app.services.voice_service import get_call_history
    calls = get_call_history(business_id, period)
    return {"calls": calls, "count": len(calls)}


@router.get("/api/v1/voice/calls/{session_id}/transcripts")
async def get_transcripts(session_id: str):
    transcripts = get_call_transcripts(session_id)
    return {"transcripts": transcripts}
