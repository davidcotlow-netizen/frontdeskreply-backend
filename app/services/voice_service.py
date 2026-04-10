"""
Voice Service — Frontdesk AI
CRUD operations for voice call sessions and transcripts.
Mirrors chat_service.py patterns for consistency.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.core.database import get_db
from app.services.chat_service import _find_or_create_contact, check_business_chat_eligible

logger = logging.getLogger(__name__)


# ── Session operations ───────────────────────────────────────────────────────

def create_call_session(
    business_id: str,
    caller_phone: Optional[str] = None,
    caller_name: Optional[str] = None,
    call_sid: Optional[str] = None,
) -> dict:
    """Create a new call session and save caller as a lead."""
    db = get_db()

    # Save caller as a lead in contacts table
    contact_id = _find_or_create_contact(
        db, business_id,
        name=caller_name,
        phone=caller_phone,
    )

    res = db.table("call_sessions").insert({
        "business_id": business_id,
        "caller_phone": caller_phone,
        "caller_name": caller_name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "call_sid": call_sid,
        "metadata": {"contact_id": contact_id},
    }).execute()
    session = res.data[0]
    logger.info(f"Call session created: {session['id']} for business {business_id}")
    return session


def end_call_session(session_id: str, duration_seconds: int = 0, caller_source: Optional[str] = None) -> None:
    """Mark a call session as ended with duration and optional source (how they heard about us)."""
    db = get_db()
    update_data = {
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "status": "ended",
        "duration_seconds": duration_seconds,
    }
    if caller_source:
        update_data["caller_source"] = caller_source
    db.table("call_sessions").update(update_data).eq("id", session_id).execute()
    logger.info(f"Call session ended: {session_id} ({duration_seconds}s) source={caller_source}")


def get_call_session(session_id: str) -> Optional[dict]:
    """Fetch a call session by ID."""
    db = get_db()
    res = db.table("call_sessions").select("*").eq("id", session_id).maybe_single().execute()
    return res.data if res else None


def get_active_calls(business_id: str) -> list:
    """Get all active call sessions for a business."""
    db = get_db()
    res = db.table("call_sessions").select("*").eq(
        "business_id", business_id
    ).eq("status", "active").order("started_at", desc=True).execute()
    return res.data or []


# ── Transcript operations ────────────────────────────────────────────────────

def add_call_transcript(
    session_id: str,
    role: str,
    content: str,
) -> dict:
    """Insert a transcript entry (caller speech or Milo response)."""
    db = get_db()
    res = db.table("call_transcripts").insert({
        "session_id": session_id,
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return res.data[0]


def get_call_transcripts(session_id: str, limit: int = 100) -> list:
    """Get transcripts for a call session, oldest first."""
    db = get_db()
    res = db.table("call_transcripts").select("*").eq(
        "session_id", session_id
    ).order("timestamp", desc=False).limit(limit).execute()
    return res.data or []


# ── Business phone mapping ───────────────────────────────────────────────────

def get_business_by_twilio_number(phone_number: str) -> Optional[dict]:
    """Look up which business owns a Twilio number (voice or SMS)."""
    db = get_db()
    # Check channels table for voice type
    res = db.table("channels").select("business_id").eq(
        "channel_type", "voice"
    ).eq("external_identifier", phone_number).maybe_single().execute()
    if res and res.data:
        return res.data

    # Also check SMS channel type (same number may be used for both)
    res = db.table("channels").select("business_id").eq(
        "channel_type", "sms"
    ).eq("external_identifier", phone_number).maybe_single().execute()
    if res and res.data:
        return res.data

    # Fallback: check businesses table directly for phone match
    clean = phone_number.replace("+1", "").replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    res = db.table("businesses").select("id, name").execute()
    for biz in (res.data or []):
        biz_phone = (biz.get("phone") or "").replace("+1", "").replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
        if biz_phone and biz_phone == clean:
            return {"business_id": biz["id"]}

    return None


def check_business_voice_eligible(business_id: str) -> bool:
    """Check if business is on Pro tier (voice is Pro-only)."""
    db = get_db()
    plan_res = db.table("subscription_plans").select(
        "plan_tier"
    ).eq("business_id", business_id).eq("status", "active").maybe_single().execute()

    if not plan_res or not plan_res.data:
        return False

    return plan_res.data.get("plan_tier") in ("pro", "enterprise")


# ── Call history for dashboard ───────────────────────────────────────────────

def get_call_history(business_id: str, period: str = "month") -> list:
    """Get past call sessions with transcripts for the dashboard."""
    from datetime import timedelta
    db = get_db()

    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "week":
        start = (now - timedelta(days=7)).isoformat()
    else:
        start = (now - timedelta(days=30)).isoformat()

    sessions_res = db.table("call_sessions").select("*").eq(
        "business_id", business_id
    ).gte("started_at", start).order("started_at", desc=True).execute()

    result = []
    for session in (sessions_res.data or []):
        transcripts = get_call_transcripts(session["id"])
        caller_msgs = [t for t in transcripts if t["role"] == "caller"]
        last_caller = caller_msgs[-1]["content"] if caller_msgs else ""

        result.append({
            **session,
            "transcript_count": len(transcripts),
            "last_caller_message": last_caller[:120],
            "transcripts": transcripts,
        })

    return result
