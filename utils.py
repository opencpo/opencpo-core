"""
Shared utilities — SMS, SMTP, timestamp parsing.

send_sms() and send_email() read provider config from the settings
cache (state.settings) rather than env vars, making them runtime-
configurable via the admin settings page.

Both functions are async. Callers that were previously sync should
use asyncio.get_running_loop().create_task(send_sms(...)) for
fire-and-forget, or await directly where possible.
"""
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── SMS ──────────────────────────────────────────────────────────────────

async def send_sms(to: str, message: str) -> bool:
    """
    Send SMS via the configured provider (Bird, Twilio, or demo).

    Provider config comes from the 'sms' setting in DB (cached 60s).
    Falls back to demo mode (log only) when no provider is configured.
    Returns True on success or in demo mode, False on provider error.
    """
    from state.settings import get_setting
    sms_cfg = await get_setting("sms")
    provider = sms_cfg.get("provider", "demo")

    phone = _normalise_phone(to)

    if provider == "bird":
        return await _send_bird(phone, message, sms_cfg)
    elif provider == "twilio":
        return await _send_twilio(phone, message, sms_cfg)
    else:
        # Demo / not configured — log code for dev/test purposes
        logger.info("SMS demo mode → %s: %s", phone[-4:], message)
        return True


def _normalise_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = ("+31" + phone[1:]) if phone.startswith("0") else ("+" + phone)
    return phone


async def _send_bird(phone: str, message: str, cfg: dict) -> bool:
    api_key   = cfg.get("api_key", "")
    workspace = cfg.get("workspace_id", "")
    channel   = cfg.get("channel_id", "")

    if not all([api_key, workspace, channel]):
        logger.warning("Bird SMS: missing api_key / workspace_id / channel_id — falling back to demo")
        logger.info("SMS demo → %s: %s", phone[-4:], message)
        return True

    url     = f"https://api.bird.com/workspaces/{workspace}/channels/{channel}/messages"
    payload = json.dumps({
        "receiver": {"contacts": [{"identifierKey": "phone", "identifierValue": phone}]},
        "body":     {"type": "text", "text": {"text": message}},
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"AccessKey {api_key}",
        "Content-Type":  "application/json",
    }, method="POST")

    try:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _bird_call, req, phone)
    except Exception as exc:
        logger.error("Bird SMS error to %s: %s", phone[-4:], exc)
        return False


def _bird_call(req: urllib.request.Request, phone: str) -> bool:
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            logger.info("Bird SMS to %s: %s (id=%s)", phone[-4:], data.get("status", "?"), data.get("id", "?"))
            return data.get("status", "") in ("accepted", "delivered", "sent")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        logger.error("Bird SMS HTTP %s to %s: %s", exc.code, phone[-4:], body)
        return False


async def _send_twilio(phone: str, message: str, cfg: dict) -> bool:
    account_sid = cfg.get("workspace_id", "")  # workspace_id reused as account_sid
    auth_token  = cfg.get("channel_id", "")    # channel_id reused as auth_token
    from_number = cfg.get("sender", "")

    if not all([account_sid, auth_token, from_number]):
        logger.warning("Twilio SMS: missing credentials — falling back to demo")
        logger.info("SMS demo → %s: %s", phone[-4:], message)
        return True

    import base64
    url     = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    body    = f"To={urllib.parse.quote(phone)}&From={urllib.parse.quote(from_number)}&Body={urllib.parse.quote(message)}".encode()
    creds   = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req     = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/x-www-form-urlencoded",
    }, method="POST")

    try:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _twilio_call, req, phone)
    except Exception as exc:
        logger.error("Twilio SMS error to %s: %s", phone[-4:], exc)
        return False


def _twilio_call(req: urllib.request.Request, phone: str) -> bool:
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            status = data.get("status", "?")
            logger.info("Twilio SMS to %s: %s (sid=%s)", phone[-4:], status, data.get("sid", "?"))
            return status in ("queued", "sent", "delivered")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        logger.error("Twilio SMS HTTP %s to %s: %s", exc.code, phone[-4:], body)
        return False


# need urllib.parse for Twilio
import urllib.parse  # noqa: E402 — imported at module level for availability


# ── SMTP ─────────────────────────────────────────────────────────────────

async def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    attachment_bytes: bytes | None = None,
    attachment_name: str | None = None,
) -> bool:
    """
    Send email via configured SMTP settings (from DB, cached 60s).
    Runs blocking smtplib in a thread executor.
    Returns True on success, False on failure or not configured.
    """
    from state.settings import get_setting
    smtp_cfg = await get_setting("smtp")

    host   = smtp_cfg.get("host", "")
    user   = smtp_cfg.get("user", "")
    passwd = smtp_cfg.get("password", "")

    if not (host and user and passwd):
        logger.warning("SMTP not configured — email to %s skipped", to_email)
        return False

    import asyncio
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            _smtp_send_blocking,
            to_email, subject, body_text,
            attachment_bytes, attachment_name,
            smtp_cfg,
        )
        logger.info("Email sent to %s: %s", to_email, subject)
        return True
    except Exception as exc:
        logger.error("Email failed to %s: %s", to_email, exc)
        return False


def _smtp_send_blocking(
    to_email: str,
    subject: str,
    body_text: str,
    attachment_bytes: bytes | None,
    attachment_name: str | None,
    smtp_cfg: dict,
) -> None:
    """Blocking SMTP send — call from run_in_executor only."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    host       = smtp_cfg.get("host", "")
    port       = int(smtp_cfg.get("port", 587))
    user       = smtp_cfg.get("user", "")
    passwd     = smtp_cfg.get("password", "")
    from_addr  = smtp_cfg.get("from_address", user)
    from_name  = smtp_cfg.get("from_name", "OpenCPO")
    use_tls    = smtp_cfg.get("tls", True)

    if attachment_bytes:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        part = MIMEApplication(attachment_bytes, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=attachment_name or "attachment.pdf")
        msg.attach(part)
    else:
        msg = MIMEText(body_text, "plain", "utf-8")

    msg["From"]    = f"{from_name} <{from_addr}>"
    msg["To"]      = to_email
    msg["Subject"] = subject

    with smtplib.SMTP(host, port) as smtp:
        if use_tls:
            smtp.starttls()
        smtp.login(user, passwd)
        smtp.send_message(msg)


# ── Timestamps ───────────────────────────────────────────────────────────

def parse_timestamp(ts: "str | datetime | None") -> datetime:
    """Parse an ISO timestamp string to datetime. Handles OCPP format quirks."""
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        return ts
    ts = ts.rstrip("Z").replace("+00:00", "")
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return datetime.now(timezone.utc)
