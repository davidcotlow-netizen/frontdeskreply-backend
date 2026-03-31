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
    updates = {k: v for k, v in body.dict().items() if v is not None}
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
        "status": "updated",
        "auto_respond_enabled": body.auto_respond_enabled
    }