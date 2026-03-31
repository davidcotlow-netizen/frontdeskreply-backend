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
