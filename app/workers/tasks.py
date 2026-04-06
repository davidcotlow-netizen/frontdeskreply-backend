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