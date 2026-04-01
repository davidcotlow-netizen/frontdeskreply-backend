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
    first_name = customer_name.split()[0] if customer_name else "there"
    body_html = "".join(
        f'<p style="margin:0 0 14px 0;font-size:14px;color:#444444;line-height:1.75;">{line}</p>'
        for line in body.strip().split("\n") if line.strip()
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f4f4f7;padding:32px 16px;">
<tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%;">
<tr><td style="background:#1a1a2e;border-radius:12px 12px 0 0;padding:28px 40px;text-align:center;">
<span style="color:#ffffff;font-size:18px;font-weight:500;">FrontdeskReply</span>
<span style="display:block;color:rgba(255,255,255,0.4);font-size:10px;letter-spacing:0.08em;text-transform:uppercase;margin-top:4px;">Powered by FrontdeskReply</span>
</td></tr>
<tr><td style="height:3px;background:linear-gradient(90deg,#f97316,#fb923c);font-size:0;">&nbsp;</td></tr>
<tr><td style="background:#ffffff;padding:36px 40px 28px;">
<p style="margin:0 0 16px 0;font-size:15px;font-weight:500;color:#1a1a2e;">Hi {first_name},</p>
{body_html}
</td></tr>
<tr><td style="background:#f0f0f5;border-radius:0 0 12px 12px;padding:20px 40px;text-align:center;">
<p style="font-size:11px;color:#999999;margin:0;">Sent via FrontdeskReply &middot; AI-powered front desk</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "FrontdeskReply <hello@frontdeskreply.com>", "to": [to_email], "subject": subject, "html": html, "text": body},
            timeout=10.0
        )
        result = response.json()
        logger.info(f"Email sent to {to_email} via Resend: {result}")
        return {"status": "sent", "to": to_email, "method": "resend", "id": result.get("id")}
    except Exception as e:
        logger.error(f"Resend error sending to {to_email}: {e}")
        return {"status": "error", "reason": str(e)}
