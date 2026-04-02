"""
Driver account API — registration, login, profile, charging history.
All account logic lives here; the charge app is a thin proxy.

Endpoints (all under /api/v1/public/account):
  POST /register  — create account
  POST /login     — email + password → JWT
  GET  /profile   — get own profile (JWT required)
  PUT  /profile   — update profile (JWT required)
  GET  /sessions  — charging history (JWT required)
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

from state.postgres import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public/account", tags=["accounts"])

# ── JWT helpers ───────────────────────────────────────────────────────────

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production")
JWT_ALGO = "HS256"
JWT_TTL_DAYS = 30


def create_token(account_id: str, email: str) -> str:
    return jwt.encode(
        {
            "sub": account_id,
            "email": email,
            "exp": datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS),
        },
        JWT_SECRET,
        algorithm=JWT_ALGO,
    )


def verify_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])


async def get_current_account(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    try:
        return verify_token(auth[7:])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid session token")


# ── Pydantic models ───────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    phone: Optional[str] = None
    language: Optional[str] = "en"


class LoginRequest(BaseModel):
    email: str
    password: str


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    language: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/register")
async def register(req: RegisterRequest):
    """Create a new driver account."""
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")
    if not req.password or len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    # Hash password
    pw_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()

    async with db.write() as conn:
        # Check for duplicate email
        existing = await conn.fetchrow(
            "SELECT id FROM ocpp.driver_accounts WHERE email = $1", email
        )
        if existing:
            raise HTTPException(409, "This email address is already in use")

        row = await conn.fetchrow("""
            INSERT INTO ocpp.driver_accounts
                (email, phone, password_hash, name, language)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id::text, email, phone, name, language, created_at
        """, email, req.phone, pw_hash, req.name, req.language or "en")

    account_id = row["id"]

    # Retroactively link sessions by phone number
    if req.phone:
        phone = req.phone.strip()
        async with db.write() as conn:
            updated = await conn.execute("""
                UPDATE ocpp.public_sessions
                   SET driver_account_id = $1::uuid
                 WHERE driver_phone = $2
                   AND driver_account_id IS NULL
            """, account_id, phone)
        if updated and updated != "UPDATE 0":
            logger.info("Linked existing sessions to new account %s (phone=%s)", account_id[:8], phone[-4:])

    token = create_token(account_id, email)
    logger.info("New account registered: %s", email)

    return {
        "token": token,
        "account": {
            "id": account_id,
            "email": row["email"],
            "name": row["name"],
            "phone": row["phone"],
            "language": row["language"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        },
    }


@router.post("/login")
async def login(req: LoginRequest):
    """Login with email + password, returns JWT."""
    email = req.email.strip().lower()

    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text, email, password_hash, name, phone, language, created_at
              FROM ocpp.driver_accounts
             WHERE email = $1
        """, email)

    if not row:
        raise HTTPException(401, "Incorrect email or password")

    # Verify password
    try:
        match = bcrypt.checkpw(req.password.encode(), row["password_hash"].encode())
    except Exception:
        match = False

    if not match:
        raise HTTPException(401, "Incorrect email or password")

    token = create_token(row["id"], row["email"])
    logger.info("Account login: %s", email)

    return {
        "token": token,
        "account": {
            "id": row["id"],
            "email": row["email"],
            "name": row["name"],
            "phone": row["phone"],
            "language": row["language"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        },
    }


@router.get("/profile")
async def get_profile(request: Request):
    """Get own profile — requires JWT."""
    payload = await get_current_account(request)
    account_id = payload["sub"]

    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT id::text, email, name, phone, language, created_at
              FROM ocpp.driver_accounts
             WHERE id = $1::uuid
        """, account_id)

    if not row:
        raise HTTPException(404, "Account not found")

    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "phone": row["phone"],
        "language": row["language"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.put("/profile")
async def update_profile(request: Request, update: ProfileUpdate):
    """Update own profile — requires JWT."""
    payload = await get_current_account(request)
    account_id = payload["sub"]

    # Build SET clause dynamically for provided fields
    fields = {}
    if update.name is not None:
        fields["name"] = update.name
    if update.phone is not None:
        fields["phone"] = update.phone.strip()
    if update.language is not None:
        fields["language"] = update.language

    if not fields:
        raise HTTPException(400, "No fields to update")

    set_parts = [f"{col} = ${i+2}" for i, col in enumerate(fields.keys())]
    values = list(fields.values())

    async with db.write() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE ocpp.driver_accounts
               SET {', '.join(set_parts)}
             WHERE id = $1::uuid
            RETURNING id::text, email, name, phone, language, created_at
            """,
            account_id, *values,
        )

    if not row:
        raise HTTPException(404, "Account not found")

    # If phone was updated, retroactively link sessions
    if "phone" in fields and fields["phone"]:
        async with db.write() as conn:
            await conn.execute("""
                UPDATE ocpp.public_sessions
                   SET driver_account_id = $1::uuid
                 WHERE driver_phone = $2
                   AND driver_account_id IS NULL
            """, account_id, fields["phone"])

    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "phone": row["phone"],
        "language": row["language"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.get("/sessions")
async def get_account_sessions(request: Request, limit: int = 50, offset: int = 0):
    """Charging history for logged-in account — requires JWT."""
    payload = await get_current_account(request)
    account_id = payload["sub"]

    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT ps.id::text         AS id,
                   ps.cp_id,
                   ps.connector_id,
                   ps.kwh_delivered,
                   ps.rate_kwh,
                   ps.started_at,
                   ps.stopped_at,
                   ps.created_at,
                   ps.payment_status,
                   cp.metadata->>'display_name' AS display_name,
                   cp.metadata->>'address' AS address,
                   cp.metadata->>'city' AS city
              FROM ocpp.public_sessions ps
         LEFT JOIN ocpp.charge_points   cp ON cp.id = ps.cp_id
             WHERE ps.driver_account_id = $1::uuid
             ORDER BY ps.created_at DESC
             LIMIT $2 OFFSET $3
        """, account_id, limit, offset)

        total_row = await conn.fetchrow("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(kwh_delivered), 0) AS total_kwh,
                   COALESCE(SUM(kwh_delivered * rate_kwh), 0) AS total_cost
              FROM ocpp.public_sessions
             WHERE driver_account_id = $1::uuid
        """, account_id)

    sessions = []
    for r in rows:
        duration_min = None
        if r["started_at"] and r["stopped_at"]:
            delta = r["stopped_at"] - r["started_at"]
            duration_min = int(delta.total_seconds() / 60)

        kwh = float(r["kwh_delivered"]) if r["kwh_delivered"] else 0.0
        rate = float(r["rate_kwh"]) if r["rate_kwh"] else 0.35
        cost = round(kwh * rate, 2)

        sessions.append({
            "id": r["id"],
            "cp_id": r["cp_id"],
            "charger_name": r["display_name"] or r["cp_id"],
            "address": r["address"] or "",
            "city": r["city"] or "",
            "connector_id": r["connector_id"],
            "kwh_delivered": round(kwh, 3),
            "rate_kwh": rate,
            "cost": cost,
            "duration_min": duration_min,
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "stopped_at": r["stopped_at"].isoformat() if r["stopped_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "payment_status": r["payment_status"],
        })

    return {
        "sessions": sessions,
        "total": total_row["total"],
        "total_kwh": round(float(total_row["total_kwh"]), 3),
        "total_cost": round(float(total_row["total_cost"]), 2),
        "limit": limit,
        "offset": offset,
    }


# ── Management endpoints (API key required, mounted separately) ──────────

mgmt_router = APIRouter(tags=["Driver Accounts (Management)"])


@mgmt_router.get("")
async def list_driver_accounts(limit: int = 100, offset: int = 0, group_id: str = None):
    """List all driver accounts with their pricing tier. Optional group_id filter."""
    async with db.read() as conn:
        group_filter = ""
        params = [limit, offset]
        if group_id:
            group_filter = "WHERE da.group_id = $3::uuid"
            params.append(group_id)

        rows = await conn.fetch(f"""
            SELECT da.id::text, da.email, da.phone, da.name, da.pricing_tier,
                   da.language, da.created_at, da.group_id::text,
                   COUNT(ps.id) AS session_count,
                   COALESCE(SUM(ps.kwh_delivered), 0) AS total_kwh
              FROM ocpp.driver_accounts da
              LEFT JOIN ocpp.public_sessions ps ON ps.driver_account_id = da.id
             {group_filter}
             GROUP BY da.id
             ORDER BY da.created_at DESC
             LIMIT $1 OFFSET $2
        """, *params)
        count_q = "SELECT COUNT(*) FROM ocpp.driver_accounts"
        if group_id:
            count = await conn.fetchval(count_q + " WHERE group_id = $1::uuid", group_id)
        else:
            count = await conn.fetchval(count_q)
    return {
        "accounts": [
            {
                "id": r["id"],
                "email": r["email"],
                "phone": r["phone"],
                "name": r["name"],
                "pricing_tier": r["pricing_tier"] or "public",
                "language": r["language"],
                "group_id": r["group_id"],
                "session_count": r["session_count"],
                "total_kwh": round(float(r["total_kwh"]), 3),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": count,
    }


class DriverAccountUpdate(BaseModel):
    pricing_tier: Optional[str] = None
    group_id: Optional[str] = None  # UUID or null to remove from group
    name: Optional[str] = None


@mgmt_router.put("/{account_id}")
async def update_driver_account(account_id: str, update: DriverAccountUpdate):
    """Update a driver account (pricing tier, name)."""
    updates = []
    values = []
    idx = 1

    if update.pricing_tier is not None:
        async with db.read() as conn:
            tier = await conn.fetchrow(
                "SELECT id FROM ocpp.pricing_tiers WHERE id = $1", update.pricing_tier
            )
        if not tier:
            raise HTTPException(404, f"Pricing tier '{update.pricing_tier}' not found")
        updates.append(f"pricing_tier = ${idx}")
        values.append(update.pricing_tier)
        idx += 1

    if update.name is not None:
        updates.append(f"name = ${idx}")
        values.append(update.name)
        idx += 1

    if update.group_id is not None:
        if update.group_id == "" or update.group_id == "null":
            updates.append(f"group_id = NULL")
        else:
            async with db.read() as conn:
                grp = await conn.fetchrow(
                    "SELECT id FROM ocpp.token_groups WHERE id = $1::uuid", update.group_id
                )
            if not grp:
                raise HTTPException(404, f"Group '{update.group_id}' not found")
            updates.append(f"group_id = ${idx}::uuid")
            values.append(update.group_id)
            idx += 1

    if not updates:
        raise HTTPException(400, "No fields to update")

    values.append(account_id)
    async with db.write() as conn:
        result = await conn.execute(
            f"UPDATE ocpp.driver_accounts SET {', '.join(updates)} WHERE id::text = ${idx}",
            *values,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, f"Account {account_id} not found")

    logger.info("Driver account %s updated: %s", account_id[:8], update.model_dump(exclude_none=True))
    return {"status": "updated", "id": account_id}
