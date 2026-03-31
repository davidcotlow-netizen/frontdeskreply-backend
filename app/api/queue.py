"""
Approval Queue Endpoints — Frontdesk AI
Handles human review: approve, edit+send, dismiss.
Routes reply via SMS or email based on customer's contact preference.
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
import logging

from app.core.database import get_db
from app.services.sms_service import send_sms
from app.services.email_service import send_email
from app.models.schemas import ApproveRequest, EditAndSendRequest, DismissRequest

router = APIRouter(prefix="/queue", tags=["queue"])
logger = logging.getLogger(__name__)


@router.get("")
async def get_queue(business_id: str, status: str = "pending"):
    db = get_db()
    query = db.table("approval_queue_items").select(
        "*, inbound_messages!message_id(*), response_drafts!draft_id(id, draft_body)"
    ).eq("business_id", business_id).order("queued_at", desc=True)

    if status != "all":
        query = query.eq("status", status)

    res = query.execute()
    urgent_count = sum(1 for item in (res.data or []) if item.get("priority") == "urgent")
    return {"items": res.data, "urgent_count": urgent_count}


@router.get("/{item_id}")
async def get_queue_item(item_id: str):
    db = get_db()
    res = db.table("approval_queue_items").select(
        "*, inbound_messages!message_id(*), response_drafts!draft_id(*)"
    ).eq("id", item_id).maybe_single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Queue item not found")
    return res.data


@router.post("/{item_id}/approve")
async def approve(item_id: str, body: ApproveRequest):
    """Approve draft as-is and send via customer's preferred channel."""
    db = get_db()
    item = _get_pending_item(db, item_id)
    draft = _get_draft(db, item["draft_id"])

    send_result = _send_response(db, item, draft["draft_body"])

    now = datetime.now(timezone.utc).isoformat()
    db.table("approval_queue_items").update({
        "status": "approved",
        "assigned_to": None,
        "reviewed_at": now,
        "resolved_at": now,
    }).eq("id", item_id).execute()

    _write_audit(db, item, "approved", body.reviewer_id, {
        "draft_id": item["draft_id"],
        "send_result": send_result,
    })
    return {"status": "approved", "sent_at": now, "send_result": send_result}


@router.post("/{item_id}/edit-and-send")
async def edit_and_send(item_id: str, body: EditAndSendRequest):
    """Approve with human edits and send edited version."""
    db = get_db()
    item = _get_pending_item(db, item_id)
    original_draft = _get_draft(db, item["draft_id"])

    send_result = _send_response(db, item, body.edited_body, edited=True)

    now = datetime.now(timezone.utc).isoformat()
    db.table("approval_queue_items").update({
        "status": "edited_and_sent",
        "assigned_to": None,
        "edited_body": body.edited_body,
        "reviewed_at": now,
        "resolved_at": now,
    }).eq("id", item_id).execute()

    _write_audit(db, item, "edited_and_sent", body.reviewer_id, {
        "original_draft": original_draft["draft_body"],
        "edited_body": body.edited_body,
        "send_result": send_result,
    })
    return {"status": "edited_and_sent", "sent_at": now, "send_result": send_result}


@router.post("/{item_id}/dismiss")
async def dismiss(item_id: str, body: DismissRequest):
    """Dismiss without sending."""
    db = get_db()
    item = _get_pending_item(db, item_id)

    now = datetime.now(timezone.utc).isoformat()
    db.table("approval_queue_items").update({
        "status": "dismissed",
        "assigned_to": None,
        "reviewer_notes": body.reason,
        "resolved_at": now,
    }).eq("id", item_id).execute()

    db.table("inbound_messages").update({"status": "dismissed"}).eq("id", item["message_id"]).execute()
    _write_audit(db, item, "dismissed", body.reviewer_id, {"reason": body.reason})
    return {"status": "dismissed"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_pending_item(db, item_id: str) -> dict:
    res = db.table("approval_queue_items").select("*").eq("id", item_id).maybe_single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if res.data["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Item already resolved: {res.data['status']}")
    return res.data


def _get_draft(db, draft_id: str) -> dict:
    res = db.table("response_drafts").select("*").eq("id", draft_id).maybe_single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Draft not found")
    return res.data


def _send_response(db, item: dict, body_text: str, edited: bool = False) -> dict:
    """
    Send response via the customer's preferred channel (SMS or email).
    Falls back gracefully if preferred channel info is missing.
    """
    msg_res = db.table("inbound_messages").select(
        "channel_type, sender_identifier, sender_phone, sender_email, sender_name, contact_preference, business_id"
    ).eq("id", item["message_id"]).single().execute()
    message = msg_res.data

    preference = message.get("contact_preference") or "sms"
    phone = message.get("sender_phone") or message.get("sender_identifier")
    email = message.get("sender_email")
    sender_name = message.get("sender_name") or ""
    business_id = message.get("business_id") or ""

    send_result = {"status": "not_sent", "reason": "no_contact_info"}

    email_subject = item.get("email_subject") or f"Re: Your Service Request — {business_id}"

    if preference == "email" and email:
        send_result = send_email(
            to_email=email,
            body=body_text,
            subject=email_subject,
            customer_name=sender_name,
            business_id=business_id,
        )
        logger.info(f"Email reply sent to {email}: {send_result}")

    elif preference == "sms" or message.get("channel_type") == "sms":
        # SMS via Twilio
        if phone:
            send_result = send_sms(to_number=phone, body=body_text)
            logger.info(f"SMS sent to {phone}: {send_result}")
        else:
            logger.warning(f"SMS preferred but no phone number for message {item['message_id']}")
            send_result = {"status": "skipped", "reason": "no_phone_number"}

    else:
        # Web form with no preference stored — try SMS then email
        if phone:
            send_result = send_sms(to_number=phone, body=body_text)
        elif email:
            send_result = send_email(
                to_email=email,
                body=body_text,
                subject=email_subject,
                customer_name=sender_name,
                business_id=business_id,
            )

    # Always record the sent response
    db.table("sent_responses").insert({
        "draft_id": item["draft_id"],
        "message_id": item["message_id"],
        "business_id": message["business_id"],
        "body_sent": body_text,
        "send_method": preference,
        "sent_by": "human",
        "auto_sent": False,
    }).execute()

    db.table("inbound_messages").update({"status": "sent"}).eq("id", item["message_id"]).execute()
    return send_result


def _write_audit(db, item: dict, action: str, user_id: str, metadata: dict):
    db.table("audit_logs").insert({
        "business_id": item["business_id"],
        "entity_type": "approval_queue_item",
        "entity_id": item["id"],
        "action": action,
        "performed_by": user_id,
        "metadata_json": metadata,
    }).execute()