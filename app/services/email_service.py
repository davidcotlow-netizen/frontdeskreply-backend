import logging
import httpx
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
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"},
            json={"from": "FrontdeskReply <hello@frontdeskreply.com>", "to": [to_email], "subject": subject, "text": body},
            timeout=10.0
        )
        result = response.json()
        logger.info(f"Email sent to {to_email} via Resend: {result}")
        return {"status": "sent", "to": to_email, "method": "resend", "id": result.get("id")}
    except Exception as e:
        logger.error(f"Resend error sending to {to_email}: {e}")
        return {"status": "error", "reason": str(e)}
