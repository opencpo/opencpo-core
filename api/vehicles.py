"""
Fleet Vehicles API endpoints.

CRUD for commercial.fleet_vehicles.
Replaces direct-DB access in the client portal (routes/vehicles.py).

Table: commercial.fleet_vehicles
Columns inferred from portal usage:
  id              SERIAL PRIMARY KEY
  license_plate   TEXT NOT NULL UNIQUE
  make            TEXT
  model           TEXT
  connector_type  TEXT ('CCS2'/'Type 2'/'CHAdeMO'/'Type 1'/'CCS1')
  status          TEXT ('active'/'inactive'/'maintenance')
  pnc_cert_serial TEXT (nullable)
  pnc_cert_status TEXT (nullable)
  last_session_at TIMESTAMPTZ (nullable)
  created_at      TIMESTAMPTZ DEFAULT NOW()
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_STATUSES = {"active", "inactive", "maintenance"}
VALID_CONNECTOR_TYPES = {"CCS2", "Type 2", "CHAdeMO", "Type 1", "CCS1"}


# ── Request models ─────────────────────────────────────────────────────────

class CreateVehicleRequest(BaseModel):
    license_plate: str
    make: Optional[str] = None
    model: Optional[str] = None
    connector_type: str = "CCS2"
    status: str = "active"
    pnc_cert_serial: Optional[str] = None
    pnc_cert_status: Optional[str] = None


class PatchVehicleRequest(BaseModel):
    license_plate: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    connector_type: Optional[str] = None
    status: Optional[str] = None
    pnc_cert_serial: Optional[str] = None
    pnc_cert_status: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("")
async def list_vehicles(
    status: str = Query(None, description="Filter by status: active/inactive/maintenance"),
    group: str = Query(None, description="Reserved: filter by group (future use)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """List fleet vehicles with optional status filter."""
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1

    params.extend([offset, limit])

    async with db.read() as conn:
        rows = await conn.fetch(f"""
            SELECT id, license_plate, make, model, connector_type, status,
                   pnc_cert_serial, pnc_cert_status, last_session_at, created_at
            FROM commercial.fleet_vehicles
            WHERE {' AND '.join(conditions)}
            ORDER BY status, license_plate
            OFFSET ${idx} LIMIT ${idx + 1}
        """, *params)

        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM commercial.fleet_vehicles WHERE {' AND '.join(conditions)}
        """, *params[:-2])

    vehicles = []
    for r in rows:
        item = dict(r)
        item["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        item["last_session_at"] = r["last_session_at"].isoformat() if r["last_session_at"] else None
        vehicles.append(item)

    return {"vehicles": vehicles, "total": total, "offset": offset, "limit": limit}


@router.get("/{vehicle_id}")
async def get_vehicle(vehicle_id: int):
    """Get a single fleet vehicle by ID."""
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM commercial.fleet_vehicles WHERE id = $1", vehicle_id
        )
    if not row:
        raise HTTPException(404, f"Vehicle {vehicle_id} not found")
    item = dict(row)
    item["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
    item["last_session_at"] = row["last_session_at"].isoformat() if row["last_session_at"] else None
    return item


@router.post("", status_code=201)
async def create_vehicle(req: CreateVehicleRequest):
    """Register a new fleet vehicle."""
    if req.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUSES)}")
    if req.connector_type not in VALID_CONNECTOR_TYPES:
        raise HTTPException(400, f"connector_type must be one of {sorted(VALID_CONNECTOR_TYPES)}")

    license_plate = req.license_plate.upper().strip()

    async with db.write() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM commercial.fleet_vehicles WHERE UPPER(license_plate) = $1",
            license_plate,
        )
        if existing:
            raise HTTPException(409, f"Vehicle with license plate {license_plate} already exists")

        row = await conn.fetchrow("""
            INSERT INTO commercial.fleet_vehicles
                (license_plate, make, model, connector_type, status, pnc_cert_serial, pnc_cert_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, license_plate, status, created_at
        """, license_plate, req.make, req.model, req.connector_type,
            req.status, req.pnc_cert_serial, req.pnc_cert_status)

    logger.info("Created fleet vehicle id=%s plate=%s", row["id"], license_plate)
    return {
        "id": row["id"],
        "license_plate": row["license_plate"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
    }


@router.patch("/{vehicle_id}")
async def patch_vehicle(vehicle_id: int, req: PatchVehicleRequest):
    """Update fleet vehicle details."""
    if req.status is not None and req.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUSES)}")
    if req.connector_type is not None and req.connector_type not in VALID_CONNECTOR_TYPES:
        raise HTTPException(400, f"connector_type must be one of {sorted(VALID_CONNECTOR_TYPES)}")

    async with db.write() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM commercial.fleet_vehicles WHERE id = $1", vehicle_id
        )
        if not existing:
            raise HTTPException(404, f"Vehicle {vehicle_id} not found")

        sets: list[str] = []
        params: list = []
        idx = 1

        col_map = {
            "make": req.make,
            "model": req.model,
            "connector_type": req.connector_type,
            "status": req.status,
            "pnc_cert_serial": req.pnc_cert_serial,
            "pnc_cert_status": req.pnc_cert_status,
        }
        # license_plate gets special treatment (uppercase normalise)
        if req.license_plate is not None:
            sets.append(f"license_plate = ${idx}")
            params.append(req.license_plate.upper().strip())
            idx += 1

        for col, val in col_map.items():
            if val is not None:
                sets.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1

        if not sets:
            raise HTTPException(400, "No fields to update")

        params.append(vehicle_id)
        await conn.execute(
            f"UPDATE commercial.fleet_vehicles SET {', '.join(sets)} WHERE id = ${idx}",
            *params,
        )

    logger.info("Patched fleet vehicle id=%s", vehicle_id)
    return {"id": vehicle_id, "status": "updated"}


