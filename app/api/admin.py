"""
Admin Dashboard API — Frontdesk AI
Internal-only endpoint for DJ to view all clients, plans, and usage.
"""

import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException

from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# DJ's admin key — simple auth for now
ADMIN_KEY = "fdr-admin-dj-2026"


@router.get("/clients")
async def list_all_clients(admin_key: str = ""):
    """List all businesses with plan, usage, and contact info."""
    if admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    db = get_db()

    # Get all businesses
    biz_res = db.table("businesses").select("id, name, phone, email, city, created_at").execute()
    businesses = biz_res.data or []

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    clients = []
    for biz in businesses:
        biz_id = biz["id"]

        # Get plan
        plan_res = db.table("subscription_plans").select(
            "plan_tier, status, auto_send_enabled"
        ).eq("business_id", biz_id).eq("status", "active").maybe_single().execute()
        plan = plan_res.data if plan_res else None

        # Get chat count this month
        chat_res = db.table("chat_sessions").select("id").eq(
            "business_id", biz_id
        ).gte("started_at", month_start).execute()
        chat_count = len(chat_res.data or [])

        # Get call count + minutes this month
        call_res = db.table("call_sessions").select("id, duration_seconds").eq(
            "business_id", biz_id
        ).gte("started_at", month_start).execute()
        calls = call_res.data or []
        call_count = len(calls)
        call_minutes = round(sum(c.get("duration_seconds") or 0 for c in calls) / 60, 1)

        # Get voice channel (phone number)
        voice_res = db.table("channels").select("external_identifier").eq(
            "business_id", biz_id
        ).eq("channel_type", "voice").maybe_single().execute()
        voice_number = voice_res.data.get("external_identifier", "") if voice_res and voice_res.data else ""

        # Get owner email from Clerk metadata or business record
        owner_email = biz.get("email") or ""

        clients.append({
            "id": biz_id,
            "name": biz.get("name", "Unknown"),
            "email": owner_email,
            "phone": biz.get("phone", ""),
            "city": biz.get("city", ""),
            "plan": plan.get("plan_tier", "none") if plan else "none",
            "plan_status": plan.get("status", "inactive") if plan else "inactive",
            "chats_this_month": chat_count,
            "calls_this_month": call_count,
            "call_minutes_used": call_minutes,
            "call_minutes_limit": 200 if (plan and plan.get("plan_tier") == "pro") else 0,
            "voice_number": voice_number,
        })

    # Sort by name
    clients.sort(key=lambda c: c["name"].lower())

    return {"clients": clients, "total": len(clients)}
