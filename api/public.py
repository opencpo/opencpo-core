"""
Public driver-facing API — used by the charge web app.
Endpoints: QR lookup, pricing, OTP auth, session create/poll/stop, receipt PDF.
No admin auth required — these are for anonymous drivers.
"""
import asyncio
import json
import logging
import os
import random
import string
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from state.postgres import db
from state import charger_registry
from state.redis import redis_state
from utils import send_sms as _send_sms
from api.mollie import (
    create_mollie_payment as _create_mollie_payment,
    fetch_mollie_payment as _fetch_mollie_payment,
    cancel_mollie_payment as _cancel_mollie_payment,
    session_row_to_dict as _session_row_to_dict,
    MOLLIE_KEY,
)

logger = logging.getLogger(__name__)

router         = APIRouter(prefix="/api/v1/public", tags=["public"])
webhook_router = APIRouter(tags=["payments"])


async def _get_charger_live(cp_id: str) -> dict | None:
    try:
        return await redis_state.get_charger(cp_id)
    except Exception:
        return None

class OtpRequest(BaseModel):
    phone: str

class OtpVerify(BaseModel):
    phone: str
    code: str

class SessionCreate(BaseModel):
    cp_id: str
    connector_id: int
    driver_phone: Optional[str] = None
    driver_email: Optional[str] = None


@router.get("/qr/{code}/lookup")
async def qr_lookup(code: str):
    """Resolve a QR sticker code to a charger + connector."""
    return {
        "cp_id":       code,
        "connector_id": 1,
        "site_id":     "",
        "evse_id":     "",
    }


# ── OTP Auth ─────────────────────────────────────────────────────────────

@router.post("/auth/send-otp")
async def send_otp(req: OtpRequest):
    """Send 6-digit OTP to driver's phone. Stored in Redis with 300s TTL."""
    phone = req.phone.strip().replace(" ", "")
    if len(phone) < 7:
        raise HTTPException(400, "Ongeldig telefoonnummer")

    code = "".join(random.choices(string.digits, k=6))
    otp_data = json.dumps({
        "code":     code,
        "attempts": 0,
        "created":  datetime.now(timezone.utc).isoformat(),
    })
    await redis_state.set(f"otp:{phone}", otp_data, ttl=300)

    sms_ok = _send_sms(phone, f"{os.getenv('OPERATOR_NAME', 'Your CPO')}: uw verificatiecode is {code}. Geldig voor 5 minuten.")
    if not sms_ok:
        logger.warning("OTP for %s: %s (SMS send failed — code still valid)", phone[-4:], code)
    else:
        logger.info("OTP for %s: sent via SMS", phone[-4:])

    return {"phone": phone, "sent": True}


@router.post("/auth/verify-otp")
async def verify_otp(req: OtpVerify):
    """Verify OTP code and return a session token. State stored in Redis."""
    phone = req.phone.strip().replace(" ", "")
    raw = await redis_state.get(f"otp:{phone}")

    if not raw:
        raise HTTPException(400, "Geen code gevonden. Vraag een nieuwe aan.")

    stored = json.loads(raw)
    stored["attempts"] += 1

    if stored["attempts"] > 5:
        await redis_state.client.delete(f"otp:{phone}")
        raise HTTPException(429, "Te veel pogingen. Vraag een nieuwe code aan.")

    # TTL is enforced by Redis; no need to check age manually
    if req.code != stored["code"]:
        # Write back incremented attempt count
        await redis_state.set(f"otp:{phone}", json.dumps(stored), ttl=300)
        raise HTTPException(400, "Onjuiste code")

    await redis_state.client.delete(f"otp:{phone}")
    token = str(uuid4())
    return {"token": token, "phone": phone}


# ── Sessions ─────────────────────────────────────────────────────────────

