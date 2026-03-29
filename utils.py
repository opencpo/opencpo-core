"""
Shared utilities.
"""
import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def send_sms(to: str, message: str) -> bool:
    """Send SMS via Bird Channels API. Sender: STRMLIJN."""
    api_key   = os.environ.get("BIRD_API_KEY", "")
    workspace = os.environ.get("BIRD_WORKSPACE_ID", "")
    channel   = os.environ.get("BIRD_SMS_CHANNEL_ID", "")

    if not all([api_key, workspace, channel]):
        logger.warning("SMS not configured — missing BIRD env vars")
        return False

    phone = to.strip()
    if not phone.startswith("+"):
        phone = ("+31" + phone[1:]) if phone.startswith("0") else ("+" + phone)

    url     = f"https://api.bird.com/workspaces/{workspace}/channels/{channel}/messages"
    payload = json.dumps({
        "receiver": {"contacts": [{"identifierKey": "phone", "identifierValue": phone}]},
        "body":     {"type": "text", "text": {"text": message}},
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization":  f"AccessKey {api_key}",
        "Content-Type":   "application/json",
    }, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            logger.info(f"SMS sent to {phone[-4:]}: {data.get('status','?')} (id={data.get('id','?')})")
            return data.get("status", "") in ("accepted", "delivered", "sent")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        logger.error(f"SMS failed to {phone[-4:]}: HTTP {e.code} — {body}")
        return False
    except Exception as e:
        logger.error(f"SMS error to {phone[-4:]}: {e}")
        return False


def parse_timestamp(ts: str | datetime | None) -> datetime:
    """Parse an ISO timestamp string to datetime. Handles OCPP format quirks."""
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        return ts
    # Handle various ISO formats
    ts = ts.rstrip("Z").replace("+00:00", "")
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return datetime.now(timezone.utc)