@router.delete("/{vehicle_id}")
async def delete_vehicle(vehicle_id: int):
    """Soft-delete a fleet vehicle (sets status to 'inactive')."""
    async with db.write() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM commercial.fleet_vehicles WHERE id = $1", vehicle_id
        )
        if not existing:
            raise HTTPException(404, f"Vehicle {vehicle_id} not found")

        await conn.execute(
            "UPDATE commercial.fleet_vehicles SET status = 'inactive' WHERE id = $1",
            vehicle_id,
        )

    logger.info("Soft-deleted fleet vehicle id=%s", vehicle_id)
    return {"id": vehicle_id, "status": "inactive", "deleted": True}


@router.get("/{vehicle_id}/sessions")
async def vehicle_sessions(
    vehicle_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    """Last sessions for a vehicle (matched by license plate as auth_id)."""
    async with db.read() as conn:
        vehicle = await conn.fetchrow(
            "SELECT id, license_plate FROM commercial.fleet_vehicles WHERE id = $1", vehicle_id
        )
        if not vehicle:
            raise HTTPException(404, f"Vehicle {vehicle_id} not found")

        # Normalise plate for matching (strip dashes, uppercase)
        plate_norm = vehicle["license_plate"].replace("-", "").upper()

        sessions = await conn.fetch("""
            SELECT id, charge_point, connector_id, start_time, stop_time,
                   status, energy_kwh, peak_power_kw, auth_id
            FROM ocpp.sessions
            WHERE UPPER(REPLACE(auth_id, '-', '')) = $1
            ORDER BY start_time DESC
            OFFSET $2 LIMIT $3
        """, plate_norm, offset, limit)

    return {
        "vehicle_id": vehicle_id,
        "license_plate": vehicle["license_plate"],
        "sessions": [
            {
                **dict(s),
                "id": str(s["id"]),
                "start_time": s["start_time"].isoformat() if s["start_time"] else None,
                "stop_time": s["stop_time"].isoformat() if s["stop_time"] else None,
            }
            for s in sessions
        ],
        "offset": offset,
        "limit": limit,
    }
