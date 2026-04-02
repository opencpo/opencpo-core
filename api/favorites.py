"""
Driver favorites API — save/unsync chargers for quick access.
Endpoints under /api/v1/public/account/favorites (JWT required).

Table (auto-created on first use):
  ocpp.driver_favorites (driver_account_id, charge_point_id, created_at)
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Request

from state.postgres import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public/account/favorites", tags=["favorites"])

JWT_SECRET = os.environ.get("JWT_SECRET", "stroomlijnen-jwt-2026")
JWT_ALGO = "HS256"

# ── DB setup ──────────────────────────────────────────────────────────────

_TABLE_CREATED = False

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ocpp.driver_favorites (
    driver_account_id UUID REFERENCES ocpp.driver_accounts(id) ON DELETE CASCADE,
    charge_point_id   TEXT NOT NULL,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (driver_account_id, charge_point_id)
);
"""


async def _ensure_table():
    global _TABLE_CREATED
    if _TABLE_CREATED:
        return
    async with db.write() as conn:
        await conn.execute(CREATE_TABLE_SQL)
    _TABLE_CREATED = True


# ── Auth helper ───────────────────────────────────────────────────────────

async def _require_account(request: Request) -> dict:
    """Extract and verify JWT from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Niet ingelogd")
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Sessie verlopen — log opnieuw in")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Ongeldige sessie")


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("")
async def list_favorites(request: Request):
    """Return saved charger IDs for the logged-in driver."""
    account = await _require_account(request)
    await _ensure_table()

    rows = await db.fetch(
        """
        SELECT charge_point_id, created_at
        FROM ocpp.driver_favorites
        WHERE driver_account_id = $1
        ORDER BY created_at DESC
        """,
        account["sub"],
    )

    return {
        "favorites": [
            {"cp_id": r["charge_point_id"], "saved_at": r["created_at"].isoformat()}
            for r in rows
        ]
    }


@router.post("/{cp_id}")
async def save_favorite(cp_id: str, request: Request):
    """Save a charger to favorites. Idempotent."""
    account = await _require_account(request)
    await _ensure_table()

    async with db.write() as conn:
        await conn.execute(
            """
            INSERT INTO ocpp.driver_favorites (driver_account_id, charge_point_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            account["sub"],
            cp_id,
        )

    return {"ok": True, "cp_id": cp_id}


@router.delete("/{cp_id}")
async def delete_favorite(cp_id: str, request: Request):
    """Remove a charger from favorites."""
    account = await _require_account(request)
    await _ensure_table()

    async with db.write() as conn:
        result = await conn.execute(
            """
            DELETE FROM ocpp.driver_favorites
            WHERE driver_account_id = $1 AND charge_point_id = $2
            """,
            account["sub"],
            cp_id,
        )

    deleted = result != "DELETE 0"
    return {"ok": True, "cp_id": cp_id, "deleted": deleted}
