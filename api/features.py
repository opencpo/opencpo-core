"""Feature flag management API."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from state.postgres import db

router = APIRouter(prefix="/api/v1/features", tags=["features"])


class FlagUpdate(BaseModel):
    enabled: Optional[bool] = None
    label: Optional[str] = None
    description: Optional[str] = None


@router.get("")
async def list_flags():
    """Public endpoint — returns all flags as a simple key:bool map + full details."""
    async with db.read() as conn:
        rows = await conn.fetch(
            "SELECT key, enabled, label, description, category, updated_at "
            "FROM ocpp.feature_flags ORDER BY category, key"
        )
    flags = {r["key"]: r["enabled"] for r in rows}
    details = [dict(r) for r in rows]
    for d in details:
        if d.get("updated_at"):
            d["updated_at"] = d["updated_at"].isoformat()
    return {"flags": flags, "details": details}


@router.get("/{key}")
async def get_flag(key: str):
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ocpp.feature_flags WHERE key = $1", key
        )
    if not row:
        raise HTTPException(404, f"Flag '{key}' not found")
    d = dict(row)
    if d.get("updated_at"):
        d["updated_at"] = d["updated_at"].isoformat()
    return d


@router.put("/{key}")
async def update_flag(key: str, req: FlagUpdate):
    sets, params, idx = [], [], 1
    if req.enabled is not None:
        sets.append(f"enabled = ${idx}")
        params.append(req.enabled)
        idx += 1
    if req.label is not None:
        sets.append(f"label = ${idx}")
        params.append(req.label)
        idx += 1
    if req.description is not None:
        sets.append(f"description = ${idx}")
        params.append(req.description)
        idx += 1
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_at = NOW()")
    params.append(key)
    async with db.write() as conn:
        result = await conn.execute(
            f"UPDATE ocpp.feature_flags SET {', '.join(sets)} WHERE key = ${idx}",
            *params,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, f"Flag '{key}' not found")
    return {"key": key, "updated": True}


@router.post("/{key}/toggle")
async def toggle_flag(key: str):
    async with db.write() as conn:
        row = await conn.fetchrow(
            "UPDATE ocpp.feature_flags SET enabled = NOT enabled, updated_at = NOW() "
            "WHERE key = $1 RETURNING key, enabled",
            key,
        )
    if not row:
        raise HTTPException(404, f"Flag '{key}' not found")
    return {"key": row["key"], "enabled": row["enabled"]}
