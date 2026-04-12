from fastapi import APIRouter, HTTPException, Request
from datetime import datetime, timezone, timedelta
from app.core.database import get_db
import hashlib
import secrets

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _score_lead(lead: dict) -> str:
    """Score a lead as hot/warm/cold based on engagement signals."""
    interactions = lead.get("message_count", 0) + lead.get("call_count", 0)
    intents = [i.lower() for i in lead.get("intents", [])]
    has_email = bool(lead.get("email"))
    has_phone = bool(lead.get("phone"))

    # Calculate days since last contact
    days_since = 999
    last = lead.get("last_contact", "")
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - last_dt).days
        except Exception:
            pass

    booking_intents = {"booking", "booking_request", "quote", "quote_request", "schedule"}

    # Hot: high engagement or booking intent or very recent
    if interactions >= 3 or any(i in booking_intents for i in intents) or days_since <= 2:
        return "hot"

    # Warm: moderate engagement with contact info or recent
    if (interactions >= 1 and has_email and has_phone) or days_since <= 7:
        return "warm"

    # Cold: everything else
    return "cold"


@router.get("")
async def list_conversations(
    business_id: str,
    status: str = "open",
    channel_type: str = None,
    page: int = 1,
    page_size: int = 25,
):
    db = get_db()
    query = db.table("conversations").select(
        "*, contacts(name, phone, email)"
    ).eq("business_id", business_id).order("last_message_at", desc=True)

    if status != "all":
        query = query.eq("status", status)
    if channel_type:
        query = query.eq("channel_type", channel_type)

    offset = (page - 1) * page_size
    res = query.range(offset, offset + page_size - 1).execute()
    return {"conversations": res.data, "page": page}


@router.get("/sent")
async def get_sent_messages(
    business_id: str,
    limit: int = 50,
    offset: int = 0,
    period: str = "month"
):
    """MUST be before /{conversation_id} to avoid route conflict."""
    db = get_db()
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "week":
        start = (now - timedelta(days=7)).isoformat()
    elif period == "month":
        start = (now - timedelta(days=30)).isoformat()
    else:
        start = (now - timedelta(days=30)).isoformat()

    sent_res = db.table("sent_responses").select(
        "id, message_id, body_sent, send_method, sent_by, auto_sent, sent_at"
    ).eq("business_id", business_id).gte("sent_at", start).order(
        "sent_at", desc=True
    ).range(offset, offset + limit - 1).execute()

    sent = sent_res.data or []
    if not sent:
        return {"sent": [], "total": 0}

    message_ids = [s["message_id"] for s in sent if s.get("message_id")]
    msgs_res = db.table("inbound_messages").select(
        "id, sender_name, sender_email, sender_phone, intent, body, received_at"
    ).in_("id", message_ids).execute()

    msgs_by_id = {m["id"]: m for m in (msgs_res.data or [])}

    result = []
    for s in sent:
        msg = msgs_by_id.get(s.get("message_id"), {})
        result.append({
            "id": s["id"],
            "sent_at": s["sent_at"],
            "auto_sent": s.get("auto_sent", False),
            "send_method": s.get("send_method", "email"),
            "body_sent": s.get("body_sent", ""),
            "customer_name": msg.get("sender_name", "Unknown"),
            "customer_email": msg.get("sender_email"),
            "customer_phone": msg.get("sender_phone"),
            "customer_message": msg.get("body", ""),
            "intent": msg.get("intent", "unknown"),
            "received_at": msg.get("received_at"),
        })

    return {"sent": result, "total": len(result)}


