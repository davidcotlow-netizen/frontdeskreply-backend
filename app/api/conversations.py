from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from app.core.database import get_db

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("")
async def list_conversations(
    business_id: str,
    status: str = "open",
    channel_type: str = None,
    page: int = 1,
    page_size: int = 25,
):
    db = get_db()
    query = db.table("conversations").select(
        "*, contacts(name, phone, email)"
    ).eq("business_id", business_id).order("last_message_at", desc=True)

    if status != "all":
        query = query.eq("status", status)
    if channel_type:
        query = query.eq("channel_type", channel_type)

    offset = (page - 1) * page_size
    res = query.range(offset, offset + page_size - 1).execute()
    return {"conversations": res.data, "page": page}


@router.get("/sent")
async def get_sent_messages(
    business_id: str,
    limit: int = 50,
    offset: int = 0,
    period: str = "month"
):
    """MUST be before /{conversation_id} to avoid route conflict."""
    db = get_db()
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "week":
        start = (now - timedelta(days=7)).isoformat()
    elif period == "month":
        start = (now - timedelta(days=30)).isoformat()
    else:
        start = (now - timedelta(days=30)).isoformat()

    sent_res = db.table("sent_responses").select(
        "id, message_id, body_sent, send_method, sent_by, auto_sent, sent_at"
    ).eq("business_id", business_id).gte("sent_at", start).order(
        "sent_at", desc=True
    ).range(offset, offset + limit - 1).execute()

    sent = sent_res.data or []
    if not sent:
        return {"sent": [], "total": 0}

    message_ids = [s["message_id"] for s in sent if s.get("message_id")]
    msgs_res = db.table("inbound_messages").select(
        "id, sender_name, sender_email, sender_phone, intent, body, received_at"
    ).in_("id", message_ids).execute()

    msgs_by_id = {m["id"]: m for m in (msgs_res.data or [])}

    result = []
    for s in sent:
        msg = msgs_by_id.get(s.get("message_id"), {})
        result.append({
            "id": s["id"],
            "sent_at": s["sent_at"],
            "auto_sent": s.get("auto_sent", False),
            "send_method": s.get("send_method", "email"),
            "body_sent": s.get("body_sent", ""),
            "customer_name": msg.get("sender_name", "Unknown"),
            "customer_email": msg.get("sender_email"),
            "customer_phone": msg.get("sender_phone"),
            "customer_message": msg.get("body", ""),
            "intent": msg.get("intent", "unknown"),
            "received_at": msg.get("received_at"),
        })

    return {"sent": result, "total": len(result)}


@router.get("/leads")
async def get_lead_database(business_id: str):
    """MUST be before /{conversation_id} to avoid route conflict."""
    db = get_db()

    try:
        res = db.table("inbound_messages").select(
            "sender_name, sender_email, sender_phone, intent, received_at"
        ).eq("business_id", business_id).order("received_at", desc=False).execute()
        messages = res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    leads: dict = {}

    for m in messages:
        email = (m.get("sender_email") or "").strip().lower() or None
        phone = (m.get("sender_phone") or "").strip() or None
        name = m.get("sender_name") or "Unknown"
        intent = m.get("intent") or "unknown"
        received = m.get("received_at") or ""

        key = email or phone or name

        if key not in leads:
            leads[key] = {
                "id": key,
                "name": name,
                "email": email,
                "phone": phone,
                "first_contact": received,
                "last_contact": received,
                "message_count": 0,
                "intents": [],
            }

        lead = leads[key]
        lead["message_count"] += 1
        lead["last_contact"] = received

        if name and name != "Unknown" and lead["name"] == "Unknown":
            lead["name"] = name
        if email and not lead["email"]:
            lead["email"] = email
        if phone and not lead["phone"]:
            lead["phone"] = phone
        if intent and intent not in lead["intents"]:
            lead["intents"].append(intent)

    result = []
    for lead in leads.values():
        top = lead["intents"][0] if lead["intents"] else "unknown"
        result.append({**lead, "top_intent": top})

    result.sort(key=lambda x: x["last_contact"], reverse=True)
    return {"leads": result, "total": len(result)}


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str):
    db = get_db()
    try:
        res = db.table("conversations").select(
            "*, contacts(*), channels(channel_type, external_identifier)"
        ).eq("id", conversation_id).execute()
        data = res.data[0] if res.data else None
    except Exception:
        data = None

    if not data:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return data


@router.get("/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    """All messages in a conversation — used by Claude for draft context."""
    db = get_db()
    res = db.table("inbound_messages").select(
        "*, response_drafts!message_id(draft_body), sent_responses!message_id(body_sent, sent_at)"
    ).eq("conversation_id", conversation_id).order("received_at", desc=False).execute()

    return {"messages": res.data or [], "conversation_id": conversation_id}


@router.post("/{conversation_id}/close")
async def close_conversation(conversation_id: str, user_id: str):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    res = db.table("conversations").update({
        "status": "closed",
        "closed_at": now,
    }).eq("id", conversation_id).execute()

    db.table("audit_logs").insert({
        "entity_type": "conversation",
        "entity_id": conversation_id,
        "action": "closed",
        "performed_by": user_id,
        "metadata_json": {},
    }).execute()

    return {"status": "closed", "closed_at": now}