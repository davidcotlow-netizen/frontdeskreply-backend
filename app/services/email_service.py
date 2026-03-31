import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.core.config import get_settings

logger = logging.getLogger(__name__)

def send_email(
    to_email: str,
    body: str,
    subject: str = "Reply from FrontdeskReply",
    customer_name: str = "",
    business_id: str = "",
) -> dict:
    settings = get_settings()
    if not settings.gmail_user:
        return {"status": "error", "reason": "gmail_not_configured"}
    if not settings.gmail_app_password:
        return {"status": "error", "reason": "gmail_password_not_configured"}
    try:
        msg = MIMEMultipart()
        msg["From"] = settings.gmail_user
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(settings.gmail_user, settings.gmail_app_password)
            server.sendmail(settings.gmail_user, to_email, msg.as_string())
        return {"status": "sent", "to": to_email, "method": "gmail_smtp"}
    except Exception as e:
        logger.error(f"Gmail SMTP error sending to {to_email}: {e}")
        return {"status": "error", "reason": str(e)}
