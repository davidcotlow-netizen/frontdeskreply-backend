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


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str):
    db = get_db()
    res = db.table("conversations").select(
        "*, contacts(*), channels(channel_type, external_identifier)"
    ).eq("id", conversation_id).maybe_single().execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return res.data


@router.get("/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    """All messages in a conversation — used by Claude for draft context."""
    db = get_db()
    res = db.table("inbound_messages").select(
        "*, response_drafts!message_id(draft_body), sent_responses!message_id(body_sent, sent_at)"
    ).eq("conversation_id", conversation_id).order("received_at", desc=False).execute()

    return {"messages": res.data or [], "conversation_id": conversation_id}


@router.get("/sent")
async def get_sent_messages(
    business_id: str,
    limit: int = 50,
    offset: int = 0,
    period: str = "month"
):
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