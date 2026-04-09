from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from app.core.database import get_db
from app.models.schemas import DashboardSummary

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
    db = get_db()
    start, end = _date_range(period)

    # ── Email/SMS pipeline metrics ───────────────────────────────
    msgs_res = db.table("inbound_messages").select(
        "id, status, intent, urgency_score, received_at, processed_at"
    ).eq("business_id", business_id).gte("received_at", start).lte("received_at", end).execute()

    messages = msgs_res.data or []

    form_leads = len(messages)
    auto_handled = sum(1 for m in messages if m.get("status") == "sent" and
                       _was_auto_sent(db, m["id"]))
    human_reviewed = sum(1 for m in messages if m.get("status") == "sent" and
                         not _was_auto_sent(db, m["id"]))
    urgent = sum(1 for m in messages if (m.get("urgency_score") or 0) >= 4 and
                 m.get("status") not in ("sent", "dismissed"))
    booking_requests = sum(1 for m in messages if m.get("intent") == "booking_request")

    response_times = []
    for m in messages:
        if m.get("received_at") and m.get("processed_at"):
            try:
                r = datetime.fromisoformat(m["received_at"].replace("Z", "+00:00"))
                p = datetime.fromisoformat(m["processed_at"].replace("Z", "+00:00"))
                response_times.append((p - r).total_seconds())
            except Exception:
                pass

    # ── Live chat metrics ────────────────────────────────────────
    chat_sessions_res = db.table("chat_sessions").select(
        "id, started_at, status"
    ).eq("business_id", business_id).gte("started_at", start).lte("started_at", end).execute()
    chat_sessions = chat_sessions_res.data or []
    chat_count = len(chat_sessions)

    # Count total chat messages and calculate avg chat response time
    chat_message_count = 0
    chat_response_times = []
    for session in chat_sessions:
        chat_msgs_res = db.table("chat_messages").select(
            "role, sent_at"
        ).eq("session_id", session["id"]).order("sent_at", desc=False).execute()
        chat_msgs = chat_msgs_res.data or []
        chat_message_count += len(chat_msgs)

        # Calculate response times: time between visitor message and next AI message
        for i in range(len(chat_msgs) - 1):
            if chat_msgs[i]["role"] == "visitor" and chat_msgs[i + 1]["role"] == "ai":
                try:
                    v_time = datetime.fromisoformat(chat_msgs[i]["sent_at"].replace("Z", "+00:00"))
                    a_time = datetime.fromisoformat(chat_msgs[i + 1]["sent_at"].replace("Z", "+00:00"))
                    chat_response_times.append((a_time - v_time).total_seconds())
                except Exception:
                    pass

    # ── Combined metrics ─────────────────────────────────────────
    all_response_times = response_times + chat_response_times
    avg_response = sum(all_response_times) / len(all_response_times) if all_response_times else None
    total_leads = form_leads + chat_count

    # Chat conversations are all auto-handled by AI
    total_auto = auto_handled + chat_count

    return {
        "new_leads": total_leads,
        "avg_first_response_seconds": avg_response,
        "auto_handled_count": total_auto,
        "human_reviewed_count": human_reviewed,
        "urgent_count": urgent,
        "booking_requests_captured": booking_requests,
        "period": period,
        # Chat-specific metrics
        "chat_conversations": chat_count,
        "chat_messages": chat_message_count,
        "chat_avg_response_seconds": (
            sum(chat_response_times) / len(chat_response_times)
            if chat_response_times else None
        ),
        "form_leads": form_leads,
    }


