"""
OCPI 2.2.1 Tariffs module — CPO sender interface.

EMSPs pull our tariffs to display pricing in their apps.

Per-partner roaming markup:
  Each partner record in ocpi_partners.metadata may contain:
    base_tariff_id   — which base tariff to use (if None → return all tariffs)
    roaming_fee_kwh  — extra €/kWh on top of base energy rate
    roaming_fee_flat — extra flat connection fee
    roaming_fee_time — extra €/min time fee

  The endpoint computes base + markup at request time. No duplicate tariff records.
"""
import json as _json
import logging

from fastapi import APIRouter, Depends, Query, Request

from ocpi.main import ocpi_response, verify_ocpi_token
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


async def _partner_markup(token: str) -> dict:
    """Fetch roaming markup config for the partner identified by token."""
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM ocpp.ocpi_partners WHERE token_a = $1 AND status = 'active'",
            token,
        )
    if not row:
        return {}
    meta = row["metadata"] or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    return meta


def _build_tariff_response(t: dict, markup: dict, country_code: str, party_id: str) -> dict:
    """Build an OCPI tariff object from a DB row + partner markup."""
    energy_rate = float(t["energy_rate"] or 0) + float(markup.get("roaming_fee_kwh") or 0)
    time_rate   = float(t["time_rate"]   or 0) + float(markup.get("roaming_fee_time") or 0)
    flat_fee    = float(t["flat_fee"]    or 0) + float(markup.get("roaming_fee_flat") or 0)
    idle_rate   = float(t["idle_rate"]   or 0)

    elements = []
    if energy_rate > 0:
        elements.append({"price_components": [{"type": "ENERGY",       "price": energy_rate, "step_size": 1}]})
    if time_rate > 0:
        elements.append({"price_components": [{"type": "TIME",         "price": time_rate,   "step_size": 60}]})
    if flat_fee > 0:
        elements.append({"price_components": [{"type": "FLAT",         "price": flat_fee,    "step_size": 1}]})
    if idle_rate > 0:
        elements.append({"price_components": [{"type": "PARKING_TIME", "price": idle_rate,   "step_size": 60}]})

    if not elements:
        elements = [{"price_components": [{"type": "ENERGY", "price": 0, "step_size": 1}]}]

    return {
        "country_code": country_code,
        "party_id":     party_id,
        "id":           t["id"],
        "currency":     t["currency"],
        "elements":     elements,
        "last_updated": t["created_at"].isoformat(),
    }


@router.get("")
async def get_tariffs(
    request: Request,
    date_from: str = Query(None),
    date_to: str   = Query(None),
    offset: int    = Query(0, ge=0),
    limit: int     = Query(50, ge=1, le=100),
    token: str     = Depends(verify_ocpi_token),
):
    """
    List tariffs for the requesting partner.

    If partner has a base_tariff_id set: return only that tariff with markup applied.
    Otherwise: return all tariffs (with markup applied to each).
    """
    # Resolve our OCPI identity (country_code / party_id for tariff records)
    try:
        from state.settings import get_setting
        ocpi_cfg = await get_setting("ocpi")
    except Exception:
        ocpi_cfg = {}
    import os
    country_code = ocpi_cfg.get("country_code") or os.getenv("OCPI_COUNTRY_CODE", "NL")
    party_id     = ocpi_cfg.get("party_id")     or os.getenv("OCPI_PARTY_ID",     "OCP")

    markup = await _partner_markup(token)
    base_tariff_id = markup.get("base_tariff_id")

    async with db.read() as conn:
        if base_tariff_id:
            # Partner has a specific tariff assigned
            rows = await conn.fetch(
                """
                SELECT id, name, currency, energy_rate, time_rate, idle_rate, flat_fee,
                       valid_from, valid_until, created_at, metadata
                FROM ocpp.tariffs
                WHERE id = $1
                """,
                base_tariff_id,
            )
        else:
            # Default: all tariffs
            rows = await conn.fetch(
                """
                SELECT id, name, currency, energy_rate, time_rate, idle_rate, flat_fee,
                       valid_from, valid_until, created_at, metadata
                FROM ocpp.tariffs
                ORDER BY created_at DESC
                OFFSET $1 LIMIT $2
                """,
                offset,
                limit,
            )

    result = [_build_tariff_response(dict(t), markup, country_code, party_id) for t in rows]
    return ocpi_response(result)
