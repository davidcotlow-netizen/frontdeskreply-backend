from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from collections import Counter
from app.core.database import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _date_range(period: str):
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), now.isoformat()


@router.get("/summary")
async def dashboard_summary(business_id: str, period: str = "today"):
    """Chatbot-focused analytics summary."""
    db = get_db()
    start, end = _date_range(period)

    # ── Chat sessions ────────────────────────────────────────────
    sessions_res = db.table("chat_sessions").select(
        "id, started_at, ended_at, status, visitor_name, visitor_email, metadata"
    ).eq("business_id", business_id).gte("started_at", start).lte("started_at", end).execute()
    sessions = sessions_res.data or []

    total_conversations = len(sessions)
    active_now = sum(1 for s in sessions if s.get("status") == "active")

    # Count leads with email or phone captured
    leads_with_email = 0
    leads_with_phone = 0
    for s in sessions:
        if s.get("visitor_email"):
            leads_with_email += 1
        metadata = s.get("metadata") or {}
        if metadata.get("visitor_phone"):
            leads_with_phone += 1

    # ── Chat messages ────────────────────────────────────────────
    total_messages = 0
    visitor_messages = 0
    ai_messages = 0
    response_times = []
    all_visitor_texts = []

    for session in sessions:
        msgs_res = db.table("chat_messages").select(
            "role, content, sent_at"
        ).eq("session_id", session["id"]).order("sent_at", desc=False).execute()
        msgs = msgs_res.data or []
        total_messages += len(msgs)

        for msg in msgs:
            if msg["role"] == "visitor":
                visitor_messages += 1
                all_visitor_texts.append(msg["content"])
            elif msg["role"] == "ai":
                ai_messages += 1

        # Calculate response times (visitor msg → next AI msg)
        for i in range(len(msgs) - 1):
            if msgs[i]["role"] == "visitor" and msgs[i + 1]["role"] == "ai":
                try:
                    v = datetime.fromisoformat(msgs[i]["sent_at"].replace("Z", "+00:00"))
                    a = datetime.fromisoformat(msgs[i + 1]["sent_at"].replace("Z", "+00:00"))
                    response_times.append((a - v).total_seconds())
                except Exception:
                    pass

    avg_response = round(sum(response_times) / len(response_times), 1) if response_times else None
    avg_chat_length = round(total_messages / total_conversations, 1) if total_conversations > 0 else 0

    # Conversion rate: conversations that captured an email
    conversion_rate = round((leads_with_email / total_conversations) * 100) if total_conversations > 0 else 0

    # Chatbot accuracy: % of AI responses that didn't contain fallback/phone-redirect language
    confident_responses = 0
    total_ai_responses = 0
    fallback_phrases = ["give us a call", "call us at", "reach out to", "contact us directly",
                        "let me connect you", "for the best answer"]
    for session in sessions:
        msgs_res = db.table("chat_messages").select("content, role").eq(
            "session_id", session["id"]
        ).eq("role", "ai").execute()
        for msg in (msgs_res.data or []):
            total_ai_responses += 1
            content_lower = (msg.get("content") or "").lower()
            if not any(phrase in content_lower for phrase in fallback_phrases):
                confident_responses += 1

    accuracy_rate = round((confident_responses / total_ai_responses) * 100) if total_ai_responses > 0 else 0

    # ── Call metrics ────────────────────────────────────────────
    calls_res = db.table("call_sessions").select(
        "id, duration_seconds, status"
    ).eq("business_id", business_id).gte("started_at", start).lte("started_at", end).execute()
    calls = calls_res.data or []

    total_calls = len(calls)
    total_call_seconds = sum(c.get("duration_seconds") or 0 for c in calls)
    total_call_minutes = round(total_call_seconds / 60, 1) if total_call_seconds > 0 else 0
    avg_call_duration = round(total_call_seconds / total_calls) if total_calls > 0 else 0
    active_calls = sum(1 for c in calls if c.get("status") == "active")

    return {
        "total_conversations": total_conversations,
        "total_messages": total_messages,
        "visitor_messages": visitor_messages,
        "ai_messages": ai_messages,
        "avg_response_seconds": avg_response,
        "avg_chat_length": avg_chat_length,
        "active_now": active_now,
        "leads_with_email": leads_with_email,
        "leads_with_phone": leads_with_phone,
        "conversion_rate": conversion_rate,
        "accuracy_rate": accuracy_rate,
        "total_calls": total_calls,
        "total_call_minutes": total_call_minutes,
        "avg_call_duration_seconds": avg_call_duration,
        "active_calls": active_calls,
        "period": period,
    }


