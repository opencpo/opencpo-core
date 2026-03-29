"""
Token management API — RFID cards and access tokens with full lifecycle.

Replaces auth.py with a richer model: groups, events, lifecycle tracking.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Models ───────────────────────────────────────────────────────────────────

class TokenCreate(BaseModel):
    uid: str
    type: str = "rfid"
    status: str = "active"
    group_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_email: Optional[str] = None
    driver_phone: Optional[str] = None
    label: Optional[str] = None
    card_number: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None


class TokenUpdate(BaseModel):
    group_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_email: Optional[str] = None
    driver_phone: Optional[str] = None
    label: Optional[str] = None
    card_number: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None


class BlockRequest(BaseModel):
    reason: Optional[str] = None


class ReplaceRequest(BaseModel):
    new_uid: str
    driver_name: Optional[str] = None
    label: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _log_event(conn, token_id: str, event: str, details: str = None, actor: str = "api"):
    await conn.execute(
        "INSERT INTO ocpp.token_events (token_id, event, details, actor) VALUES ($1, $2, $3, $4)",
        token_id, event, details, actor,
    )


def _token_dict(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
async def list_tokens(
    group_id: str = Query(None),
    status: str = Query(None),
    type: str = Query(None),
    search: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """List tokens with filters."""
    conditions = ["1=1"]
    params = []
    idx = 1

    if group_id:
        conditions.append(f"t.group_id = ${idx}::uuid")
        params.append(group_id)
        idx += 1
    if status:
        conditions.append(f"t.status = ${idx}")
        params.append(status)
        idx += 1
    if type:
        conditions.append(f"t.type = ${idx}")
        params.append(type)
        idx += 1
    if search:
        conditions.append(f"(t.uid ILIKE ${idx} OR t.driver_name ILIKE ${idx} OR t.label ILIKE ${idx})")
        params.append(f"%{search}%")
        idx += 1

    where = " AND ".join(conditions)
    params.extend([offset, limit])

    async with db.read() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM ocpp.tokens t WHERE {where}",
            *params[:-2],
        )
        rows = await conn.fetch(f"""
            SELECT t.*, g.name AS group_name,
                   (SELECT max(s.start_time) FROM ocpp.sessions s WHERE s.auth_id = t.uid) AS last_used
            FROM ocpp.tokens t
            LEFT JOIN ocpp.token_groups g ON g.id = t.group_id
            WHERE {where}
            ORDER BY t.created_at DESC
            OFFSET ${idx} LIMIT ${idx+1}
        """, *params)

    return {
        "tokens": [_token_dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("")
async def create_token(token: TokenCreate):
    """Create a new token and log the event."""
    async with db.write() as conn:
        row = await conn.fetchrow("""
            INSERT INTO ocpp.tokens
                (uid, type, status, group_id, driver_name, driver_email, driver_phone,
                 label, card_number, valid_from, valid_until,
                 activated_at)
            VALUES ($1, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10, $11,
                    CASE WHEN $3 = 'active' THEN NOW() ELSE NULL END)
            RETURNING id, uid, status
        """,
            token.uid, token.type, token.status,
            token.group_id, token.driver_name, token.driver_email, token.driver_phone,
            token.label, token.card_number, token.valid_from, token.valid_until,
        )
        await _log_event(conn, str(row["id"]), "created",
                         f"type={token.type} status={token.status}", "api")

    logger.info(f"Token created: {token.uid}")
    return {"id": str(row["id"]), "uid": row["uid"], "status": row["status"]}


@router.get("/{token_id}")
async def get_token(token_id: str):
    """Get token detail with recent sessions and events."""
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT t.*, g.name AS group_name
            FROM ocpp.tokens t
            LEFT JOIN ocpp.token_groups g ON g.id = t.group_id
            WHERE t.id = $1::uuid
        """, token_id)

        if not row:
            raise HTTPException(404, f"Token {token_id} not found")

        uid = row["uid"]
        sessions = await conn.fetch("""
            SELECT id, charge_point, connector_id, start_time, stop_time,
                   energy_kwh, status, stop_reason
            FROM ocpp.sessions
            WHERE auth_id = $1
            ORDER BY start_time DESC
            LIMIT 20
        """, uid)

        events = await conn.fetch("""
            SELECT id, event, details, actor, created_at
            FROM ocpp.token_events
            WHERE token_id = $1::uuid
            ORDER BY created_at DESC
            LIMIT 50
        """, token_id)

    return {
        "token": _token_dict(row),
        "sessions": [_token_dict(s) for s in sessions],
        "events": [_token_dict(e) for e in events],
    }


