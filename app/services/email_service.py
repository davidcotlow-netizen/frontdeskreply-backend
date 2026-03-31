"""
Email Service — FrontdeskReply
Sends reply emails to customers via Resend (hello@frontdeskreply.com).
Business details (name, phone, address, hours) are pulled from Supabase.
"""

import logging
import resend

from app.core.config import get_settings
from app.core.database import get_db

logger = logging.getLogger(__name__)


def _get_business_profile(business_id: str) -> dict:
    """
    Fetch business profile from Supabase for template population.
    Returns a dict with name, phone, address, hours, and social links.
    Falls back to safe defaults if anything is missing.
    """
    defaults = {
        "name": "Our Team",
        "phone": "",
        "address": "",
        "hours": [],
        "instagram_url": "",
        "facebook_url": "",
        "twitter_url": "",
        "owner_email": "",
    }

    try:
        supabase = get_db()
        result = supabase.table("businesses").select("*").eq("id", business_id).maybe_single().execute()
        if result and result.data:
            data = result.data
            return {
                "name": data.get("name") or defaults["name"],
                "phone": _format_phone(data.get("phone") or "") if data.get("phone") else "",
                "address": data.get("address") or "",
                "hours": data.get("business_hours") or [],
                "instagram_url": data.get("instagram_url") or "",
                "facebook_url": data.get("facebook_url") or "",
                "twitter_url": data.get("twitter_url") or "",
                "owner_email": data.get("owner_email") or "",
            }
    except Exception as e:
        logger.warning(f"Could not fetch business profile for {business_id}: {e}")

    return defaults


