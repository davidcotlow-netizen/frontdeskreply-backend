"""
Retell AI Webhook — Frontdesk AI
Receives post-call data from Retell AI and saves transcripts to Supabase.
Also provides a sync endpoint to pull existing calls from Retell.
"""

import logging
import re
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.database import get_db
from app.services.voice_service import create_call_session, add_call_transcript, end_call_session
from app.services.notification_service import send_call_engagement_email
from app.services.webhook_dispatcher import fire_webhook
from app.api.voice_ws import _extract_source

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/retell", tags=["retell"])

RETELL_API_KEY = "key_2b33b7e079f15e3c8351b40ad0ea"


def parse_transcript_string(transcript_str: str) -> list:
    """Parse Retell's plain text transcript into structured entries."""
    entries = []
    # Retell format: "Agent: text\nUser: text\n..."
    lines = transcript_str.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("Agent: "):
            entries.append({"role": "milo", "content": line[7:].strip()})
        elif line.startswith("User: "):
            entries.append({"role": "caller", "content": line[6:].strip()})
    return entries


def extract_source_from_transcript(entries: list) -> str | None:
    """
    Scan transcript for the 'how did you hear about us?' exchange and extract
    the caller's answer as a lead source.
    """
    for i, entry in enumerate(entries):
        if entry["role"] == "milo":
            text = entry["content"].lower()
            if "hear about" in text or "find out about" in text or "find us" in text:
                # The next caller entry is their answer
                if i + 1 < len(entries) and entries[i + 1]["role"] == "caller":
                    return _extract_source(entries[i + 1]["content"])
    return None


