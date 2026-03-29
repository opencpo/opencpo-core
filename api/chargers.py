"""
Charger management API endpoints.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

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

    # Enrich with live Redis state
    result = []
    for cp in chargers:
        live = await redis_state.get_charger(cp["id"])
        result.append({
            **dict(cp),
            "live": live or {},
            "last_boot": cp["last_boot"].isoformat() if cp["last_boot"] else None,
            "last_heartbeat": cp["last_heartbeat"].isoformat() if cp["last_heartbeat"] else None,
            "registered_at": cp["registered_at"].isoformat(),
        })

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

        # Get distinct connectors
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

    return {"sessions": [dict(s) for s in sessions]}


# ── Remote Commands ──────────────────────────────────────────────────────

class RemoteStartRequest(BaseModel):
    connector_id: int = 1
    id_tag: str = ""


class ChargingProfileRequest(BaseModel):
    connector_id: int = 0
    limit_kw: float
    duration_seconds: int = 0


@router.post("/{cp_id}/start")
async def remote_start(cp_id: str, req: RemoteStartRequest):
    """Send RemoteStartTransaction to a charger."""
    # This will be wired to the OCPP server's send_to method
    # For now: validate and return accepted
    live = await redis_state.get_charger(cp_id)
    if not live or live.get("status") != "online":
        raise HTTPException(400, f"Charger {cp_id} is not online")

    logger.info(f"Remote start: {cp_id} connector={req.connector_id}")
    return {"status": "Accepted", "charge_point": cp_id}


@router.post("/{cp_id}/stop")
async def remote_stop(cp_id: str, transaction_id: int = Query(None)):
    """Send RemoteStopTransaction to a charger."""
    live = await redis_state.get_charger(cp_id)
    if not live or live.get("status") != "online":
        raise HTTPException(400, f"Charger {cp_id} is not online")

    logger.info(f"Remote stop: {cp_id} txn={transaction_id}")
    return {"status": "Accepted", "charge_point": cp_id}


@router.post("/{cp_id}/reset")
async def reset_charger(cp_id: str, reset_type: str = Query("Soft")):
    """Send Reset command to a charger."""
    if reset_type not in ("Soft", "Hard"):
        raise HTTPException(400, "reset_type must be 'Soft' or 'Hard'")

    logger.info(f"Reset: {cp_id} type={reset_type}")
    return {"status": "Accepted", "charge_point": cp_id, "type": reset_type}


@router.post("/{cp_id}/profile")
async def set_charging_profile(cp_id: str, req: ChargingProfileRequest):
    """
    Set charging profile (power limit) on a charger.
    Used by EMS for smart charging. This is the OCPP soft ceiling —
    factory Modbus load balancer always has final say.
    """
    live = await redis_state.get_charger(cp_id)
    if not live or live.get("status") != "online":
        raise HTTPException(400, f"Charger {cp_id} is not online")

    logger.info(f"SetChargingProfile: {cp_id} limit={req.limit_kw}kW connector={req.connector_id}")
    return {"status": "Accepted", "charge_point": cp_id, "limit_kw": req.limit_kw}


# ── CRUD ──────────────────────────────────────────────────────────────────


class CreateChargerRequest(BaseModel):
    id: str
    vendor: str = ""
    model: str = ""
    serial_number: str = ""
    ocpp_version: str = "1.6"
    site: str = ""
    simulated: bool = False


class UpdateChargerRequest(BaseModel):
    vendor: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    firmware_version: Optional[str] = None
    ocpp_version: Optional[str] = None
    site: Optional[str] = None
    display_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    max_power_kw: Optional[float] = None
    tariff_kwh: Optional[float] = None


@router.post("")
async def create_charger(req: CreateChargerRequest):
    """Register a new charge point manually."""
    async with db.write() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM ocpp.charge_points WHERE id = $1", req.id
        )
        if existing:
            raise HTTPException(409, f"Charge point {req.id} already exists")

        await conn.execute("""
            INSERT INTO ocpp.charge_points
                (id, vendor, model, serial_number, ocpp_version, site, simulated)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, req.id, req.vendor, req.model, req.serial_number,
            req.ocpp_version, req.site, req.simulated)

    logger.info(f"Created charge point: {req.id} simulated={req.simulated}")
    return {"id": req.id, "status": "created"}


