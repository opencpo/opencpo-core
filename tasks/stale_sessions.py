"""
Stale Session Auto-Closer — Background task.

Runs every 60 seconds and auto-closes sessions that are:
1. Active with no MeterValues for > 15 minutes
2. Active with power_kw < 0.5 for > 5 minutes (all recent readings)

Closes by setting status='Completed', stop_reason='StaleSessionAutoClose'.
Also cleans up Redis session keys.
"""
import asyncio
import logging
from datetime import datetime, timezone

from state.postgres import db
from state.redis import redis_state

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds
NO_METER_TIMEOUT_MIN = 15
LOW_POWER_TIMEOUT_MIN = 5
LOW_POWER_THRESHOLD_KW = 0.5


async def close_stale_sessions():
    """Find and close stale charging sessions."""
    closed_count = 0

    try:
        async with db.transaction() as conn:
            # 1. Sessions with no meter values for 15+ minutes
            stale_no_meter = await conn.fetch("""
                SELECT s.id, s.charge_point, s.connector_id, s.start_time, s.energy_kwh
                FROM ocpp.sessions s
                WHERE LOWER(s.status) = 'active'
                  AND s.start_time < now() - interval '15 minutes'
                  AND NOT EXISTS (
                      SELECT 1 FROM ocpp.meter_values mv
                      WHERE mv.session_id = s.id
                        AND mv.time > now() - interval '15 minutes'
                  )
            """)

            for row in stale_no_meter:
                # Safety: don't close if connector is still actively charging
                charger = await redis_state.get_charger(row["charge_point"])
                conn_key = f"connector_{row['connector_id']}_status"
                conn_status = charger.get(conn_key, "") if charger else ""
                if conn_status in ("Charging", "SuspendedEV", "SuspendedEVSE"):
                    logger.info(
                        "Skipping auto-close: %s conn %d still %s",
                        row["charge_point"], row["connector_id"], conn_status,
                    )
                    continue

                await conn.execute("""
                    UPDATE ocpp.sessions
                    SET status = 'Completed',
                        stop_reason = 'StaleSessionAutoClose',
                        stop_time = now()
                    WHERE id = $1
                """, row["id"])

                logger.warning(
                    "Auto-closed stale session (no meter data for %d min)",
                    NO_METER_TIMEOUT_MIN,
                    extra={
                        "session_id": row["id"],
                        "charge_point": row["charge_point"],
                        "connector_id": row["connector_id"],
                        "energy_kwh": float(row["energy_kwh"]) if row["energy_kwh"] else 0,
                        "reason": "no_meter_data",
                    },
                )

                # Clean up Redis session key
                await redis_state.del_session(row["id"])
                closed_count += 1

            # 2. Sessions with low power (< 0.5 kW) for 5+ minutes
            #    All meter values in last 5 min must be < threshold, and at least one exists
            stale_low_power = await conn.fetch("""
                SELECT s.id, s.charge_point, s.connector_id, s.energy_kwh
                FROM ocpp.sessions s
                WHERE LOWER(s.status) = 'active'
                  AND s.start_time < now() - interval '5 minutes'
                  AND EXISTS (
                      SELECT 1 FROM ocpp.meter_values mv
                      WHERE mv.session_id = s.id
                        AND mv.time > now() - interval '5 minutes'
                        AND mv.power_kw < $1
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM ocpp.meter_values mv
                      WHERE mv.session_id = s.id
                        AND mv.time > now() - interval '5 minutes'
                        AND mv.power_kw >= $1
                  )
            """, LOW_POWER_THRESHOLD_KW)

            for row in stale_low_power:
                # Skip if already closed above
                already_closed = any(r["id"] == row["id"] for r in stale_no_meter)
                if already_closed:
                    continue

                # Safety: don't close if connector is still actively charging
                charger = await redis_state.get_charger(row["charge_point"])
                conn_key = f"connector_{row['connector_id']}_status"
                conn_status = charger.get(conn_key, "") if charger else ""
                if conn_status in ("Charging", "SuspendedEV", "SuspendedEVSE"):
                    logger.info(
                        "Skipping auto-close (low power): %s conn %d still %s",
                        row["charge_point"], row["connector_id"], conn_status,
                    )
                    continue

                await conn.execute("""
                    UPDATE ocpp.sessions
                    SET status = 'Completed',
                        stop_reason = 'StaleSessionAutoClose',
                        stop_time = now()
                    WHERE id = $1
                """, row["id"])

                logger.warning(
                    "Auto-closed stale session (low power for %d min)",
                    LOW_POWER_TIMEOUT_MIN,
                    extra={
                        "session_id": row["id"],
                        "charge_point": row["charge_point"],
                        "connector_id": row["connector_id"],
                        "energy_kwh": float(row["energy_kwh"]) if row["energy_kwh"] else 0,
                        "reason": "low_power",
                    },
                )

                await redis_state.del_session(row["id"])
                closed_count += 1

    except Exception as e:
        logger.error("Stale session checker error: %s", e, exc_info=True)

    return closed_count


async def run_stale_session_checker():
    """Background loop — runs close_stale_sessions every CHECK_INTERVAL seconds."""
    logger.info(
        "Stale session checker started (interval=%ds, no_meter=%dm, low_power=%dm/%.1fkW)",
        CHECK_INTERVAL, NO_METER_TIMEOUT_MIN, LOW_POWER_TIMEOUT_MIN, LOW_POWER_THRESHOLD_KW,
    )

    while True:
        try:
            closed = await close_stale_sessions()
            if closed > 0:
                logger.info("Stale session checker: closed %d session(s)", closed)
        except asyncio.CancelledError:
            logger.info("Stale session checker stopped")
            break
        except Exception as e:
            logger.error("Stale session checker loop error: %s", e, exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)
