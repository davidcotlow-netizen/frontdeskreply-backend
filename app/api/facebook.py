"""
Facebook Messenger + Instagram DMs — Frontdesk AI
Handles Meta webhook events for both Facebook Messenger and Instagram Direct Messages.
Enterprise plan only.

Setup:
1. Create Meta App at developers.facebook.com
2. Add Messenger and Instagram products
3. Set webhook URL: https://api.frontdeskreply.com/api/v1/meta/webhook
4. Subscribe to messages events
5. Store Page Access Token per business in channels table
"""

import logging
import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Response

from app.core.database import get_db
from app.services.chat_service import get_business_chat_config, _find_or_create_contact
from app.services.chat_ai_service import get_chat_ai_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meta", tags=["meta"])

# Meta webhook verify token (set this in Meta App dashboard)
VERIFY_TOKEN = "frontdeskreply-meta-verify-2026"


@router.get("/webhook")
async def meta_webhook_verify(request: Request):
    """
    Meta sends a GET request to verify the webhook URL.
    Must return the hub.challenge value.
    """
    params = dict(request.query_params)
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Meta webhook verified")
        return Response(content=challenge, media_type="text/plain")

    logger.warning(f"Meta webhook verification failed: mode={mode}")
    return Response(content="Forbidden", status_code=403)


@router.post("/webhook")
async def meta_webhook_receive(request: Request):
    """
    Meta sends POST requests when messages arrive on Facebook Messenger or Instagram.
    Vela responds using FAQs, reply sent back via Meta Graph API.
    """
    try:
        data = await request.json()
    except Exception:
        return {"status": "error"}

    obj = data.get("object", "")

    # Handle page (Facebook) and instagram events
    if obj not in ("page", "instagram"):
        return {"status": "ignored"}

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id", "")
            recipient_id = event.get("recipient", {}).get("id", "")
            message = event.get("message", {})
            message_text = message.get("text", "").strip()

            if not message_text or not sender_id:
                continue

            # Determine channel type
            channel = "instagram" if obj == "instagram" else "facebook"

            logger.info(f"Meta {channel} message: from={sender_id} to={recipient_id} text={message_text[:100]}")

            # Look up business by page/account ID
            business = get_business_by_meta_id(recipient_id)
            if not business:
                logger.warning(f"No business found for Meta ID: {recipient_id}")
                continue

            business_id = business["business_id"]
            page_token = business.get("page_token", "")

            # Check Enterprise plan
            db = get_db()
            plan_res = db.table("subscription_plans").select("plan_tier").eq(
                "business_id", business_id
            ).eq("status", "active").maybe_single().execute()
            if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") != "enterprise":
                continue

            config = get_business_chat_config(business_id)
            if not config:
                continue

            # Save sender as lead
            sender_name = f"{channel.capitalize()} User"
            try:
                # Try to get sender profile from Meta
                profile_res = httpx.get(
                    f"https://graph.facebook.com/{sender_id}",
                    params={"fields": "first_name,last_name", "access_token": page_token},
                    timeout=5,
                )
                if profile_res.status_code == 200:
                    profile = profile_res.json()
                    sender_name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
            except Exception:
                pass

            _find_or_create_contact(db, business_id, name=sender_name)

            # Get or create session
            session_id = _get_or_create_meta_session(db, business_id, sender_id, sender_name, channel)

            # Save inbound message
            db.table("chat_messages").insert({
                "session_id": session_id,
                "role": "visitor",
                "content": message_text,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

            # Get conversation history
            history_res = db.table("chat_messages").select("role, content").eq(
                "session_id", session_id
            ).order("sent_at", desc=False).limit(20).execute()
            history = [{"role": "visitor" if m["role"] == "visitor" else "ai", "content": m["content"]} for m in (history_res.data or [])]

            # Generate Vela response
            ai_service = get_chat_ai_service()
            full_response = ""
            try:
                async for chunk in ai_service.stream_chat_response(
                    business_config=config,
                    message_history=history[:-1],
                    visitor_message=message_text,
                ):
                    full_response += chunk
            except Exception as e:
                logger.error(f"Meta AI error: {e}")
                full_response = f"Thanks for reaching out! We'll get back to you shortly."

            full_response = full_response.replace("**", "*").strip()  # Convert to Messenger-friendly bold
            if len(full_response) > 2000:
                full_response = full_response[:1997] + "..."

            # Save Vela's response
            db.table("chat_messages").insert({
                "session_id": session_id,
                "role": "ai",
                "content": full_response,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

            # Send reply via Meta Graph API
            if page_token:
                try:
                    send_res = httpx.post(
                        "https://graph.facebook.com/v18.0/me/messages",
                        params={"access_token": page_token},
                        json={
                            "recipient": {"id": sender_id},
                            "message": {"text": full_response},
                        },
                        timeout=10,
                    )
                    logger.info(f"Meta reply sent: {send_res.status_code}")
                except Exception as e:
                    logger.error(f"Meta send error: {e}")

    return {"status": "ok"}


def get_business_by_meta_id(page_id: str) -> dict | None:
    """Look up business by Facebook Page ID or Instagram Account ID."""
    db = get_db()

    # Check channels for facebook or instagram type
    for ctype in ("facebook", "instagram"):
        res = db.table("channels").select("business_id, metadata").eq(
            "channel_type", ctype
        ).eq("external_identifier", page_id).maybe_single().execute()
        if res and res.data:
            meta = res.data.get("metadata") or {}
            return {
                "business_id": res.data["business_id"],
                "page_token": meta.get("page_token", ""),
            }

    return None


def _get_or_create_meta_session(db, business_id: str, sender_id: str, sender_name: str, channel: str) -> str:
    """Get existing active session or create new one for a Meta user."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    res = db.table("chat_sessions").select("id").eq(
        "business_id", business_id
    ).eq("visitor_email", f"{channel}:{sender_id}").eq(
        "status", "active"
    ).gte("started_at", cutoff).maybe_single().execute()

    if res and res.data:
        return res.data["id"]

    session = db.table("chat_sessions").insert({
        "business_id": business_id,
        "visitor_name": sender_name,
        "visitor_email": f"{channel}:{sender_id}",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "metadata": {"channel": channel, "sender_id": sender_id},
    }).execute()
    return session.data[0]["id"]