@router.post("/sessions")
async def create_session(req: SessionCreate):
    """
    Create a charging session.
    1. Inserts row into ocpp.public_sessions
    2. Creates Mollie iDEAL payment (€25 pre-auth)
    3. Returns session_id + mollie_checkout_url
    """
    # Check connector availability from Redis before creating session
    live = await _get_charger_live(req.cp_id)
    if live:
        conn_status_key = f"connector_{req.connector_id}_status"
        conn_status = live.get(conn_status_key, "Available")
        if conn_status not in ("Available", "Preparing"):
            raise HTTPException(409, "Connector is bezet")

    # Guard: prevent duplicate payments during charger reboot window (2-minute window)
    # If there's already a recent session for this charger+connector that was paid but
    # hasn't started yet, reuse it instead of creating a new Mollie payment.
    async with db.read() as conn:
        recent = await conn.fetchrow("""
            SELECT id::text AS id, mollie_status
              FROM ocpp.public_sessions
             WHERE cp_id = $1 AND connector_id = $2
               AND created_at > NOW() - INTERVAL '2 minutes'
               AND mollie_status IN ('paid', 'authorized', 'pending')
               AND ocpp_transaction_id IS NULL
               AND stopped_at IS NULL
             ORDER BY created_at DESC
             LIMIT 1
        """, req.cp_id, req.connector_id)

    if recent:
        logger.warning(
            "Duplicate session prevented: reusing %s (status=%s) for %s connector %d",
            recent["id"][:8], recent["mollie_status"], req.cp_id, req.connector_id,
        )
        return {
            "session_id": recent["id"],
            "status": "waiting",
            "message": "Lader is aan het opstarten, probeer over 10 seconden opnieuw.",
        }

    session_id = str(uuid4())

    # Fetch tariff from charge_points table; fall back to 0.35
    async with db.read() as conn:
        tariff_row = await conn.fetchrow(
            "SELECT tariff_kwh FROM ocpp.charge_points WHERE id = $1", req.cp_id
        )
    rate_kwh = float(tariff_row["tariff_kwh"]) if tariff_row and tariff_row["tariff_kwh"] else 0.35

    async with db.transaction() as conn:
        await conn.execute("""
            INSERT INTO ocpp.public_sessions
                (id, cp_id, connector_id, driver_phone, driver_email,
                 rate_kwh, mollie_status, created_at)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, 'pending', NOW())
        """, session_id, req.cp_id, req.connector_id,
             req.driver_phone, req.driver_email, rate_kwh)

    # Create Mollie payment (outside transaction — network call)
    try:
        mollie = await _create_mollie_payment(session_id, req.cp_id, req.connector_id)
        payment_id   = mollie["id"]
        checkout_url = mollie["_links"]["checkout"]["href"]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Mollie payment creation failed: {e}")
        raise HTTPException(502, "Betaalprovider niet bereikbaar")

    # Store payment id in DB
    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET mollie_payment_id = $2
             WHERE id = $1::uuid
        """, session_id, payment_id)

    logger.info(
        "Session %s created for %s connector %d | Mollie %s",
        session_id[:8], req.cp_id, req.connector_id, payment_id,
    )

    # Fire-and-forget SMS with session link
    if req.driver_phone:
        sms_msg = (
            f"{os.getenv('OPERATOR_NAME', 'Your CPO')}: je laadsessie is aangemaakt. "
            f"Volg je sessie hier: {os.getenv('CHARGE_APP_URL', 'http://localhost:8080')}/session/{session_id}"
        )
        asyncio.get_event_loop().call_soon(lambda: _send_sms(req.driver_phone, sms_msg))

    return {
        "session_id":          session_id,
        "cp_id":               req.cp_id,
        "connector_id":        req.connector_id,
        "mollie_status":       "pending",
        "mollie_checkout_url": checkout_url,
        "rate_kwh":            rate_kwh,
        "created_at":          datetime.now(timezone.utc).isoformat(),
    }


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: str):
    """Cancel a session that hasn't started yet (started_at IS NULL)."""
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text AS id, started_at, mollie_status
              FROM ocpp.public_sessions
             WHERE id = $1::uuid
        """, session_id)

    if not row:
        raise HTTPException(404, "Sessie niet gevonden")
    if row["started_at"]:
        raise HTTPException(409, "Sessie is al gestart en kan niet worden geannuleerd")

    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET mollie_status = 'cancelled', stopped_at = NOW()
             WHERE id = $1::uuid
               AND started_at IS NULL
        """, session_id)

    logger.info("Session %s cancelled", session_id[:8])
    return {"session_id": session_id, "cancelled": True}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Poll session status — used by live session screen."""
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text AS id, cp_id, connector_id, driver_phone, driver_email,
                   rate_kwh, mollie_status, mollie_payment_id,
                   ocpp_transaction_id, kwh_delivered,
                   started_at, stopped_at, created_at
              FROM ocpp.public_sessions
             WHERE id = $1::uuid
        """, session_id)

    if not row:
        raise HTTPException(404, "Sessie niet gevonden")

    result = _session_row_to_dict(row)
    result["remote_start_failed"] = (row["mollie_status"] == "remote_start_failed")

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

    Does NOT update DB — the charger confirms via StopTransaction,
    which the OCPP handler processes and updates session state.
    """
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text AS id, cp_id, ocpp_transaction_id, stopped_at, mollie_status
              FROM ocpp.public_sessions
             WHERE id = $1::uuid
        """, session_id)

    if not row:
        raise HTTPException(404, "Sessie niet gevonden")
    if row["stopped_at"]:
        return {"session_id": session_id, "stopped": True, "already_stopped": True}

    cp_id = row["cp_id"]
    txn_id = row["ocpp_transaction_id"]

    # Paid but charger hasn't started yet — don't 400, tell client to retry
    if not txn_id and row.get("mollie_status") == "paid":
        return {
            "starting": True,
            "message": "Lader is nog aan het starten, probeer over 10 seconden opnieuw",
        }

    if not cp_id or not txn_id:
        raise HTTPException(400, "Sessie mist lader of transactie-ID")

    msg_id = await charger_registry.send_command(
        cp_id, "RemoteStopTransaction", {"transactionId": int(txn_id)}
    )
    if not msg_id:
        raise HTTPException(502, "Lader niet bereikbaar — probeer opnieuw")

    logger.info("RemoteStopTransaction sent: cp=%s txn=%s", cp_id, txn_id)
    return {"session_id": session_id, "stop_requested": True, "transaction_id": txn_id}