@router.post("/webhook")
async def retell_webhook(request: Request):
    """
    Retell AI calls this after each call ends.
    Saves call data and transcript to Supabase.

    Configure in Retell dashboard: Settings → Webhook URL →
    https://api.frontdeskreply.com/api/v1/retell/webhook
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)

    event = data.get("event", "")
    call_data = data.get("call", data)

    logger.info(f"Retell webhook: event={event} call_id={call_data.get('call_id', '?')}")

    if event == "call_ended":
        await save_retell_call(call_data)
    else:
        logger.info(f"Retell webhook ignored: event={event} (only processing call_ended)")

    return {"status": "ok"}


async def save_retell_call(call_data: dict):
    """Save a Retell call and its transcript to Supabase."""
    db = get_db()
    call_id = call_data.get("call_id", "")
    agent_id = call_data.get("agent_id", "")
    from_number = call_data.get("from_number", "")
    duration_ms = call_data.get("duration_ms", 0)
    transcript_str = call_data.get("transcript", "")
    call_status = call_data.get("call_status", "ended")
    start_ts = call_data.get("start_timestamp")
    recording_url = call_data.get("recording_url") or ""

    # Look up business by Retell agent ID from voice channel
    business_id = None
    ch_res = db.table("channels").select("business_id").eq(
        "channel_type", "voice"
    ).eq("external_identifier", f"retell:{agent_id}").execute()
    if ch_res.data:
        business_id = ch_res.data[0]["business_id"]

    if not business_id:
        logger.warning(f"Unknown Retell agent: {agent_id} — no matching voice channel")
        return

    # Check if we already have this call
    existing = db.table("call_sessions").select("id").eq("call_sid", call_id).maybe_single().execute()
    if existing and existing.data:
        session_id = existing.data["id"]
        # Update with duration and recording URL
        update_payload = {
            "duration_seconds": duration_ms // 1000,
            "status": "ended" if call_status == "ended" else call_status,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }
        if recording_url:
            update_payload["recording_url"] = recording_url
        db.table("call_sessions").update(update_payload).eq("id", session_id).execute()
    else:
        # Create new session
        started_at = datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc).isoformat() if start_ts else datetime.now(timezone.utc).isoformat()
        session_res = db.table("call_sessions").insert({
            "business_id": business_id,
            "caller_phone": from_number,
            "caller_name": "Caller",
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": duration_ms // 1000,
            "status": "ended",
            "call_sid": call_id,
            "recording_url": recording_url,
            "metadata": {"retell_agent": agent_id},
        }).execute()
        session_id = session_res.data[0]["id"]

        # Save caller as lead
        from app.services.chat_service import _find_or_create_contact
        if from_number:
            _find_or_create_contact(db, business_id, phone=from_number)

    # Parse and save transcript
    if transcript_str:
        # Clear existing transcripts for this session (in case of re-sync)
        db.table("call_transcripts").delete().eq("session_id", session_id).execute()

        entries = parse_transcript_string(transcript_str)
        for entry in entries:
            db.table("call_transcripts").insert({
                "session_id": session_id,
                "role": entry["role"],
                "content": entry["content"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }).execute()

        logger.info(f"Saved {len(entries)} transcript entries for call {call_id}")

        # Extract lead source from transcript
        caller_source = extract_source_from_transcript(entries)
        if caller_source:
            db.table("call_sessions").update({
                "caller_source": caller_source,
            }).eq("id", session_id).execute()
            logger.info(f"Lead source extracted from webhook transcript: {caller_source}")

    # Send engagement email to business owner
    if business_id:
        try:
            send_call_engagement_email(business_id, session_id)
        except Exception as e:
            logger.error(f"Call engagement email failed: {e}")

        # Fire outbound webhook
        fire_webhook(business_id, "call.ended", {
            "session_id": session_id,
            "caller_phone": from_number,
            "duration_seconds": duration_ms // 1000,
            "call_status": call_status,
            "caller_source": caller_source if 'caller_source' in dir() else None,
        })


@router.post("/dynamic-variables")
async def retell_dynamic_variables(request: Request):
    """
    Retell calls this before each inbound call to get per-caller context.
    We look up the caller's history and return variables injected into the prompt.
    """
    try:
        data = await request.json()
    except Exception:
        return {"caller_history": ""}

    from_number = data.get("from_number", "")
    agent_id = data.get("agent_id", "")

    if not from_number:
        return {"caller_history": ""}

    db = get_db()

    # Find business from agent
    business_id = None
    ch_res = db.table("channels").select("business_id").eq(
        "channel_type", "voice"
    ).eq("external_identifier", f"retell:{agent_id}").execute()
    if ch_res.data:
        business_id = ch_res.data[0]["business_id"]

    if not business_id:
        return {"caller_history": ""}

    # Look up caller by phone
    clean = from_number.replace("+1", "").replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    contacts = db.table("contacts").select("name, first_seen_at").eq(
        "business_id", business_id
    ).execute()

    caller_name = None
    for c in (contacts.data or []):
        cp = (c.get("phone") or "").replace("+1", "").replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
        if cp and cp == clean:
            caller_name = c.get("name")
            break

    # Get previous call sessions
    calls = db.table("call_sessions").select(
        "id, started_at, duration_seconds, caller_source"
    ).eq("business_id", business_id).eq("caller_phone", from_number).order(
        "started_at", desc=True
    ).execute()

    prev_calls = calls.data or []

    if not prev_calls:
        return {"caller_history": ""}

    # Build caller history summary
    lines = []
    if caller_name and caller_name != "Caller":
        lines.append(f"RETURNING CALLER: This person has called before. Their name is {caller_name}.")
    else:
        lines.append("RETURNING CALLER: This phone number has called before.")

    lines.append(f"Previous calls: {len(prev_calls)}")

    if prev_calls[0].get("caller_source"):
        lines.append(f"How they heard about us: {prev_calls[0]['caller_source']}")

    # Get last call transcript summary (most recent, up to 3 exchanges)
    last_session_id = prev_calls[0]["id"]
    transcripts = db.table("call_transcripts").select("role, content").eq(
        "session_id", last_session_id
    ).order("timestamp", desc=False).execute()

    caller_msgs = [t["content"] for t in (transcripts.data or []) if t["role"] == "caller"]
    if caller_msgs:
        topics = "; ".join(caller_msgs[:3])
        lines.append(f"Last call topics: {topics[:200]}")

    history = "\n".join(lines)
    logger.info(f"Returning caller context for {from_number}: {len(prev_calls)} previous calls")

    return {"caller_history": history}


@router.post("/sync")
async def sync_retell_calls():
    """
    Pull all recent calls from Retell API and save to Supabase.
    Run this manually to backfill existing calls.
    """
    import httpx

    headers = {"Authorization": f"Bearer {RETELL_API_KEY}", "Content-Type": "application/json"}

    res = httpx.post("https://api.retellai.com/v2/list-calls", headers=headers, json={"limit": 50}, timeout=30)
    if res.status_code != 200:
        return {"status": "error", "message": f"Retell API error: {res.status_code}"}

    calls = res.json()
    synced = 0

    for call in calls:
        if isinstance(call, dict) and call.get("call_status") in ("ended", "error"):
            await save_retell_call(call)
            synced += 1

    return {"status": "ok", "synced": synced, "total": len(calls)}
