"""
Charger management API endpoints.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.api_key_auth import management_auth

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

    # Enrich with live Redis state and flatten metadata for convenience
    result = []
    for cp in chargers:
        live = await redis_state.get_charger(cp["id"])
        data = {**dict(cp)}
        # Flatten metadata fields to top level for API consumers
        meta = data.get("metadata")
        if isinstance(meta, str):
            import json as _json
            try:
                meta = _json.loads(meta)
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

    # Enrich active sessions with live data from Redis (power_kw, soc, energy)
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

# ── Remote Commands ──────────────────────────────────────────────────────

class RemoteStartRequest(BaseModel):
    connector_id: int = 1
    id_tag: str = ""

class ChargingProfileRequest(BaseModel):
    connector_id: int = 0
    limit_kw: float
    duration_seconds: int = 0

@router.post("/{cp_id}/start", dependencies=[Depends(management_auth)])
async def remote_start(cp_id: str, req: RemoteStartRequest):
    """Send RemoteStartTransaction to a charger."""
    from state.charger_registry import send_remote_start, is_connected

    if not is_connected(cp_id):
        raise HTTPException(400, f"Charger {cp_id} is not connected")

    id_tag = req.id_tag or "REMOTE"
    msg_id = await send_remote_start(cp_id, req.connector_id, id_tag)
    if msg_id is None:
        raise HTTPException(502, f"Failed to send RemoteStartTransaction to {cp_id}")

    return {"status": "Accepted", "charge_point": cp_id, "msg_id": msg_id}

@router.post("/{cp_id}/stop", dependencies=[Depends(management_auth)])
async def remote_stop(cp_id: str, transaction_id: int = Query(None), connector_id: int = Query(None)):
    """Send RemoteStopTransaction to a charger."""
    from state.charger_registry import send_remote_stop, is_connected

    if not is_connected(cp_id):
        raise HTTPException(400, f"Charger {cp_id} is not connected")

    # Look up the active session — by connector if specified, otherwise most recent
    if transaction_id is None:
        async with db.read() as conn:
            if connector_id is not None:
                row = await conn.fetchrow(
                    "SELECT transaction_id FROM ocpp.sessions WHERE charge_point = $1 AND connector_id = $2 AND status = 'active' ORDER BY start_time DESC LIMIT 1",
                    cp_id, connector_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT transaction_id FROM ocpp.sessions WHERE charge_point = $1 AND status = 'active' ORDER BY start_time DESC LIMIT 1",
                    cp_id,
                )
        if not row or not row["transaction_id"]:
            raise HTTPException(400, f"No active session found for {cp_id}" + (f" connector {connector_id}" if connector_id else ""))
        transaction_id = row["transaction_id"]

    msg_id = await send_remote_stop(cp_id, transaction_id)
    if msg_id is None:
        raise HTTPException(502, f"Failed to send RemoteStopTransaction to {cp_id}")

    return {"status": "Accepted", "charge_point": cp_id, "transaction_id": transaction_id, "msg_id": msg_id}

@router.post("/{cp_id}/reset", dependencies=[Depends(management_auth)])
async def reset_charger(cp_id: str, reset_type: str = Query("Soft")):
    """Send Reset command to a charger."""
    from state.charger_registry import send_reset, is_connected

    if reset_type not in ("Soft", "Hard"):
        raise HTTPException(400, "reset_type must be 'Soft' or 'Hard'")

    if not is_connected(cp_id):
        raise HTTPException(400, f"Charger {cp_id} is not connected")

    msg_id = await send_reset(cp_id, reset_type)
    if msg_id is None:
        raise HTTPException(502, f"Failed to send Reset to {cp_id}")

    return {"status": "Accepted", "charge_point": cp_id, "type": reset_type, "msg_id": msg_id}

@router.post("/{cp_id}/profile", dependencies=[Depends(management_auth)])
async def set_charging_profile(cp_id: str, req: ChargingProfileRequest):
    """
    Set charging profile (power limit) on a charger.
    Used by EMS for smart charging.
    """
    from state.charger_registry import send_command, is_connected

    if not is_connected(cp_id):
        raise HTTPException(400, f"Charger {cp_id} is not connected")

    # OCPP 1.6 SetChargingProfile
    profile_payload = {
        "connectorId": req.connector_id,
        "csChargingProfiles": {
            "chargingProfileId": 1,
            "stackLevel": 0,
            "chargingProfilePurpose": "TxDefaultProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "chargingRateUnit": "W",
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": req.limit_kw * 1000}
                ],
            },
        },
    }
    if req.duration_seconds > 0:
        profile_payload["csChargingProfiles"]["chargingSchedule"]["duration"] = req.duration_seconds

    msg_id = await send_command(cp_id, "SetChargingProfile", profile_payload)
    if msg_id is None:
        raise HTTPException(502, f"Failed to send SetChargingProfile to {cp_id}")

    return {"status": "Accepted", "charge_point": cp_id, "limit_kw": req.limit_kw, "msg_id": msg_id}

class GenericCommandRequest(BaseModel):
    action: str
    payload: dict = {}

@router.post("/{cp_id}/command", dependencies=[Depends(management_auth)])
async def send_generic_command(cp_id: str, req: GenericCommandRequest):
    """Send any OCPP command to a charger. Used by terminal UI and GetConfiguration."""
    from state.charger_registry import send_command, is_connected

    if not is_connected(cp_id):
        raise HTTPException(400, f"Charger {cp_id} is not connected")

    msg_id = await send_command(cp_id, req.action, req.payload)
    if msg_id is None:
        raise HTTPException(502, f"Failed to send {req.action} to {cp_id}")

    return {"status": "Accepted", "charge_point": cp_id, "action": req.action, "msg_id": msg_id}

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
    simulated: Optional[bool] = None
    display_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    max_power_kw: Optional[float] = None
    tariff_kwh: Optional[float] = None

@router.post("", dependencies=[Depends(management_auth)])
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

@router.put("/{cp_id}", dependencies=[Depends(management_auth)])
async def update_charger(cp_id: str, req: UpdateChargerRequest):
    """Update charge point details."""
    async with db.write() as conn:
        cp = await conn.fetchrow(
            "SELECT id, config, metadata FROM ocpp.charge_points WHERE id = $1", cp_id
        )
        if not cp:
            raise HTTPException(404, f"Charge point {cp_id} not found")

        # Build dynamic SET clause for non-None fields
        col_map = {
            "vendor": req.vendor,
            "model": req.model,
            "serial_number": req.serial_number,
            "firmware_version": req.firmware_version,
            "ocpp_version": req.ocpp_version,
            "site": req.site,
            "simulated": req.simulated,
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
        if req.latitude is not None:
            meta_updates["latitude"] = req.latitude
        if req.longitude is not None:
            meta_updates["longitude"] = req.longitude
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

@router.delete("/{cp_id}", dependencies=[Depends(management_auth)])
async def delete_charger(cp_id: str):
    """Delete a charge point and all its data (connectors, sessions, meter values)."""
    async with db.write() as conn:
        cp = await conn.fetchval(
            "SELECT id FROM ocpp.charge_points WHERE id = $1", cp_id
        )
        if not cp:
            raise HTTPException(404, f"Charge point {cp_id} not found")

        await conn.execute(
            "DELETE FROM ocpp.meter_values WHERE charge_point = $1", cp_id
        )
        await conn.execute(
            "DELETE FROM ocpp.cdrs WHERE charge_point = $1", cp_id
        )
        await conn.execute(
            "DELETE FROM ocpp.sessions WHERE charge_point = $1", cp_id
        )
        await conn.execute(
            "DELETE FROM ocpp.charge_points WHERE id = $1", cp_id
        )

    # Clean Redis state
    await redis_state.del_charger(cp_id)

    logger.info(f"Deleted charge point: {cp_id}")
    return {"id": cp_id, "status": "deleted"}

@router.delete("", dependencies=[Depends(management_auth)])
async def bulk_delete_chargers(simulated: bool = Query(True)):
    """Bulk delete chargers. Default: only simulated/virtual ones."""
    VIRTUAL_PREFIXES = ("STRESS-", "SIM-", "CHAOS-", "VAL-", "LOAD-", "FUZZ-", "FARM-", "PNC-")

    async with db.write() as conn:
        if simulated:
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
