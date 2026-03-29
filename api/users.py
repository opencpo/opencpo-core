"""
User Management API — Certificate-based SSO for CPO platform.

Manages users and their client certificates (.p12) used for browser/device
authentication via Caddy mTLS passthrough.

Router prefix: /api/v1/users
"""
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr

from pki.ca import ca
from config import config

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Models ────────────────────────────────────────────────────────────────

VALID_ROLES = {"admin", "client", "installer"}


class CreateUserRequest(BaseModel):
    email: EmailStr
    name: str
    role: str = "client"
    phone: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    email: str
    name: str
    phone: Optional[str]
    role: str
    cert_serial: Optional[str]
    cert_issued_at: Optional[datetime]
    cert_expires_at: Optional[datetime]
    cert_revoked_at: Optional[datetime]
    cert_status: str
    created_at: datetime
    updated_at: datetime


class IssueCertResponse(BaseModel):
    serial: str
    expires_at: datetime
    download_url: str
    password: str  # PKCS#12 password — show once


# ── Helpers ───────────────────────────────────────────────────────────────

def _p12_path(serial_hex: str) -> Path:
    return Path(config.pki.data_dir) / "users" / f"{serial_hex}.p12"


async def _get_user_or_404(email: str, conn) -> dict:
    row = await conn.fetchrow(
        "SELECT * FROM ocpp.users WHERE email = $1", email
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"User '{email}' not found")
    return dict(row)


async def _issue_cert_for_user(email: str, role: str, conn) -> tuple[str, str, datetime]:
    """
    Issue a new cert, store .p12 on disk, update DB.
    Returns (serial_hex, p12_password, expires_at).
    """
    # Revoke old cert if any
    old = await conn.fetchrow(
        "SELECT cert_serial, cert_status FROM ocpp.users WHERE email = $1", email
    )
    if old and old["cert_serial"] and old["cert_status"] == "active":
        await _revoke_cert_in_db(old["cert_serial"], conn)

    # Issue new cert
    p12_bytes, serial_hex, password = await ca.issue_user_cert(email, role)

    # Store .p12 on disk
    p12_file = _p12_path(serial_hex)
    p12_file.parent.mkdir(parents=True, exist_ok=True)
    p12_file.write_bytes(p12_bytes)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=365)

    # Generate one-time download token
    download_token = secrets.token_urlsafe(32)
    token_expires = now + timedelta(hours=24)

    # Update user record
    await conn.execute("""
        UPDATE ocpp.users SET
            cert_serial = $1,
            cert_issued_at = $2,
            cert_expires_at = $3,
            cert_revoked_at = NULL,
            cert_status = 'active',
            download_token = $4,
            download_token_expires = $5
        WHERE email = $6
    """, serial_hex, now, expires_at, download_token, token_expires, email)

    # Also record in pki_certificates for OCSP revocation checks.
    # The .crt file was saved by ca.issue_user_cert to data/pki/issued/{serial}.crt
    from pathlib import Path as _Path
    from config import config as _config
    cert_pem_path = _Path(_config.pki.data_dir) / "issued" / f"{serial_hex}.crt"
    cert_pem_str = cert_pem_path.read_text() if cert_pem_path.exists() else ""

    await conn.execute("""
        INSERT INTO ocpp.pki_certificates
            (serial, type, subject, issuer, not_before, not_after, fingerprint, status, pem)
        VALUES ($1, 'user', $2, $3, $4, $5, $6, 'active', $7)
        ON CONFLICT (serial) DO NOTHING
    """,
        serial_hex,
        f"CN={email},OU={role},O={os.getenv('OPERATOR_NAME', 'Your CPO')},C=NL",
        f"CN={os.getenv('OPERATOR_NAME', 'Your CPO')} User CA,O={os.getenv('OPERATOR_NAME', 'Your CPO')},C=NL",
        now, expires_at,
        "",  # fingerprint — computed on demand, empty is fine for now
        cert_pem_str,
    )

    return serial_hex, download_token, password, expires_at


async def _revoke_cert_in_db(serial_hex: str, conn) -> None:
    """Mark cert revoked in pki_certificates and pki_revocations."""
    now = datetime.now(timezone.utc)
    await conn.execute("""
        UPDATE ocpp.pki_certificates
        SET status = 'revoked', revoked_at = $1, revocation_reason = 'superseded'
        WHERE serial = $2
    """, now, serial_hex)

    await conn.execute("""
        INSERT INTO ocpp.pki_revocations (serial, reason, revoked_at)
        VALUES ($1, 'superseded', $2)
        ON CONFLICT (serial) DO NOTHING
    """, serial_hex, now)


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("", response_model=UserResponse, status_code=201)
async def create_user(req: CreateUserRequest):
    """Create a new user and auto-issue their first client certificate."""
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")

    from state.postgres import db
    async with db.transaction() as conn:
        # Check duplicate
        exists = await conn.fetchval(
            "SELECT id FROM ocpp.users WHERE email = $1", req.email
        )
        if exists:
            raise HTTPException(status_code=409, detail=f"User '{req.email}' already exists")

        # Insert user first
        row = await conn.fetchrow("""
            INSERT INTO ocpp.users (email, name, role, phone)
            VALUES ($1, $2, $3, $4)
            RETURNING *
        """, req.email, req.name, req.role, req.phone)

        # Issue cert
        serial_hex, download_token, password, expires_at = await _issue_cert_for_user(
            req.email, req.role, conn
        )

        # Re-fetch updated row
        row = await conn.fetchrow("SELECT * FROM ocpp.users WHERE email = $1", req.email)

    logger.info(f"User created: email={req.email} role={req.role} cert={serial_hex}")

    result = dict(row)
    # Include download info in response (only time password is shown)
    result["_cert_download_url"] = f"/api/v1/users/{req.email}/cert/download/{download_token}"
    result["_cert_password"] = password

    return UserResponse(**{k: v for k, v in result.items() if k in UserResponse.model_fields})


