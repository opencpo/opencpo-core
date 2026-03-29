"""
Tariff management API endpoints.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


class TariffCreate(BaseModel):
    id: str
    name: str
    currency: str = "EUR"
    energy_rate: float = 0       # €/kWh
    time_rate: float = 0         # €/min while charging
    idle_rate: float = 0         # €/min after charging
    flat_fee: float = 0          # € per session


class TariffUpdate(BaseModel):
    name: str | None = None
    energy_rate: float | None = None
    time_rate: float | None = None
    idle_rate: float | None = None
    flat_fee: float | None = None


@router.get("")
async def list_tariffs():
    """List all tariffs."""
    async with db.read() as conn:
        tariffs = await conn.fetch(
            "SELECT * FROM ocpp.tariffs ORDER BY created_at DESC"
        )
    return {"tariffs": [dict(t) for t in tariffs]}


@router.post("")
async def create_tariff(tariff: TariffCreate):
    """Create a new tariff."""
    async with db.write() as conn:
        await conn.execute("""
            INSERT INTO ocpp.tariffs (id, name, currency, energy_rate, time_rate, idle_rate, flat_fee)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, tariff.id, tariff.name, tariff.currency,
            tariff.energy_rate, tariff.time_rate, tariff.idle_rate, tariff.flat_fee)

    logger.info(f"Tariff created: {tariff.id} energy={tariff.energy_rate}€/kWh")
    return {"status": "created", "id": tariff.id}


@router.put("/{tariff_id}")
async def update_tariff(tariff_id: str, update: TariffUpdate):
    """Update a tariff."""
    updates = []
    values = []
    idx = 1

    for field, value in update.model_dump(exclude_none=True).items():
        updates.append(f"{field} = ${idx}")
        values.append(value)
        idx += 1

    if not updates:
        raise HTTPException(400, "No fields to update")

    values.append(tariff_id)

    async with db.write() as conn:
        result = await conn.execute(
            f"UPDATE ocpp.tariffs SET {', '.join(updates)} WHERE id = ${idx}",
            *values,
        )

    if result == "UPDATE 0":
        raise HTTPException(404, f"Tariff {tariff_id} not found")

    return {"status": "updated", "id": tariff_id}


@router.delete("/{tariff_id}")
async def delete_tariff(tariff_id: str):
    """Delete a tariff."""
    async with db.write() as conn:
        result = await conn.execute("DELETE FROM ocpp.tariffs WHERE id = $1", tariff_id)
    if result == "DELETE 0":
        raise HTTPException(404, f"Tariff {tariff_id} not found")
    return {"status": "deleted", "id": tariff_id}
