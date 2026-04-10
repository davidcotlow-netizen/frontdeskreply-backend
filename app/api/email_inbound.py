"""
Email Inbound — Frontdesk AI
Receives forwarded emails and auto-replies using Vela AI + FAQs.
Available on ALL plans.

Setup: Business forwards their inbox to leads-{business_id}@frontdeskreply.com
We receive the email, Vela generates a response, and we reply using the branded template.
"""

import logging
import re
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException

from app.core.database import get_db
from app.services.chat_service import get_business_chat_config, _find_or_create_contact
from app.services.chat_ai_service import get_chat_ai_service
from app.services.email_service import send_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email-inbound", tags=["email-inbound"])


@router.post("/receive")
async def receive_email(request: Request):
    """
    Receives inbound emails (via Resend webhook or direct POST).
    Vela reads the email, generates a response, and replies.
    """
    try:
        data = await request.json()
    except Exception:
        # Try form data (some email providers send as form)
        data = dict(await request.form())

    # Parse email fields — support multiple webhook formats
    sender_email = data.get("from") or data.get("sender") or data.get("From") or ""
    sender_name = data.get("from_name") or data.get("sender_name") or ""
    to_email = data.get("to") or data.get("To") or ""
    subject = data.get("subject") or data.get("Subject") or ""
    body = data.get("text") or data.get("body") or data.get("Body") or data.get("html") or ""

    # Clean up HTML from body if present
    if "<" in body and ">" in body:
        body = re.sub(r'<[^>]+>', '', body)  # Strip HTML tags
    body = body.strip()

    # Extract business_id from the "to" address: leads-{business_id}@frontdeskreply.com
    business_id = None
    to_lower = to_email.lower() if isinstance(to_email, str) else ""
    match = re.search(r'leads-([a-f0-9-]+)@', to_lower)
    if match:
        business_id = match.group(1)
    else:
        # Try looking up by the to email directly in channels
        db = get_db()
        ch_res = db.table("channels").select("business_id").eq(
            "channel_type", "email"
        ).eq("external_identifier", to_lower).maybe_single().execute()
        if ch_res and ch_res.data:
            business_id = ch_res.data["business_id"]

    if not business_id:
        logger.warning(f"Email inbound: no business found for {to_email}")
        return {"status": "skipped", "reason": "no_business_match"}

    logger.info(f"Email inbound: from={sender_email} to={to_email} subject={subject[:50]} business={business_id}")

    if not body or not sender_email:
        return {"status": "skipped", "reason": "empty_email"}

    # Check plan — email auto-reply requires Growth or above
    db = get_db()
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").maybe_single().execute()
    if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") not in ("growth", "pro", "enterprise"):
        return {"status": "skipped", "reason": "plan_not_eligible"}

    # Load business config
    config = get_business_chat_config(business_id)
    if not config:
        return {"status": "skipped", "reason": "business_not_found"}
    contact_name = sender_name or sender_email.split("@")[0]
    _find_or_create_contact(db, business_id, email=sender_email, name=contact_name)

    # Save inbound email as a chat session
    session = db.table("chat_sessions").insert({
        "business_id": business_id,
        "visitor_name": contact_name,
        "visitor_email": sender_email,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "ended",
        "metadata": {"channel": "email", "subject": subject},
    }).execute()
    session_id = session.data[0]["id"]

    # Save the inbound message
    db.table("chat_messages").insert({
        "session_id": session_id,
        "role": "visitor",
        "content": f"Subject: {subject}\n\n{body[:2000]}",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    # Generate Vela response
    ai_service = get_chat_ai_service()
    full_response = ""
    try:
        async for chunk in ai_service.stream_chat_response(
            business_config=config,
            message_history=[],
            visitor_message=f"Email subject: {subject}\n\nEmail body: {body[:1500]}",
            voice_mode=False,
        ):
            full_response += chunk
    except Exception as e:
        logger.error(f"Email AI error: {e}")
        full_response = f"Thank you for reaching out! We received your message and will get back to you shortly. For immediate help, give us a call at {config.get('phone', 'our office')}."

    # Clean response for email
    full_response = full_response.replace("**", "").strip()

    # Save Vela's response
    db.table("chat_messages").insert({
        "session_id": session_id,
        "role": "ai",
        "content": full_response,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    # Send branded reply email
    reply_subject = f"Re: {subject}" if subject else f"Reply from {config.get('name', 'us')}"
    send_email(
        to_email=sender_email,
        body=full_response,
        subject=reply_subject,
        customer_name=contact_name,
        business_id=business_id,
    )

    logger.info(f"Email auto-reply sent to {sender_email} for business {business_id}")

    return {"status": "replied", "to": sender_email, "session_id": session_id}


@router.post("/receive/{business_id}")
async def receive_email_by_id(business_id: str, request: Request):
    """
    Alternative endpoint with business_id in the URL.
    For direct webhook setups where the to-address parsing isn't needed.
    """
    try:
        data = await request.json()
    except Exception:
        data = dict(await request.form())

    # Inject business_id into the to field for the main handler
    data["to"] = f"leads-{business_id}@frontdeskreply.com"

    # Re-create the request context
    from starlette.requests import Request as StarletteRequest
    # Just call the main handler logic directly
    sender_email = data.get("from") or data.get("sender") or ""
    sender_name = data.get("from_name") or ""
    subject = data.get("subject") or ""
    body = data.get("text") or data.get("body") or ""

    if "<" in body and ">" in body:
        body = re.sub(r'<[^>]+>', '', body)
    body = body.strip()

    if not body or not sender_email:
        return {"status": "skipped", "reason": "empty_email"}

    config = get_business_chat_config(business_id)
    if not config:
        return {"status": "skipped", "reason": "business_not_found"}

    db = get_db()
    contact_name = sender_name or sender_email.split("@")[0]
    _find_or_create_contact(db, business_id, email=sender_email, name=contact_name)

    session = db.table("chat_sessions").insert({
        "business_id": business_id,
        "visitor_name": contact_name,
        "visitor_email": sender_email,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "ended",
        "metadata": {"channel": "email", "subject": subject},
    }).execute()
    session_id = session.data[0]["id"]

    db.table("chat_messages").insert({
        "session_id": session_id, "role": "visitor",
        "content": f"Subject: {subject}\n\n{body[:2000]}",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    ai_service = get_chat_ai_service()
    full_response = ""
    try:
        async for chunk in ai_service.stream_chat_response(
            business_config=config, message_history=[],
            visitor_message=f"Email subject: {subject}\n\nEmail body: {body[:1500]}",
        ):
            full_response += chunk
    except Exception as e:
        logger.error(f"Email AI error: {e}")
        full_response = f"Thank you for reaching out! We will get back to you shortly."

    full_response = full_response.replace("**", "").strip()

    db.table("chat_messages").insert({
        "session_id": session_id, "role": "ai", "content": full_response,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    reply_subject = f"Re: {subject}" if subject else f"Reply from {config.get('name', 'us')}"
    send_email(to_email=sender_email, body=full_response, subject=reply_subject,
               customer_name=contact_name, business_id=business_id)

    return {"status": "replied", "to": sender_email, "session_id": session_id}