@router.get("/conversations-by-day")
async def conversations_by_day(business_id: str, period: str = "week"):
    """Chat conversations grouped by day for charting."""
    db = get_db()
    start, end = _date_range(period)

    sessions_res = db.table("chat_sessions").select(
        "started_at"
    ).eq("business_id", business_id).gte("started_at", start).lte("started_at", end).execute()

    # Group by date
    by_day: dict[str, int] = {}
    for s in (sessions_res.data or []):
        try:
            day = datetime.fromisoformat(s["started_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
            by_day[day] = by_day.get(day, 0) + 1
        except Exception:
            pass

    # Fill in missing days
    now = datetime.now(timezone.utc)
    if period == "today":
        days = 1
    elif period == "week":
        days = 7
    else:
        days = 30

    result = []
    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        result.append({"date": day, "count": by_day.get(day, 0)})

    return {"data": result}


@router.get("/calls-by-day")
async def calls_by_day(business_id: str, period: str = "week"):
    """Phone calls grouped by day for charting."""
    db = get_db()
    start, end = _date_range(period)

    calls_res = db.table("call_sessions").select(
        "started_at"
    ).eq("business_id", business_id).gte("started_at", start).lte("started_at", end).execute()

    by_day: dict[str, int] = {}
    for c in (calls_res.data or []):
        try:
            day = datetime.fromisoformat(c["started_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
            by_day[day] = by_day.get(day, 0) + 1
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    days = 1 if period == "today" else 7 if period == "week" else 30
    result = []
    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        result.append({"date": day, "count": by_day.get(day, 0)})

    return {"data": result}


@router.get("/top-questions")
async def top_questions(business_id: str, period: str = "month"):
    """Top visitor questions/messages from chat conversations."""
    db = get_db()
    start, end = _date_range(period)

    sessions_res = db.table("chat_sessions").select("id").eq(
        "business_id", business_id
    ).gte("started_at", start).lte("started_at", end).execute()

    session_ids = [s["id"] for s in (sessions_res.data or [])]
    if not session_ids:
        return {"questions": [], "total": 0}

    # Get all visitor messages
    visitor_texts = []
    for sid in session_ids:
        msgs_res = db.table("chat_messages").select("content").eq(
            "session_id", sid
        ).eq("role", "visitor").execute()
        for m in (msgs_res.data or []):
            text = (m.get("content") or "").strip()
            if text and len(text) > 5:  # Skip very short messages like "hi"
                visitor_texts.append(text)

    # Simple frequency — group similar short messages, show unique longer ones
    # For now, return the most common messages
    counter = Counter()
    for text in visitor_texts:
        # Normalize: lowercase, strip punctuation for grouping
        normalized = text.lower().strip("?!., ")
        counter[normalized] += 1

    # Map back to original casing (use first occurrence)
    original_map = {}
    for text in visitor_texts:
        normalized = text.lower().strip("?!., ")
        if normalized not in original_map:
            original_map[normalized] = text

    questions = [
        {"question": original_map.get(q, q), "count": c}
        for q, c in counter.most_common(15)
    ]

    return {"questions": questions, "total": len(visitor_texts)}


@router.get("/response-time-trend")
async def response_time_trend(business_id: str, period: str = "week"):
    """Average AI response time by day."""
    db = get_db()
    start, end = _date_range(period)

    sessions_res = db.table("chat_sessions").select("id, started_at").eq(
        "business_id", business_id
    ).gte("started_at", start).lte("started_at", end).execute()

    daily_times: dict[str, list] = {}

    for session in (sessions_res.data or []):
        msgs_res = db.table("chat_messages").select(
            "role, sent_at"
        ).eq("session_id", session["id"]).order("sent_at", desc=False).execute()
        msgs = msgs_res.data or []

        for i in range(len(msgs) - 1):
            if msgs[i]["role"] == "visitor" and msgs[i + 1]["role"] == "ai":
                try:
                    v = datetime.fromisoformat(msgs[i]["sent_at"].replace("Z", "+00:00"))
                    a = datetime.fromisoformat(msgs[i + 1]["sent_at"].replace("Z", "+00:00"))
                    day = v.strftime("%Y-%m-%d")
                    daily_times.setdefault(day, []).append((a - v).total_seconds())
                except Exception:
                    pass

    # Fill missing days
    now = datetime.now(timezone.utc)
    days = 7 if period == "week" else 30 if period == "month" else 1
    result = []
    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        times = daily_times.get(day, [])
        avg = round(sum(times) / len(times), 1) if times else None
        result.append({"date": day, "avg_seconds": avg, "count": len(times)})

    return {"data": result}
