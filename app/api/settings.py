"""
Settings Endpoints — Frontdesk AI
Lets business owners manage their profile, hours, FAQs, and emergency contact.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from app.core.database import get_db

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
    return {"status": "created", "faq": res.data[0]}


@router.patch("/faqs/{faq_id}")
async def update_faq(faq_id: str, business_id: str, body: FAQItem):
    db = get_db()
    res = db.table("faqs").update({
        "question": body.question,
        "answer": body.answer,
        "category": body.category,
        "active": body.active,
    }).eq("id", faq_id).eq("business_id", business_id).execute()
    return {"status": "updated"}


@router.delete("/faqs/{faq_id}")
async def delete_faq(faq_id: str, business_id: str):
    db = get_db()
    db.table("faqs").delete().eq("id", faq_id).eq("business_id", business_id).execute()
    return {"status": "deleted"}


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