@router.get("/leads/email-templates")
async def get_email_templates(business_id: str):
    """Pre-built email templates for lead follow-up. Growth+ only."""
    db = get_db()
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").execute()
    tier = plan_res.data[0].get("plan_tier", "starter") if plan_res.data else "starter"
    if tier not in ("growth", "pro", "enterprise"):
        raise HTTPException(status_code=403, detail="Email templates require Growth+ plan")

    biz = db.table("businesses").select("name, phone").eq("id", business_id).execute()
    name = biz.data[0].get("name", "our team") if biz.data else "our team"
    phone = biz.data[0].get("phone", "") if biz.data else ""

    templates = [
        {
            "id": "follow_up",
            "name": "Following Up",
            "subject": f"Following up from {name}",
            "body": f"Hi {{customer_name}},\n\nThank you for reaching out to {name}! I wanted to follow up and see if you had any additional questions or if there's anything else we can help with.\n\nWe'd love to assist you further. Feel free to reply to this email or call us at {phone}.\n\nBest regards,\n{name}",
        },
        {
            "id": "thank_you_call",
            "name": "Thank You for Calling",
            "subject": f"Thanks for calling {name}!",
            "body": f"Hi {{customer_name}},\n\nThank you for calling {name} today! It was great speaking with you.\n\nIf you have any follow-up questions or need anything else, don't hesitate to reach out. We're here to help!\n\nBest regards,\n{name}",
        },
        {
            "id": "book_appointment",
            "name": "Book Your Appointment",
            "subject": f"Ready to book with {name}?",
            "body": f"Hi {{customer_name}},\n\nWe'd love to get you on the schedule! Based on our conversation, it sounds like you're interested in learning more.\n\nYou can book your preferred time directly, or reply to this email and we'll get you set up.\n\nLooking forward to seeing you!\n\nBest regards,\n{name}",
        },
        {
            "id": "re_engage",
            "name": "We Miss You",
            "subject": f"We'd love to hear from you again - {name}",
            "body": f"Hi {{customer_name}},\n\nIt's been a little while since we last connected, and we wanted to check in! If you're still interested or have any new questions, we're here and ready to help.\n\nFeel free to reply to this email or give us a call at {phone}.\n\nHope to hear from you soon!\n\nBest regards,\n{name}",
        },
    ]

    return {"templates": templates}