@router.get("/intent-breakdown")
async def intent_breakdown(business_id: str, period: str = "today"):
    db = get_db()
    start, end = _date_range(period)
    res = db.table("inbound_messages").select("intent").eq(
        "business_id", business_id
    ).gte("received_at", start).lte("received_at", end).execute()

    counts = {}
    for m in (res.data or []):
        i = m.get("intent") or "unknown"
        counts[i] = counts.get(i, 0) + 1

    return {"data": [{"intent": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]}


@router.get("/response-time")
async def response_time_trend(business_id: str):
    """Hourly avg response time for past 7 days."""
    db = get_db()
    start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    res = db.table("inbound_messages").select(
        "received_at, processed_at"
    ).eq("business_id", business_id).gte("received_at", start).not_.is_(
        "processed_at", "null"
    ).execute()

    hourly = {}
    for m in (res.data or []):
        try:
            r = datetime.fromisoformat(m["received_at"].replace("Z", "+00:00"))
            p = datetime.fromisoformat(m["processed_at"].replace("Z", "+00:00"))
            hour_key = r.strftime("%Y-%m-%dT%H:00")
            diff = (p - r).total_seconds()
            if hour_key not in hourly:
                hourly[hour_key] = []
            hourly[hour_key].append(diff)
        except Exception:
            pass

    return {"data": [
        {"hour": h, "avg_seconds": round(sum(v) / len(v), 1)}
        for h, v in sorted(hourly.items())
    ]}


def _was_auto_sent(db, message_id: str) -> bool:
    try:
        res = db.table("sent_responses").select("auto_sent").eq(
            "message_id", message_id
        ).maybe_single().execute()
        return bool(res.data and res.data.get("auto_sent"))
    except Exception:
        return False

# Common English stop words to filter out of keyword extraction
_STOP_WORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "my","i","we","you","your","our","me","us","it","its","is","are","was",
    "were","be","been","being","have","has","had","do","does","did","will",
    "would","could","should","may","might","can","this","that","these","those",
    "they","them","their","there","here","when","where","what","how","who",
    "which","just","also","so","up","out","if","about","get","got","need",
    "want","like","know","help","please","hi","hello","hey","thanks","thank",
    "not","no","yes","re","ve","ll","am","as","by","from","any","some","more",
    "than","then","now","after","before","since","back","still","come","came",
    "call","let","make","look","see","go","going","time","day","work","home",
    "house","last","new","one","two","three","first","much","many","few","well",
    "really","very","quite","soon","today","tomorrow","week","month","year",
    "morning","afternoon","evening","night","service","services","company",
    "business","someone","something","anything","everything","nothing","think",
}

def _extract_keywords(texts: list[str], top_n: int = 12) -> list[dict]:
    """
    Extract top N meaningful keywords/bigrams from a list of message texts.
    Returns list of {phrase, count} sorted by frequency descending.
    """
    import re
    from collections import Counter

    unigrams: Counter = Counter()
    bigrams: Counter = Counter()

    for text in texts:
        # Lowercase, strip punctuation
        clean = re.sub(r"[^\w\s]", " ", text.lower())
        words = [w for w in clean.split() if w not in _STOP_WORDS and len(w) > 2]

        unigrams.update(words)
        # Build meaningful bigrams
        for i in range(len(words) - 1):
            bigram = f"{words[i]} {words[i+1]}"
            bigrams[bigram] += 1

    # Prefer bigrams that appear 2+ times (more specific/meaningful)
    candidates: Counter = Counter()
    for phrase, count in bigrams.items():
        if count >= 2:
            candidates[phrase] = count

    # Fill remaining slots with top unigrams not already covered by a bigram
    covered_words = set()
    for phrase in candidates:
        covered_words.update(phrase.split())

    for word, count in unigrams.most_common(30):
        if word not in covered_words and count >= 1:
            candidates[word] = count

    return [
        {"phrase": phrase, "count": count}
        for phrase, count in candidates.most_common(top_n)
    ]


@router.get("/top-keywords")
async def top_keywords(business_id: str, period: str = "month"):
    """
    Returns top keywords/phrases per intent group for the given period.
    Used to power the 'What Customers Are Asking' section on the analytics page.
    """
    db = get_db()
    start, end = _date_range(period)

    res = db.table("inbound_messages").select(
        "intent, body"
    ).eq("business_id", business_id).gte(
        "received_at", start
    ).lte("received_at", end).not_.is_("intent", "null").execute()

    messages = res.data or []

    # Group message bodies by intent
    intent_texts: dict[str, list[str]] = {}
    for m in messages:
        intent = m.get("intent") or "unknown"
        body = m.get("body") or ""
        if body:
            intent_texts.setdefault(intent, []).append(body)

    # Extract keywords per intent, sorted by message volume
    result = []
    for intent, texts in sorted(intent_texts.items(), key=lambda x: -len(x[1])):
        keywords = _extract_keywords(texts, top_n=12)
        result.append({
            "intent": intent,
            "message_count": len(texts),
            "keywords": keywords,
        })

    return {"data": result, "total_messages": len(messages)}