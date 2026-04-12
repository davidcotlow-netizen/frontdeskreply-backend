"""
Settings Endpoints — Frontdesk AI
Lets business owners manage their profile, hours, FAQs, and emergency contact.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class BusinessProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    hours: Optional[str] = None
    emergency_policy: Optional[str] = None
    service_areas: Optional[str] = None
    tone: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    owner_email: Optional[str] = None

class FAQItem(BaseModel):
    id: Optional[str] = None
    question: str
    answer: str
    category: Optional[str] = "general"
    active: bool = True

class FAQUpdate(BaseModel):
    faqs: List[FAQItem]


# ── Retell Voice AI sync ─────────────────────────────────────────────────────

def _get_retell_config(business_id: str) -> dict | None:
    """
    Look up Retell IDs from the voice channel's config column.
    Returns dict with retell_llm_id and retell_agent_id, or None.
    """
    db = get_db()
    res = db.table("channels").select("config").eq(
        "business_id", business_id
    ).eq("channel_type", "voice").execute()

    for ch in (res.data or []):
        config = ch.get("config") or {}
        if config.get("retell_llm_id"):
            return config

    return None


def _sync_retell_prompt(business_id: str) -> dict:
    """
    Rebuild the voice prompt from current FAQs + business config and push
    it to the Retell LLM, then publish the agent so changes go live.

    Source of truth for IDs: channels.config where channel_type='voice'.
    """
    try:
        import httpx
        from app.core.config import get_settings
        from app.services.chat_service import get_business_chat_config
        from app.api.voice_provision import build_voice_prompt

        settings = get_settings()
        if not settings.retell_api_key:
            return {"status": "skipped", "reason": "no_api_key", "faq_count": 0}

        retell_config = _get_retell_config(business_id)
        if not retell_config:
            return {"status": "skipped", "reason": "no_retell_llm", "faq_count": 0}

        llm_id = retell_config["retell_llm_id"]
        agent_id = retell_config.get("retell_agent_id")

        config = get_business_chat_config(business_id)
        if not config:
            return {"status": "error", "reason": "business_not_found", "faq_count": 0}

        faq_count = len(config.get("faqs", []))
        prompt = build_voice_prompt(config)

        retell_headers = {
            "Authorization": f"Bearer {settings.retell_api_key}",
            "Content-Type": "application/json",
        }

        # Step 1: Update the LLM prompt
        for attempt in range(2):
            res = httpx.patch(
                f"https://api.retellai.com/update-retell-llm/{llm_id}",
                headers=retell_headers,
                json={"general_prompt": prompt},
                timeout=30,
            )

            if res.status_code == 200:
                break

            # Retry once on 5xx
            if res.status_code >= 500 and attempt == 0:
                logger.warning(f"Retell API returned {res.status_code}, retrying...")
                continue

            logger.error(f"Retell LLM sync failed ({res.status_code}): {res.text[:200]}")
            return {"status": "error", "reason": f"retell_api_{res.status_code}", "faq_count": faq_count}

        # Step 2: Publish the agent so the updated LLM goes live
        # Without this, Retell serves the old "published" version of the prompt.
        if agent_id:
            pub_res = httpx.post(
                f"https://api.retellai.com/publish-agent/{agent_id}",
                headers=retell_headers,
                json={},
                timeout=15,
            )
            if pub_res.status_code == 200:
                logger.info(f"Retell agent {agent_id} published with {faq_count} FAQs")
            else:
                logger.warning(f"Retell agent publish returned {pub_res.status_code}: {pub_res.text[:100]}")

        logger.info(f"Retell LLM {llm_id} synced with {faq_count} FAQs for business {business_id}")
        return {"status": "synced", "faq_count": faq_count}

    except httpx.TimeoutException:
        logger.error(f"Retell sync timeout for business {business_id}")
        return {"status": "error", "reason": "timeout", "faq_count": 0}
    except Exception as e:
        logger.error(f"Retell sync error for business {business_id}: {e}")
        return {"status": "error", "reason": str(e), "faq_count": 0}


# ── Business Profile ──────────────────────────────────────────────────────────

@router.get("/profile")
async def get_profile(business_id: str):
    db = get_db()
    res = db.table("businesses").select("*").eq("id", business_id).maybe_single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return res.data


@router.patch("/profile")
async def update_profile(business_id: str, body: BusinessProfileUpdate):
    db = get_db()
    updates = {k: v for k, v in body.dict().items() if v is not None and v != ""}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    res = db.table("businesses").update(updates).eq("id", business_id).execute()
    return {"status": "updated", "fields": list(updates.keys())}


# ── FAQs ──────────────────────────────────────────────────────────────────────

@router.post("/faqs/sync-voice")
async def sync_voice_faqs(business_id: str):
    """Push current FAQs to Retell Voice AI. Returns sync status and FAQ count."""
    result = _sync_retell_prompt(business_id)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result.get("reason", "Sync failed"))
    return result


@router.get("/faqs")
async def get_faqs(business_id: str):
    db = get_db()
    res = db.table("faqs").select("*").eq("business_id", business_id).order("category").execute()
    return {"faqs": res.data or []}


@router.post("/faqs")
async def create_faq(business_id: str, body: FAQItem):
    db = get_db()
    res = db.table("faqs").insert({
        "business_id": business_id,
        "question": body.question,
        "answer": body.answer,
        "category": body.category or "general",
        "active": body.active,
    }).execute()
    sync = _sync_retell_prompt(business_id)
    return {"status": "created", "faq": res.data[0], "voice_sync": sync}


@router.patch("/faqs/{faq_id}")
async def update_faq(faq_id: str, business_id: str, body: FAQItem):
    db = get_db()
    res = db.table("faqs").update({
        "question": body.question,
        "answer": body.answer,
        "category": body.category,
        "active": body.active,
    }).eq("id", faq_id).eq("business_id", business_id).execute()
    sync = _sync_retell_prompt(business_id)
    return {"status": "updated", "voice_sync": sync}


@router.delete("/faqs/{faq_id}")
async def delete_faq(faq_id: str, business_id: str):
    db = get_db()
    db.table("faqs").delete().eq("id", faq_id).eq("business_id", business_id).execute()
    sync = _sync_retell_prompt(business_id)
    return {"status": "deleted", "voice_sync": sync}


class BulkFAQImport(BaseModel):
    faqs: List[FAQItem]
    replace: bool = True  # True = delete existing FAQs first


@router.post("/faqs/bulk")
async def bulk_import_faqs(business_id: str, body: BulkFAQImport):
    """
    Bulk import FAQs — inserts all FAQs then syncs to Retell ONCE.
    If replace=True (default), deletes existing FAQs first.
    """
    db = get_db()

    if body.replace:
        db.table("faqs").delete().eq("business_id", business_id).execute()

    inserted = 0
    for faq in body.faqs:
        db.table("faqs").insert({
            "business_id": business_id,
            "question": faq.question,
            "answer": faq.answer,
            "category": faq.category or "general",
            "active": faq.active,
        }).execute()
        inserted += 1

    sync = _sync_retell_prompt(business_id)
    return {"status": "imported", "faq_count": inserted, "voice_sync": sync}


# ── Auto-Respond Toggle ───────────────────────────────────────────────────────

class AutoRespondUpdate(BaseModel):
    auto_respond_enabled: bool


@router.get("/auto-respond")
async def get_auto_respond(business_id: str):
    db = get_db()
    res = db.table("businesses").select(
        "auto_respond_enabled"
    ).eq("id", business_id).maybe_single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return {"auto_respond_enabled": res.data.get("auto_respond_enabled", False)}


@router.patch("/auto-respond")
async def update_auto_respond(business_id: str, body: AutoRespondUpdate):
    db = get_db()
    db.table("businesses").update({
        "auto_respond_enabled": body.auto_respond_enabled
    }).eq("id", business_id).execute()
    return {
        "status": "updated_auto",
        "auto_respond_enabled": body.auto_respond_enabled
    }


# ── Widget Branding (Pro only) ───────────────────────────────────────────────

class WidgetBrandingUpdate(BaseModel):
    chatbot_name: Optional[str] = None
    greeting_message: Optional[str] = None
    brand_color: Optional[str] = None
    show_powered_by: Optional[bool] = None


@router.get("/widget-branding")
async def get_widget_branding(business_id: str):
    db = get_db()
    biz = db.table("businesses").select("metadata").eq("id", business_id).maybe_single().execute()
    if not biz or not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")

    meta = biz.data.get("metadata") or {}
    return {
        "chatbot_name": meta.get("chatbot_name", "Vela"),
        "greeting_message": meta.get("greeting_message", ""),
        "brand_color": meta.get("brand_color", "#E8714A"),
        "show_powered_by": meta.get("show_powered_by", True),
    }


@router.patch("/widget-branding")
async def update_widget_branding(business_id: str, body: WidgetBrandingUpdate):
    db = get_db()

    # Check Pro plan
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").maybe_single().execute()
    if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") not in ("pro", "enterprise"):
        raise HTTPException(status_code=403, detail="Widget branding customization requires Pro plan")

    # Get current metadata
    biz = db.table("businesses").select("metadata").eq("id", business_id).maybe_single().execute()
    meta = (biz.data.get("metadata") or {}) if biz and biz.data else {}

    updates = {k: v for k, v in body.dict().items() if v is not None}
    meta.update(updates)

    db.table("businesses").update({"metadata": meta}).eq("id", business_id).execute()

    return {"status": "updated", **meta}


@router.get("/widget-config")
async def get_widget_config(business_id: str):
    """Public endpoint — widget.js calls this to get branding settings."""
    db = get_db()
    try:
        biz = db.table("businesses").select("name, metadata").eq("id", business_id).maybe_single().execute()
    except Exception:
        # Fallback if metadata column doesn't exist
        biz = db.table("businesses").select("name").eq("id", business_id).maybe_single().execute()
    if not biz or not biz.data:
        return {"chatbot_name": "Vela", "brand_color": "#E8714A", "show_powered_by": True, "business_name": ""}

    meta = biz.data.get("metadata") or {} if "metadata" in (biz.data or {}) else {}
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").maybe_single().execute()
    is_pro = plan_res and plan_res.data and plan_res.data.get("plan_tier") == "pro"

    return {
        "chatbot_name": meta.get("chatbot_name", "Vela"),
        "greeting_message": meta.get("greeting_message", ""),
        "brand_color": meta.get("brand_color", "#E8714A"),
        "show_powered_by": False if (is_pro and meta.get("show_powered_by") == False) else True,
        "booking_url": meta.get("booking_url", "") if is_pro else "",
        "business_name": biz.data.get("name", ""),
    }


@router.get("/booking")
async def get_booking_settings(business_id: str):
    db = get_db()
    biz = db.table("businesses").select("metadata").eq("id", business_id).maybe_single().execute()
    if not biz or not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")
    meta = biz.data.get("metadata") or {}
    return {
        "booking_url": meta.get("booking_url", ""),
        "booking_enabled": bool(meta.get("booking_url")),
    }


@router.patch("/booking")
async def update_booking_settings(business_id: str, body: dict):
    db = get_db()
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").maybe_single().execute()
    if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") not in ("pro", "enterprise"):
        raise HTTPException(status_code=403, detail="Appointment booking requires Pro plan")

    biz = db.table("businesses").select("metadata").eq("id", business_id).maybe_single().execute()
    meta = (biz.data.get("metadata") or {}) if biz and biz.data else {}
    meta["booking_url"] = body.get("booking_url", "")
    db.table("businesses").update({"metadata": meta}).eq("id", business_id).execute()
    return {"status": "updated", "booking_url": meta["booking_url"]}


# ── Notification Preferences ────────────────────────────────────────────────

@router.get("/notifications")
async def get_notification_prefs(business_id: str):
    """Get notification preferences for a business."""
    db = get_db()
    res = db.table("notification_preferences").select("*").eq(
        "business_id", business_id
    ).maybe_single().execute()

    if res and res.data:
        return {
            "notify_on_chat": res.data.get("notify_on_chat", True),
            "notify_on_call": res.data.get("notify_on_call", True),
            "notify_on_sms": res.data.get("notify_on_sms", True),
        }

    # Defaults: all on
    return {"notify_on_chat": True, "notify_on_call": True, "notify_on_sms": True}


@router.patch("/notifications")
async def update_notification_prefs(business_id: str, body: dict):
    """Update notification preferences. Plan-gated on backend."""
    db = get_db()

    # Get plan tier for gating
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").maybe_single().execute()
    plan_tier = plan_res.data.get("plan_tier", "starter") if plan_res and plan_res.data else "starter"

    updates = {}

    # Chat notifications: all plans
    if "notify_on_chat" in body:
        updates["notify_on_chat"] = bool(body["notify_on_chat"])

    # Call notifications: pro and enterprise only
    if "notify_on_call" in body:
        if plan_tier not in ("pro", "enterprise"):
            raise HTTPException(status_code=403, detail="Call notifications require Pro plan")
        updates["notify_on_call"] = bool(body["notify_on_call"])

    # SMS notifications: pro and enterprise only
    if "notify_on_sms" in body:
        if plan_tier not in ("pro", "enterprise"):
            raise HTTPException(status_code=403, detail="SMS notifications require Pro plan")
        updates["notify_on_sms"] = bool(body["notify_on_sms"])

    if not updates:
        return {"status": "no_changes"}

    updates["updated_at"] = "now()"

    # Upsert
    existing = db.table("notification_preferences").select("id").eq(
        "business_id", business_id
    ).maybe_single().execute()

    if existing and existing.data:
        db.table("notification_preferences").update(updates).eq(
            "business_id", business_id
        ).execute()
    else:
        updates["business_id"] = business_id
        updates["notify_on_chat"] = updates.get("notify_on_chat", True)
        updates["notify_on_call"] = updates.get("notify_on_call", True)
        updates["notify_on_sms"] = updates.get("notify_on_sms", True)
        db.table("notification_preferences").insert(updates).execute()

    return {"status": "updated", **updates}


# ── Email Auto-Reply ───────────────────────────────────────────────────────

ELIGIBLE_EMAIL_PLANS = ("growth", "pro", "enterprise")


@router.get("/email-status")
async def email_status(business_id: str):
    """Check if email auto-reply is enabled for a business."""
    db = get_db()
    ch = db.table("channels").select("id, active, external_identifier").eq(
        "business_id", business_id
    ).eq("channel_type", "email").execute()

    if ch.data:
        row = ch.data[0]
        return {
            "enabled": row.get("active", False),
            "forwarding_address": row.get("external_identifier", ""),
        }

    return {"enabled": False, "forwarding_address": ""}


@router.post("/email-enable")
async def email_enable(business_id: str):
    """Enable email auto-reply. Creates an email channel. Growth+ only."""
    db = get_db()

    # Plan gate
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").execute()
    tier = plan_res.data[0].get("plan_tier", "starter") if plan_res.data else "starter"
    if tier not in ELIGIBLE_EMAIL_PLANS:
        raise HTTPException(status_code=403, detail="Email auto-reply requires Growth plan or above")

    forwarding = f"leads-{business_id}@frontdeskreply.com"

    # Check if channel already exists
    existing = db.table("channels").select("id").eq(
        "business_id", business_id
    ).eq("channel_type", "email").execute()

    if existing.data:
        # Re-activate
        db.table("channels").update({"active": True}).eq("id", existing.data[0]["id"]).execute()
    else:
        # Create new email channel
        db.table("channels").insert({
            "business_id": business_id,
            "channel_type": "email",
            "external_identifier": forwarding,
            "provider": "resend",
            "active": True,
            "config": {},
        }).execute()

    logger.info(f"Email auto-reply enabled for business {business_id}: {forwarding}")
    return {"status": "enabled", "forwarding_address": forwarding}


@router.post("/email-disable")
async def email_disable(business_id: str):
    """Disable email auto-reply."""
    db = get_db()
    db.table("channels").update({"active": False}).eq(
        "business_id", business_id
    ).eq("channel_type", "email").execute()
    logger.info(f"Email auto-reply disabled for business {business_id}")
    return {"status": "disabled"}


@router.post("/email-test")
async def email_test(business_id: str):
    """Send a test email so the business owner can see Vela's response."""
    db = get_db()

    # Get owner email
    biz = db.table("businesses").select("owner_email, email, name").eq(
        "id", business_id
    ).execute()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")

    business = biz.data[0]
    owner_email = business.get("owner_email") or business.get("email")
    if not owner_email:
        raise HTTPException(status_code=400, detail="No owner email configured. Add it in Business Profile first.")

    # Generate a sample Vela response
    from app.services.chat_service import get_business_chat_config
    from app.services.chat_ai_service import get_chat_ai_service

    config = get_business_chat_config(business_id)
    ai = get_chat_ai_service()

    test_question = "Hi, I'd like to know more about your services. What do you offer and how can I book?"
    response = ""
    async for chunk in ai.stream_chat_response(
        business_config=config,
        message_history=[],
        visitor_message=test_question,
    ):
        response += chunk

    # Send via email service
    from app.services.email_service import send_email
    result = send_email(
        to_email=owner_email,
        body=response,
        subject=f"Test: Vela Email Auto-Reply for {business.get('name', 'your business')}",
        customer_name="Test Customer",
        business_id=business_id,
    )

    return {"status": "sent", "to": owner_email}