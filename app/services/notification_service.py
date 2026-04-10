"""
Notification Service — Frontdesk AI
Handles escalation notifications for live chat (SMS + email alerts).
Also sends engagement summary emails after chat/call/SMS interactions.
Reuses existing Twilio integration from sms_service.py.
"""

import logging
from app.services.sms_service import send_sms
from app.core.config import get_settings
from app.core.database import get_db

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


# ── Engagement notification helpers ─────────────────────────────────────────

def _get_owner_email(business_id: str) -> str | None:
    """Get the business owner's email from the businesses table."""
    db = get_db()
    res = db.table("businesses").select("email").eq("id", business_id).maybe_single().execute()
    if res and res.data:
        return res.data.get("email")
    return None


def _get_business_name(business_id: str) -> str:
    """Get the business name."""
    db = get_db()
    res = db.table("businesses").select("name").eq("id", business_id).maybe_single().execute()
    if res and res.data:
        return res.data.get("name", "Your Business")
    return "Your Business"


def _format_transcript_html(messages: list, channel: str = "chat") -> str:
    """Format a list of message dicts into HTML for email."""
    if not messages:
        return "<p style='color:#888;'>No messages recorded.</p>"

    html_parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role in ("visitor", "caller", "human"):
            label = "Customer"
            color = "#2563EB"
        elif role in ("ai", "milo", "vela"):
            label = "Vela AI"
            color = "#E8714A"
        else:
            label = role.capitalize()
            color = "#666"

        html_parts.append(
            f'<p style="margin:8px 0;"><strong style="color:{color};">{label}:</strong> {content}</p>'
        )

    return "".join(html_parts)


# ── Chat engagement notification ────────────────────────────────────────────

