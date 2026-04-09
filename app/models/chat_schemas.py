"""
Chat Schemas — Frontdesk AI
Pydantic models for the live chat WebSocket feature.
Separate from schemas.py to avoid modifying the existing async pipeline models.
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


# ── WebSocket frame types ────────────────────────────────────────────────────

class InboundChatFrame(BaseModel):
    """JSON frame received from the visitor's browser via WebSocket."""
    type: Literal["init", "message", "escalate_request", "pong"]
    content: Optional[str] = None
    visitor_name: Optional[str] = None
    visitor_email: Optional[str] = None
    visitor_phone: Optional[str] = None
    session_id: Optional[str] = None  # for reconnecting to existing session


class OutboundChatFrame(BaseModel):
    """JSON frame sent to the visitor's browser via WebSocket."""
    type: Literal[
        "session_created", "greeting", "typing", "ai_chunk", "ai_done",
        "system", "human_message", "human_takeover", "human_handback",
        "error", "ping", "session_ended"
    ]
    content: Optional[str] = None
    session_id: Optional[str] = None
    confidence: Optional[float] = None
    agent_name: Optional[str] = None
    business_name: Optional[str] = None


# ── Dashboard admin frames ───────────────────────────────────────────────────

class OwnerMessageRequest(BaseModel):
    """Owner sends a message to the visitor from the dashboard."""
    content: str


class TakeoverRequest(BaseModel):
    """Owner takes over or hands back a chat session."""
    human_active: bool


# ── Chat session & message models ────────────────────────────────────────────

class ChatSessionResponse(BaseModel):
    id: str
    business_id: str
    visitor_name: Optional[str] = None
    visitor_email: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    human_active: bool = False
    escalated: bool = False
    status: str = "active"
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None
    message_count: int = 0


class ChatMessageResponse(BaseModel):
    id: str
    session_id: str
    role: Literal["visitor", "ai", "human"]
    content: str
    sent_at: datetime
    confidence_score: Optional[float] = None
