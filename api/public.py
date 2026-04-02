"""
Public driver-facing API — core endpoints.
QR lookup, charger discovery, payment webhook, cert identity, and management.
OTP auth: see api/public_auth.py
Session management: see api/public_sessions.py
"""
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from typing import Optional

from state.postgres import db
from state import charger_registry
from state.redis import redis_state

logger = logging.getLogger(__name__)

router         = APIRouter(prefix="/api/v1/public", tags=["public"])
webhook_router = APIRouter(tags=["payments"])


async def _get_charger_live(cp_id: str) -> dict | None:
    try:
        return await redis_state.get_charger(cp_id)
    except Exception:
        return None


# ── QR Lookup ────────────────────────────────────────────────────────────

@router.get("/qr/{code}/lookup")
async def qr_lookup(code: str):
    """Resolve a QR sticker code to a charger + connector."""
    return {
        "cp_id":       code,
        "connector_id": 1,
        "site_id":     "",
        "evse_id":     "",
    }


# ── Charger Discovery ────────────────────────────────────────────────────

@router.get("/chargers/nearby")
async def chargers_nearby(lat: float = 0.0, lng: float = 0.0, radius: float = 50):
    """Return chargers from DB with live status from Redis."""
    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT id, vendor, model, status,
                   display_name, address, city,
                   latitude, longitude, max_power_kw, tariff_kwh
              FROM ocpp.charge_points
             WHERE (access_type = 'public' OR access_type IS NULL)
               AND latitude IS NOT NULL AND longitude IS NOT NULL
               AND simulated = false
             ORDER BY id
        """)

    chargers = []
    for r in rows:
        live = await _get_charger_live(r["id"])
        connectors = []
        if live:
            for key, val in live.items():
                if key.startswith("connector_") and key.endswith("_status"):
                    conn_id = int(key.split("_")[1])
                    if conn_id > 0:
                        connectors.append({"id": conn_id, "type": "CCS2", "status": val})

        energy_rate = float(r["tariff_kwh"]) if r["tariff_kwh"] else 0.35
        chargers.append({
            "id": r["id"],
            "name": r["display_name"] or r["id"],
            "address": r["address"] or "",
            "city": r["city"] or "",
            "lat": float(r["latitude"]),
            "lng": float(r["longitude"]),
            "power_kw": r["max_power_kw"] or 0,
            "connectors": connectors,
            "status": live.get("status", "offline") if live else "offline",
            "operator": os.getenv("OPERATOR_NAME", "Your CPO"),
            "tariff_kwh": energy_rate,
            "energy_rate": energy_rate,
            "time_rate": 0.00,
            "idle_rate": 0.05,
            "flat_fee": 0.00,
        })

    return {"chargers": chargers, "total": len(chargers)}


# ── Payment Webhook (generic) ────────────────────────────────────────────
# Operators plug in their own payment provider. This stub handles the common
# flow: update payment_status, trigger RemoteStartTransaction on success.

@webhook_router.post("/api/payments/webhook")
async def payment_webhook(request: Request):
    """
    Generic payment webhook handler.

    Expects JSON body: {"session_id": "<uuid>", "status": "paid"|"cancelled"|...}
    Or form data: id=<payment_id> (legacy format — looks up by payment_id).

    Payment provider adapters should call this after translating their native format.
    Must always return 200 — providers retry on any other status.
    """
    content_type = request.headers.get("content-type", "")

    if "application/x-www-form-urlencoded" in content_type:
        # Legacy form data: look up by external payment ID
        form = await request.form()
        payment_id = form.get("id", "")
        if not payment_id:
            logger.warning("Payment webhook: missing payment id in form data")
            return {"ok": True}
        async with db.read() as conn:
            row = await conn.fetchrow("""
                SELECT id::text AS id, cp_id, connector_id, payment_status
                  FROM ocpp.public_sessions
                 WHERE external_payment_id = $1
            """, payment_id)
        if not row:
            logger.warning("Payment webhook: no session for payment_id=%s", payment_id)
            return {"ok": True}
        session_id = row["id"]
        logger.info("Payment webhook: payment_id=%s session=%s (no adapter configured)", payment_id, session_id[:8])
        return {"ok": True}

    else:
        try:
            body = await request.json()
        except Exception:
            return {"ok": True}

        session_id = body.get("session_id", "")
        status = body.get("status", "")

        if not session_id or not status:
            logger.warning("Payment webhook: missing session_id or status in body")
            return {"ok": True}

    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text AS id, cp_id, connector_id, payment_status
              FROM ocpp.public_sessions
             WHERE id = $1::uuid
        """, session_id)

    if not row:
        logger.warning("Payment webhook: session %s not found", session_id)
        return {"ok": True}

    prev_status = row["payment_status"]

    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET payment_status = $2
             WHERE id = $1::uuid
        """, session_id, status)

    logger.info("Session %s payment_status → %s", session_id[:8], status)

    # Trigger RemoteStartTransaction on payment success
    if status in ("paid", "authorized") and prev_status not in ("paid", "authorized"):
        await _trigger_remote_start(session_id, row["cp_id"], row["connector_id"])

    return {"ok": True}


async def _trigger_remote_start(session_id: str, cp_id: str, connector_id: int) -> None:
    """Send RemoteStartTransaction to the charger after successful payment.

    If the charger is not currently connected (reboot window), the command is
    queued in Redis (TTL 120s). It will be retried when the charger reconnects.
    """
    id_tag = f"APP_{session_id[:8].upper()}"
    msg_id = await charger_registry.send_remote_start(cp_id, connector_id, id_tag)

    if msg_id:
        logger.info(
            "RemoteStartTransaction sent: session=%s cp=%s connector=%d id_tag=%s",
            session_id[:8], cp_id, connector_id, id_tag,
        )
    else:
        logger.warning(
            "RemoteStart queued — charger %s not connected (session=%s). "
            "Will retry when charger reconnects.",
            cp_id, session_id[:8],
        )

        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT external_payment_id FROM ocpp.public_sessions WHERE id = $1::uuid",
                session_id,
            )
        external_payment_id = row["external_payment_id"] if row else None

        await redis_state.set(
            f"pending_start:{cp_id}:{session_id}",
            json.dumps({
                "session_id": session_id,
                "cp_id": cp_id,
                "connector_id": connector_id,
                "id_tag": id_tag,
                "external_payment_id": external_payment_id,
                "retries": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }),
            ttl=120,
        )


# ── Management endpoints for public sessions (API key required) ──────────

mgmt_public_router = APIRouter(tags=["Public Sessions (Management)"])


@mgmt_public_router.get("/receipts")
async def list_receipt_sessions(limit: int = 100, offset: int = 0):
    """List completed public sessions eligible for receipts."""
    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT ps.id::text AS id, ps.cp_id, ps.connector_id,
                   ps.driver_phone, ps.driver_email,
                   ps.kwh_delivered, ps.rate_kwh, ps.started_at, ps.stopped_at,
                   ps.payment_status, ps.pricing_tier,
                   cp.display_name, cp.address, cp.city
              FROM ocpp.public_sessions ps
              LEFT JOIN ocpp.charge_points cp ON cp.id = ps.cp_id
             WHERE ps.stopped_at IS NOT NULL AND ps.kwh_delivered > 0
             ORDER BY ps.stopped_at DESC
             LIMIT $1 OFFSET $2
        """, limit, offset)
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM ocpp.public_sessions WHERE stopped_at IS NOT NULL AND kwh_delivered > 0"
        )
    return {
        "sessions": [
            {
                "id": r["id"],
                "cp_id": r["cp_id"],
                "charger_name": r["display_name"] or r["cp_id"],
                "address": r["address"] or "",
                "city": r["city"] or "",
                "driver_phone": r["driver_phone"] or "",
                "driver_email": r["driver_email"] or "",
                "kwh": round(float(r["kwh_delivered"]), 2),
                "rate_kwh": round(float(r["rate_kwh"] or 0), 4),
                "cost_incl": round(float(r["kwh_delivered"]) * float(r["rate_kwh"] or 0) * 1.21, 2),
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "stopped_at": r["stopped_at"].isoformat() if r["stopped_at"] else None,
                "payment_status": r["payment_status"],
                "pricing_tier": r["pricing_tier"] or "public",
            }
            for r in rows
        ],
        "total": count,
    }


