"""
OCPP 2.0.1 MeterValues processing.

Mixin for ChargePointHandler201 — handles standalone MeterValues messages
and the shared _process_meter_values / _parse_sampled_values_201 helpers
used by TransactionEvent (Updated/Ended) as well.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from events.types import Event, EventType
from state.postgres import db
from state.redis import redis_state

logger = logging.getLogger(__name__)


class MeterMixin:
    """Mixin providing MeterValues handlers and parsing helpers."""

    async def _on_meter_values(self, payload: dict) -> dict:
        """Standalone MeterValues (outside transaction context)."""
        evse_id = payload.get("evseId", 0)
        meter_values = payload.get("meterValue", [])
        await self._process_meter_values(None, meter_values, None, evse_id=evse_id)
        return {}

    async def _process_meter_values(
        self, session_id: str | None, meter_values: list,
        timestamp: str | None, evse_id: int = 0,
    ) -> None:
        """Process 2.0.1 meter values — unified format."""
        for mv in meter_values:
            ts = mv.get("timestamp", timestamp or datetime.now(timezone.utc).isoformat())
            sampled = mv.get("sampledValue", [])
            readings = self._parse_sampled_values_201(sampled)

            connector_id = readings.pop("_connector_id", 0)

            if session_id:
                await redis_state.update_session_meter(session_id, {
                    "power_kw": readings.get("power_kw", 0),
                    "energy_kwh": readings.get("energy_kwh", 0),
                    "soc_pct": readings.get("soc_pct", 0),
                    "timestamp": ts,
                })

                # Update session energy in PG
                if readings.get("energy_kwh"):
                    async with db.write() as conn:
                        await conn.execute("""
                            UPDATE ocpp.sessions SET energy_kwh = GREATEST(energy_kwh, $1),
                                peak_power_kw = GREATEST(peak_power_kw, $2)
                            WHERE id::text = $3
                        """, readings["energy_kwh"], readings.get("power_kw", 0), session_id)

            # TimescaleDB insert
            async with db.write() as conn:
                await conn.execute("""
                    INSERT INTO ocpp.meter_values 
                        (time, charge_point, connector_id, session_id,
                         energy_kwh, power_kw, soc_pct, voltage_v, current_a, temperature_c)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                    ts, self.cp_id, connector_id,
                    session_id,
                    readings.get("energy_kwh"),
                    readings.get("power_kw"),
                    readings.get("soc_pct"),
                    readings.get("voltage_v"),
                    readings.get("current_a"),
                    readings.get("temperature_c"),
                )

            await self.event_bus.publish(Event(
                type=EventType.SESSION_METER,
                charge_point=self.cp_id,
                connector=connector_id,
                session_id=session_id or "",
                simulated=self._simulated,
                data=readings,
            ))

    def _parse_sampled_values_201(self, sampled: list[dict]) -> dict:
        """Parse 2.0.1 SampledValue format."""
        readings: dict[str, Any] = {}
        voltages = [None, None, None]
        currents = [None, None, None]

        for sv in sampled:
            measurand = sv.get("measurand", "Energy.Active.Import.Register")
            value = sv.get("value", 0)
            phase = sv.get("phase")
            location = sv.get("location", "Outlet")
            unit = sv.get("unitOfMeasure", {})
            unit_name = unit.get("unit", "")
            multiplier = unit.get("multiplier", 0)

            try:
                val = float(value) * (10 ** multiplier)
            except (ValueError, TypeError):
                continue

            if "Energy.Active.Import" in measurand:
                readings["energy_kwh"] = val / 1000 if unit_name == "Wh" else val
            elif "Power.Active.Import" in measurand:
                readings["power_kw"] = val / 1000 if unit_name == "W" else val
            elif measurand == "SoC":
                readings["soc_pct"] = int(val)
            elif measurand == "Temperature":
                readings["temperature_c"] = val
            elif "Voltage" in measurand:
                idx = {"L1": 0, "L2": 1, "L3": 2}.get(phase, 0)
                voltages[idx] = val
            elif "Current.Import" in measurand:
                idx = {"L1": 0, "L2": 1, "L3": 2}.get(phase, 0)
                currents[idx] = val

        if any(v is not None for v in voltages):
            readings["voltage_v"] = voltages
        if any(c is not None for c in currents):
            readings["current_a"] = currents

        return readings
