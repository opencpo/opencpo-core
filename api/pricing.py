"""
Dynamic pricing API — spot price + cost basis + tier margins.

Formula:
    rate_excl_tax = spot_price + cost_basis + tier_margin
    rate_incl_tax = rate_excl_tax * (1 + tax_rate)

Spot price is cached 60 seconds to avoid hammering the source API.
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.api_key_auth import management_auth
from state.postgres import db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Pricing"])

# ── Spot price cache (module-level, 60-second TTL) ──────────────────────
_spot_cache: dict[str, Any] = {"price": None, "ts": 0.0}
_SPOT_CACHE_TTL = 60  # seconds

# Spot price source URL (configure via env; defaults to disabled/manual)
SPOT_PRICE_URL = os.getenv("SPOT_PRICE_URL", "")


# ── Helpers ──────────────────────────────────────────────────────────────

async def get_spot_price() -> float:
    """
    Fetch current spot price (€/kWh).

    Priority:
      1. External spot price API (SPOT_PRICE_URL env) — cached 60s
      2. spot_manual value from pricing_config (fallback)
    """
    now = time.monotonic()
    if _spot_cache["price"] is not None and (now - _spot_cache["ts"]) < _SPOT_CACHE_TTL:
        return _spot_cache["price"]

    # Check configured source
    async with db.read() as conn:
        source_row = await conn.fetchrow(
            "SELECT value FROM ocpp.pricing_config WHERE key = 'spot_source'"
        )
        manual_row = await conn.fetchrow(
            "SELECT value FROM ocpp.pricing_config WHERE key = 'spot_manual'"
        )
    source = int(source_row["value"]) if source_row else 0
    manual_price = float(manual_row["value"]) if manual_row else 0.10

    if source == 2:
        # Manual override
        _spot_cache["price"] = manual_price
        _spot_cache["ts"] = now
        return manual_price

    # source == 0 (external API) or source == 1 (future ENTSO-E)
    if SPOT_PRICE_URL:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(SPOT_PRICE_URL)
                resp.raise_for_status()
                data = resp.json()
            price = float(data["spot_price_eur"])
            _spot_cache["price"] = price
            _spot_cache["ts"] = now
            logger.debug("Spot price from API: €%.4f/kWh", price)
            return price
        except Exception as exc:
            logger.warning("Spot price fetch failed (%s), using manual fallback: €%.4f", exc, manual_price)

    # Fallback: manual price (cache briefly to avoid hammering on outage)
    _spot_cache["price"] = manual_price
    _spot_cache["ts"] = now - (_SPOT_CACHE_TTL - 10)
    return manual_price


async def get_cost_basis() -> tuple[float, dict[str, float]]:
    """
    Return (total_cost_basis, components_dict).
    Sums all pricing_config rows except meta keys (tax_rate, spot_source, spot_manual).
    """
    _meta_keys = {"tax_rate", "btw_rate", "spot_source", "spot_manual"}
    async with db.read() as conn:
        rows = await conn.fetch("SELECT key, value FROM ocpp.pricing_config")
    components: dict[str, float] = {}
    for row in rows:
        if row["key"] not in _meta_keys:
            components[row["key"]] = float(row["value"])
    total = sum(components.values())
    return total, components


async def get_tax_rate() -> float:
    """Fetch tax rate from config (falls back to btw_rate for backward compat)."""
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM ocpp.pricing_config WHERE key IN ('tax_rate', 'btw_rate') ORDER BY key LIMIT 1"
        )
    return float(row["value"]) if row else 0.21


async def get_tier_rate(tier_id: str) -> dict:
    """
    Calculate price for a single tier.
    Returns {"margin", "rate_excl", "rate_incl"}.
    """
    async with db.read() as conn:
        tier = await conn.fetchrow(
            "SELECT id, name, margin_kwh FROM ocpp.pricing_tiers WHERE id = $1", tier_id
        )
    if not tier:
        raise HTTPException(404, f"Pricing tier '{tier_id}' not found")

    spot = await get_spot_price()
    cost_basis, _ = await get_cost_basis()
    tax = await get_tax_rate()
    margin = float(tier["margin_kwh"])
    rate_excl = round(spot + cost_basis + margin, 4)
    rate_incl = round(rate_excl * (1 + tax), 4)
    return {
        "tier_id": tier_id,
        "name": tier["name"],
        "margin": margin,
        "rate_excl": rate_excl,
        "rate_incl": rate_incl,
    }


async def get_all_tier_rates() -> dict:
    """Fetch all tiers and compute rates. Returns full pricing snapshot."""
    spot = await get_spot_price()
    cost_basis, components = await get_cost_basis()
    tax = await get_tax_rate()

    async with db.read() as conn:
        tiers = await conn.fetch("SELECT id, name, margin_kwh FROM ocpp.pricing_tiers ORDER BY id")

    tier_rates: dict[str, dict] = {}
    for t in tiers:
        margin = float(t["margin_kwh"])
        rate_excl = round(spot + cost_basis + margin, 4)
        rate_incl = round(rate_excl * (1 + tax), 4)
        tier_rates[t["id"]] = {
            "name": t["name"],
            "margin": margin,
            "rate_excl": rate_excl,
            "rate_incl": rate_incl,
        }

    return {
        "spot_price": spot,
        "cost_basis": round(cost_basis, 4),
        "tax_rate": tax,
        "tiers": tier_rates,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Public endpoints ─────────────────────────────────────────────────────

@router.get("/api/v1/pricing/current")
async def pricing_current():
    """
    Public — returns current spot price + all tier rates.
    Used by charge app to display price before/during session.
    """
    return await get_all_tier_rates()


# ── Management endpoints ─────────────────────────────────────────────────

_mgmt = [Depends(management_auth)]


class ConfigUpdate(BaseModel):
    updates: dict[str, float]  # {key: new_value}


class TierUpdate(BaseModel):
    margin_kwh: float | None = None
    name: str | None = None
    description: str | None = None


class TierCreate(BaseModel):
    id: str
    name: str
    margin_kwh: float = 0.0
    description: str | None = None


@router.get("/api/v1/pricing/config", dependencies=_mgmt)
async def pricing_config():
    """Management — returns all cost components and tier definitions."""
    async with db.read() as conn:
        config_rows = await conn.fetch(
            "SELECT key, value, description, updated_at, updated_by FROM ocpp.pricing_config ORDER BY key"
        )
        tier_rows = await conn.fetch(
            "SELECT id, name, margin_kwh, description, updated_at FROM ocpp.pricing_tiers ORDER BY id"
        )
    spot = await get_spot_price()
    cost_basis, _ = await get_cost_basis()
    tax = await get_tax_rate()

    return {
        "components": [
            {
                "key": r["key"],
                "value": float(r["value"]),
                "description": r["description"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "updated_by": r["updated_by"],
            }
            for r in config_rows
        ],
        "tiers": [
            {
                "id": r["id"],
                "name": r["name"],
                "margin_kwh": float(r["margin_kwh"]),
                "description": r["description"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "rate_excl": round(spot + cost_basis + float(r["margin_kwh"]), 4),
                "rate_incl": round((spot + cost_basis + float(r["margin_kwh"])) * (1 + tax), 4),
            }
            for r in tier_rows
        ],
        "spot_price": spot,
        "cost_basis": round(cost_basis, 4),
    }


@router.put("/api/v1/pricing/config", dependencies=_mgmt)
async def update_pricing_config(body: ConfigUpdate):
    """Management — update one or more cost components."""
    if not body.updates:
        raise HTTPException(400, "No updates provided")

    updated = []
    async with db.write() as conn:
        for key, value in body.updates.items():
            result = await conn.execute("""
                UPDATE ocpp.pricing_config
                   SET value = $2, updated_at = NOW(), updated_by = 'admin'
                 WHERE key = $1
            """, key, value)
            if result == "UPDATE 0":
                raise HTTPException(404, f"Config key '{key}' not found")
            updated.append(key)

    # Bust spot cache if spot_source or spot_manual changed
    if "spot_source" in updated or "spot_manual" in updated:
        _spot_cache["ts"] = 0.0

    logger.info("Pricing config updated: %s", updated)
    return {"status": "updated", "keys": updated}


@router.put("/api/v1/pricing/tiers/{tier_id}", dependencies=_mgmt)
async def update_pricing_tier(tier_id: str, body: TierUpdate):
    """Management — update a pricing tier's margin and/or name."""
    updates = []
    values: list = []
    idx = 1

    if body.margin_kwh is not None:
        updates.append(f"margin_kwh = ${idx}")
        values.append(body.margin_kwh)
        idx += 1
    if body.name is not None:
        updates.append(f"name = ${idx}")
        values.append(body.name)
        idx += 1
    if body.description is not None:
        updates.append(f"description = ${idx}")
        values.append(body.description)
        idx += 1

    if not updates:
        raise HTTPException(400, "No fields to update")

    updates.append(f"updated_at = ${idx}")
    values.append(datetime.now(timezone.utc))
    idx += 1
    values.append(tier_id)

    async with db.write() as conn:
        result = await conn.execute(
            f"UPDATE ocpp.pricing_tiers SET {', '.join(updates)} WHERE id = ${idx}",
            *values,
        )

    if result == "UPDATE 0":
        raise HTTPException(404, f"Tier '{tier_id}' not found")

    logger.info("Pricing tier '%s' updated", tier_id)
    return {"status": "updated", "id": tier_id}


@router.post("/api/v1/pricing/tiers", dependencies=_mgmt)
async def create_pricing_tier(body: TierCreate):
    """Management — create a new pricing tier."""
    async with db.write() as conn:
        try:
            await conn.execute("""
                INSERT INTO ocpp.pricing_tiers (id, name, margin_kwh, description, updated_at)
                VALUES ($1, $2, $3, $4, NOW())
            """, body.id, body.name, body.margin_kwh, body.description)
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise HTTPException(409, f"Tier '{body.id}' already exists")
            raise HTTPException(500, f"DB error: {exc}")

    logger.info("Pricing tier '%s' created (margin=%.4f)", body.id, body.margin_kwh)
    return {"status": "created", "id": body.id}
