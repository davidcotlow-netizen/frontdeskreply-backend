"""
SMS Service — Frontdesk AI
Handles outbound SMS via Twilio.
"""

import logging
from app.core.config import get_settings

logger = logging.getLogger(__name__)


def send_sms(to_number: str, body: str) -> dict:
    """Send outbound SMS via Twilio. Returns message SID or error."""
    settings = get_settings()

    if not settings.twilio_account_sid:
        logger.warning("Twilio not configured — SMS send skipped")
        return {"status": "skipped", "reason": "twilio_not_configured"}

    try:
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        message = client.messages.create(
            body=body,
            from_=settings.twilio_phone_number,
            to=to_number,
        )
        logger.info(f"SMS sent to {to_number}: SID={message.sid}")
        return {"status": "sent", "sid": message.sid}
    except Exception as e:
        logger.error(f"SMS send failed to {to_number}: {e}")
        return {"status": "error", "error": str(e)}


def send_escalation_alert(owner_phone: str, message_preview: str, reason: str) -> dict:
    """Send escalation push notification to business owner via SMS."""
    body = (
        f"🚨 FRONTDESK AI ALERT\n"
        f"Reason: {reason}\n"
        f"Message: {message_preview[:100]}...\n"
        f"Open your dashboard to respond immediately."
    )
    return send_sms(owner_phone, body)
