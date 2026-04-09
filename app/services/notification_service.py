"""
Notification Service — Frontdesk AI
Handles escalation notifications for live chat (SMS + email alerts).
Reuses existing Twilio integration from sms_service.py.
"""

import logging
from app.services.sms_service import send_sms
from app.core.config import get_settings

logger = logging.getLogger(__name__)


def send_chat_escalation(
    owner_phone: str,
    business_name: str,
    visitor_name: str,
    reason: str,
    dashboard_url: str = "https://app.frontdeskreply.com",
) -> dict:
    """
    Alert business owner that a live chat needs human attention.
    Sends SMS to the owner's phone number.
    """
    visitor_label = visitor_name or "A visitor"
    body = (
        f"💬 LIVE CHAT ALERT — {business_name}\n"
        f"{visitor_label} needs to speak with you.\n"
        f"Reason: {reason}\n"
        f"Open your dashboard to respond: {dashboard_url}"
    )
    result = send_sms(to_number=owner_phone, body=body)
    logger.info(f"Chat escalation SMS sent to {owner_phone}: {result}")
    return result


def send_chat_escalation_email(
    owner_email: str,
    business_name: str,
    visitor_name: str,
    reason: str,
    session_summary: str = "",
    dashboard_url: str = "https://app.frontdeskreply.com",
) -> dict:
    """
    Send escalation email to business owner.
    Uses Resend if available, otherwise skips.
    """
    settings = get_settings()
    if not settings.resend_api_key:
        logger.warning("Resend not configured — escalation email skipped")
        return {"status": "skipped", "reason": "resend_not_configured"}

    try:
        import resend
        resend.api_key = settings.resend_api_key

        visitor_label = visitor_name or "A visitor"
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px;">
            <h2 style="color: #E8714A;">💬 Live Chat Needs You</h2>
            <p><strong>{visitor_label}</strong> is waiting in live chat on <strong>{business_name}</strong>.</p>
            <p><strong>Reason:</strong> {reason}</p>
            {'<p><strong>Recent messages:</strong><br>' + session_summary + '</p>' if session_summary else ''}
            <a href="{dashboard_url}" style="display: inline-block; padding: 12px 24px; background: #E8714A; color: white; text-decoration: none; border-radius: 6px; margin-top: 12px;">
                Open Live Chat Dashboard
            </a>
        </div>
        """

        result = resend.Emails.send({
            "from": f"{business_name} <hello@frontdeskreply.com>",
            "to": [owner_email],
            "subject": f"💬 Live chat — {visitor_label} needs you",
            "html": html_body,
        })
        logger.info(f"Chat escalation email sent to {owner_email}")
        return {"status": "sent", "id": result.get("id", "")}

    except Exception as e:
        logger.error(f"Escalation email failed: {e}")
        return {"status": "error", "error": str(e)}
