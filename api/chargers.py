"""
Charger read endpoints (list, get, connectors, meter values, sessions).

Write endpoints (create, update, delete, commands) live in api/charger_commands.py.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from state.postgres import db
from state.redis import redis_state

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_chargers(
    status: str = Query(None, description="Filter: online/offline"),
    site: str = Query(None, description="Filter by site"),
    simulated: bool = Query(None, description="Include simulated chargers"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List all charge points with live status from Redis."""
    conditions = ["1=1"]
    params = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if site:
        conditions.append(f"site = ${idx}")
        params.append(site)
        idx += 1
    if simulated is not None:
        conditions.append(f"simulated = ${idx}")
        params.append(simulated)
        idx += 1

    params.extend([offset, limit])

    async with db.read() as conn:
        chargers = await conn.fetch(f"""
            SELECT id, vendor, model, serial_number, firmware_version,
                   ocpp_version, site, status, simulated, last_boot, last_heartbeat,
                   registered_at, config, metadata
            FROM ocpp.charge_points
            WHERE {' AND '.join(conditions)}
            ORDER BY registered_at DESC
            OFFSET ${idx} LIMIT ${idx + 1}
        """, *params)

        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM ocpp.charge_points WHERE {' AND '.join(conditions)}
        """, *params[:-2])

    result = []
    for cp in chargers:
        live = await redis_state.get_charger(cp["id"])
        data = {**dict(cp)}
        meta = data.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        elif not isinstance(meta, dict):
            meta = {}
        for mk in ("display_name", "address", "city", "latitude", "longitude",
                    "max_power_kw", "tariff_kwh", "access_type"):
            if mk not in data:
                data[mk] = meta.get(mk)
        data["live"] = live or {}
        data["last_boot"] = cp["last_boot"].isoformat() if cp["last_boot"] else None
        data["last_heartbeat"] = cp["last_heartbeat"].isoformat() if cp["last_heartbeat"] else None
        data["registered_at"] = cp["registered_at"].isoformat()
        result.append(data)

    return {"chargers": result, "total": total, "offset": offset, "limit": limit}


@router.get("/{cp_id}")
async def get_charger(cp_id: str):
    """Get a specific charge point with connectors and live state."""
    async with db.read() as conn:
        cp = await conn.fetchrow(
            "SELECT * FROM ocpp.charge_points WHERE id = $1", cp_id
        )
        if not cp:
            raise HTTPException(404, f"Charge point {cp_id} not found")

        connectors = await conn.fetch(
            "SELECT * FROM ocpp.connectors WHERE charge_point = $1 ORDER BY connector_id", cp_id
        )

    live = await redis_state.get_charger(cp_id)
    return {
        **dict(cp),
        "connectors": [dict(c) for c in connectors],
        "live": live or {},
    }


@router.get("/{cp_id}/meter-values")
async def charger_meter_values(
    cp_id: str,
    limit: int = Query(1, ge=1, le=100, description="Number of latest readings per connector"),
):
    """Latest meter values per connector for a charge point (live telemetry)."""
    async with db.read() as conn:
        cp = await conn.fetchval(
            "SELECT id FROM ocpp.charge_points WHERE id = $1", cp_id
        )
        if not cp:
            raise HTTPException(404, f"Charge point {cp_id} not found")

        connectors = await conn.fetch(
            "SELECT DISTINCT connector_id FROM ocpp.connectors WHERE charge_point = $1 ORDER BY connector_id",
            cp_id,
        )

        result = {}
        for row in connectors:
            cid = row["connector_id"]
            values = await conn.fetch("""
                SELECT time, energy_kwh, power_kw, soc_pct, voltage_v, current_a, temperature_c
                FROM ocpp.meter_values
                WHERE charge_point = $1 AND connector_id = $2
                ORDER BY time DESC
                LIMIT $3
            """, cp_id, cid, limit)
            result[str(cid)] = [
                {
                    "time": v["time"].isoformat(),
                    "energy_kwh": v["energy_kwh"],
                    "power_kw": v["power_kw"],
                    "soc_pct": v["soc_pct"],
                    "voltage_v": v["voltage_v"],
                    "current_a": v["current_a"],
                    "temperature_c": v["temperature_c"],
                }
                for v in values
            ]

    return {"charge_point": cp_id, "connectors": result}


@router.get("/{cp_id}/sessions")
async def charger_sessions(
    cp_id: str,
    status: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List sessions for a specific charge point."""
    async with db.read() as conn:
        conditions = ["charge_point = $1"]
        params = [cp_id]
        idx = 2

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        params.extend([offset, limit])

        sessions = await conn.fetch(f"""
            SELECT id, connector_id, transaction_id, status, auth_method, auth_id,
                   start_time, stop_time, stop_reason, energy_kwh, peak_power_kw,
                   start_soc, end_soc, simulated
            FROM ocpp.sessions
            WHERE {' AND '.join(conditions)}
            ORDER BY start_time DESC
            OFFSET ${idx} LIMIT ${idx + 1}
        """, *params)

    result = []
    for s in sessions:
        session_data = dict(s)
        session_data["id"] = str(s["id"])
        session_data["start_time"] = s["start_time"].isoformat() if s["start_time"] else None
        session_data["stop_time"] = s["stop_time"].isoformat() if s["stop_time"] else None
        if s["status"] == "active":
            live = await redis_state.get_session(str(s["id"]))
            if live:
                session_data["power_kw"] = float(live.get("power_kw", 0) or 0)
                session_data["energy_kwh"] = float(live.get("energy_kwh", s["energy_kwh"] or 0))
                session_data["soc_pct"] = int(live["soc_pct"]) if live.get("soc_pct") else None
        result.append(session_data)

    return {"sessions": result}
