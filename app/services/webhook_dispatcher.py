"""
Outbound Webhook Dispatcher — Frontdesk AI
Fires events to configured webhook URLs (Zapier, Make, custom).
Fire-and-forget: failures are logged but never block the main flow.
"""

import logging
import threading
import httpx
from datetime import datetime, timezone

from app.core.database import get_db

logger = logging.getLogger(__name__)


def fire_webhook(business_id: str, event: str, payload: dict) -> None:
    """
    Fire an outbound webhook event in a background thread.
    Never raises — failures are logged silently.

    Events: call.ended, chat.ended, lead.created, email.received
    """
    def _send():
        try:
            db = get_db()
            biz = db.table("businesses").select("metadata").eq("id", business_id).execute()
            meta = (biz.data[0].get("metadata") or {}) if biz.data else {}
            url = meta.get("outbound_webhook_url", "")

            if not url:
                return  # No webhook configured

            body = {
                "event": event,
                "business_id": business_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": payload,
            }

            res = httpx.post(url, json=body, timeout=10)
            logger.info(f"Webhook fired: {event} → {url} ({res.status_code})")

        except Exception as e:
            logger.error(f"Webhook dispatch failed for {event}: {e}")

    # Fire in background thread so it never blocks
    threading.Thread(target=_send, daemon=True).start()