@router.get("", response_model=list[UserResponse])
async def list_users():
    """List all users with their cert status."""
    from state.postgres import db
    async with db.read() as conn:
        rows = await conn.fetch(
            "SELECT * FROM ocpp.users ORDER BY created_at DESC"
        )
    return [UserResponse(**dict(r)) for r in rows]


@router.get("/certs/verify")
async def verify_client_cert(
    x_client_cert_cn: Optional[str] = None,
    x_client_cert_ou: Optional[str] = None,
    x_client_cert_serial: Optional[str] = None,
):
    """
    Verify a client cert passed via Caddy headers.
    Used by other apps to validate SSO identity.
    Returns user identity if cert is valid, 403 if not.
    """
    from fastapi import Request
    # NOTE: In real use the headers come from the request directly.
    # This endpoint is called by other services with the cert headers.
    if not x_client_cert_serial:
        raise HTTPException(status_code=403, detail="No client certificate presented")

    from state.postgres import db
    async with db.read() as conn:
        user = await conn.fetchrow("""
            SELECT u.email, u.name, u.role, u.cert_status, u.cert_expires_at
            FROM ocpp.users u
            WHERE u.cert_serial = $1
        """, x_client_cert_serial)

    if not user:
        raise HTTPException(status_code=403, detail="Certificate not recognized")

    if user["cert_status"] == "revoked":
        raise HTTPException(status_code=403, detail="Certificate has been revoked")

    if user["cert_expires_at"] and user["cert_expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=403, detail="Certificate has expired")

    if user["cert_status"] != "active":
        raise HTTPException(status_code=403, detail=f"Certificate status: {user['cert_status']}")

    return {
        "valid": True,
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
    }


@router.get("/{email}", response_model=UserResponse)
async def get_user(email: str):
    """Get user detail by email."""
    from state.postgres import db
    async with db.read() as conn:
        row = await _get_user_or_404(email, conn)
    return UserResponse(**row)


@router.post("/{email}/cert/issue", response_model=IssueCertResponse)
async def issue_cert(email: str):
    """
    Issue a new client certificate for this user.
    Revokes the previous cert immediately.
    Returns a one-time download URL valid for 24h.
    """
    from state.postgres import db
    async with db.transaction() as conn:
        user = await _get_user_or_404(email, conn)
        serial_hex, download_token, password, expires_at = await _issue_cert_for_user(
            email, user["role"], conn
        )

    logger.info(f"Cert issued: email={email} serial={serial_hex}")
    return IssueCertResponse(
        serial=serial_hex,
        expires_at=expires_at,
        download_url=f"/api/v1/users/{email}/cert/download/{download_token}",
        password=password,
    )


@router.post("/{email}/cert/revoke", status_code=200)
async def revoke_cert(email: str):
    """Revoke the user's active certificate immediately. OCSP will return 'revoked'."""
    from state.postgres import db
    async with db.transaction() as conn:
        user = await _get_user_or_404(email, conn)

        if user["cert_status"] != "active":
            raise HTTPException(
                status_code=400,
                detail=f"No active certificate to revoke (status: {user['cert_status']})"
            )

        now = datetime.now(timezone.utc)
        await _revoke_cert_in_db(user["cert_serial"], conn)

        await conn.execute("""
            UPDATE ocpp.users SET
                cert_status = 'revoked',
                cert_revoked_at = $1,
                download_token = NULL,
                download_token_expires = NULL
            WHERE email = $2
        """, now, email)

    logger.warning(f"Cert revoked: email={email} serial={user['cert_serial']}")
    return {"revoked": True, "serial": user["cert_serial"]}


@router.get("/{email}/cert/download/{token}")
async def download_cert(email: str, token: str):
    """
    One-time download of the user's .p12 certificate bundle.
    Token expires after 1 use or 24 hours.
    """
    from state.postgres import db
    async with db.transaction() as conn:
        user = await conn.fetchrow("""
            SELECT cert_serial, cert_status, download_token, download_token_expires
            FROM ocpp.users WHERE email = $1
        """, email)

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if not user["download_token"]:
            raise HTTPException(status_code=410, detail="Download token already used or expired")

        if user["download_token"] != token:
            raise HTTPException(status_code=403, detail="Invalid download token")

        if user["download_token_expires"] < datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="Download token expired")

        serial_hex = user["cert_serial"]
        p12_file = _p12_path(serial_hex)

        if not p12_file.exists():
            raise HTTPException(status_code=404, detail="Certificate file not found on disk")

        p12_bytes = p12_file.read_bytes()

        # Consume token — one-time use
        await conn.execute("""
            UPDATE ocpp.users SET download_token = NULL, download_token_expires = NULL
            WHERE email = $1
        """, email)

    logger.info(f"Cert downloaded: email={email} serial={serial_hex}")
    return Response(
        content=p12_bytes,
        media_type="application/x-pkcs12",
        headers={
            "Content-Disposition": f'attachment; filename="{email}-cert.p12"',
            "Content-Length": str(len(p12_bytes)),
            "Cache-Control": "no-store",
        },
    )
