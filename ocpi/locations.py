"""
OCPI 2.2.1 Locations module — CPO sender interface.

EMSPs pull our charge point locations to display in their apps.
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
async def get_locations(
    date_from: str = Query(None, description="Filter: updated after this ISO timestamp"),
    date_to: str = Query(None, description="Filter: updated before this ISO timestamp"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    token: str = Depends(verify_ocpi_token),
):
    """List all published locations with their EVSEs and connectors."""
    async with db.read() as conn:
        locations = await conn.fetch("""
            SELECT l.id, l.name, l.address, l.city, l.country, 
                   l.coordinates[0] as lat, l.coordinates[1] as lon,
                   l.last_updated
            FROM ocpp.ocpi_locations l
            WHERE l.published = TRUE
            ORDER BY l.last_updated DESC
            OFFSET $1 LIMIT $2
        """, offset, limit)

    result = []
    for loc in locations:
        # Get EVSEs for this location
        async with db.read() as conn:
            charge_points = await conn.fetch("""
                SELECT cp.id, c.connector_id, c.status
                FROM ocpp.charge_points cp
                JOIN ocpp.connectors c ON c.charge_point = cp.id
                WHERE cp.id = ANY(
                    SELECT unnest(charge_points) FROM ocpp.ocpi_locations WHERE id = $1
                )
                AND cp.simulated = FALSE
            """, loc["id"])

        evses = []
        for cp in charge_points:
            evses.append({
                "uid": cp["id"],
                "evse_id": f"{os.getenv('OCPI_COUNTRY_CODE', 'XX')}*{os.getenv('OCPI_PARTY_ID', 'CPO')}*E{cp['id']}*{cp['connector_id']}",
                "status": _map_status(cp["status"]),
                "connectors": [{
                    "id": str(cp["connector_id"]),
                    "standard": "IEC_62196_T2_COMBO",  # Default connector type — override per charge point in DB
                    "format": "CABLE",
                    "power_type": "DC",
                    "max_voltage": 500,
                    "max_amperage": 240,
                    "max_electric_power": 120000,
                    "last_updated": loc["last_updated"].isoformat() if loc["last_updated"] else "",
                }],
                "last_updated": loc["last_updated"].isoformat() if loc["last_updated"] else "",
            })

        result.append({
            "country_code": os.getenv("OCPI_COUNTRY_CODE", "XX"),
            "party_id": os.getenv("OCPI_PARTY_ID", "CPO"),
            "id": loc["id"],
            "publish": True,
            "name": loc["name"],
            "address": loc["address"],
            "city": loc["city"],
            "country": loc["country"] or "NLD",
            "coordinates": {
                "latitude": str(loc["lat"]) if loc["lat"] else "0",
                "longitude": str(loc["lon"]) if loc["lon"] else "0",
            },
            "evses": evses,
            "time_zone": "Europe/Amsterdam",
            "last_updated": loc["last_updated"].isoformat() if loc["last_updated"] else "",
        })

    return ocpi_response(result)


@router.get("/{country_code}/{party_id}/{location_id}")
async def get_location(country_code: str, party_id: str, location_id: str,
                       token: str = Depends(verify_ocpi_token)):
    """Get a specific location by ID."""
    # Reuse the list logic with filter
    async with db.read() as conn:
        loc = await conn.fetchrow(
            "SELECT * FROM ocpp.ocpi_locations WHERE id = $1 AND published = TRUE",
            location_id,
        )

    if not loc:
        return ocpi_response(None, 2003, "Unknown location")

    # Build full location object (simplified — would call get_locations logic)
    return ocpi_response({
        "country_code": os.getenv("OCPI_COUNTRY_CODE", "XX"),
        "party_id": os.getenv("OCPI_PARTY_ID", "CPO"),
        "id": location_id,
        "name": loc["name"],
        "address": loc["address"],
        "city": loc["city"],
        "country": loc["country"],
        "last_updated": loc["last_updated"].isoformat(),
    })


def _map_status(ocpp_status: str) -> str:
    """Map OCPP connector status to OCPI status."""
    mapping = {
        "Available": "AVAILABLE",
        "Occupied": "CHARGING",
        "Charging": "CHARGING",
        "Reserved": "RESERVED",
        "Unavailable": "INOPERATIVE",
        "Faulted": "OUTOFORDER",
        "SuspendedEV": "CHARGING",
        "SuspendedEVSE": "BLOCKED",
        "Preparing": "AVAILABLE",
        "Finishing": "CHARGING",
    }
    return mapping.get(ocpp_status, "UNKNOWN")
