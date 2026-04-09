"""
Voice Retell WebSocket — Frontdesk AI
Handles Retell AI's custom LLM WebSocket protocol.
Retell sends transcribed speech, we send Claude responses.

Retell Protocol:
- Retell sends: {"interaction_type": "call_details", ...} on connect
- Retell sends: {"interaction_type": "update_only", "transcript": [...]}
- Retell sends: {"interaction_type": "response_required", "transcript": [...]}
- We send: {"response_id": N, "content": "text", "content_complete": false}
- We send: {"response_id": N, "content": "", "content_complete": true}
"""

import asyncio
import json
import logging
import re
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.voice_service import (
    create_call_session,
    add_call_transcript,
    end_call_session,
)
from app.services.chat_service import get_business_chat_config
from app.services.chat_ai_service import get_chat_ai_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice-retell"])


def strip_emojis(text: str) -> str:
    text = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27BF\u2B50\u2764\u2705\u274C\u26A0\u2728\u2615\u270B\u270C\u261D\u25AA-\u25FE\u2600-\u26FF\u2702-\u27B0\u3030\u303D\u3297\u3299\uFE0F\u200D]', '', text)
    return text.replace("**", "").replace("*", "").replace("#", "").replace("_", "").strip()


@router.websocket("/ws/voice-retell/{business_id}")
async def voice_retell_ws(websocket: WebSocket, business_id: str):
    """
    WebSocket endpoint for Retell AI custom LLM integration.
    Retell handles STT + TTS + telephony. We provide Claude responses.
    """
    await websocket.accept()

    config = get_business_chat_config(business_id)
    if not config:
        await websocket.close(code=4404, reason="Business not found")
        return

    ai_service = get_chat_ai_service()
    conversation_history = []
    session_id = None
    call_start = time.time()
    response_id = 0

    logger.info(f"Retell WS connected: business={business_id}")

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            interaction_type = data.get("interaction_type", "")

            # ── Call details (initial setup) ─────────────────────
            if interaction_type == "call_details":
                call_info = data.get("call", {})
                caller_phone = call_info.get("from_number", "")
                call_sid = call_info.get("call_id", "")

                # Create session
                session = create_call_session(
                    business_id=business_id,
                    caller_phone=caller_phone,
                    call_sid=call_sid,
                )
                session_id = session["id"]

                # Add greeting to history
                greeting = f"Hi! I'm Milo from {config.get('name', 'our business')}. How can I help you today?"
                conversation_history.append({"role": "ai", "content": greeting})
                add_call_transcript(session_id=session_id, role="milo", content=greeting)

                logger.info(f"Retell call started: session={session_id} caller={caller_phone}")
                continue

            # ── Response required (caller finished speaking) ─────
            if interaction_type == "response_required":
                transcript = data.get("transcript", [])
                if not transcript:
                    continue

                # Get the last caller utterance
                last_utterance = ""
                for entry in reversed(transcript):
                    if entry.get("role") == "user":
                        last_utterance = entry.get("content", "").strip()
                        break

                if not last_utterance:
                    continue

                logger.info(f"Caller: {last_utterance[:100]}")

                # Save caller transcript
                if session_id:
                    add_call_transcript(session_id=session_id, role="caller", content=last_utterance)

                # Build history from Retell transcript
                conversation_history = []
                for entry in transcript:
                    role = "visitor" if entry.get("role") == "user" else "ai"
                    conversation_history.append({"role": role, "content": entry.get("content", "")})

                response_id += 1
                current_response_id = response_id

                # Check for goodbye
                goodbye_phrases = ["goodbye", "bye", "that's all", "nothing else", "no thanks", "i'm good"]
                if any(phrase in last_utterance.lower() for phrase in goodbye_phrases):
                    farewell = "Thanks for calling! Have a great day!"
                    await websocket.send_text(json.dumps({
                        "response_id": current_response_id,
                        "content": farewell,
                        "content_complete": True,
                    }))
                    if session_id:
                        add_call_transcript(session_id=session_id, role="milo", content=farewell)
                    continue

                # Check for transfer
                transfer_phrases = ["real person", "human", "transfer", "speak to someone", "operator"]
                if any(phrase in last_utterance.lower() for phrase in transfer_phrases):
                    transfer_msg = "Absolutely! Let me get you connected right now!"
                    await websocket.send_text(json.dumps({
                        "response_id": current_response_id,
                        "content": transfer_msg,
                        "content_complete": True,
                    }))
                    if session_id:
                        add_call_transcript(session_id=session_id, role="milo", content=transfer_msg)
                    continue

                # Stream Claude response
                full_response = ""
                try:
                    async for chunk in ai_service.stream_chat_response(
                        business_config=config,
                        message_history=conversation_history[:-1],
                        visitor_message=last_utterance,
                        voice_mode=True,
                    ):
                        clean = strip_emojis(chunk)
                        if clean:
                            full_response += clean
                            # Send each chunk to Retell — it speaks immediately
                            await websocket.send_text(json.dumps({
                                "response_id": current_response_id,
                                "content": clean,
                                "content_complete": False,
                            }))

                    # Signal end of response
                    await websocket.send_text(json.dumps({
                        "response_id": current_response_id,
                        "content": "",
                        "content_complete": True,
                    }))

                except Exception as e:
                    logger.error(f"Retell AI error: {e}", exc_info=True)
                    fallback = f"I'm sorry, I'm having trouble right now. You can reach us at {config.get('phone', 'our office')}."
                    await websocket.send_text(json.dumps({
                        "response_id": current_response_id,
                        "content": fallback,
                        "content_complete": True,
                    }))
                    full_response = fallback

                # Save Milo response
                if session_id and full_response:
                    add_call_transcript(session_id=session_id, role="milo", content=full_response)

            # ── Update only (mid-speech transcript update) ───────
            if interaction_type == "update_only":
                continue

    except WebSocketDisconnect:
        logger.info(f"Retell WS disconnected: session={session_id}")
    except Exception as e:
        logger.error(f"Retell WS error: {e}", exc_info=True)
    finally:
        duration = int(time.time() - call_start)
        if session_id:
            end_call_session(session_id, duration_seconds=duration)
        logger.info(f"Retell call ended: session={session_id} duration={duration}s")