# ── Certificate Identity ─────────────────────────────────────────────────

@router.get("/cert/identify")
async def cert_identify(serial: str):
    """
    Resolve a client cert serial to a driver account.

    Returns the driver's account info + pricing tier if the cert is valid
    and linked to a driver_account. Returns 404 if cert is unknown, revoked,
    or not linked to any account.
    """
    if not serial or not serial.strip():
        raise HTTPException(status_code=400, detail="Missing serial")

    serial = serial.strip()

    # Caddy sends serial as decimal integer, we store as lowercase hex
    if serial.isdigit():
        serial = hex(int(serial))[2:].lower()
    else:
        serial = serial.lower()

    async with db.read() as conn:
        cert_row = await conn.fetchrow("""
            SELECT serial, subject, not_after
            FROM ocpp.pki_certificates
            WHERE LOWER(serial) = $1
              AND type = 'user'
              AND status = 'active'
              AND revoked_at IS NULL
        """, serial)

        if not cert_row:
            raise HTTPException(status_code=404, detail="Unknown or revoked certificate")

        # Extract email from CN= in subject DN
        subject = cert_row["subject"]
        cert_email = None
        for part in subject.split(","):
            part = part.strip()
            if part.upper().startswith("CN="):
                cert_email = part[3:]
                break

        if not cert_email:
            raise HTTPException(status_code=404, detail="Certificate has no CN email")

        row = await conn.fetchrow("""
            SELECT id, email, name, pricing_tier, group_id, phone, language
            FROM ocpp.driver_accounts
            WHERE email = $1
        """, cert_email)

    if not row:
        raise HTTPException(status_code=404, detail="Certificate not linked to a driver account")

    return {
        "id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "phone": row["phone"],
        "language": row["language"],
        "pricing_tier": row["pricing_tier"],
        "group_id": str(row["group_id"]) if row["group_id"] else None,
        "cert_serial": cert_row["serial"],
        "cert_expires_at": cert_row["not_after"].isoformat() if cert_row["not_after"] else None,
    }