@router.get("/leads")
async def get_lead_database(business_id: str):
    """
    Lead database — all plans get basic leads from their channels.
    Enterprise gets unified cross-channel merge (chat + call + form + email in one view).
    MUST be before /{conversation_id}.
    """
    db = get_db()

    # Check plan tier for unified lead source gating
    plan_res = db.table("subscription_plans").select("plan_tier").eq(
        "business_id", business_id
    ).eq("status", "active").execute()
    plan_tier = plan_res.data[0].get("plan_tier", "starter") if plan_res.data else "starter"
    is_enterprise = plan_tier == "enterprise"
    is_growth_plus = plan_tier in ("growth", "pro", "enterprise")

    leads: dict = {}

    # ── 1. Pull contacts from the contacts table (includes chat visitors) ─
    try:
        contacts_res = db.table("contacts").select(
            "id, name, email, phone, source_channel, first_seen_at, last_seen_at"
        ).eq("business_id", business_id).execute()
        for c in (contacts_res.data or []):
            email = (c.get("email") or "").strip().lower() or None
            phone = (c.get("phone") or "").strip() or None
            key = email or phone or c.get("name", "Unknown")
            leads[key] = {
                "id": c["id"],
                "name": c.get("name") or "Unknown",
                "email": email,
                "phone": phone,
                "first_contact": c.get("first_seen_at") or "",
                "last_contact": c.get("last_seen_at") or "",
                "message_count": 0,
                "source": c.get("source_channel") or "unknown",
                "intents": [],
                "status": "new",
                "chat_session_ids": [],
            }
    except Exception:
        pass

    # ── 2. Enrich with chat session data ─────────────────────────────
    try:
        sessions_res = db.table("chat_sessions").select(
            "id, visitor_name, visitor_email, started_at, metadata"
        ).eq("business_id", business_id).execute()
        for s in (sessions_res.data or []):
            email = (s.get("visitor_email") or "").strip().lower() or None
            metadata = s.get("metadata") or {}
            phone = metadata.get("visitor_phone") or None
            key = email or phone or s.get("visitor_name", "Unknown")

            if key in leads:
                leads[key]["chat_session_ids"].append(s["id"])
                if not leads[key].get("source") or leads[key]["source"] == "unknown":
                    leads[key]["source"] = "live_chat"
            else:
                leads[key] = {
                    "id": key,
                    "name": s.get("visitor_name") or "Unknown",
                    "email": email,
                    "phone": phone,
                    "first_contact": s.get("started_at") or "",
                    "last_contact": s.get("started_at") or "",
                    "message_count": 0,
                    "source": "live_chat",
                    "intents": [],
                    "status": "new",
                    "chat_session_ids": [s["id"]],
                }

            # Count messages in this session
            msgs_res = db.table("chat_messages").select("id").eq(
                "session_id", s["id"]
            ).execute()
            leads[key]["message_count"] += len(msgs_res.data or [])
    except Exception:
        pass

    # ── 2b. Enrich with call session data (Enterprise: unified cross-channel) ──
    if is_enterprise:
        try:
            calls_res = db.table("call_sessions").select(
                "id, caller_phone, caller_name, started_at, duration_seconds, caller_source"
            ).eq("business_id", business_id).execute()
            for c in (calls_res.data or []):
                phone = (c.get("caller_phone") or "").strip()
                if not phone:
                    continue
                clean_phone = phone.replace("+1", "").replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
                caller_source = c.get("caller_source") or None
                matched = False
                for key, lead in leads.items():
                    lead_phone = (lead.get("phone") or "").replace("+1", "").replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
                    if lead_phone and lead_phone == clean_phone:
                        lead["call_count"] = lead.get("call_count", 0) + 1
                        lead["call_session_ids"] = lead.get("call_session_ids", []) + [c["id"]]
                        if caller_source and not lead.get("heard_about_us"):
                            lead["heard_about_us"] = caller_source
                        if not lead.get("source") or lead["source"] == "unknown":
                            lead["source"] = "phone_call"
                        elif lead["source"] != "phone_call":
                            lead["source"] = "multi"
                        matched = True
                        break
                if not matched:
                    key = phone
                    leads[key] = {
                        "id": key,
                        "name": c.get("caller_name") or "Caller",
                        "email": None,
                        "phone": phone,
                        "first_contact": c.get("started_at") or "",
                        "last_contact": c.get("started_at") or "",
                        "message_count": 0,
                        "source": "phone_call",
                        "intents": [],
                        "status": "new",
                        "chat_session_ids": [],
                        "call_count": 1,
                        "call_session_ids": [c["id"]],
                        "heard_about_us": caller_source,
                    }
        except Exception:
            pass

    # ── 3. Also pull from inbound_messages for legacy form leads ─────
    try:
        msgs_res = db.table("inbound_messages").select(
            "sender_name, sender_email, sender_phone, intent, received_at"
        ).eq("business_id", business_id).order("received_at", desc=False).execute()
        for m in (msgs_res.data or []):
            email = (m.get("sender_email") or "").strip().lower() or None
            phone = (m.get("sender_phone") or "").strip() or None
            key = email or phone or m.get("sender_name", "Unknown")
            intent = m.get("intent") or "unknown"

            if key not in leads:
                leads[key] = {
                    "id": key,
                    "name": m.get("sender_name") or "Unknown",
                    "email": email, "phone": phone,
                    "first_contact": m.get("received_at") or "",
                    "last_contact": m.get("received_at") or "",
                    "message_count": 0, "source": "web_form",
                    "intents": [], "status": "new", "chat_session_ids": [],
                }

            lead = leads[key]
            lead["message_count"] += 1
            if m.get("received_at") and m["received_at"] > lead["last_contact"]:
                lead["last_contact"] = m["received_at"]
            if intent and intent not in lead["intents"]:
                lead["intents"].append(intent)
    except Exception:
        pass

    result = []
    for lead in leads.values():
        top = lead["intents"][0] if lead["intents"] else "chat"
        lead.setdefault("call_count", 0)
        lead.setdefault("call_session_ids", [])
        lead.setdefault("heard_about_us", None)
        lead["quality"] = _score_lead(lead) if is_growth_plus else None
        result.append({**lead, "top_intent": top})

    result.sort(key=lambda x: x["last_contact"], reverse=True)
    return {"leads": result, "total": len(result)}


@router.patch("/leads/{lead_id}/status")
async def update_lead_status(lead_id: str, body: dict):
    """Update a lead's lifecycle status. Requires Growth or Pro plan."""
    db = get_db()
    business_id = body.get("business_id", "")
    if business_id:
        plan_res = db.table("subscription_plans").select("plan_tier").eq("business_id", business_id).eq("status", "active").maybe_single().execute()
        if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") not in ("growth", "pro"):
            raise HTTPException(status_code=403, detail="Lead status updates require Growth or Pro plan")
    status = body.get("status", "new")
    if status not in ("new", "contacted", "quoted", "converted"):
        raise HTTPException(status_code=400, detail="Invalid status")

    # Update in contacts table
    try:
        db.table("contacts").update({"source_channel": status}).eq("id", lead_id).execute()
    except Exception:
        pass
    return {"status": status, "lead_id": lead_id}