def send_chat_engagement_email(business_id: str, session_id: str) -> dict:
    """
    Send a summary email to the business owner after a chat session ends.
    Includes visitor info + full conversation transcript.
    """
    settings = get_settings()
    if not settings.resend_api_key:
        logger.warning("Resend not configured — chat engagement email skipped")
        return {"status": "skipped", "reason": "resend_not_configured"}

    owner_email = _get_owner_email(business_id)
    if not owner_email:
        logger.warning(f"No owner email for business {business_id}")
        return {"status": "skipped", "reason": "no_owner_email"}

    business_name = _get_business_name(business_id)

    # Get session details
    db = get_db()
    session = db.table("chat_sessions").select("*").eq("id", session_id).maybe_single().execute()
    session_data = session.data if session else {}

    visitor_name = session_data.get("visitor_name", "Unknown visitor")
    visitor_email = session_data.get("visitor_email", "Not provided")
    visitor_phone = session_data.get("visitor_phone", "Not provided")

    # Get messages
    messages_res = db.table("chat_messages").select("role, content, sent_at").eq(
        "session_id", session_id
    ).order("sent_at", desc=False).execute()
    messages = messages_res.data or []

    transcript_html = _format_transcript_html(messages, "chat")
    msg_count = len([m for m in messages if m["role"] == "visitor"])

    try:
        import resend
        resend.api_key = settings.resend_api_key

        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #1a1a2e; border-radius: 12px 12px 0 0; padding: 24px 32px; text-align: center;">
                <div style="color: #E8714A; font-size: 24px; font-weight: 800;">{business_name}</div>
                <div style="color: rgba(255,255,255,0.5); font-size: 12px; margin-top: 4px;">New Chat Engagement</div>
            </div>
            <div style="background: #ffffff; padding: 28px 32px; border: 1px solid #eee; border-top: none;">
                <h2 style="margin: 0 0 16px; font-size: 18px; color: #1a1a2e;">Someone chatted with Vela AI</h2>

                <table style="width: 100%; margin-bottom: 20px; font-size: 14px;">
                    <tr><td style="padding: 6px 0; color: #888; width: 100px;">Name:</td><td style="padding: 6px 0; font-weight: 600;">{visitor_name}</td></tr>
                    <tr><td style="padding: 6px 0; color: #888;">Email:</td><td style="padding: 6px 0;">{visitor_email}</td></tr>
                    <tr><td style="padding: 6px 0; color: #888;">Phone:</td><td style="padding: 6px 0;">{visitor_phone}</td></tr>
                    <tr><td style="padding: 6px 0; color: #888;">Messages:</td><td style="padding: 6px 0;">{msg_count} from visitor</td></tr>
                </table>

                <div style="background: #f8f7f5; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                    <div style="font-size: 13px; font-weight: 700; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px;">Conversation</div>
                    {transcript_html}
                </div>

                <a href="https://app.frontdeskreply.com" style="display: inline-block; padding: 12px 28px; background: #E8714A; color: white; text-decoration: none; border-radius: 8px; font-weight: 700; font-size: 14px;">
                    Open Dashboard
                </a>
            </div>
            <div style="background: #f8f7f5; border-radius: 0 0 12px 12px; padding: 16px 32px; text-align: center; font-size: 11px; color: #aaa;">
                Powered by FrontDeskReply &middot; Automated engagement notification
            </div>
        </div>
        """

        result = resend.Emails.send({
            "from": f"{business_name} Notifications <hello@frontdeskreply.com>",
            "to": [owner_email],
            "subject": f"New chat from {visitor_name} — {business_name}",
            "html": html_body,
        })
        logger.info(f"Chat engagement email sent to {owner_email} for session {session_id}")
        return {"status": "sent", "id": result.get("id", "")}

    except Exception as e:
        logger.error(f"Chat engagement email failed: {e}")
        return {"status": "error", "error": str(e)}


# ── Voice call engagement notification ──────────────────────────────────────

def send_call_engagement_email(business_id: str, session_id: str) -> dict:
    """
    Send a summary email to the business owner after a voice call ends.
    Includes caller info + transcript.
    """
    settings = get_settings()
    if not settings.resend_api_key:
        logger.warning("Resend not configured — call engagement email skipped")
        return {"status": "skipped", "reason": "resend_not_configured"}

    owner_email = _get_owner_email(business_id)
    if not owner_email:
        logger.warning(f"No owner email for business {business_id}")
        return {"status": "skipped", "reason": "no_owner_email"}

    business_name = _get_business_name(business_id)

    db = get_db()
    session = db.table("call_sessions").select("*").eq("id", session_id).maybe_single().execute()
    session_data = session.data if session else {}

    caller_phone = session_data.get("caller_phone", "Unknown number")
    duration = session_data.get("duration_seconds", 0)
    duration_str = f"{duration // 60}m {duration % 60}s" if duration else "Unknown"

    # Get transcript
    transcripts_res = db.table("call_transcripts").select("role, content, timestamp").eq(
        "session_id", session_id
    ).order("timestamp", desc=False).execute()
    transcripts = transcripts_res.data or []

    transcript_html = _format_transcript_html(transcripts, "call")
    caller_msgs = [t for t in transcripts if t["role"] == "caller"]
    first_question = caller_msgs[0]["content"][:150] if caller_msgs else "No caller speech recorded"

    try:
        import resend
        resend.api_key = settings.resend_api_key

        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #1a1a2e; border-radius: 12px 12px 0 0; padding: 24px 32px; text-align: center;">
                <div style="color: #E8714A; font-size: 24px; font-weight: 800;">{business_name}</div>
                <div style="color: rgba(255,255,255,0.5); font-size: 12px; margin-top: 4px;">New Phone Call</div>
            </div>
            <div style="background: #ffffff; padding: 28px 32px; border: 1px solid #eee; border-top: none;">
                <h2 style="margin: 0 0 16px; font-size: 18px; color: #1a1a2e;">Someone called and spoke with Vela AI</h2>

                <table style="width: 100%; margin-bottom: 20px; font-size: 14px;">
                    <tr><td style="padding: 6px 0; color: #888; width: 100px;">Caller:</td><td style="padding: 6px 0; font-weight: 600;">{caller_phone}</td></tr>
                    <tr><td style="padding: 6px 0; color: #888;">Duration:</td><td style="padding: 6px 0;">{duration_str}</td></tr>
                    <tr><td style="padding: 6px 0; color: #888;">Asked about:</td><td style="padding: 6px 0;">{first_question}</td></tr>
                </table>

                <div style="background: #f8f7f5; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                    <div style="font-size: 13px; font-weight: 700; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px;">Call Transcript</div>
                    {transcript_html}
                </div>

                <a href="https://app.frontdeskreply.com" style="display: inline-block; padding: 12px 28px; background: #E8714A; color: white; text-decoration: none; border-radius: 8px; font-weight: 700; font-size: 14px;">
                    Open Dashboard
                </a>
            </div>
            <div style="background: #f8f7f5; border-radius: 0 0 12px 12px; padding: 16px 32px; text-align: center; font-size: 11px; color: #aaa;">
                Powered by FrontDeskReply &middot; Automated engagement notification
            </div>
        </div>
        """

        result = resend.Emails.send({
            "from": f"{business_name} Notifications <hello@frontdeskreply.com>",
            "to": [owner_email],
            "subject": f"New call from {caller_phone} — {business_name}",
            "html": html_body,
        })
        logger.info(f"Call engagement email sent to {owner_email} for session {session_id}")
        return {"status": "sent", "id": result.get("id", "")}

    except Exception as e:
        logger.error(f"Call engagement email failed: {e}")
        return {"status": "error", "error": str(e)}


