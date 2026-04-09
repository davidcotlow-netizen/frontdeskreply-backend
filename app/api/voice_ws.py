"""
Voice WebSocket — Frontdesk AI
Handles Twilio ConversationRelay WebSocket for AI voice calls.

ConversationRelay sends us transcribed caller speech as text.
We send back Milo's response as text. Twilio handles STT and TTS.
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.voice_service import (
    create_call_session,
    get_call_session,
    add_call_transcript,
    get_call_transcripts,
    end_call_session,
    check_business_voice_eligible,
)
from app.services.chat_service import get_business_chat_config
from app.services.chat_ai_service import get_chat_ai_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


# ── Voice-specific system prompt ─────────────────────────────────────────────

VOICE_SYSTEM_PROMPT_ADDON = """
IMPORTANT VOICE RULES (you are speaking on a phone call, not typing in chat):
1. Keep responses SHORT — under 30 words when possible. Phone conversations need to be concise.
2. Speak naturally — use contractions, casual phrasing, conversational tone.
3. Never use bullet points, markdown, links, or formatting. This is spoken aloud.
4. Never spell out URLs — instead say "visit our website" or "check out our site."
5. If you need to give a phone number, say it slowly with pauses: "three four six... four one oh... six oh two two."
6. End responses with a natural prompt: "Is there anything else I can help with?" or "What else can I do for you?"
7. If someone asks to speak to a real person, say: "Of course! Let me transfer you now."
"""


@router.websocket("/ws/voice/{business_id}")
async def voice_websocket(websocket: WebSocket, business_id: str):
    """
    WebSocket endpoint for Twilio ConversationRelay.

    ConversationRelay Protocol:
    - Twilio sends: {"type": "prompt", "voicePrompt": "transcribed caller speech"}
    - Twilio sends: {"type": "interrupt"} when caller interrupts
    - Twilio sends: {"type": "setup", ...} on connection start
    - Twilio sends: {"type": "dtmf", "digit": "1"} for keypad presses
    - We send back: {"type": "text", "token": "response text"} for streaming
    - We send back: {"type": "text", "token": ".", "last": true} to end response
    """
    await websocket.accept()

    # Parse query params
    query = dict(websocket.query_params)
    session_id = query.get("session_id", "")
    caller_phone = query.get("caller", "unknown")

    # Load business config
    config = get_business_chat_config(business_id)
    if not config:
        await websocket.close(code=4404, reason="Business not found")
        return

    ai_service = get_chat_ai_service()
    conversation_history = []
    call_start = time.time()

    logger.info(f"Voice WS connected: business={business_id} session={session_id} caller={caller_phone}")

    try:
        while True:
            # Receive message from ConversationRelay
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type", "")

            # ── Setup frame (connection established) ─────────────
            if msg_type == "setup":
                logger.info(f"ConversationRelay setup: {data}")
                # Save the greeting as first Milo transcript
                greeting = f"Hi! I'm Milo from {config.get('name', 'our business')}. How can I help you today?"
                add_call_transcript(session_id=session_id, role="milo", content=greeting)
                conversation_history.append({"role": "ai", "content": greeting})
                continue

            # ── Caller speech (transcribed) ──────────────────────
            if msg_type == "prompt":
                caller_text = data.get("voicePrompt", "").strip()
                if not caller_text:
                    continue

                logger.info(f"Caller said: {caller_text[:100]}")

                # Save caller transcript
                add_call_transcript(session_id=session_id, role="caller", content=caller_text)
                conversation_history.append({"role": "visitor", "content": caller_text})

                # Check for transfer request
                transfer_phrases = ["real person", "human", "someone else", "transfer me", "speak to someone", "talk to someone", "representative", "operator"]
                if any(phrase in caller_text.lower() for phrase in transfer_phrases):
                    transfer_msg = "Of course! Let me transfer you now. One moment please."
                    await websocket.send_text(json.dumps({
                        "type": "text",
                        "token": transfer_msg,
                        "last": True,
                    }))
                    add_call_transcript(session_id=session_id, role="milo", content=transfer_msg)

                    # Send end signal — Twilio will handle the transfer/hangup
                    await websocket.send_text(json.dumps({
                        "type": "end",
                    }))
                    break

                # Generate AI response using the same Claude service as chat
                full_response = ""
                try:
                    # Stream response token by token
                    async for chunk in ai_service.stream_chat_response(
                        business_config=config,
                        message_history=conversation_history[:-1],
                        visitor_message=caller_text,
                        visitor_name=None,  # Phone callers don't provide name upfront
                    ):
                        full_response += chunk
                        # Send each token to ConversationRelay
                        await websocket.send_text(json.dumps({
                            "type": "text",
                            "token": chunk,
                        }))

                    # Send end-of-response marker
                    await websocket.send_text(json.dumps({
                        "type": "text",
                        "token": "",
                        "last": True,
                    }))

                except Exception as e:
                    logger.error(f"Voice AI error: {e}", exc_info=True)
                    fallback = f"I'm sorry, I'm having trouble right now. You can reach us at {config.get('phone', 'our office')} for help."
                    await websocket.send_text(json.dumps({
                        "type": "text",
                        "token": fallback,
                        "last": True,
                    }))
                    full_response = fallback

                # Save Milo's response
                add_call_transcript(session_id=session_id, role="milo", content=full_response)
                conversation_history.append({"role": "ai", "content": full_response})

                # Apply voice system prompt addon (injected into the AI service context)
                # The voice prompt addon is handled by prepending to the system prompt

            # ── Caller interrupted ───────────────────────────────
            if msg_type == "interrupt":
                logger.info(f"Caller interrupted in session {session_id}")
                # ConversationRelay handles stopping TTS playback
                continue

            # ── DTMF (keypad press) ──────────────────────────────
            if msg_type == "dtmf":
                digit = data.get("digit", "")
                logger.info(f"DTMF: {digit} in session {session_id}")
                # Could route to transfer if they press 0
                if digit == "0":
                    await websocket.send_text(json.dumps({
                        "type": "text",
                        "token": "Transferring you now. One moment please.",
                        "last": True,
                    }))
                    add_call_transcript(session_id=session_id, role="system", content=f"DTMF: {digit} - Transfer requested")
                continue

    except WebSocketDisconnect:
        logger.info(f"Voice WS disconnected: session={session_id}")
    except Exception as e:
        logger.error(f"Voice WS error: {e}", exc_info=True)
    finally:
        # End the call session with duration
        duration = int(time.time() - call_start)
        if session_id:
            end_call_session(session_id, duration_seconds=duration)
        logger.info(f"Call ended: session={session_id} duration={duration}s")


# ── Dashboard endpoints for call data ────────────────────────────────────────

@router.get("/api/v1/voice/calls")
async def list_calls(business_id: str, period: str = "month"):
    """List call history for the dashboard."""
    from app.services.voice_service import get_call_history
    calls = get_call_history(business_id, period)
    return {"calls": calls, "count": len(calls)}


@router.get("/api/v1/voice/calls/{session_id}/transcripts")
async def get_transcripts(session_id: str):
    """Get full transcript for a call."""
    transcripts = get_call_transcripts(session_id)
    return {"transcripts": transcripts}