@router.get("/sessions/{session_id}/pdf")
async def session_receipt_pdf(session_id: str):
    """Download VAT-compliant branded PDF receipt for a completed session."""
    from fastapi.responses import Response as FastResponse
    from api.receipt_pdf import generate_receipt_pdf

    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT ps.id::text         AS id,
                   ps.cp_id,
                   ps.connector_id,
                   ps.kwh_delivered,
                   ps.rate_kwh,
                   ps.started_at,
                   ps.stopped_at,
                   ps.mollie_payment_id,
                   cp.display_name,
                   cp.address,
                   cp.city
              FROM ocpp.public_sessions ps
         LEFT JOIN ocpp.charge_points   cp ON cp.id = ps.cp_id
             WHERE ps.id = $1::uuid
        """, session_id)

    if not row:
        raise HTTPException(404, "Sessie niet gevonden")
    if not row["stopped_at"]:
        raise HTTPException(400, "Sessie is nog actief — PDF beschikbaar na afronding")

    # Don't generate receipt for cancelled/empty sessions
    if not row["started_at"] or (row["kwh_delivered"] or 0) <= 0:
        raise HTTPException(400, "Geen laadsessie — er is niet geladen")

    session_data = dict(row)

    try:
        pdf_bytes = generate_receipt_pdf(session_data)
    except Exception as e:
        logger.exception("PDF generation failed for session %s: %s", session_id[:8], e)
        raise HTTPException(500, "PDF kon niet worden gegenereerd")

    filename = f"laadbon-{session_id[:8].upper()}.pdf"
    return FastResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ── Charger Discovery ────────────────────────────────────────────────────

@router.get("/chargers/nearby")
async def chargers_nearby(lat: float = 52.3676, lng: float = 4.9041, radius: float = 50):
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
            # Full tariff breakdown for pricing screen
            "energy_rate": energy_rate,
            "time_rate": 0.00,       # €/min while charging (currently none)
            "idle_rate": 0.05,       # €/min after session, cable still connected
            "flat_fee": 0.00,        # Starttarief (currently none)
        })

    return {"chargers": chargers, "total": len(chargers)}


# ── Mollie Webhook ───────────────────────────────────────────────────────

@webhook_router.post("/api/payments/webhook")
async def mollie_webhook(request: Request):
    """
    Mollie payment webhook.
    Mollie POSTs form data: id=<payment_id>
    Must always return 200 — Mollie retries on any other status.
    """
    # Parse payment_id from form data or JSON
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        payment_id = form.get("id", "")
    else:
        try:
            body = await request.json()
            payment_id = body.get("id", "")
        except Exception:
            body_bytes = await request.body()
            payment_id = body_bytes.decode().strip()

    if not payment_id:
        logger.warning("Mollie webhook: missing payment_id")
        return {"ok": True}

    logger.info(f"Mollie webhook: payment_id={payment_id}")

    # Look up session
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text AS id, cp_id, connector_id, mollie_status
              FROM ocpp.public_sessions
             WHERE mollie_payment_id = $1
        """, payment_id)

    if not row:
        logger.warning(f"Mollie webhook: no session for payment {payment_id}")
        return {"ok": True}

    session_id = row["id"]

    # Fetch actual status from Mollie
    try:
        mollie_data = await _fetch_mollie_payment(payment_id)
    except Exception as e:
        logger.error(f"Mollie fetch failed for {payment_id}: {e}")
        return {"ok": True}

    status = mollie_data.get("status", "")

    # Update DB
    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET mollie_status = $2
             WHERE id = $1::uuid
        """, session_id, status)

    logger.info(f"Session {session_id[:8]} Mollie status → {status}")

    # Trigger RemoteStartTransaction on payment success
    if status in ("paid", "authorized") and row["mollie_status"] not in ("paid", "authorized"):
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
            f"RemoteStartTransaction sent: session={session_id[:8]} cp={cp_id} "
            f"connector={connector_id} id_tag={id_tag}"
        )
        # ocpp_transaction_id will be set by StartTransaction handler
    else:
        # Charger not connected — queue for retry on reconnect (reboot window)
        logger.warning(
            f"RemoteStart queued — charger {cp_id} not connected (session={session_id[:8]}). "
            f"Will retry when charger reconnects."
        )

        # Fetch mollie_payment_id so the retry handler can cancel on exhaustion
        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT mollie_payment_id FROM ocpp.public_sessions WHERE id = $1::uuid",
                session_id,
            )
        mollie_payment_id = row["mollie_payment_id"] if row else None

        await redis_state.set(
            f"pending_start:{cp_id}:{session_id}",
            json.dumps({
                "session_id": session_id,
                "cp_id": cp_id,
                "connector_id": connector_id,
                "id_tag": id_tag,
                "mollie_payment_id": mollie_payment_id,
                "retries": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }),
            ttl=120,
        )


async def _fail_pending_start(data: dict) -> None:
    """Mark a queued RemoteStart as failed and cancel the Mollie payment."""
    session_id = data["session_id"]
    mollie_payment_id = data.get("mollie_payment_id")

    logger.error(
        f"RemoteStart retry exhausted — charger {data['cp_id']} never reconnected "
        f"(session={session_id[:8]}). Marking failed."
    )

    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET mollie_status = 'remote_start_failed'
             WHERE id = $1::uuid
        """, session_id)

    if mollie_payment_id:
        try:
            cancelled = await _cancel_mollie_payment(mollie_payment_id)
            if cancelled:
                logger.info(f"Mollie payment {mollie_payment_id} cancelled for session {session_id[:8]}")
            else:
                logger.warning(f"Mollie payment {mollie_payment_id} could not be cancelled (may already be settled)")
        except Exception as e:
            logger.error(f"Failed to cancel Mollie payment {mollie_payment_id}: {e}")
