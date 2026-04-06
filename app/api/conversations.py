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


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str):
    db = get_db()
    res = db.table("conversations").select(
        "*, contacts(*), channels(channel_type, external_identifier)"
    ).eq("id", conversation_id).maybe_single().execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return res.data


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


@router.get("/leads")
async def get_lead_database(business_id: str):
    """
    Returns a deduplicated master lead database for a business.
    Groups all inbound messages by sender email/phone to create unique lead records.
    """
    db = get_db()

    res = db.table("inbound_messages").select(
        "sender_name, sender_email, sender_phone, intent, received_at"
    ).eq("business_id", business_id).order("received_at", desc=False).execute()

    messages = res.data or []

    # Deduplicate by email first, then phone
    leads: dict[str, dict] = {}

    for m in messages:
        email = (m.get("sender_email") or "").strip().lower() or None
        phone = (m.get("sender_phone") or "").strip() or None
        name = m.get("sender_name") or "Unknown"
        intent = m.get("intent") or "unknown"
        received = m.get("received_at") or ""

        # Use email as primary key, fallback to phone, fallback to name
        key = email or phone or name

        if key not in leads:
            leads[key] = {
                "id": key,
                "name": name,
                "email": email,
                "phone": phone,
                "first_contact": received,
                "last_contact": received,
                "message_count": 0,
                "intents": [],
            }

        lead = leads[key]
        lead["message_count"] += 1
        lead["last_contact"] = received

        # Update name if we get a better one
        if name and name != "Unknown" and lead["name"] == "Unknown":
            lead["name"] = name

        # Update contact info if missing
        if email and not lead["email"]:
            lead["email"] = email
        if phone and not lead["phone"]:
            lead["phone"] = phone

        if intent and intent not in lead["intents"]:
            lead["intents"].append(intent)

    # Add top_intent (most common)
    from collections import Counter
    result = []
    for lead in leads.values():
        top = lead["intents"][0] if lead["intents"] else "unknown"
        result.append({**lead, "top_intent": top})

    # Sort by last contact descending
    result.sort(key=lambda x: x["last_contact"], reverse=True)

    return {"leads": result, "total": len(result)}