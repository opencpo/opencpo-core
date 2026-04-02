"""
Charger command and CRUD endpoints (write operations).

Separated from api/chargers.py (read endpoints) to keep files under 500 lines.
All write endpoints require management_auth via Depends or router dependency.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.api_key_auth import management_auth
from state.postgres import db
from state.redis import redis_state

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Request Models ────────────────────────────────────────────────────────────

class RemoteStartRequest(BaseModel):
    connector_id: int = 1
    id_tag: str = ""


class ChargingProfileRequest(BaseModel):
    connector_id: int = 0
    limit_kw: float
    duration_seconds: int = 0


class GenericCommandRequest(BaseModel):
    action: str
    payload: dict = {}


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


# ── Remote Commands ───────────────────────────────────────────────────────────

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
    """Set charging profile (power limit) on a charger. Used by EMS for smart charging."""
    from state.charger_registry import send_command, is_connected

    if not is_connected(cp_id):
        raise HTTPException(400, f"Charger {cp_id} is not connected")

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


# ── CRUD ──────────────────────────────────────────────────────────────────────

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

        col_map = {
            "vendor": req.vendor, "model": req.model,
            "serial_number": req.serial_number, "firmware_version": req.firmware_version,
            "ocpp_version": req.ocpp_version, "site": req.site, "simulated": req.simulated,
        }
        sets, params, idx = [], [], 1
        for col, val in col_map.items():
            if val is not None:
                sets.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1

        meta_updates = {}
        for field in ("display_name", "address", "city", "latitude", "longitude",
                      "max_power_kw", "tariff_kwh"):
            val = getattr(req, field)
            if val is not None:
                meta_updates[field] = val

        if meta_updates:
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

        for table in ("meter_values", "cdrs", "sessions"):
            await conn.execute(f"DELETE FROM ocpp.{table} WHERE charge_point = $1", cp_id)
        await conn.execute("DELETE FROM ocpp.charge_points WHERE id = $1", cp_id)

    await redis_state.del_charger(cp_id)
    logger.info(f"Deleted charge point: {cp_id}")
    return {"id": cp_id, "status": "deleted"}


@router.delete("", dependencies=[Depends(management_auth)])
async def bulk_delete_chargers(simulated: bool = Query(True)):
    """Bulk delete chargers. Default: only simulated/virtual ones."""
    VIRTUAL_PREFIXES = ("STRESS-", "SIM-", "CHAOS-", "VAL-", "LOAD-", "FUZZ-", "FARM-", "PNC-")

    async with db.write() as conn:
        if simulated:
            prefix_conditions = " OR ".join(f"id LIKE '{p}%'" for p in VIRTUAL_PREFIXES)
            ids = await conn.fetch(f"""
                SELECT id FROM ocpp.charge_points
                WHERE simulated = true OR ({prefix_conditions})
            """)
            cp_ids = [r["id"] for r in ids]
            if cp_ids:
                for table in ("meter_values", "cdrs", "sessions"):
                    await conn.execute(
                        f"DELETE FROM ocpp.{table} WHERE charge_point = ANY($1::text[])", cp_ids
                    )
                await conn.execute(
                    "DELETE FROM ocpp.charge_points WHERE id = ANY($1::text[])", cp_ids
                )
        else:
            ids = await conn.fetch("SELECT id FROM ocpp.charge_points")
            cp_ids = [r["id"] for r in ids]
            for table in ("meter_values", "cdrs", "sessions", "charge_points"):
                await conn.execute(f"DELETE FROM ocpp.{table}")

    for cp_id in cp_ids:
        await redis_state.del_charger(cp_id)

    logger.info(f"Bulk deleted {len(cp_ids)} charge points (simulated={simulated})")
    return {"deleted": len(cp_ids), "simulated_only": simulated}
