"""
Settings API — runtime configuration for SMS, SMTP, and OTP.

All endpoints require management auth (MANAGEMENT_API_KEY).
Secret fields (api_key, password, etc.) are masked to "****" in
GET responses. PUT merges: sending "****" for a secret field
preserves the existing value rather than overwriting it.

Endpoints:
    GET  /api/v1/settings              → all settings (secrets masked)
    GET  /api/v1/settings/{key}        → single setting (secrets masked)
    PUT  /api/v1/settings/{key}        → update setting
    POST /api/v1/settings/sms/test     → send test SMS
    POST /api/v1/settings/smtp/test    → send test email
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state.settings import get_setting, get_all_settings, put_setting, mask_secrets, _SECRET_FIELDS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])

# Valid setting keys and their defaults
_DEFAULTS: dict[str, dict] = {
    "sms": {
        "provider":     "demo",
        "api_key":      "",
        "workspace_id": "",
        "channel_id":   "",
        "sender":       "",
    },
    "smtp": {
        "host":         "",
        "port":         587,
        "user":         "",
        "password":     "",
        "from_address": "",
        "from_name":    "OpenCPO",
        "tls":          True,
    },
    "otp": {
        "enabled":      True,
        "demo_mode":    False,
        "code_length":  6,
        "ttl_seconds":  300,
    },
    "ocpi": {
        "country_code":       "NL",
        "party_id":           "OCP",
        "role":               "CPO",
        "operator_name":      "OpenCPO",
        "emsp_country_code":  "NL",
        "emsp_party_id":      "OCP",
        "base_url":           "http://localhost:8000",
        "versions_path":      "/ocpi/versions",
    },
}

VALID_KEYS = set(_DEFAULTS.keys())


class SettingUpdate(BaseModel):
    value: dict


class TestSmsRequest(BaseModel):
    phone: str


class TestSmtpRequest(BaseModel):
    to_email: str


# ── Helpers ───────────────────────────────────────────────────────────────

def _merge_update(existing: dict, incoming: dict, key: str) -> dict:
    """
    Merge incoming dict into existing, preserving secret fields when the
    incoming value is "****" (meaning: don't change this field).
    Unknown fields for the key are dropped to keep settings tidy.
    """
    template = _DEFAULTS.get(key, {})
    merged = dict(existing)
    for field, default in template.items():
        if field in incoming:
            new_val = incoming[field]
            if field in _SECRET_FIELDS and new_val == "****":
                # Preserve existing secret
                pass
            else:
                merged[field] = new_val
        elif field not in merged:
            merged[field] = default
    return merged


async def _get_with_defaults(key: str) -> dict:
    """Get setting merged over defaults so all fields are always present."""
    stored = await get_setting(key)
    result = dict(_DEFAULTS.get(key, {}))
    result.update(stored)
    return result


# ── Read endpoints ────────────────────────────────────────────────────────

@router.get("")
async def list_settings():
    """Return all settings with secrets masked."""
    all_s = await get_all_settings()
    # Fill in defaults for any key not yet in DB
    for key, defaults in _DEFAULTS.items():
        if key not in all_s:
            all_s[key] = dict(defaults)
        else:
            merged = dict(defaults)
            merged.update(all_s[key])
            all_s[key] = merged

    return {key: mask_secrets(val) for key, val in all_s.items()}


@router.get("/{key}")
async def get_one_setting(key: str):
    """Return single setting with secrets masked."""
    if key not in VALID_KEYS:
        raise HTTPException(404, f"Unknown setting key: {key}")
    val = await _get_with_defaults(key)
    return {"key": key, "value": mask_secrets(val)}


# ── Write endpoints ───────────────────────────────────────────────────────

@router.put("/{key}")
async def update_setting(key: str, body: SettingUpdate):
    """Update a setting. Secret fields sent as '****' are preserved unchanged."""
    if key not in VALID_KEYS:
        raise HTTPException(404, f"Unknown setting key: {key}")

    existing = await _get_with_defaults(key)
    merged   = _merge_update(existing, body.value, key)
    await put_setting(key, merged)

    return {"ok": True, "key": key, "value": mask_secrets(merged)}


# ── Test endpoints ────────────────────────────────────────────────────────

@router.post("/sms/test")
async def test_sms(body: TestSmsRequest):
    """Send a test SMS to verify SMS configuration."""
    from utils import send_sms
    phone = body.phone.strip()
    if not phone:
        raise HTTPException(400, "phone is required")

    ok = await send_sms(phone, "OpenCPO settings test: SMS is working correctly.")
    if not ok:
        raise HTTPException(502, "SMS send failed — check provider configuration")
    return {"ok": True, "sent_to": phone}


@router.post("/smtp/test")
async def test_smtp(body: TestSmtpRequest):
    """Send a test email to verify SMTP configuration."""
    from utils import send_email
    to = body.to_email.strip()
    if not to:
        raise HTTPException(400, "to_email is required")

    ok = await send_email(
        to_email=to,
        subject="OpenCPO SMTP test",
        body_text="This is a test email from OpenCPO settings.\n\nIf you received this, your SMTP configuration is working correctly.",
    )
    if not ok:
        raise HTTPException(502, "Email send failed — check SMTP configuration or logs")
    return {"ok": True, "sent_to": to}