# ── SMS engagement notification ─────────────────────────────────────────────

def send_sms_engagement_email(
    business_id: str,
    from_number: str,
    visitor_message: str,
    vela_response: str,
) -> dict:
    """
    Send a summary email to the business owner after an SMS exchange.
    """
    settings = get_settings()
    if not settings.resend_api_key:
        logger.warning("Resend not configured — SMS engagement email skipped")
        return {"status": "skipped", "reason": "resend_not_configured"}

    owner_email = _get_owner_email(business_id)
    if not owner_email:
        logger.warning(f"No owner email for business {business_id}")
        return {"status": "skipped", "reason": "no_owner_email"}

    business_name = _get_business_name(business_id)

    try:
        import resend
        resend.api_key = settings.resend_api_key

        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #1a1a2e; border-radius: 12px 12px 0 0; padding: 24px 32px; text-align: center;">
                <div style="color: #E8714A; font-size: 24px; font-weight: 800;">{business_name}</div>
                <div style="color: rgba(255,255,255,0.5); font-size: 12px; margin-top: 4px;">New Text Message</div>
            </div>
            <div style="background: #ffffff; padding: 28px 32px; border: 1px solid #eee; border-top: none;">
                <h2 style="margin: 0 0 16px; font-size: 18px; color: #1a1a2e;">Someone texted your business number</h2>

                <table style="width: 100%; margin-bottom: 20px; font-size: 14px;">
                    <tr><td style="padding: 6px 0; color: #888; width: 100px;">From:</td><td style="padding: 6px 0; font-weight: 600;">{from_number}</td></tr>
                </table>

                <div style="background: #f8f7f5; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                    <div style="font-size: 13px; font-weight: 700; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px;">Conversation</div>
                    <p style="margin:8px 0;"><strong style="color:#2563EB;">Customer:</strong> {visitor_message}</p>
                    <p style="margin:8px 0;"><strong style="color:#E8714A;">Vela AI:</strong> {vela_response}</p>
                </div>

                <a href="https://app.frontdeskreply.com" style="display: inline-block; padding: 12px 28px; background: #E8714A; color: white; text-decoration: none; border-radius: 8px; font-weight: 700; font-size: 14px;">
                    Open Dashboard
                </a>
            </div>
            <div style="background: #f8f7f5; border-radius: 0 0 12px 12px; padding: 16px 32px; text-align: center; font-size: 11px; color: #aaa;">
                Powered by FrontDeskReply &middot; Automated engagement notification
            </div>
        </div>
        """

        result = resend.Emails.send({
            "from": f"{business_name} Notifications <hello@frontdeskreply.com>",
            "to": [owner_email],
            "subject": f"New text from {from_number} — {business_name}",
            "html": html_body,
        })
        logger.info(f"SMS engagement email sent to {owner_email} for {from_number}")
        return {"status": "sent", "id": result.get("id", "")}

    except Exception as e:
        logger.error(f"SMS engagement email failed: {e}")
        return {"status": "error", "error": str(e)}
