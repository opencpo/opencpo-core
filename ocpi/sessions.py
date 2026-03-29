"""
OCPI 2.2.1 Sessions module — CPO sender interface.

EMSPs pull active/completed sessions for their users.
"""
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from ocpi.main import ocpi_response, verify_ocpi_token
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_sessions(
    date_from: str = Query(None),
    date_to: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    token: str = Depends(verify_ocpi_token),
):
    """List sessions — EMSPs pull these to track their users' charging."""
    async with db.read() as conn:
        sessions = await conn.fetch("""
            SELECT s.id, s.charge_point, s.connector_id, s.auth_id, s.auth_method,
                   s.start_time, s.stop_time, s.energy_kwh, s.status
            FROM ocpp.sessions s
            WHERE s.simulated = FALSE
            ORDER BY s.start_time DESC
            OFFSET $1 LIMIT $2
        """, offset, limit)

    result = []
    for s in sessions:
        ocpi_status = "ACTIVE" if s["status"] == "active" else "COMPLETED"
        result.append({
            "country_code": os.getenv("OCPI_COUNTRY_CODE", "XX"),
            "party_id": os.getenv("OCPI_PARTY_ID", "CPO"),
            "id": str(s["id"]),
            "start_date_time": s["start_time"].isoformat(),
            "end_date_time": s["stop_time"].isoformat() if s["stop_time"] else None,
            "kwh": float(s["energy_kwh"]),
            "cdr_token": {
                "uid": s["auth_id"] or "",
                "type": "RFID",
                "contract_id": s["auth_id"] or "",
            },
            "auth_method": "WHITELIST" if s["auth_method"] == "rfid" else "AUTH_REQUEST",
            "location_id": s["charge_point"],
            "evse_uid": s["charge_point"],
            "connector_id": str(s["connector_id"]),
            "currency": "EUR",
            "status": ocpi_status,
            "last_updated": (s["stop_time"] or s["start_time"]).isoformat(),
        })

    return ocpi_response(result)
