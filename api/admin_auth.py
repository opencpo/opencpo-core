"""
Admin Authentication — JWT-based login for the admin dashboard.

Uses bcrypt-verified email/password against ocpp.users.
Replaces the old demo-login system entirely.

Endpoints (all under /api/v1/admin/auth):
  POST /login   — email + password → JWT
  GET  /me      — verify token, return user profile
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from state.postgres import db
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/auth", tags=["Admin Auth"])

# ── JWT Config ───────────────────────────────────────────────────────────

JWT_SECRET = config.api.api_key or os.getenv("JWT_SECRET", "")
JWT_ALGO = "HS256"
JWT_TTL_HOURS = 24


def _create_token(user_id: int, email: str, role: str) -> str:
    """Create a signed JWT for the admin dashboard session."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "email": email,
            "role": role,
            "iat": now,
            "exp": now + timedelta(hours=JWT_TTL_HOURS),
        },
        JWT_SECRET,
        algorithm=JWT_ALGO,
    )


def verify_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session token")


# ── Models ───────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: dict


class UserProfile(BaseModel):
    id: int
    email: str
    name: str
    role: str


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post("/login")
async def login(body: LoginRequest):
    """Authenticate an admin user by email + password.

    Validates against ocpp.users using bcrypt. Returns a JWT on success.
    """
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    if not body.password or len(body.password) < 1:
        raise HTTPException(status_code=400, detail="Password is required")

    async with db.read() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, name, role, password_hash
            FROM ocpp.users
            WHERE email = $1
            """,
            email,
        )

    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    password_hash = row["password_hash"]
    if not password_hash:
        raise HTTPException(
            status_code=401,
            detail="This account uses certificate authentication — log in via client certificate",
        )

    if not bcrypt.checkpw(body.password.encode(), password_hash.encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = _create_token(row["id"], row["email"], row["role"])
    logger.info(f"Admin login: email={email} role={row['role']}")

    return LoginResponse(
        token=token,
        user={
            "id": row["id"],
            "email": row["email"],
            "name": row["name"],
            "role": row["role"],
        },
    )


@router.get("/me")
async def get_me(token: Optional[str] = None, authorization: Optional[str] = Header(None)):
    """Verify a JWT and return the current user profile.

    Pass token as: ?token=<jwt> or Authorization: Bearer <jwt>
    """
    if not token and authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    payload = verify_token(token)

    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, role, created_at FROM ocpp.users WHERE id = $1",
            int(payload["sub"]),
        )

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return UserProfile(
        id=row["id"],
        email=row["email"],
        name=row["name"],
        role=row["role"],
    )