def _format_phone(phone: str) -> str:
    """Format a raw phone number into +1 (XXX) XXX-XXXX for display."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone


def _format_hours_rows(hours: list) -> str:
    """
    Convert business_hours list to HTML table rows.
    Expects list of dicts: [{"day": "Mon – Fri", "open": "8:00 am", "close": "6:00 pm"}, ...]
    """
    if not hours:
        return ""

    rows = ""
    for entry in hours:
        day = entry.get("day", "")
        open_t = entry.get("open", "")
        close_t = entry.get("close", "")
        value = f"{open_t} – {close_t}" if open_t and close_t else "Closed"
        rows += f"""
        <tr>
          <td style="font-weight:500;color:#1a1a2e;font-size:12px;padding:3px 16px 3px 0;white-space:nowrap;">{day}</td>
          <td style="font-size:12px;color:#555;padding:3px 0;">{value}</td>
        </tr>"""
    return rows


def _social_icon_block(instagram_url: str, facebook_url: str, twitter_url: str) -> str:
    """Render social icon links only for URLs that are actually set."""
    icons = []

    if instagram_url:
        icons.append(f"""
        <a href="{instagram_url}" style="display:inline-block;width:32px;height:32px;border-radius:50%;background:#ffffff;border:0.5px solid #d0d0d8;text-align:center;line-height:32px;text-decoration:none;margin:0 4px;">
          <img src="https://cdn.jsdelivr.net/npm/simple-icons@9/icons/instagram.svg" width="14" height="14" style="vertical-align:middle;filter:invert(40%);" alt="Instagram">
        </a>""")

    if facebook_url:
        icons.append(f"""
        <a href="{facebook_url}" style="display:inline-block;width:32px;height:32px;border-radius:50%;background:#ffffff;border:0.5px solid #d0d0d8;text-align:center;line-height:32px;text-decoration:none;margin:0 4px;">
          <img src="https://cdn.jsdelivr.net/npm/simple-icons@9/icons/facebook.svg" width="14" height="14" style="vertical-align:middle;filter:invert(40%);" alt="Facebook">
        </a>""")

    if twitter_url:
        icons.append(f"""
        <a href="{twitter_url}" style="display:inline-block;width:32px;height:32px;border-radius:50%;background:#ffffff;border:0.5px solid #d0d0d8;text-align:center;line-height:32px;text-decoration:none;margin:0 4px;">
          <img src="https://cdn.jsdelivr.net/npm/simple-icons@9/icons/x.svg" width="14" height="14" style="vertical-align:middle;filter:invert(40%);" alt="X / Twitter">
        </a>""")

    return "".join(icons)


def _build_html_email(customer_name: str, body: str, business: dict) -> str:
    """Render the full branded HTML email template."""
    first_name = customer_name.split()[0] if customer_name else "there"
    hours_rows = _format_hours_rows(business["hours"])
    social_icons = _social_icon_block(
        business["instagram_url"],
        business["facebook_url"],
        business["twitter_url"],
    )

    hours_block = ""
    if hours_rows:
        hours_block = f"""
        <tr>
          <td style="padding:6px 0 0 0;">
            <table cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="padding-right:8px;vertical-align:top;padding-top:1px;">
                  <img src="https://cdn.jsdelivr.net/npm/heroicons@2.0.18/24/outline/clock.svg"
                       width="14" height="14" style="display:block;filter:invert(40%) sepia(80%) saturate(400%) hue-rotate(190deg);" alt="">
                </td>
                <td>
                  <div style="font-weight:500;color:#333;font-size:12px;margin-bottom:4px;">Business hours</div>
                  <table cellpadding="0" cellspacing="0" border="0">
                    {hours_rows}
                  </table>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    phone_block = ""
    if business["phone"]:
        phone_block = f"""
        <tr>
          <td style="padding:4px 0;">
            <table cellpadding="0" cellspacing="0" border="0"><tr>
              <td style="padding-right:8px;vertical-align:middle;">
                <img src="https://cdn.jsdelivr.net/npm/heroicons@2.0.18/24/outline/phone.svg"
                     width="14" height="14" style="display:block;filter:invert(40%) sepia(80%) saturate(400%) hue-rotate(190deg);" alt="">
              </td>
              <td style="font-size:13px;color:#555;">{business["phone"]}</td>
            </tr></table>
          </td>
        </tr>"""

    address_block = ""
    if business["address"]:
        address_block = f"""
        <tr>
          <td style="padding:4px 0;">
            <table cellpadding="0" cellspacing="0" border="0"><tr>
              <td style="padding-right:8px;vertical-align:middle;">
                <img src="https://cdn.jsdelivr.net/npm/heroicons@2.0.18/24/outline/map-pin.svg"
                     width="14" height="14" style="display:block;filter:invert(40%) sepia(80%) saturate(400%) hue-rotate(190deg);" alt="">
              </td>
              <td style="font-size:13px;color:#555;">{business["address"]}</td>
            </tr></table>
          </td>
        </tr>"""

    social_section = ""
    if social_icons:
        social_section = f"""
        <tr>
          <td align="center" style="padding:0 0 12px 0;">
            {social_icons}
          </td>
        </tr>"""

    body_html = "".join(
        f'<p style="margin:0 0 14px 0;font-size:14px;color:#444444;line-height:1.75;">{line}</p>'
        for line in body.strip().split("\n")
        if line.strip()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Reply from {business["name"]}</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f4f4f7;padding:32px 16px;">
    <tr>
      <td align="center">
        <table cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%;">

          <!-- HEADER -->
          <tr>
            <td style="background:#1a1a2e;border-radius:12px 12px 0 0;padding:28px 40px 22px;text-align:center;">
              <table cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;">
                <tr>
                  <td style="padding-right:10px;vertical-align:middle;">
                    <div style="width:32px;height:32px;background:#4f8ef7;border-radius:6px;text-align:center;line-height:32px;">
                      <img src="https://cdn.jsdelivr.net/npm/heroicons@2.0.18/24/outline/envelope.svg"
                           width="18" height="18"
                           style="display:inline-block;vertical-align:middle;filter:invert(100%);" alt="">
                    </div>
                  </td>
                  <td style="vertical-align:middle;text-align:left;">
                    <div style="color:#ffffff;font-size:18px;font-weight:500;letter-spacing:0.01em;">{business["name"]}</div>
                    <div style="color:rgba(255,255,255,0.4);font-size:10px;letter-spacing:0.08em;text-transform:uppercase;margin-top:2px;">Powered by FrontdeskReply</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- ACCENT BAR -->
          <tr>
            <td style="height:3px;background:linear-gradient(90deg,#4f8ef7 0%,#7c5cbf 100%);font-size:0;line-height:0;">&nbsp;</td>
          </tr>

          <!-- BODY -->
          <tr>
            <td style="background:#ffffff;padding:36px 40px 28px;">
              <p style="margin:0 0 16px 0;font-size:15px;font-weight:500;color:#1a1a2e;">Hi {first_name},</p>
              {body_html}

              <!-- CTA BUTTON -->
              <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:28px 0;">
                <tr>
                  <td align="center">
                    <a href="tel:{business['phone']}"
                       style="display:inline-block;background:#1a1a2e;color:#ffffff;font-size:13px;font-weight:500;padding:11px 28px;border-radius:6px;text-decoration:none;letter-spacing:0.02em;">
                      Call Us to Schedule
                    </a>
                  </td>
                </tr>
              </table>

              <!-- DIVIDER -->
              <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:24px 0;">
                <tr><td style="height:0.5px;background:#e8e8e8;font-size:0;line-height:0;">&nbsp;</td></tr>
              </table>

              <!-- BUSINESS CARD -->
              <table cellpadding="0" cellspacing="0" border="0" width="100%"
                     style="background:#f8f9fb;border-radius:8px;padding:18px 20px;">
                <tr>
                  <td>
                    <p style="margin:0 0 10px 0;font-size:14px;font-weight:500;color:#1a1a2e;">{business["name"]}</p>
                    <table cellpadding="0" cellspacing="0" border="0" width="100%">
                      {phone_block}
                      {address_block}
                      {hours_block}
                    </table>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- FOOTER -->
          <tr>
            <td style="background:#f0f0f5;border-radius:0 0 12px 12px;padding:20px 40px;">
              <table cellpadding="0" cellspacing="0" border="0" width="100%">
                {social_section}
                <tr>
                  <td align="center" style="font-size:11px;color:#999999;line-height:1.7;padding-bottom:8px;">
                    You're receiving this because you contacted {business["name"]}.<br>
                    <a href="#" style="color:#777777;text-decoration:underline;">Unsubscribe</a>
                    &nbsp;·&nbsp;
                    <a href="#" style="color:#777777;text-decoration:underline;">Privacy Policy</a>
                  </td>
                </tr>
                <tr>
                  <td align="center" style="font-size:10px;color:#bbbbbb;letter-spacing:0.04em;padding-top:2px;">
                    Sent via FrontdeskReply · AI-powered front desk
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>"""


def send_email(
    to_email: str,
    body: str,
    subject: str = "Reply from FrontdeskReply",
    customer_name: str = "",
    business_id: str = "",
) -> dict:
    """
    Send a branded HTML reply email via Resend (hello@frontdeskreply.com).

    Args:
        to_email:      Recipient email address.
        body:          Plain-text AI draft body (rendered into HTML template).
        subject:       Email subject line.
        customer_name: Customer's name for the greeting (e.g. "Barbara Thomas").
        business_id:   Supabase business UUID — used to pull branding & contact info.

    Returns:
        Dict with status and details.
    """
    settings = get_settings()

    if not settings.resend_api_key:
        logger.error("RESEND_API_KEY is not set in environment variables")
        return {"status": "error", "reason": "resend_not_configured"}

    try:
        resend.api_key = settings.resend_api_key

        business = _get_business_profile(business_id) if business_id else {
            "name": "Our Team",
            "phone": "",
            "address": "",
            "hours": [],
            "instagram_url": "",
            "facebook_url": "",
            "twitter_url": "",
            "owner_email": "",
        }

        html_body = _build_html_email(customer_name, body, business)

        params: resend.Emails.SendParams = {
            "from": f"{business['name']} <hello@frontdeskreply.com>",
            "to": [to_email],
            "subject": subject,
            "html": html_body,
            "text": body,
        }
        # Route customer replies to the business owner's email directly.
        # If they reply to this email, it goes to the owner — not a dead inbox.
        if business.get("owner_email"):
            params["reply_to"] = business["owner_email"]

        result = resend.Emails.send(params)
        logger.info(f"Email sent to {to_email} via Resend — id: {result.get('id')}")
        return {"status": "sent", "to": to_email, "method": "resend", "id": result.get("id")}

    except Exception as e:
        logger.error(f"Resend error sending to {to_email}: {e}")
        return {"status": "error", "reason": str(e)}