@router.put("/{cp_id}")
async def update_charger(cp_id: str, req: UpdateChargerRequest):
    """Update charge point details."""
    async with db.write() as conn:
        cp = await conn.fetchrow(
            "SELECT id, config, metadata FROM ocpp.charge_points WHERE id = $1", cp_id
        )
        if not cp:
            raise HTTPException(404, f"Charge point {cp_id} not found")

        # Build dynamic SET clause for non-None fields
        # Standard columns
        col_map = {
            "vendor": req.vendor,
            "model": req.model,
            "serial_number": req.serial_number,
            "firmware_version": req.firmware_version,
            "ocpp_version": req.ocpp_version,
            "site": req.site,
        }
        sets = []
        params = []
        idx = 1
        for col, val in col_map.items():
            if val is not None:
                sets.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1

        # Extra fields go into metadata JSONB
        meta_updates = {}
        if req.display_name is not None:
            meta_updates["display_name"] = req.display_name
        if req.address is not None:
            meta_updates["address"] = req.address
        if req.city is not None:
            meta_updates["city"] = req.city
        if req.max_power_kw is not None:
            meta_updates["max_power_kw"] = req.max_power_kw
        if req.tariff_kwh is not None:
            meta_updates["tariff_kwh"] = req.tariff_kwh

        if meta_updates:
            import json
            sets.append(f"metadata = metadata || ${idx}::jsonb")
            params.append(json.dumps(meta_updates))
            idx += 1

        if not sets:
            raise HTTPException(400, "No fields to update")

        params.append(cp_id)
        await conn.execute(
            f"UPDATE ocpp.charge_points SET {', '.join(sets)} WHERE id = ${idx}",
            *params
        )

    logger.info(f"Updated charge point: {cp_id}")
    return {"id": cp_id, "status": "updated"}


@router.delete("/{cp_id}")
async def delete_charger(cp_id: str):
    """Delete a charge point and all its data (connectors, sessions, meter values)."""
    async with db.write() as conn:
        cp = await conn.fetchval(
            "SELECT id FROM ocpp.charge_points WHERE id = $1", cp_id
        )
        if not cp:
            raise HTTPException(404, f"Charge point {cp_id} not found")

        # Delete meter values first (not FK-linked to charge_points)
        await conn.execute(
            "DELETE FROM ocpp.meter_values WHERE charge_point = $1", cp_id
        )
        # Delete CDRs tied to sessions of this charger
        await conn.execute("""
            DELETE FROM ocpp.cdrs WHERE charge_point = $1
        """, cp_id)
        # Sessions (connectors cascade from charge_points)
        await conn.execute(
            "DELETE FROM ocpp.sessions WHERE charge_point = $1", cp_id
        )
        # Charge point (connectors cascade via FK ON DELETE CASCADE)
        await conn.execute(
            "DELETE FROM ocpp.charge_points WHERE id = $1", cp_id
        )

    # Clean Redis state
    await redis_state.del_charger(cp_id)

    logger.info(f"Deleted charge point: {cp_id}")
    return {"id": cp_id, "status": "deleted"}


@router.delete("")
async def bulk_delete_chargers(simulated: bool = Query(True)):
    """Bulk delete chargers. Default: only simulated/virtual ones."""
    # Virtual charger prefixes (from charger farm tool)
    VIRTUAL_PREFIXES = ("STRESS-", "SIM-", "CHAOS-", "VAL-", "LOAD-", "FUZZ-", "FARM-", "PNC-")

    async with db.write() as conn:
        if simulated:
            # Match both simulated=true AND known virtual prefixes
            prefix_conditions = " OR ".join(
                f"id LIKE '{p}%'" for p in VIRTUAL_PREFIXES
            )
            ids = await conn.fetch(f"""
                SELECT id FROM ocpp.charge_points
                WHERE simulated = true OR ({prefix_conditions})
            """)
            cp_ids = [r["id"] for r in ids]

            if cp_ids:
                await conn.execute(
                    "DELETE FROM ocpp.meter_values WHERE charge_point = ANY($1::text[])", cp_ids
                )
                await conn.execute(
                    "DELETE FROM ocpp.cdrs WHERE charge_point = ANY($1::text[])", cp_ids
                )
                await conn.execute(
                    "DELETE FROM ocpp.sessions WHERE charge_point = ANY($1::text[])", cp_ids
                )
                await conn.execute(
                    "DELETE FROM ocpp.charge_points WHERE id = ANY($1::text[])", cp_ids
                )
            deleted = len(cp_ids)
        else:
            ids = await conn.fetch("SELECT id FROM ocpp.charge_points")
            cp_ids = [r["id"] for r in ids]

            await conn.execute("DELETE FROM ocpp.meter_values")
            await conn.execute("DELETE FROM ocpp.cdrs")
            await conn.execute("DELETE FROM ocpp.sessions")
            await conn.execute("DELETE FROM ocpp.charge_points")
            deleted = len(cp_ids)

    # Clean Redis for each deleted charger
    for cp_id in cp_ids:
        await redis_state.del_charger(cp_id)

    logger.info(f"Bulk deleted {len(cp_ids)} charge points (simulated={simulated})")
    return {"deleted": len(cp_ids), "simulated_only": simulated}
