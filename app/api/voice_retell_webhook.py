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

    # Look up business by agent
    # For now, map the known agent to Pawty Yoga
    business_id = None
    if agent_id == "agent_87ddc13524a76156ba11f73b6e":
        business_id = "90d3ad7a-bac2-4a20-90ee-39f52db08669"

    if not business_id:
        logger.warning(f"Unknown agent: {agent_id}")
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

    # Send engagement email to business owner
    if business_id:
        try:
            send_call_engagement_email(business_id, session_id)
        except Exception as e:
            logger.error(f"Call engagement email failed: {e}")


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
