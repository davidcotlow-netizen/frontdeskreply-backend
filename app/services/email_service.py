import logging
import httpx

logger = logging.getLogger(__name__)

RESEND_API_KEY = "re_HgGkxGUj_LTzdQZVT43TyDPnPdgnTZmJU"

def send_email(
    to_email: str,
    body: str,
    subject: str = "Reply from FrontdeskReply",
    customer_name: str = "",
    business_id: str = "",
) -> dict:
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "FrontdeskReply <hello@frontdeskreply.com>", "to": [to_email], "subject": subject, "text": body},
            timeout=10.0
        )
        result = response.json()
        logger.info(f"Email sent to {to_email} via Resend: {result}")
        return {"status": "sent", "to": to_email, "method": "resend", "id": result.get("id")}
    except Exception as e:
        logger.error(f"Resend error sending to {to_email}: {e}")
        return {"status": "error", "reason": str(e)}
