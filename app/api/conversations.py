from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from app.core.database import get_db

router = APIRouter(prefix="/conversations", tags=["conversations"])


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


@router.get("/leads")
async def get_lead_database(business_id: str):
    """
    Unified lead database — merges contacts from both the old form pipeline
    and the new live chat sessions. MUST be before /{conversation_id}.
    """
    db = get_db()
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
        result.append({**lead, "top_intent": top})

    result.sort(key=lambda x: x["last_contact"], reverse=True)
    return {"leads": result, "total": len(result)}


@router.patch("/leads/{lead_id}/status")
async def update_lead_status(lead_id: str, body: dict):
    """Update a lead's lifecycle status (new, contacted, quoted, converted)."""
    db = get_db()
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