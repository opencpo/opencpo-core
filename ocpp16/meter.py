"""
OCPP 1.6 meter value processing.

Data model:
- Charger sends Energy.Active.Import.Register = CUMULATIVE lifetime Wh counter
- meter_register_kwh: raw cumulative value stored in TimescaleDB (audit)
- session_energy_kwh: register minus meterStart (billing/live display)
- _session_energy() is the SINGLE function that computes the session delta
"""
import logging

from events.bus import EventBus
from events.types import Event, EventType
from state.postgres import db
from state.redis import redis_state
from utils import parse_timestamp

logger = logging.getLogger(__name__)


def session_energy(meter_register_kwh: float, meter_start_wh: float) -> float:
    """Compute session energy from cumulative meter register and session start.

    meter_register_kwh: current cumulative reading from charger (in kWh)
    meter_start_wh: meterStart from StartTransaction (in Wh, as OCPP sends it)

    Returns session energy in kWh. Never negative.
    """
    if meter_register_kwh <= 0 or meter_start_wh <= 0:
        return 0.0
    return max(0.0, meter_register_kwh - (meter_start_wh / 1000.0))


def parse_sampled_values(sampled: list[dict]) -> dict:
    """Parse OCPP 1.6 SampledValue array into a flat dict."""
    readings = {}
    voltages = [None, None, None]
    currents = [None, None, None]

    import logging as _log
    _log.getLogger('meter').info(f'SampledValues: {[{"m": sv.get("measurand"), "v": sv.get("value"), "u": sv.get("unit")} for sv in sampled]}')
    for sv in sampled:
        measurand = sv.get("measurand", "Energy.Active.Import.Register")
        value = sv.get("value", "0")
        phase = sv.get("phase")
        unit = sv.get("unit", "")

        try:
            val = float(value)
        except (ValueError, TypeError):
            continue

        if "Energy.Active.Import" in measurand:
            # Cumulative meter register — convert Wh to kWh if needed
            # This is the charger's lifetime counter, NOT session energy
            readings["meter_register_kwh"] = val / 1000 if unit == "Wh" else val

        elif "Power.Active.Import" in measurand:
            # Convert W to kW if needed
            readings["power_kw"] = val / 1000 if unit == "W" else val

        elif measurand == "SoC":
            readings["soc_pct"] = int(val)

        elif measurand == "Temperature":
            readings["temperature_c"] = val

        elif "Voltage" in measurand:
            if phase == "L1":
                voltages[0] = val
            elif phase == "L2":
                voltages[1] = val
            elif phase == "L3":
                voltages[2] = val
            elif not phase:
                voltages[0] = val  # single phase

        elif "Current.Import" in measurand:
            if phase == "L1":
                currents[0] = val
            elif phase == "L2":
                currents[1] = val
            elif phase == "L3":
                currents[2] = val
            elif not phase:
                currents[0] = val

    if any(v is not None for v in voltages):
        readings["voltage_v"] = voltages
    if any(c is not None for c in currents):
        readings["current_a"] = currents

    # Calculate power from V×I if charger doesn't send Power.Active.Import
    # Profile-driven: some chargers send Power, some don't. Always calc as fallback.
    if "power_kw" not in readings and any(v is not None for v in voltages) and any(c is not None for c in currents):
        power_w = sum(
            (v or 0) * (c or 0)
            for v, c in zip(voltages, currents)
        )
        if power_w > 0:
            readings["power_kw"] = round(power_w / 1000, 3)
            logger.info(f"Calculated power from V×I: {readings['power_kw']:.1f} kW "
                        f"(V={[v for v in voltages if v]}, A={[c for c in currents if c]})")

    return readings


