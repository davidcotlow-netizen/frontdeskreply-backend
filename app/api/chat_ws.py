"""
Chat WebSocket Endpoint — Frontdesk AI
Real-time live chat via native FastAPI/Starlette WebSocket.

Visitor connects → sends init frame → receives greeting → exchanges messages
with Claude AI in real-time. Business owner can take over from the dashboard.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.chat_service import (
    create_chat_session,
    get_chat_session,
    add_chat_message,
    get_session_messages,
    end_chat_session,
    set_session_escalated,
    check_business_chat_eligible,
    get_business_chat_config,
)
from app.services.chat_ai_service import get_chat_ai_service
from app.services.notification_service import send_chat_escalation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


# ── Connection Manager ───────────────────────────────────────────────────────

class ConnectionManager:
    """
    Tracks active WebSocket connections per business and session.
    In-memory dict — works for single Railway instance.
    Can be swapped to Redis pub/sub later for multi-instance scaling.
    """

    def __init__(self):
        # {business_id: {session_id: WebSocket}}
        self._connections: dict[str, dict[str, WebSocket]] = {}
        # {session_id: business_id} — reverse lookup
        self._session_to_business: dict[str, str] = {}

    def connect(self, business_id: str, session_id: str, websocket: WebSocket):
        if business_id not in self._connections:
            self._connections[business_id] = {}
        self._connections[business_id][session_id] = websocket
        self._session_to_business[session_id] = business_id
        logger.info(f"WS connected: business={business_id} session={session_id}")

    def disconnect(self, session_id: str):
        business_id = self._session_to_business.pop(session_id, None)
        if business_id and business_id in self._connections:
            self._connections[business_id].pop(session_id, None)
            if not self._connections[business_id]:
                del self._connections[business_id]
        logger.info(f"WS disconnected: session={session_id}")

    def get_websocket(self, session_id: str) -> Optional[WebSocket]:
        """Get WebSocket for a session (used by dashboard to relay owner messages)."""
        business_id = self._session_to_business.get(session_id)
        if business_id:
            return self._connections.get(business_id, {}).get(session_id)
        return None

    def get_active_count(self, business_id: str) -> int:
        return len(self._connections.get(business_id, {}))

    def get_all_sessions(self, business_id: str) -> list[str]:
        return list(self._connections.get(business_id, {}).keys())


# Global connection manager instance
manager = ConnectionManager()


# ── Helper: send JSON frame ─────────────────────────────────────────────────

async def send_frame(ws: WebSocket, frame: dict):
    """Send a JSON frame to the visitor, handling errors gracefully."""
    try:
        await ws.send_json(frame)
    except Exception as e:
        logger.warning(f"Failed to send frame: {e}")


# ── WebSocket endpoint ───────────────────────────────────────────────────────

@router.websocket("/ws/chat/{business_id}")
async def chat_websocket(websocket: WebSocket, business_id: str):
    """
    Main live chat WebSocket endpoint.

    Protocol:
    1. Client connects to /ws/chat/{business_id}
    2. Client sends init frame: {type: "init", visitor_name: "...", visitor_email: "..."}
       Or reconnect: {type: "init", session_id: "existing-uuid"}
    3. Server sends session_created + greeting
    4. Client sends messages: {type: "message", content: "..."}
    5. Server streams AI response: typing → ai_chunk(s) → ai_done
    6. Client can request escalation: {type: "escalate_request"}
    7. Server sends ping every 25s, client responds with pong
    """
    await websocket.accept()

    # ── Validate business eligibility ────────────────────────────────
    if not check_business_chat_eligible(business_id):
        await send_frame(websocket, {
            "type": "error",
            "content": "Live chat is not available for this business plan. Please upgrade to Growth or Pro.",
        })
        await websocket.close(code=4403, reason="Plan not eligible for live chat")
        return

    config = get_business_chat_config(business_id)
    if not config:
        await send_frame(websocket, {
            "type": "error",
            "content": "Business not found.",
        })
        await websocket.close(code=4404, reason="Business not found")
        return

    session_id = None
    ai_service = get_chat_ai_service()
    ai_exchange_count = 0
    last_activity = time.time()

    try:
        # ── Wait for init frame ──────────────────────────────────────
        init_data = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)

        if init_data.get("type") != "init":
            await send_frame(websocket, {
                "type": "error",
                "content": "Expected init frame as first message.",
            })
            await websocket.close(code=4400, reason="Missing init frame")
            return

        # ── Create or reconnect session ──────────────────────────────
        existing_session_id = init_data.get("session_id")
        visitor_name = init_data.get("visitor_name", "Visitor")
        visitor_email = init_data.get("visitor_email")
        visitor_phone = init_data.get("visitor_phone")

        if existing_session_id:
            # Reconnect to existing session
            existing = get_chat_session(existing_session_id)
            if existing and existing.get("status") == "active" and existing.get("business_id") == business_id:
                session_id = existing_session_id
                visitor_name = existing.get("visitor_name") or visitor_name
                logger.info(f"Reconnected to session: {session_id}")
            else:
                # Invalid session — create new
                existing_session_id = None

        if not existing_session_id:
            session = create_chat_session(
                business_id=business_id,
                visitor_name=visitor_name,
                visitor_email=visitor_email,
                visitor_phone=visitor_phone,
            )
            session_id = session["id"]

        manager.connect(business_id, session_id, websocket)

        # ── Send session confirmation ────────────────────────────────
        await send_frame(websocket, {
            "type": "session_created",
            "session_id": session_id,
            "agent_name": "Milo",
            "business_name": config.get("name", ""),
        })

        # ── Send greeting (only for new sessions) ────────────────────
        if not existing_session_id:
            business_name = config.get("name", "us")
            greeting = (
                f"Hi{' ' + visitor_name if visitor_name and visitor_name != 'Visitor' else ''}! "
                f"I'm Milo, your assistant at {business_name}. How can I help you today?"
            )
            await send_frame(websocket, {
                "type": "greeting",
                "content": greeting,
                "agent_name": "Milo",
            })
            add_chat_message(session_id=session_id, role="ai", content=greeting, confidence_score=1.0)
        else:
            # For reconnect, send existing message history
            history = get_session_messages(session_id)
            for msg in history:
                msg_type = "human_message" if msg["role"] == "human" else (
                    "ai_done" if msg["role"] == "ai" else "system"
                )
                if msg["role"] == "visitor":
                    continue  # Visitor already has their own messages
                await send_frame(websocket, {
                    "type": msg_type,
                    "content": msg["content"],
                })

        # ── Ping task (keep connection alive) ────────────────────────
        async def ping_loop():
            while True:
                await asyncio.sleep(25)
                try:
                    await send_frame(websocket, {"type": "ping"})
                except Exception:
                    break

        ping_task = asyncio.create_task(ping_loop())

        # ── Message loop ─────────────────────────────────────────────
        try:
            while True:
                # Receive with timeout (session timeout)
                try:
                    raw_data = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=1800,  # 30 minute session timeout
                    )
                except asyncio.TimeoutError:
                    await send_frame(websocket, {
                        "type": "session_ended",
                        "content": "Session timed out due to inactivity.",
                    })
                    break

                msg_type = raw_data.get("type", "")
                last_activity = time.time()

                # ── Pong response ────────────────────────────────────
                if msg_type == "pong":
                    continue

                # ── Visitor message ──────────────────────────────────
                if msg_type != "message":
                    continue

                content = raw_data.get("content", "").strip()
                if not content:
                    continue

                # Save visitor message
                add_chat_message(session_id=session_id, role="visitor", content=content)

                # Check if human has taken over
                session_state = get_chat_session(session_id)
                if session_state and session_state.get("human_active"):
                    # Human is active — don't generate AI response.
                    # The dashboard will relay owner messages via the admin endpoint.
                    continue

                # ── Generate AI response ─────────────────────────────

                # Send typing indicator
                await send_frame(websocket, {"type": "typing"})

                # Brief artificial delay for natural feel (1-1.5 seconds)
                await asyncio.sleep(1.2)

                # Get conversation history for context
                history = get_session_messages(session_id, limit=20)

                # Stream Claude response
                full_response = ""
                try:
                    async for chunk in ai_service.stream_chat_response(
                        business_config=config,
                        message_history=history[:-1],  # exclude the message we just added
                        visitor_message=content,
                        visitor_name=visitor_name,
                    ):
                        full_response += chunk
                        await send_frame(websocket, {
                            "type": "ai_chunk",
                            "content": chunk,
                        })
                except Exception as e:
                    logger.error(f"AI stream error in session {session_id}: {e}")
                    full_response = f"I'm sorry, I'm having trouble right now. Please call us at {config.get('phone', 'our office')} for immediate help."
                    await send_frame(websocket, {
                        "type": "ai_chunk",
                        "content": full_response,
                    })

                # Calculate confidence
                confidence = await ai_service.get_confidence_score(full_response, config)

                # Send ai_done frame
                await send_frame(websocket, {
                    "type": "ai_done",
                    "content": full_response,
                    "confidence": confidence,
                })

                # Save AI message
                add_chat_message(
                    session_id=session_id, role="ai",
                    content=full_response, confidence_score=confidence,
                )

                ai_exchange_count += 1

        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: session={session_id}")
    except asyncio.TimeoutError:
        logger.info(f"WebSocket init timeout: business={business_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        if session_id:
            manager.disconnect(session_id)
            # Don't end session on disconnect — visitor might reconnect
            # Sessions are ended by timeout or explicit close


# ── Admin endpoint: relay owner message to visitor ───────────────────────────

@router.post("/api/v1/chat/sessions/{session_id}/message")
async def owner_send_message(session_id: str, body: dict):
    """
    Business owner sends a message to the visitor via REST.
    The message is relayed to the visitor's WebSocket if connected.
    """
    content = body.get("content", "").strip()
    if not content:
        return {"error": "Empty message"}

    # Save to database
    add_chat_message(session_id=session_id, role="human", content=content)

    # Relay to visitor's WebSocket if connected
    ws = manager.get_websocket(session_id)
    if ws:
        await send_frame(ws, {
            "type": "human_message",
            "content": content,
        })
        return {"status": "delivered"}

    return {"status": "saved", "note": "Visitor not currently connected"}


@router.post("/api/v1/chat/sessions/{session_id}/takeover")
async def takeover_session(session_id: str, body: dict):
    """Owner takes over the chat (Claude stops responding)."""
    from app.services.chat_service import set_human_active
    human_active = body.get("human_active", True)
    set_human_active(session_id, human_active)

    ws = manager.get_websocket(session_id)
    if ws:
        if human_active:
            await send_frame(ws, {
                "type": "human_takeover",
                "content": "A team member has joined the chat.",
            })
        else:
            await send_frame(ws, {
                "type": "human_handback",
                "content": "Our AI assistant is back to help you.",
            })

    return {"status": "ok", "human_active": human_active}


@router.get("/api/v1/chat/sessions")
async def list_chat_sessions(business_id: str):
    """List active chat sessions for a business (used by dashboard)."""
    from app.services.chat_service import get_active_sessions
    sessions = get_active_sessions(business_id)

    # Enrich with last message info
    for session in sessions:
        messages = get_session_messages(session["id"], limit=1)
        if messages:
            last = messages[-1] if messages else None
            # Get the actual last message (need to query desc)
            db = __import__('app.core.database', fromlist=['get_db']).get_db()
            last_res = db.table("chat_messages").select("content, sent_at, role").eq(
                "session_id", session["id"]
            ).order("sent_at", desc=True).limit(1).execute()
            if last_res.data:
                session["last_message"] = last_res.data[0]["content"][:100]
                session["last_message_at"] = last_res.data[0]["sent_at"]

    return {"sessions": sessions, "count": len(sessions)}


@router.get("/api/v1/chat/sessions/{session_id}/messages")
async def get_chat_messages(session_id: str):
    """Get all messages for a chat session (used by dashboard)."""
    messages = get_session_messages(session_id, limit=200)
    return {"messages": messages}
