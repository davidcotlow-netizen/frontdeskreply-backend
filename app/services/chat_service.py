"""
Chat Service — Frontdesk AI
CRUD operations for live chat sessions and messages.
Uses the same Supabase client and patterns as webhooks.py.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.core.database import get_db

logger = logging.getLogger(__name__)


# ── Session operations ───────────────────────────────────────────────────────

def create_chat_session(
    business_id: str,
    visitor_name: Optional[str] = None,
    visitor_email: Optional[str] = None,
) -> dict:
    """Create a new chat session. Returns the full row."""
    db = get_db()
    res = db.table("chat_sessions").insert({
        "business_id": business_id,
        "visitor_name": visitor_name,
        "visitor_email": visitor_email,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
    }).execute()
    session = res.data[0]
    logger.info(f"Chat session created: {session['id']} for business {business_id}")
    return session


def get_chat_session(session_id: str) -> Optional[dict]:
    """Fetch a chat session by ID."""
    db = get_db()
    res = db.table("chat_sessions").select("*").eq("id", session_id).maybe_single().execute()
    return res.data if res else None


def end_chat_session(session_id: str) -> None:
    """Mark a chat session as ended."""
    db = get_db()
    db.table("chat_sessions").update({
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "status": "ended",
    }).eq("id", session_id).execute()
    logger.info(f"Chat session ended: {session_id}")


def set_human_active(session_id: str, active: bool) -> None:
    """Toggle human takeover on a chat session."""
    db = get_db()
    db.table("chat_sessions").update({
        "human_active": active,
    }).eq("id", session_id).execute()
    logger.info(f"Chat session {session_id} human_active={active}")


def set_session_escalated(session_id: str) -> None:
    """Mark a chat session as escalated."""
    db = get_db()
    db.table("chat_sessions").update({
        "escalated": True,
        "status": "escalated",
    }).eq("id", session_id).execute()
    logger.info(f"Chat session escalated: {session_id}")


def get_active_sessions(business_id: str) -> list:
    """Get all active chat sessions for a business, most recent first."""
    db = get_db()
    res = db.table("chat_sessions").select("*").eq(
        "business_id", business_id
    ).in_("status", ["active", "escalated"]).order(
        "started_at", desc=True
    ).execute()
    return res.data or []


# ── Message operations ───────────────────────────────────────────────────────

def add_chat_message(
    session_id: str,
    role: str,
    content: str,
    confidence_score: Optional[float] = None,
) -> dict:
    """Insert a chat message. Returns the full row."""
    db = get_db()
    row = {
        "session_id": session_id,
        "role": role,
        "content": content,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    if confidence_score is not None:
        row["confidence_score"] = confidence_score

    res = db.table("chat_messages").insert(row).execute()
    return res.data[0]


def get_session_messages(session_id: str, limit: int = 50) -> list:
    """Get messages for a session, oldest first (for display and Claude context)."""
    db = get_db()
    res = db.table("chat_messages").select("*").eq(
        "session_id", session_id
    ).order("sent_at", desc=False).limit(limit).execute()
    return res.data or []


# ── Business eligibility ─────────────────────────────────────────────────────

def check_business_chat_eligible(business_id: str) -> bool:
    """Check if business exists and is on Growth or Pro tier (live chat enabled)."""
    db = get_db()
    plan_res = db.table("subscription_plans").select(
        "plan_tier"
    ).eq("business_id", business_id).eq("status", "active").maybe_single().execute()

    if not plan_res or not plan_res.data:
        return False

    tier = plan_res.data.get("plan_tier", "starter")
    return tier in ("growth", "pro")


def get_business_chat_config(business_id: str) -> Optional[dict]:
    """
    Load everything needed to power the chat AI:
    business profile, FAQs, and tone settings.
    Returns None if business not found.
    """
    db = get_db()

    # Business profile
    biz_res = db.table("businesses").select("*").eq("id", business_id).maybe_single().execute()
    if not biz_res or not biz_res.data:
        return None
    business = biz_res.data

    # Active FAQs
    faq_res = db.table("faqs").select("question, answer, category").eq(
        "business_id", business_id
    ).eq("active", True).execute()
    faqs = faq_res.data or []

    return {
        "business_id": business_id,
        "name": business.get("name", ""),
        "type": business.get("business_type", ""),
        "city": business.get("city", ""),
        "phone": business.get("phone", ""),
        "hours": business.get("hours", ""),
        "emergency_policy": business.get("emergency_policy", ""),
        "service_areas": business.get("service_areas", ""),
        "tone": business.get("tone", "professional but warm"),
        "email": business.get("email", ""),
        "owner_phone": business.get("phone", ""),
        "faqs": faqs,
    }