async def handle_meter_values(
    cp_id: str,
    event_bus: EventBus,
    simulated: bool,
    payload: dict,
) -> dict:
    """Meter readings during a session.

    Data model:
    - Charger sends cumulative Energy.Active.Import.Register (lifetime Wh)
    - We store the RAW register reading in TimescaleDB (unchanged, for audit)
    - Session energy = latest_register - meter_start (computed, not stored in meter_values)
    - Session energy is updated in ocpp.sessions and Redis (for live display)
    """
    connector_id = payload.get("connectorId", 0)
    transaction_id = payload.get("transactionId")
    meter_values = payload.get("meterValue", [])

    for mv in meter_values:
        timestamp = parse_timestamp(mv.get("timestamp"))
        sampled = mv.get("sampledValue", [])
        readings = parse_sampled_values(sampled)

        # Live charger state: write power/SoC to charger Redis hash (always, regardless of session)
        power_kw = readings.get("power_kw", 0) or 0
        charger_update = {}
        if power_kw > 0:
            charger_update[f"connector_{connector_id}_power_kw"] = str(round(power_kw, 2))
        if readings.get("soc_pct"):
            charger_update[f"connector_{connector_id}_soc_pct"] = str(readings["soc_pct"])
        if charger_update:
            await redis_state.set_charger(cp_id, charger_update)

        # Look up session context (meter_start for energy delta)
        session_id = None
        meter_start_wh = 0.0
        if transaction_id:
            async with db.read() as conn:
                row = await conn.fetchrow(
                    "SELECT id::text as session_id, COALESCE(meter_start, 0) as meter_start "
                    "FROM ocpp.sessions WHERE charge_point = $1 AND transaction_id = $2 AND status = 'active'",
                    cp_id, transaction_id,
                )
                if row:
                    session_id = row["session_id"]
                    meter_start_wh = float(row["meter_start"])

        # Derive session energy from cumulative register
        # meter_register_kwh = raw charger value (cumulative, in kWh)
        # session_energy_kwh = meter_register - meter_start (what this session delivered)
        meter_register_kwh = readings.get("meter_register_kwh", 0)
        session_energy_kwh = session_energy(meter_register_kwh, meter_start_wh)

        # 1. TimescaleDB: store RAW register reading (audit trail, immutable)
        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.meter_values
                    (time, charge_point, connector_id, session_id,
                     energy_kwh, power_kw, soc_pct, voltage_v, current_a, temperature_c)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
                timestamp, cp_id, connector_id, session_id,
                meter_register_kwh,  # RAW cumulative register, NOT session delta
                readings.get("power_kw"),
                readings.get("soc_pct"),
                readings.get("voltage_v"),
                readings.get("current_a"),
                readings.get("temperature_c"),
            )

        if session_id:
            # 2. Redis: live session state (consumers read this for dashboards)
            await redis_state.update_session_meter(session_id, {
                "power_kw": readings.get("power_kw", 0),
                "energy_kwh": session_energy_kwh,
                "soc_pct": readings.get("soc_pct", 0),
                "timestamp": timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp),
            })

            # 3. Sessions table: running session energy + peak power (single source of truth)
            power_kw = readings.get("power_kw", 0) or 0
            if session_energy_kwh > 0 or power_kw > 0:
                async with db.write() as conn:
                    await conn.execute(
                        "UPDATE ocpp.sessions SET "
                        "energy_kwh = GREATEST(energy_kwh, $1), "
                        "peak_power_kw = GREATEST(COALESCE(peak_power_kw, 0), $3) "
                        "WHERE id::text = $2 AND LOWER(status) = 'active'",
                        session_energy_kwh, session_id, power_kw,
                    )

        # 4. Public sessions (charge app): same derived energy
        if transaction_id and session_energy_kwh > 0:
            async with db.write() as conn:
                await conn.execute(
                    "UPDATE ocpp.public_sessions SET kwh_delivered = $1 WHERE ocpp_transaction_id = $2",
                    session_energy_kwh, transaction_id,
                )

        # Publish event (consumers get both raw register and session energy)
        event_data = {
            **readings,
            "energy_kwh": session_energy_kwh,  # session delta (what consumers want)
            "meter_register_kwh": meter_register_kwh,  # raw cumulative (for auditing)
        }
        await event_bus.publish(Event(
            type=EventType.SESSION_METER,
            charge_point=cp_id,
            connector=connector_id,
            session_id=session_id or "",
            simulated=simulated,
            data=event_data,
        ))

    return {}
