"""
OCPI 2.2.1 CDRs module — Charge Detail Records.

EMSPs pull CDRs for billing reconciliation.
"""
import logging

from fastapi import APIRouter, Depends, Query

from ocpi.main import ocpi_response, verify_ocpi_token
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_cdrs(
    date_from: str = Query(None),
    date_to: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    token: str = Depends(verify_ocpi_token),
):
    """List CDRs for billing reconciliation."""
    async with db.read() as conn:
        cdrs = await conn.fetch("""
            SELECT c.id, c.session_id, c.charge_point, c.connector_id,
                   c.auth_method, c.auth_id, c.start_time, c.stop_time,
                   c.energy_kwh, c.duration_min, c.cost, c.created_at
            FROM ocpp.cdrs c
            JOIN ocpp.sessions s ON s.id = c.session_id
            WHERE s.simulated = FALSE
            ORDER BY c.created_at DESC
            OFFSET $1 LIMIT $2
        """, offset, limit)

    result = []
    for cdr in cdrs:
        cost = cdr["cost"] or {}
        result.append({
            "country_code": "NL",
            "party_id": "STM",
            "id": str(cdr["id"]),
            "start_date_time": cdr["start_time"].isoformat(),
            "end_date_time": cdr["stop_time"].isoformat(),
            "cdr_token": {
                "uid": cdr["auth_id"] or "",
                "type": "RFID",
                "contract_id": cdr["auth_id"] or "",
            },
            "auth_method": "WHITELIST" if cdr["auth_method"] == "rfid" else "AUTH_REQUEST",
            "currency": "EUR",
            "total_cost": cost.get("total", 0),
            "total_energy": float(cdr["energy_kwh"]),
            "total_time": round(float(cdr["duration_min"]) / 60, 2),
            "last_updated": cdr["created_at"].isoformat(),
        })

    return ocpi_response(result)
