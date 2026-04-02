"""
Public session management endpoints.
Drivers create, poll, stop, and cancel charging sessions.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from state.postgres import db
from state import charger_registry
from state.redis import redis_state
from api.pricing import get_tier_rate, get_spot_price, get_cost_basis
from utils import send_sms as _send_sms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["public-sessions"])


def _session_row_to_dict(row) -> dict:
    """Convert a public_sessions DB row to a JSON-serialisable dict."""
    d = {}
    for k, v in dict(row).items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif v is None:
            d[k] = None
        else:
            d[k] = v
    return d


class SessionCreate(BaseModel):
    cp_id: str
    connector_id: int
    driver_phone: Optional[str] = None
    driver_email: Optional[str] = None
    pricing_tier: Optional[str] = None  # Override tier; if None, resolved from driver account


async def _get_charger_live(cp_id: str) -> dict | None:
    try:
        return await redis_state.get_charger(cp_id)
    except Exception:
        return None


@router.post("/sessions")
async def create_session(req: SessionCreate):
    """
    Create a charging session.
    1. Inserts row into ocpp.public_sessions
    2. Resolves pricing tier (dynamic pricing if configured, else tariff_kwh fallback)
    3. Returns session_id + initial rate

    Note: Payment integration is handled by the operator's payment plugin.
    The session starts in 'pending' status; payment plugin updates to 'paid'.
    """
    # Check connector availability from Redis before creating session
    live = await _get_charger_live(req.cp_id)
    if live:
        conn_status_key = f"connector_{req.connector_id}_status"
        conn_status = live.get(conn_status_key, "Available")
        if conn_status not in ("Available", "Preparing"):
            raise HTTPException(409, "Connector is busy")

    # Guard: prevent duplicate sessions during charger reboot window (2-minute window)
    async with db.read() as conn:
        recent = await conn.fetchrow("""
            SELECT id::text AS id, payment_status
              FROM ocpp.public_sessions
             WHERE cp_id = $1 AND connector_id = $2
               AND created_at > NOW() - INTERVAL '2 minutes'
               AND payment_status IN ('paid', 'authorized', 'pending')
               AND ocpp_transaction_id IS NULL
               AND stopped_at IS NULL
             ORDER BY created_at DESC
             LIMIT 1
        """, req.cp_id, req.connector_id)

    if recent:
        logger.warning(
            "Duplicate session prevented: reusing %s (status=%s) for %s connector %d",
            recent["id"][:8], recent["payment_status"], req.cp_id, req.connector_id,
        )
        return {
            "session_id": recent["id"],
            "status": "waiting",
            "message": "Charger is starting, please try again in 10 seconds.",
        }

    session_id = str(uuid4())

    # Resolve pricing tier: explicit > driver account > default "public"
    resolved_tier = req.pricing_tier
    if not resolved_tier and req.driver_phone:
        try:
            async with db.read() as conn:
                acct = await conn.fetchrow(
                    "SELECT pricing_tier FROM ocpp.driver_accounts WHERE phone = $1",
                    req.driver_phone,
                )
            if acct and acct["pricing_tier"]:
                resolved_tier = acct["pricing_tier"]
        except Exception:
            pass  # Non-fatal -- fall through to default
    if not resolved_tier:
        resolved_tier = "public"

    # Lock dynamic pricing at session start
    try:
        tier_data = await get_tier_rate(resolved_tier)
        spot_price_at_start = await get_spot_price()
        cost_basis_at_start, _ = await get_cost_basis()
        rate_kwh = tier_data["rate_incl"]  # driver pays incl. tax
        pricing_tier = resolved_tier
    except Exception as exc:
        logger.warning(
            "Dynamic pricing unavailable for tier '%s' (%s), falling back to tariff_kwh",
            resolved_tier, exc
        )
        async with db.read() as conn:
            tariff_row = await conn.fetchrow(
                "SELECT metadata->>'tariff_kwh' AS tariff_kwh FROM ocpp.charge_points WHERE id = $1", req.cp_id
            )
        rate_kwh = float(tariff_row["tariff_kwh"]) if tariff_row and tariff_row["tariff_kwh"] else 0.35
        spot_price_at_start = None
        cost_basis_at_start = None
        pricing_tier = resolved_tier

    async with db.transaction() as conn:
        await conn.execute("""
            INSERT INTO ocpp.public_sessions
                (id, cp_id, connector_id, driver_phone, driver_email,
                 rate_kwh, payment_status, created_at,
                 pricing_tier, spot_price_at_start, cost_basis_at_start, rate_kwh_at_start)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, 'pending', NOW(),
                    $7, $8, $9, $10)
        """, session_id, req.cp_id, req.connector_id,
             req.driver_phone, req.driver_email, rate_kwh,
             pricing_tier, spot_price_at_start, cost_basis_at_start, rate_kwh)

    logger.info(
        "Session %s created for %s connector %d | tier=%s rate=%.4f",
        session_id[:8], req.cp_id, req.connector_id, pricing_tier, rate_kwh,
    )

    # Fire-and-forget SMS with session link
    if req.driver_phone:
        charge_app_url = os.getenv("CHARGE_APP_URL", "http://localhost:8080")
        operator_name = os.getenv("OPERATOR_NAME", "Your CPO")
        sms_msg = (
            f"{operator_name}: your charging session has been created. "
            f"Track it here: {charge_app_url}/session/{session_id}"
        )
        asyncio.get_event_loop().create_task(_send_sms(req.driver_phone, sms_msg))

    return {
        "session_id":    session_id,
        "cp_id":         req.cp_id,
        "connector_id":  req.connector_id,
        "payment_status": "pending",
        "rate_kwh":      rate_kwh,
        "pricing_tier":  pricing_tier,
        "created_at":    datetime.now(timezone.utc).isoformat(),
    }


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: str):
    """Cancel a session that hasn't started yet (started_at IS NULL)."""
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text AS id, started_at, payment_status
              FROM ocpp.public_sessions
             WHERE id = $1::uuid
        """, session_id)

    if not row:
        raise HTTPException(404, "Session not found")
    if row["started_at"]:
        raise HTTPException(409, "Session has already started and cannot be cancelled")

    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET payment_status = 'cancelled', stopped_at = NOW()
             WHERE id = $1::uuid
               AND started_at IS NULL
        """, session_id)

    logger.info("Session %s cancelled", session_id[:8])
    return {"session_id": session_id, "cancelled": True}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Poll session status -- used by live session screen."""
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text AS id, cp_id, connector_id, driver_phone, driver_email,
                   rate_kwh, payment_status,
                   ocpp_transaction_id, kwh_delivered,
                   started_at, stopped_at, created_at
              FROM ocpp.public_sessions
             WHERE id = $1::uuid
        """, session_id)

    if not row:
        raise HTTPException(404, "Session not found")

    result = _session_row_to_dict(row)
    result["remote_start_failed"] = (row["payment_status"] == "remote_start_failed")

    # Enrich with live OCPP data if session has a linked transaction
    if row["ocpp_transaction_id"]:
        async with db.read() as conn:
            ocpp_session = await conn.fetchrow("""
                SELECT id::text as sid FROM ocpp.sessions
                WHERE charge_point = $1 AND transaction_id = $2 AND status = 'active'
            """, row["cp_id"], row["ocpp_transaction_id"])
        if ocpp_session:
            live = await redis_state.get_session(ocpp_session["sid"])
            if live:
                result["current_kw"] = float(live.get("power_kw", 0))
                result["soc_pct"] = float(live.get("soc_pct", 0)) or None
                if live.get("energy_kwh"):
                    result["kwh_delivered"] = float(live["energy_kwh"])

    return result


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    """Request charger to stop a session via RemoteStopTransaction.

    Does NOT update DB -- the charger confirms via StopTransaction,
    which the OCPP handler processes and updates session state.
    """
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text AS id, cp_id, ocpp_transaction_id, stopped_at, payment_status
              FROM ocpp.public_sessions
             WHERE id = $1::uuid
        """, session_id)

    if not row:
        raise HTTPException(404, "Session not found")
    if row["stopped_at"]:
        return {"session_id": session_id, "stopped": True, "already_stopped": True}

    cp_id = row["cp_id"]
    txn_id = row["ocpp_transaction_id"]

    # Paid but charger hasn't started yet -- don't 400, tell client to retry
    if not txn_id and row.get("payment_status") == "paid":
        return {
            "starting": True,
            "message": "Charger is still starting, please retry in 10 seconds",
        }

    if not cp_id or not txn_id:
        raise HTTPException(400, "Session is missing charger or transaction ID")

    msg_id = await charger_registry.send_command(
        cp_id, "RemoteStopTransaction", {"transactionId": int(txn_id)}
    )
    if not msg_id:
        raise HTTPException(502, "Charger not reachable -- please try again")

    logger.info("RemoteStopTransaction sent: cp=%s txn=%s", cp_id, txn_id)
    return {"session_id": session_id, "stop_requested": True, "transaction_id": txn_id}
