"""
OCPI 2.2.1 Tariffs module — CPO sender interface.

EMSPs pull our tariffs to display pricing in their apps.
"""
import logging

from fastapi import APIRouter, Depends, Query

from ocpi.main import ocpi_response, verify_ocpi_token
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_tariffs(
    date_from: str = Query(None),
    date_to: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    token: str = Depends(verify_ocpi_token),
):
    """List all tariffs."""
    async with db.read() as conn:
        tariffs = await conn.fetch("""
            SELECT id, name, currency, energy_rate, time_rate, idle_rate, flat_fee,
                   valid_from, valid_until, created_at, metadata
            FROM ocpp.tariffs
            ORDER BY created_at DESC
            OFFSET $1 LIMIT $2
        """, offset, limit)

    result = []
    for t in tariffs:
        elements = []

        # Energy component
        if t["energy_rate"] > 0:
            elements.append({
                "price_components": [{
                    "type": "ENERGY",
                    "price": float(t["energy_rate"]),
                    "step_size": 1,
                }]
            })

        # Time component
        if t["time_rate"] > 0:
            elements.append({
                "price_components": [{
                    "type": "TIME",
                    "price": float(t["time_rate"]),
                    "step_size": 60,  # per minute
                }]
            })

        # Flat fee
        if t["flat_fee"] > 0:
            elements.append({
                "price_components": [{
                    "type": "FLAT",
                    "price": float(t["flat_fee"]),
                    "step_size": 1,
                }]
            })

        # Idle fee
        if t["idle_rate"] > 0:
            elements.append({
                "price_components": [{
                    "type": "PARKING_TIME",
                    "price": float(t["idle_rate"]),
                    "step_size": 60,
                }]
            })

        result.append({
            "country_code": "NL",
            "party_id": "STM",
            "id": t["id"],
            "currency": t["currency"],
            "elements": elements or [{"price_components": [{"type": "ENERGY", "price": 0, "step_size": 1}]}],
            "last_updated": t["created_at"].isoformat(),
        })

    return ocpi_response(result)
