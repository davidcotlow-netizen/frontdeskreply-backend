from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from app.core.database import get_db

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("")
async def list_messages(
    business_id: str,
    status: Optional[str] = None,
    channel_type: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
):
    db = get_db()
    query = db.table("inbound_messages").select(
        "*, response_drafts!message_id(id, draft_body), approval_queue_items!message_id(id, status, priority)"
    ).eq("business_id", business_id).order("received_at", desc=True)

    if status:
        query = query.eq("status", status)
    if channel_type:
        query = query.eq("channel_type", channel_type)

    offset = (page - 1) * page_size
    res = query.range(offset, offset + page_size - 1).execute()
    return {"messages": res.data, "page": page, "page_size": page_size}


@router.get("/{message_id}")
async def get_message(message_id: str):
    db = get_db()
    res = db.table("inbound_messages").select(
        "*, response_drafts!message_id(*), approval_queue_items!message_id(*), escalation_events!message_id(*)"
    ).eq("id", message_id).maybe_single().execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Message not found")
    return res.data
