"""
Push notification API — VAPID-based Web Push for charge sessions.
Endpoints: subscribe, unsubscribe, public key.
Push is triggered by session.py on StopTransaction.
"""
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public/push", tags=["push"])

# ── VAPID config ──────────────────────────────────────────────────────────

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL = os.getenv("VAPID_EMAIL", "mailto:info@example.com")

if not VAPID_PRIVATE_KEY:
    logger.warning(
        "VAPID_PRIVATE_KEY not set — push notifications disabled. "
        "Generate keys with: python -c \"from py_vapid import Vapid; v=Vapid(); v.generate_keys(); "
        "print('Private:', v.private_pem().decode()); print('Public:', v.public_key)\""
    )


# ── DB init ───────────────────────────────────────────────────────────────

async def ensure_table() -> None:
    """Create push_subscriptions table if not exists."""
    async with db.write() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ocpp.push_subscriptions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                session_id UUID NOT NULL,
                subscription JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS push_subs_session_idx
            ON ocpp.push_subscriptions (session_id)
        """)


# ── Models ────────────────────────────────────────────────────────────────

class PushSubscribeRequest(BaseModel):
    session_id: str
    subscription: dict  # {endpoint, keys: {p256dh, auth}}


class PushUnsubscribeRequest(BaseModel):
    session_id: str


# ── Routes ────────────────────────────────────────────────────────────────

@router.get("/key")
async def get_vapid_key():
    """Return VAPID public key for client-side push subscription."""
    return {"publicKey": VAPID_PUBLIC_KEY}


@router.post("/subscribe")
async def subscribe(body: PushSubscribeRequest):
    """Store a push subscription for a session."""
    await ensure_table()
    async with db.write() as conn:
        # Remove any existing subscription for this session first
        await conn.execute(
            "DELETE FROM ocpp.push_subscriptions WHERE session_id = $1::uuid",
            body.session_id,
        )
        await conn.execute(
            """
            INSERT INTO ocpp.push_subscriptions (session_id, subscription)
            VALUES ($1::uuid, $2::jsonb)
            """,
            body.session_id,
            json.dumps(body.subscription),
        )
    logger.info(f"Push subscription stored for session {body.session_id}")
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(body: PushUnsubscribeRequest):
    """Remove push subscription for a session."""
    await ensure_table()
    async with db.write() as conn:
        await conn.execute(
            "DELETE FROM ocpp.push_subscriptions WHERE session_id = $1::uuid",
            body.session_id,
        )
    logger.info(f"Push subscription removed for session {body.session_id}")
    return {"ok": True}


# ── Send helpers ─────────────────────────────────────────────────────────

def send_push(subscription_info: dict, title: str, body: str, url: Optional[str] = None) -> None:
    """Send a Web Push notification. Runs synchronously — call from asyncio via run_in_executor."""
    if not VAPID_PRIVATE_KEY:
        logger.debug("Push skipped — VAPID_PRIVATE_KEY not configured")
        return
    try:
        from pywebpush import webpush, WebPushException

        payload = {"title": title, "body": body}
        if url:
            payload["url"] = url

        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_EMAIL},
        )
        logger.info(f"Push sent: {title}")
    except Exception as e:
        logger.warning(f"Push failed: {e}")


async def send_push_for_session(session_id: str, title: str, body: str, url: Optional[str] = None) -> bool:
    """Look up subscription for session and send push. Returns True if sent."""
    try:
        await ensure_table()
        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT subscription FROM ocpp.push_subscriptions WHERE session_id = $1::uuid LIMIT 1",
                session_id,
            )
        if not row:
            logger.debug(f"No push subscription for session {session_id}")
            return False

        subscription_info = json.loads(row["subscription"]) if isinstance(row["subscription"], str) else dict(row["subscription"])

        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_push, subscription_info, title, body, url)
        return True
    except Exception as e:
        logger.warning(f"send_push_for_session failed: {e}")
        return False
