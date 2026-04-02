"""
Public OTP authentication endpoints.
Drivers authenticate with phone number + 6-digit OTP stored in Redis.
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


class OtpRequest(BaseModel):
    phone: str


class OtpVerify(BaseModel):
    phone: str
    code: str


@router.post("/auth/send-otp")
async def send_otp(req: OtpRequest):
    """Send 6-digit OTP to driver's phone. Stored in Redis with 300s TTL."""
    phone = req.phone.strip().replace(" ", "")
    if len(phone) < 7:
        raise HTTPException(400, "Invalid phone number")

    code = "".join(random.choices(string.digits, k=6))
    otp_data = json.dumps({
        "code":     code,
        "attempts": 0,
        "created":  datetime.now(timezone.utc).isoformat(),
    })
    await redis_state.set(f"otp:{phone}", otp_data, ttl=300)

    operator_name = os.getenv("OPERATOR_NAME", "Your CPO")
    sms_ok = _send_sms(phone, f"{operator_name}: your verification code is {code}. Valid for 5 minutes.")
    if not sms_ok:
        logger.warning("OTP for %s: %s (SMS send failed - code still valid)", phone[-4:], code)
    else:
        logger.info("OTP for %s: sent via SMS", phone[-4:])

    return {"phone": phone, "sent": True}


@router.post("/auth/verify-otp")
async def verify_otp(req: OtpVerify):
    """Verify OTP code and return a session token. State stored in Redis."""
    phone = req.phone.strip().replace(" ", "")
    raw = await redis_state.get(f"otp:{phone}")

    if not raw:
        raise HTTPException(400, "No code found. Request a new one.")

    stored = json.loads(raw)
    stored["attempts"] += 1

    if stored["attempts"] > 5:
        await redis_state.client.delete(f"otp:{phone}")
        raise HTTPException(429, "Too many attempts. Request a new code.")

    if req.code != stored["code"]:
        await redis_state.set(f"otp:{phone}", json.dumps(stored), ttl=300)
        raise HTTPException(400, "Incorrect code")

    await redis_state.client.delete(f"otp:{phone}")
    token = str(uuid4())
    return {"token": token, "phone": phone}