@router.put("/{token_id}")
async def update_token(token_id: str, update: TokenUpdate):
    """Update token details (not status — use lifecycle endpoints)."""
    fields = update.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "No fields to update")

    sets = []
    vals = []
    idx = 1
    for k, v in fields.items():
        sets.append(f"{k} = ${idx}")
        vals.append(v)
        idx += 1

    vals.extend([token_id])
    async with db.write() as conn:
        result = await conn.execute(
            f"UPDATE ocpp.tokens SET {', '.join(sets)}, updated_at=NOW() WHERE id = ${idx}::uuid",
            *vals,
        )
        if result == "UPDATE 0":
            raise HTTPException(404, f"Token {token_id} not found")
        row = await conn.fetchrow("SELECT id FROM ocpp.tokens WHERE id = $1::uuid", token_id)
        await _log_event(conn, token_id, "updated", f"fields: {', '.join(fields.keys())}", "api")

    return {"status": "updated", "id": token_id}


@router.post("/{token_id}/activate")
async def activate_token(token_id: str):
    """Activate a token (ordered/shipped → active)."""
    async with db.write() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM ocpp.tokens WHERE id = $1::uuid", token_id
        )
        if not row:
            raise HTTPException(404, f"Token {token_id} not found")
        await conn.execute(
            "UPDATE ocpp.tokens SET status='active', activated_at=NOW(), updated_at=NOW() WHERE id=$1::uuid",
            token_id,
        )
        await _log_event(conn, token_id, "activated", f"previous status: {row['status']}", "api")

    return {"status": "activated", "id": token_id}


@router.post("/{token_id}/block")
async def block_token(token_id: str, body: BlockRequest = None):
    """Block a token with optional reason."""
    reason = (body.reason if body else None) or "manual block"
    async with db.write() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM ocpp.tokens WHERE id = $1::uuid", token_id
        )
        if not row:
            raise HTTPException(404, f"Token {token_id} not found")
        await conn.execute(
            "UPDATE ocpp.tokens SET status='blocked', blocked_at=NOW(), blocked_reason=$2, updated_at=NOW() WHERE id=$1::uuid",
            token_id, reason,
        )
        await _log_event(conn, token_id, "blocked", reason, "api")

    return {"status": "blocked", "id": token_id}


@router.post("/{token_id}/unblock")
async def unblock_token(token_id: str):
    """Unblock a token → active."""
    async with db.write() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM ocpp.tokens WHERE id = $1::uuid", token_id
        )
        if not row:
            raise HTTPException(404, f"Token {token_id} not found")
        await conn.execute(
            "UPDATE ocpp.tokens SET status='active', blocked_reason=NULL, updated_at=NOW() WHERE id=$1::uuid",
            token_id,
        )
        await _log_event(conn, token_id, "unblocked", "manually unblocked", "api")

    return {"status": "active", "id": token_id}


