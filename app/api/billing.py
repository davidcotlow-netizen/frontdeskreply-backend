import stripe
import httpx
from fastapi import APIRouter, HTTPException, Request
from app.core.config import get_settings
from app.core.database import get_db

router = APIRouter(prefix="/billing", tags=["billing"])

PLAN_PRICE_IDS = {
    "starter": "price_1TFeutCFpKJIPROQ1ULDY6mm",
    "growth":  "price_1TFev9CFpKJIPROQjxB8XNHx",
    "pro":     "price_1TFevKCFpKJIPROQWLAtWneU",
}

PLAN_LIMITS = {
    "starter": {"monthly_conversation_limit": 300,    "auto_send_enabled": False},
    "growth":  {"monthly_conversation_limit": 1000,   "auto_send_enabled": True},
    "pro":     {"monthly_conversation_limit": 999999, "auto_send_enabled": True},
}


async def sync_plan_to_clerk(clerk_secret_key: str, business_id: str, plan_tier: str):
    """Update Clerk user publicMetadata with current plan tier."""
    try:
        async with httpx.AsyncClient() as client:
            # Fetch all users and find the one with matching business_id
            r = await client.get(
                "https://api.clerk.com/v1/users",
                headers={"Authorization": f"Bearer {clerk_secret_key}"},
                params={"limit": 100},
            )
            if r.status_code != 200:
                return
            users = r.json()
            target_user = None
            for u in users:
                meta = u.get("public_metadata", {})
                if meta.get("business_id") == business_id:
                    target_user = u
                    break
            if not target_user:
                return
            # Merge plan into existing publicMetadata
            existing_meta = target_user.get("public_metadata", {})
            await client.patch(
                f"https://api.clerk.com/v1/users/{target_user['id']}",
                headers={
                    "Authorization": f"Bearer {clerk_secret_key}",
                    "Content-Type": "application/json",
                },
                json={"public_metadata": {**existing_meta, "plan": plan_tier}},
            )
    except Exception:
        pass  # Don't fail the webhook if Clerk sync fails


@router.get("/plan")
async def get_plan(business_id: str):
    db = get_db()
    try:
        res = db.table("subscription_plans").select("*").eq(
            "business_id", business_id
        ).eq("status", "active").execute()
        data = res.data[0] if res.data else None
    except Exception:
        data = None
    if not data:
        return {"plan_tier": "starter", "conversations_used": 0, "monthly_limit": 300}
    return data


@router.post("/create-checkout")
async def create_checkout(business_id: str, plan_tier: str, return_url: str):
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key

    price_id = PLAN_PRICE_IDS.get(plan_tier)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan tier")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{return_url}?success=true",
        cancel_url=f"{return_url}?canceled=true",
        metadata={"business_id": business_id, "plan_tier": plan_tier},
    )
    return {"checkout_url": session.url}


@router.post("/portal")
async def billing_portal(business_id: str, return_url: str):
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key
    db = get_db()

    try:
        plan_res = db.table("subscription_plans").select("stripe_subscription_id").eq(
            "business_id", business_id
        ).eq("status", "active").execute()
        plan_data = plan_res.data[0] if plan_res.data else None
    except Exception:
        plan_data = None

    if not plan_data:
        raise HTTPException(status_code=404, detail="No active subscription")

    sub = stripe.Subscription.retrieve(plan_data["stripe_subscription_id"])
    portal = stripe.billing_portal.Session.create(
        customer=sub["customer"],
        return_url=return_url,
    )
    return {"portal_url": portal.url}


@router.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """Handles Stripe subscription lifecycle events."""
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, settings.stripe_webhook_secret)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")

    db = get_db()
    data = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        business_id = data["metadata"].get("business_id")
        plan_tier = data["metadata"].get("plan_tier", "starter")
        limits = PLAN_LIMITS.get(plan_tier, PLAN_LIMITS["starter"])

        # Update Supabase
        db.table("subscription_plans").upsert({
            "business_id": business_id,
            "plan_tier": plan_tier,
            "stripe_subscription_id": data.get("subscription"),
            "status": "active",
            "conversations_used": 0,
            **limits,
        }).execute()

        # Sync plan to Clerk publicMetadata
        await sync_plan_to_clerk(settings.clerk_secret_key, business_id, plan_tier)

    elif event["type"] == "invoice.paid":
        sub_id = data.get("subscription")
        if sub_id:
            db.table("subscription_plans").update({
                "conversations_used": 0,
                "status": "active",
            }).eq("stripe_subscription_id", sub_id).execute()

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        sub_id = data.get("id")
        db.table("subscription_plans").update({"status": "canceled"}).eq(
            "stripe_subscription_id", sub_id
        ).execute()

        # Find business_id from Supabase and reset plan in Clerk
        try:
            res = db.table("subscription_plans").select("business_id").eq(
                "stripe_subscription_id", sub_id
            ).execute()
            if res.data:
                await sync_plan_to_clerk(
                    settings.clerk_secret_key,
                    res.data[0]["business_id"],
                    "starter"
                )
        except Exception:
            pass

    return {"status": "ok"}


@router.get("/history")
async def billing_history(business_id: str):
    """Fetch invoice history from Stripe for a business."""
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key
    db = get_db()

    try:
        plan_res = db.table("subscription_plans").select(
            "stripe_subscription_id, plan_tier"
        ).eq("business_id", business_id).eq("status", "active").execute()
        plan_data = plan_res.data[0] if plan_res.data else None
    except Exception:
        plan_data = None

    if not plan_data or not plan_data.get("stripe_subscription_id"):
        return {"invoices": [], "has_subscription": False}

    sub = stripe.Subscription.retrieve(plan_data["stripe_subscription_id"])
    customer_id = sub["customer"]

    invoices = stripe.Invoice.list(customer=customer_id, limit=24)

    history = []
    for inv in invoices.auto_paging_iter():
        if inv.get("status") not in ("paid", "open", "void", "uncollectible"):
            continue
        history.append({
            "id": inv["id"],
            "number": inv.get("number", "—"),
            "date": inv["created"],
            "amount": inv["amount_paid"] / 100,
            "currency": inv.get("currency", "usd").upper(),
            "status": inv["status"],
            "plan_tier": plan_data.get("plan_tier", "starter"),
            "invoice_url": inv.get("hosted_invoice_url", ""),
            "invoice_pdf": inv.get("invoice_pdf", ""),
            "period_start": inv.get("period_start"),
            "period_end": inv.get("period_end"),
        })
        if len(history) >= 24:
            break

    return {"invoices": history, "has_subscription": True}