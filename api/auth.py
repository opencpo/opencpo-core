"""
Authorization management API — OCPP token CRUD.

Canonical token management endpoints. The status mapping (Accepted↔active,
Blocked↔blocked) preserves OCPP 1.6j naming conventions for external callers.
"""
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


class TokenCreate(BaseModel):
    token: str
    type: str = "rfid"
    status: str = "Accepted"
    display_name: str = ""
    group_id: str = ""


class TokenUpdate(BaseModel):
    status: str | None = None
    display_name: str | None = None
    group_id: str | None = None


@router.get("")
async def list_tokens(
    type: str = Query(None),
    status: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List tokens (backwards-compat: maps ocpp.tokens → old format)."""
    conditions = ["1=1"]
    params = []
    idx = 1

    if type:
        conditions.append(f"type = ${idx}")
        params.append(type)
        idx += 1
    if status:
        # Map old 'Accepted'/'Blocked' to new 'active'/'blocked'
        mapped = {"Accepted": "active", "Blocked": "blocked"}.get(status, status.lower())
        conditions.append(f"status = ${idx}")
        params.append(mapped)
        idx += 1

    params.extend([offset, limit])

    async with db.read() as conn:
        rows = await conn.fetch(f"""
            SELECT uid AS token, type,
                   CASE status
                       WHEN 'active'  THEN 'Accepted'
                       WHEN 'blocked' THEN 'Blocked'
                       ELSE initcap(status)
                   END AS status,
                   coalesce(driver_name, '') AS display_name,
                   coalesce(group_id::text, '') AS group_id,
                   valid_from, valid_until, created_at
            FROM ocpp.tokens
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
            OFFSET ${idx} LIMIT ${idx + 1}
        """, *params)

    return {"tokens": [dict(t) for t in rows]}


@router.post("")
async def create_token(token: TokenCreate):
    """Register a token (backwards-compat wrapper over ocpp.tokens)."""
    mapped_status = {"Accepted": "active", "Blocked": "blocked"}.get(token.status, token.status.lower())
    async with db.write() as conn:
        await conn.execute("""
            INSERT INTO ocpp.tokens (uid, type, status, driver_name, group_id)
            VALUES ($1, $2, $3, $4, NULLIF($5,'')::uuid)
            ON CONFLICT (uid) DO UPDATE SET
                type=$2, status=$3, driver_name=$4, updated_at=NOW()
        """, token.token, token.type, mapped_status, token.display_name or None,
            token.group_id or "")
    return {"status": "created", "token": token.token}


@router.put("/{token_id}")
async def update_token(token_id: str, update: TokenUpdate):
    """Update a token (backwards-compat)."""
    updates = []
    values = []
    idx = 1

    if update.status is not None:
        mapped = {"Accepted": "active", "Blocked": "blocked"}.get(update.status, update.status.lower())
        updates.append(f"status = ${idx}")
        values.append(mapped)
        idx += 1
    if update.display_name is not None:
        updates.append(f"driver_name = ${idx}")
        values.append(update.display_name)
        idx += 1
    if update.group_id is not None:
        updates.append(f"group_id = NULLIF(${idx},'')::uuid")
        values.append(update.group_id)
        idx += 1

    if not updates:
        raise HTTPException(400, "No fields to update")

    values.append(token_id)
    async with db.write() as conn:
        result = await conn.execute(
            f"UPDATE ocpp.tokens SET {', '.join(updates)}, updated_at=NOW() WHERE uid = ${idx}",
            *values,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, f"Token {token_id} not found")
    return {"status": "updated", "token": token_id}


@router.delete("/{token_id}")
async def delete_token(token_id: str):
    """Soft-revoke a token (backwards-compat)."""
    async with db.write() as conn:
        result = await conn.execute(
            "UPDATE ocpp.tokens SET status='revoked', updated_at=NOW() WHERE uid = $1",
            token_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, f"Token {token_id} not found")
    return {"status": "deleted", "token": token_id}
