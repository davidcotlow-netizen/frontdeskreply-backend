import os
import logging
import httpx
from app.core.database import get_db

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

def _get_business_profile(business_id: str) -> dict:
    defaults = {"name": "Our Team", "phone": "", "address": "", "hours": [], "instagram_url": "", "facebook_url": "", "twitter_url": "", "owner_email": ""}
    try:
        supabase = get_db()
        result = supabase.table("businesses").select("*").eq("id", business_id).maybe_single().execute()
        if result and result.data:
            data = result.data
            return {"name": data.get("name") or defaults["name"], "phone": _format_phone(data.get("phone") or "") if data.get("phone") else "", "address": data.get("address") or "", "hours": data.get("business_hours") or [], "instagram_url": data.get("instagram_url") or "", "facebook_url": data.get("facebook_url") or "", "twitter_url": data.get("twitter_url") or "", "owner_email": data.get("owner_email") or ""}
    except Exception as e:
        logger.warning(f"Could not fetch business profile for {business_id}: {e}")
    return defaults

def _format_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone

def _format_hours_rows(hours: list) -> str:
    if not hours:
        return ""
    rows = ""
    for entry in hours:
        day = entry.get("day", "")
        open_t = entry.get("open", "")
        close_t = entry.get("close", "")
        value = f"{open_t} - {close_t}" if open_t and close_t else "Closed"
        rows += f'<tr><td style="font-weight:500;color:#1a1a2e;font-size:12px;padding:3px 16px 3px 0;white-space:nowrap;">{day}</td><td style="font-size:12px;color:#555;padding:3px 0;">{value}</td></tr>'
    return rows

def _social_icons(instagram_url, facebook_url, twitter_url) -> str:
    icons = []
    if facebook_url:
        icons.append(f'<a href="{facebook_url}" style="display:inline-block;width:32px;height:32px;border-radius:50%;background:#1877f2;text-align:center;line-height:32px;text-decoration:none;margin:0 4px;color:white;font-size:13px;font-weight:700;">f</a>')
    if instagram_url:
        icons.append(f'<a href="{instagram_url}" style="display:inline-block;width:32px;height:32px;border-radius:50%;background:#e1306c;text-align:center;line-height:32px;text-decoration:none;margin:0 4px;color:white;font-size:13px;font-weight:700;">in</a>')
    if twitter_url:
        icons.append(f'<a href="{twitter_url}" style="display:inline-block;width:32px;height:32px;border-radius:50%;background:#000000;text-align:center;line-height:32px;text-decoration:none;margin:0 4px;color:white;font-size:13px;font-weight:700;">X</a>')
    return "".join(icons)

def _build_html(customer_name: str, body: str, business: dict) -> str:
    first_name = customer_name.split()[0] if customer_name else "there"
    hours_rows = _format_hours_rows(business["hours"])
    social_icons = _social_icons(business["instagram_url"], business["facebook_url"], business["twitter_url"])
    body_html = "".join(f'<p style="margin:0 0 14px 0;font-size:14px;color:#444444;line-height:1.75;">{line}</p>' for line in body.strip().split("\n") if line.strip())

    hours_block = ""
    if hours_rows:
        hours_block = f'<tr><td style="padding:8px 0 0 0;"><div style="font-weight:500;color:#333;font-size:12px;margin-bottom:4px;">Business Hours</div><table cellpadding="0" cellspacing="0" border="0">{hours_rows}</table></td></tr>'

    phone_block = ""
    if business["phone"]:
        phone_block = f'<tr><td style="padding:4px 0;font-size:13px;color:#555;">&#128222; {business["phone"]}</td></tr>'

    address_block = ""
    if business["address"]:
        address_block = f'<tr><td style="padding:4px 0;font-size:13px;color:#555;">&#128205; {business["address"]}</td></tr>'

    social_section = ""
    if social_icons:
        social_section = f'<tr><td align="center" style="padding:0 0 12px 0;">{social_icons}</td></tr>'

    cta_phone = business["phone"].replace(" ", "").replace("(", "").replace(")", "").replace("-", "") if business["phone"] else ""
    cta_href = f"tel:{cta_phone}" if cta_phone else "#"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f4f4f7;padding:32px 16px;">
<tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%;">

<tr><td style="background:#1a1a2e;border-radius:12px 12px 0 0;padding:28px 40px 22px;text-align:center;">
<div style="color:#ffffff;font-size:20px;font-weight:600;letter-spacing:0.01em;">{business["name"]}</div>
<div style="color:rgba(255,255,255,0.4);font-size:10px;letter-spacing:0.08em;text-transform:uppercase;margin-top:4px;">Powered by FrontdeskReply</div>
</td></tr>

<tr><td style="height:3px;background:linear-gradient(90deg,#f97316,#fb923c);font-size:0;">&nbsp;</td></tr>

<tr><td style="background:#ffffff;padding:36px 40px 28px;">
<p style="margin:0 0 16px 0;font-size:15px;font-weight:500;color:#1a1a2e;">Hi {first_name},</p>
{body_html}

<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:28px 0;">
<tr><td align="center">
<a href="{cta_href}" style="display:inline-block;background:#f97316;color:#ffffff;font-size:13px;font-weight:600;padding:12px 32px;border-radius:8px;text-decoration:none;letter-spacing:0.02em;">&#128222; Call Us to Schedule</a>
</td></tr>
</table>

<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:24px 0;">
<tr><td style="height:1px;background:#e8e8e8;font-size:0;">&nbsp;</td></tr>
</table>

<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f8f9fb;border-radius:8px;padding:18px 20px;">
<tr><td>
<p style="margin:0 0 10px 0;font-size:14px;font-weight:600;color:#1a1a2e;">{business["name"]}</p>
<table cellpadding="0" cellspacing="0" border="0" width="100%">
{phone_block}
{address_block}
{hours_block}
</table>
</td></tr>
</table>
</td></tr>

<tr><td style="background:#f0f0f5;border-radius:0 0 12px 12px;padding:20px 40px;">
<table cellpadding="0" cellspacing="0" border="0" width="100%">
{social_section}
<tr><td align="center" style="font-size:11px;color:#999999;line-height:1.7;">
You are receiving this because you contacted {business["name"]}.<br>
<span style="font-size:10px;color:#bbbbbb;">Sent via FrontdeskReply &middot; AI-powered front desk</span>
</td></tr>
</table>
</td></tr>

</table>
</td></tr>
</table>
</body></html>"""

def send_email(
    to_email: str,
    body: str,
    subject: str = "Reply from FrontdeskReply",
    customer_name: str = "",
    business_id: str = "",
) -> dict:
    business = _get_business_profile(business_id) if business_id else {"name": "Our Team", "phone": "", "address": "", "hours": [], "instagram_url": "", "facebook_url": "", "twitter_url": "", "owner_email": ""}
    html = _build_html(customer_name, body, business)
    payload = {"from": f"{business['name']} <hello@frontdeskreply.com>", "to": [to_email], "subject": subject, "html": html, "text": body}
    if business.get("owner_email"):
        payload["reply_to"] = business["owner_email"]
    try:
        response = httpx.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}, json=payload, timeout=10.0)
        result = response.json()
        logger.info(f"Email sent to {to_email} via Resend: {result}")
        return {"status": "sent", "to": to_email, "method": "resend", "id": result.get("id")}
    except Exception as e:
        logger.error(f"Resend error sending to {to_email}: {e}")
        return {"status": "error", "reason": str(e)}