@router.post("/{token_id}/replace")
async def replace_token(token_id: str, body: ReplaceRequest):
    """Create replacement token, link old→new, block old."""
    async with db.write() as conn:
        old = await conn.fetchrow(
            "SELECT * FROM ocpp.tokens WHERE id = $1::uuid", token_id
        )
        if not old:
            raise HTTPException(404, f"Token {token_id} not found")

        new_row = await conn.fetchrow("""
            INSERT INTO ocpp.tokens
                (uid, type, status, group_id, driver_name, driver_email, driver_phone,
                 label, replaces_id, activated_at)
            VALUES ($1, $2, 'active', $3, $4, $5, $6, $7, $8::uuid, NOW())
            RETURNING id, uid
        """,
            body.new_uid, old["type"], old["group_id"],
            body.driver_name or old["driver_name"],
            old["driver_email"], old["driver_phone"],
            body.label or old["label"],
            token_id,
        )
        new_id = str(new_row["id"])

        # Block old, link to new
        await conn.execute("""
            UPDATE ocpp.tokens
            SET status='blocked', blocked_at=NOW(),
                blocked_reason='replaced by ' || $2,
                replaced_by_id=$3::uuid, updated_at=NOW()
            WHERE id=$1::uuid
        """, token_id, body.new_uid, new_id)

        await _log_event(conn, token_id, "replaced", f"replaced by {body.new_uid}", "api")
        await _log_event(conn, new_id, "created", f"replaces token id={token_id}", "api")

    return {"status": "replaced", "old_id": token_id, "new_id": new_id, "new_uid": body.new_uid}


@router.delete("/{token_id}")
async def revoke_token(token_id: str):
    """Revoke (soft delete) a token."""
    async with db.write() as conn:
        row = await conn.fetchrow(
            "SELECT uid FROM ocpp.tokens WHERE id = $1::uuid", token_id
        )
        if not row:
            raise HTTPException(404, f"Token {token_id} not found")
        await conn.execute(
            "UPDATE ocpp.tokens SET status='revoked', updated_at=NOW() WHERE id=$1::uuid",
            token_id,
        )
        await _log_event(conn, token_id, "revoked", "soft deleted via API", "api")

    return {"status": "revoked", "id": token_id}


@router.post("/purge-test")
async def purge_test_tokens(request: Request):
    """Bulk delete test tokens by UID prefix patterns."""
    body = await request.json()
    prefixes = body.get("prefixes", ["STRESS-%", "SIM-%", "VAL-%", "CHAOS-%", "E2E-%"])

    async with db.write() as conn:
        # Build OR conditions for each prefix
        conditions = " OR ".join(f"uid LIKE ${i+1}" for i in range(len(prefixes)))
        # Delete events first (FK cascade), then tokens
        ids = await conn.fetch(
            f"SELECT id FROM ocpp.tokens WHERE {conditions}", *prefixes
        )
        token_ids = [r["id"] for r in ids]
        if token_ids:
            await conn.execute(
                "DELETE FROM ocpp.token_events WHERE token_id = ANY($1::uuid[])", token_ids
            )
            await conn.execute(
                f"DELETE FROM ocpp.tokens WHERE {conditions}", *prefixes
            )

    logger.info(f"Purged {len(token_ids)} test tokens")
    return {"deleted": len(token_ids)}


@router.get("/{token_id}/events")
async def get_token_events(token_id: str, limit: int = Query(100, le=500)):
    """Get audit trail for a token."""
    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT id, event, details, actor, created_at
            FROM ocpp.token_events
            WHERE token_id = $1::uuid
            ORDER BY created_at DESC
            LIMIT $2
        """, token_id, limit)
    return {"events": [_token_dict(r) for r in rows]}


@router.get("/{token_id}/sessions")
async def get_token_sessions(token_id: str, limit: int = Query(50, le=200)):
    """Get all sessions for a token."""
    async with db.read() as conn:
        token = await conn.fetchrow(
            "SELECT uid FROM ocpp.tokens WHERE id = $1::uuid", token_id
        )
        if not token:
            raise HTTPException(404, f"Token {token_id} not found")
        rows = await conn.fetch("""
            SELECT id, charge_point, connector_id, start_time, stop_time,
                   energy_kwh, status, stop_reason, auth_method
            FROM ocpp.sessions
            WHERE auth_id = $1
            ORDER BY start_time DESC
            LIMIT $2
        """, token["uid"], limit)
    return {"sessions": [_token_dict(r) for r in rows], "uid": token["uid"]}
