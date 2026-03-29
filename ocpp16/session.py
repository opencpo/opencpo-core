"""
OCPP 1.6 session lifecycle — StartTransaction, StopTransaction, CDR generation.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

from events.bus import EventBus
from events.types import Event, EventType
from state.postgres import db
from state.redis import redis_state
from ocpp16.protocol import AuthorizationStatus
from ocpp16.meter import session_energy
from utils import parse_timestamp
from api.push import send_push_for_session

logger = logging.getLogger(__name__)


async def handle_start_transaction(
    cp_id: str,
    event_bus: EventBus,
    simulated: bool,
    payload: dict,
    billing_context: dict | None = None,
) -> dict:
    """Charging session started."""
    connector_id = payload.get("connectorId", 1)
    id_tag = payload.get("idTag", "")
    meter_start = payload.get("meterStart", 0)
    timestamp = parse_timestamp(payload.get("timestamp"))

    billing_type = (billing_context or {}).get("type", "prepaid")
    billing_group_id = (billing_context or {}).get("group_id")

    # Create session in DB
    async with db.transaction() as conn:
        session = await conn.fetchrow("""
            INSERT INTO ocpp.sessions 
                (charge_point, connector_id, auth_method, auth_id, start_time, simulated, meter_start, billing_type, billing_group_id)
            VALUES ($1, $2, 'rfid', $3, $4, $5, $6, $7, $8)
            RETURNING id, id::text as session_id
        """, cp_id, connector_id, id_tag, timestamp, simulated, float(meter_start), billing_type, billing_group_id)

        # Generate a transaction ID (OCPP 1.6 requires integer)
        transaction_id = await conn.fetchval(
            "UPDATE ocpp.sessions SET transaction_id = (SELECT COALESCE(MAX(transaction_id), 0) + 1 FROM ocpp.sessions) WHERE id = $1 RETURNING transaction_id",
            session["id"],
        )

    session_id = session["session_id"]

    # Update Redis
    await redis_state.set_session(session_id, {
        "charge_point": cp_id,
        "connector_id": str(connector_id),
        "auth_id": id_tag,
        "transaction_id": str(transaction_id),
        "start_time": timestamp,
        "meter_start": str(meter_start),
        "energy_kwh": "0",
        "power_kw": "0",
        "status": "active",
    })

    # Publish event
    await event_bus.publish(Event(
        type=EventType.SESSION_START,
        charge_point=cp_id,
        connector=connector_id,
        session_id=session_id,
        simulated=simulated,
        data={
            "transaction_id": transaction_id,
            "id_tag": id_tag,
            "meter_start": meter_start,
        },
    ))

    logger.info(f"[{cp_id}] Session started: txn={transaction_id} tag={id_tag} connector={connector_id}")

    # Link to public session if APP_ tag (charge app payment flow)
    if id_tag.startswith("APP_"):
        public_session_prefix = id_tag[4:].lower()  # APP_88FBC7B6 → 88fbc7b6
        async with db.write() as conn:
            await conn.execute("""
                UPDATE ocpp.public_sessions SET
                    ocpp_transaction_id = $1, started_at = NOW()
                WHERE LEFT(id::text, 8) = $2
                  AND ocpp_transaction_id IS NULL
                  AND started_at IS NULL
                  AND id = (
                      SELECT id FROM ocpp.public_sessions
                       WHERE LEFT(id::text, 8) = $2
                         AND ocpp_transaction_id IS NULL
                         AND started_at IS NULL
                       ORDER BY created_at DESC
                       LIMIT 1
                  )
            """, transaction_id, public_session_prefix)
        logger.info(f"[{cp_id}] Linked public session {public_session_prefix}* to txn={transaction_id}")

    return {
        "transactionId": transaction_id,
        "idTagInfo": {"status": AuthorizationStatus.ACCEPTED},
    }


async def handle_stop_transaction(
    cp_id: str,
    event_bus: EventBus,
    simulated: bool,
    payload: dict,
) -> dict:
    """Charging session stopped."""
    transaction_id = payload.get("transactionId")
    meter_stop = payload.get("meterStop", 0)
    timestamp = parse_timestamp(payload.get("timestamp"))
    reason = payload.get("reason", "Local")
    id_tag = payload.get("idTag", "")

    # ⚠️ MAXPOWER quirk: sends StopTransaction with reason="Other" on reconnect
    # Don't finalize session if connector still shows Charging after reconnect
    # For now: log it, process normally, flag for review if reason="Other"
    if reason == "Other":
        logger.warning(f"[{cp_id}] StopTransaction reason='Other' (possible reconnect artifact)")

    # Find session, compute final energy via session_energy (single formula)
    session = None
    async with db.transaction() as conn:
        row = await conn.fetchrow(
            "SELECT id::text as session_id, COALESCE(meter_start, 0) as meter_start "
            "FROM ocpp.sessions WHERE charge_point = $1 AND transaction_id = $2 AND status = 'active'",
            cp_id, transaction_id,
        )
        if row:
            final_energy = session_energy(float(meter_stop) / 1000.0, float(row["meter_start"]))
            session = await conn.fetchrow("""
                UPDATE ocpp.sessions SET
                    status = 'completed',
                    stop_time = $1,
                    stop_reason = $2,
                    meter_stop = $3,
                    energy_kwh = $4
                WHERE charge_point = $5 AND transaction_id = $6 AND status = 'active'
                RETURNING id::text as session_id, energy_kwh, start_time, meter_start
            """, timestamp, reason, float(meter_stop), final_energy, cp_id, transaction_id)

    if session:
        session_id = session["session_id"]

        # Clean up Redis
        await redis_state.del_session(session_id)

        # Generate CDR
        await generate_cdr(cp_id, event_bus, simulated, session_id)

        # Publish event
        await event_bus.publish(Event(
            type=EventType.SESSION_STOP,
            charge_point=cp_id,
            session_id=session_id,
            simulated=simulated,
            data={
                "transaction_id": transaction_id,
                "reason": reason,
                "energy_kwh": float(session["energy_kwh"]),
                "meter_stop": meter_stop,
            },
        ))

        logger.info(f"[{cp_id}] Session stopped: txn={transaction_id} reason={reason} energy={session['energy_kwh']:.1f}kWh")

        # Send push notification if driver subscribed
        await _notify_session_complete(session_id, float(session["energy_kwh"]), transaction_id)
    else:
        logger.warning(f"[{cp_id}] StopTransaction for unknown/inactive txn={transaction_id}")

    return {"idTagInfo": {"status": AuthorizationStatus.ACCEPTED}} if id_tag else {}


async def _notify_session_complete(
    ocpp_session_id: str,
    energy_kwh: float,
    transaction_id: int,
) -> None:
    """Send push notification to driver on session completion (best-effort)."""
    try:
        # Look up public session by ocpp_transaction_id to get rate and public session id
        async with db.read() as conn:
            row = await conn.fetchrow("""
                SELECT id::text as public_id, rate_kwh
                FROM ocpp.public_sessions
                WHERE ocpp_transaction_id = $1
                LIMIT 1
            """, transaction_id)

        if not row:
            return  # No public session (e.g. RFID session) — skip push

        public_id = row["public_id"]
        rate_kwh = float(row["rate_kwh"] or 0)
        cost = energy_kwh * rate_kwh * 1.21  # incl BTW

        title = "⚡ Laden voltooid"
        body = f"{energy_kwh:.2f} kWh geladen — €{cost:.2f}".replace(".", ",")
        url = f"/receipt/{public_id}"

        await send_push_for_session(public_id, title, body, url)
    except Exception as e:
        logger.warning(f"Push notification failed for session {ocpp_session_id}: {e}")


def _calculate_cdr_cost(energy_kwh: float, tariff_rate: Decimal = Decimal("0.35")) -> dict:
    """Calculate CDR cost using Decimal arithmetic. Returns cost JSONB dict."""
    energy = Decimal(str(energy_kwh))
    energy_cost = (energy * tariff_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    vat_rate = Decimal("0.21")
    vat_amount = (energy_cost * vat_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total = energy_cost + vat_amount
    return {
        "energy_kwh": float(energy),
        "rate_kwh": float(tariff_rate),
        "energy_cost": float(energy_cost),
        "time_cost": 0.0,
        "flat_fee": 0.0,
        "subtotal": float(energy_cost),
        "vat_rate": 21.0,
        "vat_amount": float(vat_amount),
        "total": float(total),
        "currency": "EUR",
    }


async def generate_cdr(
    cp_id: str,
    event_bus: EventBus,
    simulated: bool,
    session_id: str,
) -> None:
    """Generate a Charge Detail Record for a completed session."""
    import json
    async with db.write() as c:
        session = await c.fetchrow("""
            SELECT charge_point, connector_id, auth_method, auth_id,
                   start_time, stop_time, energy_kwh
            FROM ocpp.sessions WHERE id::text = $1
        """, session_id)

        if not session:
            return

        duration_min = 0
        if session["stop_time"] and session["start_time"]:
            delta = session["stop_time"] - session["start_time"]
            duration_min = delta.total_seconds() / 60

        # Calculate cost with Decimal arithmetic
        try:
            cost_json = _calculate_cdr_cost(float(session["energy_kwh"]))
        except Exception as exc:
            logger.warning(f"[{cp_id}] Failed to calculate CDR cost for session {session_id}: {exc}")
            cost_json = None

        await c.execute("""
            INSERT INTO ocpp.cdrs 
                (session_id, charge_point, connector_id, auth_method, auth_id,
                 start_time, stop_time, energy_kwh, duration_min, cost)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
            session_id, session["charge_point"], session["connector_id"],
            session["auth_method"], session["auth_id"],
            session["start_time"], session["stop_time"],
            session["energy_kwh"], duration_min,
            json.dumps(cost_json) if cost_json is not None else None,
        )

        # Publish CDR event
        await event_bus.publish(Event(
            type=EventType.SESSION_CDR,
            charge_point=cp_id,
            session_id=session_id,
            simulated=simulated,
            data={
                "energy_kwh": float(session["energy_kwh"]),
                "duration_min": round(duration_min, 1),
                "cost_total": cost_json["total"] if cost_json else None,
            },
        ))
