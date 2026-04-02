"""
Public OTP authentication endpoints.
Drivers authenticate with phone number + 6-digit OTP stored in Redis.

Demo mode (when SMS provider is "demo" or not configured):
  - send_otp returns {"sent": true, "demo_mode": true}
  - Code "000000" is always accepted alongside the real stored code
  - Client app shows a hint so developers can test without real SMS
"""
import json
import logging
import os
import random
import string
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state.redis import redis_state
from utils import send_sms as _send_sms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["public-auth"])

DEMO_CODE = "000000"


class OtpRequest(BaseModel):
    phone: str


class OtpVerify(BaseModel):
    phone: str
    code: str


async def _is_demo_mode() -> bool:
    """Return True if SMS provider is demo/unconfigured or OTP demo_mode is enabled."""
    from state.settings import get_setting
    sms_cfg = await get_setting("sms")
    otp_cfg = await get_setting("otp")
    if otp_cfg.get("demo_mode", False):
        return True
    provider = sms_cfg.get("provider", "demo")
    return provider in ("demo", "")


async def _get_otp_config() -> dict:
    """Return OTP settings with safe defaults."""
    from state.settings import get_setting
    otp_cfg = await get_setting("otp")
    return {
        "enabled":      otp_cfg.get("enabled", True),
        "code_length":  int(otp_cfg.get("code_length", 6)),
        "ttl_seconds":  int(otp_cfg.get("ttl_seconds", 300)),
    }


@router.post("/auth/send-otp")
async def send_otp(req: OtpRequest):
    """Send OTP code to driver's phone. Stored in Redis with configured TTL."""
    phone = req.phone.strip().replace(" ", "")
    if len(phone) < 7:
        raise HTTPException(400, "Invalid phone number")

    otp_cfg = await _get_otp_config()
    if not otp_cfg["enabled"]:
        raise HTTPException(503, "OTP authentication is disabled")

    code_len = otp_cfg["code_length"]
    ttl      = otp_cfg["ttl_seconds"]
    code     = "".join(random.choices(string.digits, k=code_len))

    otp_data = json.dumps({
        "code":     code,
        "attempts": 0,
        "created":  datetime.now(timezone.utc).isoformat(),
    })
    await redis_state.set(f"otp:{phone}", otp_data, ttl=ttl)

    demo = await _is_demo_mode()
    if demo:
        logger.info("OTP demo mode for %s — real code stored, 000000 also accepted", phone[-4:])
        return {"phone": phone, "sent": True, "demo_mode": True}

    operator_name = os.getenv("OPERATOR_NAME", "Your CPO")
    sms_ok = await _send_sms(phone, f"{operator_name}: your verification code is {code}. Valid for {ttl // 60} minutes.")
    if not sms_ok:
        logger.warning("OTP for %s: SMS failed — code still valid in Redis", phone[-4:])
    else:
        logger.info("OTP for %s: sent via SMS", phone[-4:])

    return {"phone": phone, "sent": True, "demo_mode": False}


@router.post("/auth/verify-otp")
async def verify_otp(req: OtpVerify):
    """Verify OTP code and return a session token."""
    phone = req.phone.strip().replace(" ", "")
    raw   = await redis_state.get(f"otp:{phone}")

    if not raw:
        raise HTTPException(400, "No code found. Request a new one.")

    stored = json.loads(raw)
    stored["attempts"] += 1

    if stored["attempts"] > 5:
        await redis_state.client.delete(f"otp:{phone}")
        raise HTTPException(429, "Too many attempts. Request a new code.")

    demo = await _is_demo_mode()
    code_match = (req.code == stored["code"]) or (demo and req.code == DEMO_CODE)

    if not code_match:
        await redis_state.set(f"otp:{phone}", json.dumps(stored), ttl=300)
        raise HTTPException(400, "Incorrect code")

    await redis_state.client.delete(f"otp:{phone}")
    token = str(uuid4())
    return {"token": token, "phone": phone}
