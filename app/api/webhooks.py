"""
Webhook Endpoints — Frontdesk AI
Handles inbound messages from SMS (Twilio), web forms, and chat widgets.
Returns 200 immediately, enqueues async Celery task.
"""

from fastapi import APIRouter, Request, HTTPException
from datetime import datetime, timezone
import logging

from app.core.database import get_db
from app.core.config import get_settings
from app.models.schemas import InboundFormPayload, InboundChatPayload, WebhookAck
from app.workers.tasks import process_inbound_message
import hashlib

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def validate_api_key(request: Request) -> str | None:
    """
    Validate an API key from the Authorization header.
    Returns business_id if valid, None if no key provided.
    Raises HTTPException if key is invalid or plan is not Enterprise.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer fdr_"):
        return None  # No API key — fall through to normal channel-based auth

    token = auth.replace("Bearer ", "").strip()
    key_hash = hashlib.sha256(token.encode()).hexdigest()

    db = get_db()
    res = db.table("api_keys").select("business_id, id").eq(
        "key_hash", key_hash
    ).eq("active", True).execute()

    if not res.data:
        raise HTTPException(status_code=401, detail="Invalid API key")

    key_row = res.data[0]
    business_id = key_row["business_id"]

    # Update last_used_at
    db.table("api_keys").update({
        "last_used_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", key_row["id"]).execute()

    # Verify Enterprise plan
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").execute()
    tier = plan_res.data[0].get("plan_tier", "starter") if plan_res.data else "starter"
    if tier != "enterprise":
        raise HTTPException(status_code=403, detail="API access requires Enterprise plan")

    return business_id


def _find_or_create_contact(db, business_id: str, phone: str = None, email: str = None, sender_name: str = None) -> str:
    """Look up contact by phone or email, or create new one. Returns contact_id."""
    # Try phone first
    if phone:
        try:
            res = db.table("contacts").select("id").eq("business_id", business_id).eq("phone", phone).maybe_single().execute()
            if res and res.data:
                updates = {"last_seen_at": datetime.now(timezone.utc).isoformat()}
                if email:
                    updates["email"] = email
                db.table("contacts").update(updates).eq("id", res.data["id"]).execute()
                return res.data["id"]
        except Exception:
            pass

    # Try email if no phone match
    if email:
        try:
            res = db.table("contacts").select("id").eq("business_id", business_id).eq("email", email).maybe_single().execute()
            if res and res.data:
                updates = {"last_seen_at": datetime.now(timezone.utc).isoformat()}
                if phone:
                    updates["phone"] = phone
                db.table("contacts").update(updates).eq("id", res.data["id"]).execute()
                return res.data["id"]
        except Exception:
            pass

    # Create new contact
    new_contact = db.table("contacts").insert({
        "business_id": business_id,
        "name": sender_name or "Unknown",
        "phone": phone,
        "email": email,
        "source_channel": "web_form",
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return new_contact.data[0]["id"]


def _find_or_create_conversation(db, business_id: str, contact_id: str, channel_id: str, channel_type: str) -> str:
    """Find open conversation or create new one. Returns conversation_id."""
    try:
        res = db.table("conversations").select("id").eq(
            "business_id", business_id
        ).eq("contact_id", contact_id).eq("status", "open").maybe_single().execute()

        if res and res.data:
            db.table("conversations").update({
                "last_message_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", res.data["id"]).execute()
            return res.data["id"]
    except Exception:
        pass

    new_conv = db.table("conversations").insert({
        "business_id": business_id,
        "contact_id": contact_id,
        "channel_id": channel_id,
        "channel_type": channel_type,
        "status": "open",
        "last_message_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return new_conv.data[0]["id"]


def _create_message_and_enqueue(
    db,
    business_id: str,
    channel_id: str,
    channel_type: str,
    body: str,
    sender_identifier: str,
    sender_name: str = None,
    sender_phone: str = None,
    sender_email: str = None,
    contact_preference: str = "sms",
) -> str:
    contact_id = _find_or_create_contact(
        db, business_id,
        phone=sender_phone,
        email=sender_email,
        sender_name=sender_name
    )
    conversation_id = _find_or_create_conversation(db, business_id, contact_id, channel_id, channel_type)

    msg = db.table("inbound_messages").insert({
        "business_id": business_id,
        "conversation_id": conversation_id,
        "contact_id": contact_id,
        "channel_id": channel_id,
        "channel_type": channel_type,
        "direction": "inbound",
        "body": body,
        "sender_identifier": sender_identifier,
        "sender_name": sender_name,
        "sender_phone": sender_phone,
        "sender_email": sender_email,
        "contact_preference": contact_preference,
        "status": "received",
        "received_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    message_id = msg.data[0]["id"]
    process_inbound_message.delay(message_id)
    logger.info(f"Enqueued message {message_id} via {channel_type} (reply via {contact_preference})")
    return message_id


# ── SMS (Twilio) ──────────────────────────────────────────────────────────────

@router.post("/sms")
async def sms_webhook(request: Request):
    settings = get_settings()
    form_data = await request.form()
    db = get_db()

    from_number = form_data.get("From", "")
    body = form_data.get("Body", "").strip()
    to_number = form_data.get("To", "")

    if not body:
        return {"status": "ignored", "reason": "empty_body"}

    channel_res = db.table("channels").select("id, business_id").eq(
        "external_identifier", to_number
    ).eq("channel_type", "sms").maybe_single().execute()

    if not channel_res or not channel_res.data:
        logger.warning(f"Received SMS to unknown number: {to_number}")
        raise HTTPException(status_code=400, detail="Unknown channel")

    message_id = _create_message_and_enqueue(
        db=db,
        business_id=channel_res.data["business_id"],
        channel_id=channel_res.data["id"],
        channel_type="sms",
        body=body,
        sender_identifier=from_number,
        sender_phone=from_number,
        contact_preference="sms",
    )

    return {"message_id": message_id, "status": "received"}


# ── Web Form ──────────────────────────────────────────────────────────────────

@router.post("/form", response_model=WebhookAck)
async def form_webhook(payload: InboundFormPayload):
    db = get_db()

    channel_res = db.table("channels").select("id, business_id").eq(
        "id", payload.channel_id
    ).eq("channel_type", "web_form").maybe_single().execute()

    if not channel_res or not channel_res.data:
        raise HTTPException(status_code=400, detail="Invalid channel")

    # Validate preference matches provided contact info
    preference = payload.contact_preference or "sms"
    if preference == "sms" and not payload.sender_phone:
        preference = "email"
    if preference == "email" and not payload.sender_email:
        preference = "sms"

    sender_identifier = payload.sender_phone or payload.sender_email or "unknown"

    message_id = _create_message_and_enqueue(
        db=db,
        business_id=channel_res.data["business_id"],
        channel_id=payload.channel_id,
        channel_type="web_form",
        body=payload.body,
        sender_identifier=sender_identifier,
        sender_name=payload.sender_name,
        sender_phone=payload.sender_phone,
        sender_email=payload.sender_email,
        contact_preference=preference,
    )

    return WebhookAck(message_id=message_id)


# ── Chat Widget ───────────────────────────────────────────────────────────────

@router.post("/chat", response_model=WebhookAck)
async def chat_webhook(payload: InboundChatPayload):
    db = get_db()

    channel_res = db.table("channels").select("id, business_id").eq(
        "id", payload.channel_id
    ).eq("channel_type", "chat_widget").maybe_single().execute()

    if not channel_res or not channel_res.data:
        raise HTTPException(status_code=400, detail="Invalid channel")

    message_id = _create_message_and_enqueue(
        db=db,
        business_id=channel_res.data["business_id"],
        channel_id=payload.channel_id,
        channel_type="chat_widget",
        body=payload.body,
        sender_identifier=payload.session_id or "unknown",
        sender_name=payload.sender_name,
        contact_preference="sms",
    )

    return WebhookAck(message_id=message_id)