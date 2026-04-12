"""
Celery Tasks — Frontdesk AI
Async job pipeline for inbound message processing.
"""

import logging
from datetime import datetime, timezone

from app.workers.celery_app import celery_app
from app.core.database import get_db
from app.services.ai_service import get_ai_service
from app.services.routing import determine_routing, get_queue_priority
from app.services.sms_service import send_sms, send_escalation_alert
from app.models.schemas import RecommendedAction

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def process_inbound_message(self, message_id: str):
    logger.info(f"Processing message: {message_id}")
    db = get_db()
    ai = get_ai_service()

    try:
        # ── 1. Fetch message ──────────────────────────────────────────────
        msg_res = db.table("inbound_messages").select("*").eq("id", message_id).single().execute()
        message = msg_res.data
        if not message:
            logger.error(f"Message not found: {message_id}")
            return

        # ── 2. Fetch business context ────────────────────────────────────
        biz_res = db.table("businesses").select("*").eq("id", message["business_id"]).single().execute()
        business = biz_res.data

        # ── 3. Fetch FAQs ────────────────────────────────────────────────
        faq_res = db.table("faqs").select("question, answer, category").eq(
            "business_id", message["business_id"]
        ).eq("active", True).execute()
        faqs = faq_res.data or []

        # ── 4. Fetch business rules ──────────────────────────────────────
        rules_res = db.table("business_rules").select("*").eq(
            "business_id", message["business_id"]
        ).eq("active", True).execute()
        business_rules = rules_res.data or []

        # ── 5. Fetch plan tier + auto_respond setting ────────────────────
        plan_res = db.table("subscription_plans").select(
            "plan_tier, auto_send_enabled"
        ).eq("business_id", message["business_id"]).eq("status", "active").maybe_single().execute()
        plan_tier = plan_res.data["plan_tier"] if plan_res.data else "starter"

        auto_respond_enabled = bool(
            plan_res.data.get("auto_send_enabled", False) if plan_res.data else False
        )

        # ── 6. Fetch conversation history ────────────────────────────────
        conversation_history = []
        if message.get("conversation_id"):
            hist_res = db.table("inbound_messages").select("body, direction").eq(
                "conversation_id", message["conversation_id"]
            ).neq("id", message_id).order("received_at", desc=False).limit(5).execute()
            conversation_history = hist_res.data or []

        # ── 7. Build business profile ────────────────────────────────────
        business_profile = {
            "name": business.get("name"),
            "type": business.get("business_type"),
            "city": business.get("city", ""),
            "phone": business.get("phone", ""),
            "hours": business.get("hours", ""),
            "emergency_policy": business.get("emergency_policy", ""),
            "service_areas": business.get("service_areas", ""),
            "tone": business.get("tone", "professional but warm"),
        }

        # ── 8. Classify ──────────────────────────────────────────────────
        db.table("inbound_messages").update({"status": "processing"}).eq("id", message_id).execute()

        classification = ai.classify_message(
            message_text=message["body"],
            business_context=business_profile,
            sender_name=message.get("sender_name"),
        )

        # ── 9. Persist classification ────────────────────────────────────
        db.table("inbound_messages").update({
            "status": "classified",
            "intent": classification.intent.value,
            "urgency_score": classification.urgency_score,
            "sentiment": classification.sentiment.value,
            "confidence": classification.confidence,
            "requires_human_review": classification.requires_human_review,
            "safe_to_auto_send": classification.safe_to_auto_send,
            "recommended_action": classification.recommended_action.value,
            "reasoning_short": classification.reasoning_short,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", message_id).execute()

        # ── 10. Determine routing ────────────────────────────────────────
        final_action = determine_routing(
            classification=classification,
            business_rules=business_rules,
            plan_tier=plan_tier,
            auto_respond_enabled=auto_respond_enabled,
        )

        # ── 11. Generate draft ───────────────────────────────────────────
        draft_text = ai.generate_draft(
            message_text=message["body"],
            classification=classification,
            business_profile=business_profile,
            faq_context=faqs,
            conversation_history=conversation_history,
        )

        # ── 12. Strip markdown formatting ────────────────────────────────
        draft_text = draft_text.replace("**", "").replace("__", "").replace("*", "").replace("_", "")

        # ── 13. Save draft ───────────────────────────────────────────────
        draft_res = db.table("response_drafts").insert({
            "message_id": message_id,
            "draft_body": draft_text,
            "draft_status": "generated",
            "facts_verified": True,
            "model_version": ai.model,
        }).execute()
        draft_id = draft_res.data[0]["id"]

        # ── 13b. Build dynamic email subject ────────────────────────────
        email_subject = _build_subject(
            intent=classification.intent.value,
            business_name=business.get("name", ""),
        )

        # ── 14. Route to outcome ─────────────────────────────────────────
        if final_action == RecommendedAction.escalate_immediately:
            _handle_escalation(
                db=db, message=message, draft_id=draft_id, business=business,
                reason=classification.escalation_reason or classification.intent.value,
                email_subject=email_subject,
            )
        elif final_action == RecommendedAction.auto_send:
            _handle_auto_send(
                db=db, message=message, draft_id=draft_id, draft_text=draft_text,
                email_subject=email_subject,
            )
        else:
            _handle_queue(
                db=db, message=message, draft_id=draft_id,
                priority=get_queue_priority(classification),
                email_subject=email_subject,
            )

        # ── 15. Audit log ────────────────────────────────────────────────
        db.table("audit_logs").insert({
            "business_id": message["business_id"],
            "entity_type": "inbound_message",
            "entity_id": message_id,
            "action": f"processed_{final_action.value}",
            "performed_by": "ai_worker",
            "metadata_json": {
                "intent": classification.intent.value,
                "confidence": classification.confidence,
                "urgency_score": classification.urgency_score,
                "routing": final_action.value,
                "draft_id": draft_id,
                "auto_respond_enabled": auto_respond_enabled,
                "plan_tier": plan_tier,
            },
        }).execute()

        logger.info(f"Message {message_id} processed → {final_action.value}")

    except Exception as e:
        logger.error(f"Error processing message {message_id}: {e}", exc_info=True)
        db.table("inbound_messages").update({"status": "error"}).eq("id", message_id).execute()
        raise self.retry(exc=e)


def _handle_escalation(db, message, draft_id, business, reason, email_subject=""):
    db.table("escalation_events").insert({
        "message_id": message["id"],
        "business_id": message["business_id"],
        "reason": reason,
        "severity": "high",
        "notification_method": "sms_push",
        "acknowledged": False,
    }).execute()
    db.table("inbound_messages").update({"status": "escalated"}).eq("id", message["id"]).execute()
    owner_phone = business.get("phone")
    if owner_phone:
        send_escalation_alert(owner_phone=owner_phone, message_preview=message["body"], reason=reason)
    db.table("approval_queue_items").insert({
        "message_id": message["id"],
        "draft_id": draft_id,
        "business_id": message["business_id"],
        "status": "pending",
        "priority": "urgent",
        "email_subject": email_subject,
    }).execute()


def _handle_auto_send(db, message, draft_id, draft_text, email_subject=""):
    from app.services.email_service import send_email
    preference = message.get("contact_preference", "sms")
    email = message.get("sender_email")
    phone = message.get("sender_phone") or message.get("sender_identifier")
    sender_name = message.get("sender_name", "")
    business_id = message.get("business_id", "")
    send_result = {"status": "skipped"}

    if preference == "email" and email:
        send_result = send_email(
            to_email=email,
            body=draft_text,
            subject=email_subject,
            customer_name=sender_name,
            business_id=business_id,
        )
        logger.info(f"Auto-sent email to {email}: {send_result}")
    elif phone:
        send_result = send_sms(to_number=phone, body=draft_text)
        logger.info(f"Auto-sent SMS to {phone}: {send_result}")

    db.table("sent_responses").insert({
        "draft_id": draft_id,
        "message_id": message["id"],
        "business_id": message["business_id"],
        "body_sent": draft_text,
        "send_method": preference,
        "sent_by": "auto",
        "auto_sent": True,
    }).execute()
    db.table("inbound_messages").update({"status": "sent"}).eq("id", message["id"]).execute()
    logger.info(f"Auto-sent response for message {message['id']}: {send_result}")


def _handle_queue(db, message, draft_id, priority, email_subject=""):
    db.table("approval_queue_items").insert({
        "message_id": message["id"],
        "draft_id": draft_id,
        "business_id": message["business_id"],
        "status": "pending",
        "priority": priority,
        "email_subject": email_subject,
    }).execute()
    db.table("inbound_messages").update({"status": "draft_generated"}).eq("id", message["id"]).execute()


def _build_subject(intent: str, business_name: str) -> str:
    """Build a dynamic email subject line from Claude's intent classification."""
    intent_map = {
        "booking":           "Your Appointment Request",
        "booking_request":   "Your Appointment Request",
        "quote":             "Your Quote Request",
        "quote_request":     "Your Quote Request",
        "emergency":         "Urgent Service Request",
        "emergency_service": "Urgent Service Request",
        "complaint":         "We Heard You",
        "faq":               "Your Question",
        "cancellation":      "Your Cancellation Request",
        "follow_up":         "Following Up on Your Request",
        "general_inquiry":   "Your Inquiry",
        "inquiry":           "Your Inquiry",
    }
    label = intent_map.get(intent, "Your Message")
    if business_name:
        return f"{label} — {business_name}"
    return label


@celery_app.task
def send_escalation_notification(escalation_id: str):
    db = get_db()
    esc_res = db.table("escalation_events").select("*").eq("id", escalation_id).single().execute()
    escalation = esc_res.data
    if not escalation:
        return
    biz_res = db.table("businesses").select("phone, name").eq(
        "id", escalation["business_id"]
    ).single().execute()
    business = biz_res.data
    if business and business.get("phone"):
        send_escalation_alert(
            owner_phone=business["phone"],
            message_preview=escalation.get("reason", ""),
            reason=escalation.get("reason", "unknown"),
        )


# ── Weekly Email Reports ────────────────────────────────────────────────────

@celery_app.task
def weekly_report_sweep():
    """Find all Pro/Enterprise businesses and send weekly reports."""
    db = get_db()
    plans = db.table("subscription_plans").select("business_id, plan_tier").eq(
        "status", "active"
    ).execute()

    sent = 0
    for plan in (plans.data or []):
        if plan.get("plan_tier") not in ("pro", "enterprise"):
            continue
        # Check if weekly report is enabled (default True)
        prefs = db.table("notification_preferences").select("weekly_report_enabled").eq(
            "business_id", plan["business_id"]
        ).execute()
        enabled = True
        if prefs.data:
            enabled = prefs.data[0].get("weekly_report_enabled", True)
        if enabled:
            send_weekly_report.delay(plan["business_id"])
            sent += 1

    logger.info(f"Weekly report sweep: dispatched {sent} reports")
    return {"dispatched": sent}


@celery_app.task
def send_weekly_report(business_id: str):
    """Generate and send a weekly performance digest email."""
    from datetime import timedelta

    db = get_db()

    # Get business info
    biz = db.table("businesses").select("name, owner_email, email, phone").eq("id", business_id).execute()
    if not biz.data:
        return
    business = biz.data[0]
    owner_email = business.get("owner_email") or business.get("email")
    if not owner_email:
        return

    biz_name = business.get("name", "Your Business")
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()

    # Gather stats
    chats = db.table("chat_sessions").select("id").eq("business_id", business_id).gte("started_at", week_ago).execute()
    calls = db.table("call_sessions").select("id, duration_seconds").eq("business_id", business_id).gte("started_at", week_ago).execute()
    contacts = db.table("contacts").select("id").eq("business_id", business_id).gte("first_seen_at", week_ago).execute()

    total_chats = len(chats.data or [])
    total_calls = len(calls.data or [])
    total_minutes = sum(c.get("duration_seconds", 0) for c in (calls.data or [])) // 60
    new_leads = len(contacts.data or [])

    # Top questions (from chat messages)
    from collections import Counter
    msgs = db.table("chat_messages").select("content, role").eq("role", "visitor").execute()
    questions = [m["content"][:80] for m in (msgs.data or []) if len(m.get("content", "")) > 10]
    top_questions = [q for q, _ in Counter(questions).most_common(3)]

    # Build HTML
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#ffffff;">
      <div style="background:#1a1f2e;padding:24px 28px;border-radius:12px 12px 0 0;">
        <h1 style="color:#ffffff;margin:0;font-size:20px;">{biz_name} — Weekly Report</h1>
        <p style="color:#94a3b8;margin:6px 0 0;font-size:13px;">Week of {(now - timedelta(days=7)).strftime('%b %d')} - {now.strftime('%b %d, %Y')}</p>
      </div>
      <div style="padding:24px 28px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;">
        <div style="display:flex;gap:16px;margin-bottom:24px;">
          <div style="flex:1;background:#f8fafc;border-radius:10px;padding:16px;text-align:center;">
            <div style="font-size:28px;font-weight:700;color:#f97316;">{total_chats}</div>
            <div style="font-size:12px;color:#64748b;margin-top:4px;">Chats</div>
          </div>
          <div style="flex:1;background:#f8fafc;border-radius:10px;padding:16px;text-align:center;">
            <div style="font-size:28px;font-weight:700;color:#f97316;">{total_calls}</div>
            <div style="font-size:12px;color:#64748b;margin-top:4px;">Calls</div>
          </div>
          <div style="flex:1;background:#f8fafc;border-radius:10px;padding:16px;text-align:center;">
            <div style="font-size:28px;font-weight:700;color:#f97316;">{new_leads}</div>
            <div style="font-size:12px;color:#64748b;margin-top:4px;">New Leads</div>
          </div>
          <div style="flex:1;background:#f8fafc;border-radius:10px;padding:16px;text-align:center;">
            <div style="font-size:28px;font-weight:700;color:#f97316;">{total_minutes}</div>
            <div style="font-size:12px;color:#64748b;margin-top:4px;">Call Min</div>
          </div>
        </div>
        {"<h3 style='font-size:14px;color:#1a1f2e;margin:0 0 10px;'>Top Questions This Week</h3><ol style='margin:0;padding-left:18px;color:#475569;font-size:13px;line-height:1.8;'>" + "".join(f"<li>{q}</li>" for q in top_questions) + "</ol>" if top_questions else "<p style='color:#94a3b8;font-size:13px;'>No visitor questions this week.</p>"}
        <div style="margin-top:24px;text-align:center;">
          <a href="https://app.frontdeskreply.com/analytics" style="display:inline-block;padding:10px 24px;background:#f97316;color:#fff;border-radius:8px;text-decoration:none;font-weight:600;font-size:13px;">View Full Analytics</a>
        </div>
        <p style="margin-top:20px;font-size:11px;color:#94a3b8;text-align:center;">Powered by FrontDeskReply</p>
      </div>
    </div>
    """

    # Send via Resend
    import httpx
    try:
        from app.core.config import get_settings
        settings = get_settings()
        resend_key = settings.resend_api_key
    except Exception:
        import os
        resend_key = os.getenv("RESEND_API_KEY", "")

    if not resend_key:
        logger.error(f"Weekly report: no Resend API key for {business_id}")
        return

    res = httpx.post("https://api.resend.com/emails", headers={
        "Authorization": f"Bearer {resend_key}",
        "Content-Type": "application/json",
    }, json={
        "from": f"{biz_name} <hello@frontdeskreply.com>",
        "to": [owner_email],
        "subject": f"Your Weekly Report — {biz_name}",
        "html": html,
    }, timeout=15)

    if res.status_code in (200, 201):
        logger.info(f"Weekly report sent to {owner_email} for {biz_name}")
    else:
        logger.error(f"Weekly report send failed ({res.status_code}): {res.text[:200]}")