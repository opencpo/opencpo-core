"""
Token Group management API — billing groups for RFID tokens.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


async def _resolve_group_id(group_id: str) -> str:
    """Resolve a group identifier — accepts UUID or name (case-insensitive)."""
    try:
        import uuid
        uuid.UUID(group_id)
        return group_id  # Already a valid UUID
    except ValueError:
        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT id::text FROM ocpp.token_groups WHERE LOWER(name) = LOWER($1)",
                group_id,
            )
        if not row:
            raise HTTPException(404, f"Group '{group_id}' not found")
        return row["id"]


# ── Models ───────────────────────────────────────────────────────────────────

class GroupCreate(BaseModel):
    name: str
    billing_email: Optional[str] = None
    billing_address: Optional[str] = None
    billing_reference: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    billing_email: Optional[str] = None
    billing_address: Optional[str] = None
    billing_reference: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row(r) -> dict:
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
async def list_groups():
    """List all groups with token counts and monthly usage."""
    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT
                g.*,
                count(t.id)                                       AS token_count,
                count(t.id) FILTER (WHERE t.status = 'active')   AS active_count,
                coalesce(sum(s.energy_kwh), 0)                    AS month_kwh,
                coalesce(sum(s.energy_kwh * cp.tariff_kwh), 0)   AS month_cost
            FROM ocpp.token_groups g
            LEFT JOIN ocpp.tokens t ON t.group_id = g.id
            LEFT JOIN ocpp.sessions s ON s.auth_id = t.uid
                AND s.start_time >= date_trunc('month', NOW())
                AND s.status = 'completed'
            LEFT JOIN ocpp.charge_points cp ON cp.id = s.charge_point
            GROUP BY g.id
            ORDER BY g.name
        """)
    return {"groups": [_row(r) for r in rows], "total": len(rows)}


@router.post("")
async def create_group(body: GroupCreate):
    """Create a new token group."""
    async with db.write() as conn:
        row = await conn.fetchrow("""
            INSERT INTO ocpp.token_groups
                (name, billing_email, billing_address, billing_reference,
                 contact_name, contact_phone, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, name
        """,
            body.name, body.billing_email, body.billing_address, body.billing_reference,
            body.contact_name, body.contact_phone, body.notes,
        )
    return {"id": str(row["id"]), "name": row["name"]}


@router.get("/{group_id}")
async def get_group(group_id: str):
    """Get group detail with all tokens and monthly usage summary."""
    group_id = await _resolve_group_id(group_id)
    async with db.read() as conn:
        g = await conn.fetchrow(
            "SELECT * FROM ocpp.token_groups WHERE id = $1::uuid", group_id
        )
        if not g:
            raise HTTPException(404, f"Group {group_id} not found")

        tokens = await conn.fetch("""
            SELECT t.*,
                   (SELECT max(s.start_time) FROM ocpp.sessions s WHERE s.auth_id = t.uid) AS last_used,
                   (SELECT count(*) FROM ocpp.sessions s WHERE s.auth_id = t.uid) AS session_count,
                   (SELECT coalesce(sum(s.energy_kwh),0) FROM ocpp.sessions s WHERE s.auth_id = t.uid) AS total_kwh
            FROM ocpp.tokens t
            WHERE t.group_id = $1::uuid
            ORDER BY t.driver_name, t.uid
        """, group_id)

        # Monthly summary (last 6 months)
        monthly = await conn.fetch("""
            SELECT
                to_char(date_trunc('month', s.start_time), 'YYYY-MM') AS month,
                count(s.id) AS sessions,
                coalesce(sum(s.energy_kwh), 0) AS kwh,
                coalesce(sum(s.energy_kwh * cp.tariff_kwh), 0) AS cost
            FROM ocpp.sessions s
            JOIN ocpp.tokens t ON t.uid = s.auth_id AND t.group_id = $1::uuid
            JOIN ocpp.charge_points cp ON cp.id = s.charge_point
            WHERE s.status = 'completed'
              AND s.start_time >= NOW() - INTERVAL '6 months'
            GROUP BY 1
            ORDER BY 1 DESC
        """, group_id)

    return {
        "group": _row(g),
        "tokens": [_row(t) for t in tokens],
        "monthly_summary": [_row(m) for m in monthly],
    }


@router.put("/{group_id}")
async def update_group(group_id: str, body: GroupUpdate):
    """Update group details."""
    group_id = await _resolve_group_id(group_id)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "No fields to update")

    sets = []
    vals = []
    idx = 1
    for k, v in fields.items():
        sets.append(f"{k} = ${idx}")
        vals.append(v)
        idx += 1

    vals.append(group_id)
    async with db.write() as conn:
        result = await conn.execute(
            f"UPDATE ocpp.token_groups SET {', '.join(sets)}, updated_at=NOW() WHERE id=${idx}::uuid",
            *vals,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, f"Group {group_id} not found")
    return {"status": "updated", "id": group_id}


@router.delete("/{group_id}")
async def delete_group(group_id: str):
    """Delete group — only if no active tokens."""
    group_id = await _resolve_group_id(group_id)
    async with db.write() as conn:
        active = await conn.fetchval(
            "SELECT count(*) FROM ocpp.tokens WHERE group_id=$1::uuid AND status='active'",
            group_id,
        )
        if active > 0:
            raise HTTPException(409, f"Cannot delete group with {active} active token(s)")
        result = await conn.execute(
            "DELETE FROM ocpp.token_groups WHERE id=$1::uuid", group_id
        )
    if result == "DELETE 0":
        raise HTTPException(404, f"Group {group_id} not found")
    return {"status": "deleted", "id": group_id}


@router.get("/{group_id}/usage")
async def get_group_usage(group_id: str, month: str = Query(None)):
    """Per-card usage breakdown for a given month (YYYY-MM, defaults to current)."""
    group_id = await _resolve_group_id(group_id)
    if not month:
        month = datetime.now().strftime("%Y-%m")
    try:
        month_start = datetime.strptime(month + "-01", "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "month must be YYYY-MM")

    async with db.read() as conn:
        g = await conn.fetchrow(
            "SELECT id, name FROM ocpp.token_groups WHERE id=$1::uuid", group_id
        )
        if not g:
            raise HTTPException(404, f"Group {group_id} not found")

        rows = await conn.fetch("""
            SELECT
                t.uid,
                t.driver_name,
                t.label,
                count(s.id) AS sessions,
                coalesce(sum(s.energy_kwh), 0) AS kwh,
                coalesce(sum(s.energy_kwh * cp.tariff_kwh), 0) AS cost
            FROM ocpp.tokens t
            LEFT JOIN ocpp.sessions s ON s.auth_id = t.uid
                AND date_trunc('month', s.start_time) = date_trunc('month', $2::timestamptz)
                AND s.status = 'completed'
            LEFT JOIN ocpp.charge_points cp ON cp.id = s.charge_point
            WHERE t.group_id = $1::uuid
            GROUP BY t.uid, t.driver_name, t.label
            ORDER BY t.driver_name, t.uid
        """, group_id, month_start)

    return {
        "group_id": group_id,
        "group_name": g["name"],
        "month": month,
        "cards": [_row(r) for r in rows],
        "total_kwh": sum(float(r["kwh"]) for r in rows),
        "total_cost": sum(float(r["cost"]) for r in rows),
        "total_sessions": sum(int(r["sessions"]) for r in rows),
    }