@router.get("/leads/{lead_id}/chats")
async def get_lead_chats(lead_id: str):
    """Get chat transcripts for a specific lead by matching email."""
    db = get_db()

    # Look up the contact
    contact_res = db.table("contacts").select("email, phone").eq("id", lead_id).maybe_single().execute()
    if not contact_res or not contact_res.data:
        return {"sessions": []}

    email = contact_res.data.get("email")
    phone = contact_res.data.get("phone")

    # Find chat sessions by email match
    sessions = []
    if email:
        sess_res = db.table("chat_sessions").select("*").eq("visitor_email", email).order("started_at", desc=True).execute()
        sessions = sess_res.data or []

    # Get messages for each session
    result = []
    for s in sessions:
        msgs_res = db.table("chat_messages").select(
            "id, role, content, sent_at"
        ).eq("session_id", s["id"]).order("sent_at", desc=False).execute()
        result.append({
            "id": s["id"],
            "started_at": s.get("started_at"),
            "ended_at": s.get("ended_at"),
            "status": s.get("status"),
            "message_count": len(msgs_res.data or []),
            "messages": msgs_res.data or [],
        })

    return {"sessions": result}


@router.get("/call-history")
async def get_call_history_endpoint(
    business_id: str,
    period: str = "month",
):
    """Fetch past phone call sessions with transcripts for the dashboard."""
    db = get_db()
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "week":
        start = (now - timedelta(days=7)).isoformat()
    else:
        start = (now - timedelta(days=30)).isoformat()

    sessions_res = db.table("call_sessions").select("*").eq(
        "business_id", business_id
    ).gte("started_at", start).order("started_at", desc=True).execute()

    result = []
    for session in (sessions_res.data or []):
        transcripts_res = db.table("call_transcripts").select(
            "id, role, content, timestamp"
        ).eq("session_id", session["id"]).order("timestamp", desc=False).execute()
        transcripts = transcripts_res.data or []

        caller_msgs = [t for t in transcripts if t["role"] == "caller"]
        last_caller = caller_msgs[-1]["content"][:120] if caller_msgs else ""

        result.append({
            "id": session["id"],
            "caller_phone": session.get("caller_phone") or "",
            "caller_name": session.get("caller_name") or "Caller",
            "started_at": session.get("started_at"),
            "ended_at": session.get("ended_at"),
            "duration_seconds": session.get("duration_seconds") or 0,
            "status": session.get("status", "ended"),
            "transcript_count": len(transcripts),
            "last_caller_message": last_caller,
            "recording_url": session.get("recording_url") or "",
            "transcripts": transcripts,
        })

    return {"calls": result, "total": len(result)}


@router.get("/chat-history")
async def get_chat_history(
    business_id: str,
    period: str = "month",
    status: str = "all",
):
    """Fetch past live chat conversations with visitor info and message counts."""
    db = get_db()
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "week":
        start = (now - timedelta(days=7)).isoformat()
    elif period == "month":
        start = (now - timedelta(days=30)).isoformat()
    else:
        start = (now - timedelta(days=90)).isoformat()

    query = db.table("chat_sessions").select("*").eq(
        "business_id", business_id
    ).gte("started_at", start).order("started_at", desc=True)

    if status != "all":
        query = query.eq("status", status)

    sessions_res = query.execute()
    sessions = sessions_res.data or []

    result = []
    for session in sessions:
        # Fetch messages for this session
        msgs_res = db.table("chat_messages").select(
            "id, role, content, sent_at, confidence_score"
        ).eq("session_id", session["id"]).order("sent_at", desc=False).execute()
        messages = msgs_res.data or []

        # Get visitor phone from metadata if stored there
        metadata = session.get("metadata") or {}
        visitor_phone = metadata.get("visitor_phone", "")

        # Get last visitor message as preview
        visitor_msgs = [m for m in messages if m["role"] == "visitor"]
        last_visitor_msg = visitor_msgs[-1]["content"] if visitor_msgs else ""

        result.append({
            "id": session["id"],
            "visitor_name": session.get("visitor_name") or "Visitor",
            "visitor_email": session.get("visitor_email") or "",
            "visitor_phone": visitor_phone,
            "started_at": session.get("started_at"),
            "ended_at": session.get("ended_at"),
            "status": session.get("status", "active"),
            "human_active": session.get("human_active", False),
            "message_count": len(messages),
            "last_message_preview": last_visitor_msg[:120] if last_visitor_msg else "",
            "messages": messages,
        })

    return {"conversations": result, "total": len(result)}


@router.post("/leads/{lead_id}/notes")
async def add_lead_note(lead_id: str, body: dict):
    """Add an internal note to a lead. Requires Pro plan."""
    db = get_db()
    business_id = body.get("business_id", "")
    if business_id:
        plan_res = db.table("subscription_plans").select("plan_tier").eq("business_id", business_id).eq("status", "active").maybe_single().execute()
        if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") not in ("pro", "enterprise"):
            raise HTTPException(status_code=403, detail="Lead notes require Pro or Enterprise plan")
    note = body.get("note", "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="Note cannot be empty")

    # Store notes in the contact's metadata via a notes table approach
    # We'll use the audit_logs table with entity_type="lead_note"
    db.table("audit_logs").insert({
        "entity_type": "lead_note",
        "entity_id": lead_id,
        "action": "note_added",
        "performed_by": body.get("user_id", "owner"),
        "metadata_json": {"note": note, "created_at": datetime.now(timezone.utc).isoformat()},
    }).execute()
    return {"status": "saved", "lead_id": lead_id}


@router.get("/leads/{lead_id}/notes")
async def get_lead_notes(lead_id: str):
    """Get all internal notes for a lead."""
    db = get_db()
    res = db.table("audit_logs").select(
        "id, metadata_json, performed_by, created_at"
    ).eq("entity_type", "lead_note").eq("entity_id", lead_id).order(
        "created_at", desc=True
    ).execute()

    notes = []
    for row in (res.data or []):
        meta = row.get("metadata_json") or {}
        notes.append({
            "id": row["id"],
            "note": meta.get("note", ""),
            "created_at": meta.get("created_at") or row.get("created_at", ""),
            "author": row.get("performed_by", "owner"),
        })
    return {"notes": notes}


@router.post("/leads/send-email")
async def send_bulk_email(body: dict):
    """Send an email to selected leads. Requires Growth or Pro plan."""
    from app.services.email_service import send_email

    emails = body.get("emails", [])
    subject = body.get("subject", "")
    message = body.get("message", "")
    business_id = body.get("business_id", "")

    # Plan gate: Growth or Pro required
    if business_id:
        _db = get_db()
        plan_res = _db.table("subscription_plans").select("plan_tier").eq("business_id", business_id).eq("status", "active").maybe_single().execute()
        if not plan_res or not plan_res.data or plan_res.data.get("plan_tier") not in ("growth", "pro"):
            raise HTTPException(status_code=403, detail="Email outreach requires Growth or Pro plan")

    if not emails or not subject or not message:
        raise HTTPException(status_code=400, detail="emails, subject, and message are required")

    results = []
    for email in emails:
        if not email or "@" not in email:
            continue
        result = send_email(
            to_email=email,
            body=message,
            subject=subject,
            customer_name="",
            business_id=business_id,
        )
        results.append({"email": email, **result})

    sent_count = sum(1 for r in results if r.get("status") == "sent")
    return {
        "sent": sent_count,
        "failed": len(results) - sent_count,
        "total": len(results),
        "results": results,
    }


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str):
    db = get_db()
    try:
        res = db.table("conversations").select(
            "*, contacts(*), channels(channel_type, external_identifier)"
        ).eq("id", conversation_id).execute()
        data = res.data[0] if res.data else None
    except Exception:
        data = None

    if not data:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return data


@router.get("/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    """All messages in a conversation — used by Claude for draft context."""
    db = get_db()
    res = db.table("inbound_messages").select(
        "*, response_drafts!message_id(draft_body), sent_responses!message_id(body_sent, sent_at)"
    ).eq("conversation_id", conversation_id).order("received_at", desc=False).execute()

    return {"messages": res.data or [], "conversation_id": conversation_id}


@router.post("/{conversation_id}/close")
async def close_conversation(conversation_id: str, user_id: str):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    res = db.table("conversations").update({
        "status": "closed",
        "closed_at": now,
    }).eq("id", conversation_id).execute()

    db.table("audit_logs").insert({
        "entity_type": "conversation",
        "entity_id": conversation_id,
        "action": "closed",
        "performed_by": user_id,
        "metadata_json": {},
    }).execute()

    return {"status": "closed", "closed_at": now}