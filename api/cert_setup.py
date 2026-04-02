"""
Driver certificate setup API.

Endpoints for the cert install wizard:
- POST /setup/create-token — admin creates a setup token for a driver
- GET  /setup/validate      — validate a token (wizard landing)
- POST /setup/issue          — issue cert + return P12 download URL
- GET  /setup/download       — one-time P12 download
- GET  /setup/root-ca        — download root CA cert (always public)
- GET  /setup/verify         — check if client cert is being sent
- GET  /setup/mobileconfig   — one-time .mobileconfig profile for iOS
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from typing import Optional

from state.postgres import db
from pki.ca import ca
from api.api_key_auth import management_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public/cert-setup", tags=["cert-setup"])

CERT_VALIDITY_DAYS = 730  # 2 years
TOKEN_VALIDITY_HOURS = 72

_OPERATOR_NAME = os.getenv("OPERATOR_NAME", "OpenCPO")
_CHARGE_APP_URL = os.getenv("CHARGE_APP_URL", "http://localhost:8080")


class CreateTokenRequest(BaseModel):
    email: str


class CreateTokenResponse(BaseModel):
    token: str
    email: str
    expires_at: str
    setup_url: str


# ── Admin: create setup token ─────────────────────────────────────────────

@router.post("/create-token", response_model=CreateTokenResponse,
              dependencies=[Depends(management_auth)])
async def create_setup_token(req: CreateTokenRequest, request: Request):
    """
    Create a one-time setup token for a driver.
    Called by CPO Admin when admin clicks "Send cert to driver".

    Requires management API key (X-API-Key header).
    """
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")

    # Verify driver account exists
    async with db.read() as conn:
        driver = await conn.fetchrow(
            "SELECT id FROM ocpp.driver_accounts WHERE email = $1", email
        )
    if not driver:
        raise HTTPException(status_code=404, detail="No driver account for this email")

    # Generate token
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=TOKEN_VALIDITY_HOURS)

    async with db.write() as conn:
        await conn.execute("""
            INSERT INTO ocpp.cert_setup_tokens (token, email, expires_at)
            VALUES ($1, $2, $3)
        """, token, email, expires_at)

    setup_url = f"{_CHARGE_APP_URL}/setup/cert?token={token}"

    logger.info("Cert setup token created for %s, expires %s", email, expires_at)

    return CreateTokenResponse(
        token=token,
        email=email,
        expires_at=expires_at.isoformat(),
        setup_url=setup_url,
    )


# ── Driver: validate token (wizard landing) ───────────────────────────────

@router.get("/validate")
async def validate_token(token: str):
    """
    Validate a setup token. Returns driver info if valid.
    Called by charge app wizard on page load.
    """
    if not token or not token.strip():
        raise HTTPException(status_code=400, detail="Missing token")

    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT t.token, t.email, t.expires_at, t.used_at, t.cert_serial,
                   da.name, da.phone
            FROM ocpp.cert_setup_tokens t
            JOIN ocpp.driver_accounts da ON da.email = t.email
            WHERE t.token = $1
        """, token.strip())

    if not row:
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    if row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Token expired")

    if row["used_at"] is not None:
        raise HTTPException(status_code=410, detail="Token already used")

    return {
        "email": row["email"],
        "name": row["name"],
        "phone": row["phone"],
        "expires_at": row["expires_at"].isoformat(),
    }


# ── Driver: issue certificate ──────────────────────────────────────────────

@router.post("/issue")
async def issue_driver_cert(token: str, request: Request):
    """
    Issue a client certificate for the driver.
    Returns the P12 password and a one-time download URL.

    Called after driver confirms on the wizard page.
    """
    if not token or not token.strip():
        raise HTTPException(status_code=400, detail="Missing token")

    # Validate token
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT token, email, expires_at, used_at
            FROM ocpp.cert_setup_tokens
            WHERE token = $1
        """, token.strip())

    if not row:
        raise HTTPException(status_code=404, detail="Invalid token")
    if row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Token expired")
    if row["used_at"] is not None:
        raise HTTPException(status_code=410, detail="Token already used")

    email = row["email"]

    # Detect OS for cert format
    ua = (request.headers.get("User-Agent") or "").lower()
    if "iphone" in ua or "ipad" in ua:
        cert_format = "legacy"  # iOS needs 3DES P12
    else:
        cert_format = "modern"  # Android/desktop: AES-256

    # Issue certificate via PKI
    try:
        p12_bytes, serial_hex, password = await ca.issue_user_cert(
            email=email,
            role="driver",
            validity_days=CERT_VALIDITY_DAYS,
            cert_format=cert_format,
        )
    except Exception as e:
        logger.error("Failed to issue cert for %s: %s", email, e)
        raise HTTPException(status_code=500, detail="Certificate generation failed")

    # Save password for mobileconfig generation
    pwd_path = ca.data_dir / "users" / f"{serial_hex}.pwd"
    pwd_path.write_text(password)

    # Record in pki_certificates table
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=CERT_VALIDITY_DAYS)
    cert_pem_path = ca.data_dir / "issued" / f"{serial_hex}.crt"
    cert_pem_str = cert_pem_path.read_text() if cert_pem_path.exists() else ""

    operator_name = _OPERATOR_NAME

    async with db.write() as conn:
        await conn.execute("""
            INSERT INTO ocpp.pki_certificates
                (serial, type, subject, issuer, not_before, not_after, fingerprint, status, pem)
            VALUES ($1, 'user', $2, $3, $4, $5, '', 'active', $6)
            ON CONFLICT (serial) DO NOTHING
        """,
            serial_hex,
            f"CN={email},OU=driver,O={operator_name},C=NL",
            f"CN={operator_name} User CA,O={operator_name},C=NL",
            now, expires_at,
            cert_pem_str,
        )

    # Mark token as used + store cert serial
    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.cert_setup_tokens
            SET used_at = NOW(), cert_serial = $1
            WHERE token = $2
        """, serial_hex, token.strip())

    # Generate one-time download token (expires in 1h)
    download_token = secrets.token_urlsafe(32)
    from state.redis import redis_state
    await redis_state.client.set(
        f"cert_download:{download_token}",
        f"{serial_hex}:{email}",
        ex=3600,
    )

    download_url = f"/api/v1/public/cert-setup/download?token={download_token}"

    logger.info("Driver cert issued: email=%s serial=%s", email, serial_hex)

    return {
        "serial": serial_hex,
        "password": password,
        "download_url": download_url,
        "cert_format": cert_format,
        "expires_at": (now + timedelta(days=CERT_VALIDITY_DAYS)).isoformat(),
    }


# ── Driver: download P12 ──────────────────────────────────────────────────

@router.get("/download")
async def download_p12(token: str):
    """
    One-time download of the P12 certificate bundle.
    Token is stored in Redis with 1h TTL.
    """
    if not token or not token.strip():
        raise HTTPException(status_code=400, detail="Missing download token")

    from state.redis import redis_state
    value = await redis_state.client.get(f"cert_download:{token.strip()}")
    if not value:
        raise HTTPException(status_code=410, detail="Download link expired or already used")

    serial_hex, email = value.split(":", 1)

    p12_path = ca.data_dir / "users" / f"{serial_hex}.p12"
    if not p12_path.exists():
        raise HTTPException(status_code=404, detail="Certificate file not found")

    p12_bytes = p12_path.read_bytes()

    # Consume download token (one-time use)
    await redis_state.client.delete(f"cert_download:{token.strip()}")

    logger.info("P12 downloaded: email=%s serial=%s", email, serial_hex)

    operator_slug = _OPERATOR_NAME.lower().replace(" ", "-")

    return Response(
        content=p12_bytes,
        media_type="application/x-pkcs12",
        headers={
            "Content-Disposition": f'attachment; filename="{operator_slug}-{email}.p12"',
            "Content-Length": str(len(p12_bytes)),
            "Cache-Control": "no-store, no-cache",
        },
    )


# ── Public: download root CA ──────────────────────────────────────────────

@router.get("/root-ca")
async def download_root_ca():
    """
    Download the Root CA certificate.
    Always public — needed for trust store installation.
    """
    root_ca_path = ca.data_dir / "root-ca.crt"
    if not root_ca_path.exists():
        raise HTTPException(status_code=404, detail="Root CA not found")

    pem_bytes = root_ca_path.read_bytes()
    operator_slug = _OPERATOR_NAME.lower().replace(" ", "-")

    return Response(
        content=pem_bytes,
        media_type="application/x-pem-file",
        headers={
            "Content-Disposition": f'attachment; filename="{operator_slug}-root-ca.crt"',
            "Content-Length": str(len(pem_bytes)),
        },
    )


# ── Driver: verify cert is working ────────────────────────────────────────

@router.get("/verify")
async def verify_cert(request: Request):
    """
    Check if the client is sending a valid certificate.
    Called by the "Test my certificate" button in the wizard.

    Returns the identity if cert is present + valid, or an error if not.
    """
    serial = (request.headers.get("X-Client-Cert-Serial") or "").strip()

    if not serial:
        return {"status": "no_cert", "message": "No certificate detected"}

    # Caddy sends serial as decimal integer, we store as lowercase hex
    if serial.isdigit():
        serial = hex(int(serial))[2:].lower()
    else:
        serial = serial.lower()

    async with db.read() as conn:
        cert = await conn.fetchrow("""
            SELECT serial, subject, status
            FROM ocpp.pki_certificates
            WHERE LOWER(serial) = $1 AND type = 'user' AND status = 'active'
        """, serial)

    if not cert:
        return {"status": "invalid", "message": "Certificate not recognized or revoked"}

    # Extract email from CN
    email = None
    for part in cert["subject"].split(","):
        part = part.strip()
        if part.upper().startswith("CN="):
            email = part[3:]
            break

    return {
        "status": "ok",
        "message": "Certificate verified",
        "email": email,
        "serial": cert["serial"],
    }


# ── iOS .mobileconfig profile generation ───────────────────────────────────

def _generate_mobileconfig(
    root_ca_pem: bytes,
    user_sub_ca_pem: bytes,
    p12_data: bytes,
    p12_password: str,
    email: str,
    operator_name: str,
) -> bytes:
    """Generate a .mobileconfig profile with Root CA + Sub-CA + client identity."""
    import plistlib
    import uuid

    profile_uuid = str(uuid.uuid4()).upper()
    ca_uuid = str(uuid.uuid4()).upper()
    subca_uuid = str(uuid.uuid4()).upper()
    p12_uuid = str(uuid.uuid4()).upper()

    # Reverse-DNS identifier derived from operator name
    ident_base = "cpo." + operator_name.lower().replace(" ", "").replace("-", "")

    ca_payload = {
        "PayloadType": "com.apple.security.root",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{ident_base}.cert.rootca.{ca_uuid}",
        "PayloadUUID": ca_uuid,
        "PayloadDisplayName": f"{operator_name} Root CA",
        "PayloadDescription": f"{operator_name} trusted root certificate",
        "PayloadOrganization": operator_name,
        "PayloadContent": root_ca_pem,
        "PayloadCertificateFileName": "root-ca.crt",
    }

    subca_payload = {
        "PayloadType": "com.apple.security.pkcs1",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{ident_base}.cert.subca.{subca_uuid}",
        "PayloadUUID": subca_uuid,
        "PayloadDisplayName": f"{operator_name} User CA",
        "PayloadDescription": f"{operator_name} intermediate certificate",
        "PayloadOrganization": operator_name,
        "PayloadContent": user_sub_ca_pem,
        "PayloadCertificateFileName": "user-ca.crt",
    }

    p12_payload = {
        "PayloadType": "com.apple.security.pkcs12",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{ident_base}.cert.identity.{p12_uuid}",
        "PayloadUUID": p12_uuid,
        "PayloadDisplayName": f"{operator_name} ({email})",
        "PayloadDescription": f"Client certificate for {email}",
        "PayloadOrganization": operator_name,
        "PayloadContent": p12_data,
        "PayloadCertificateFileName": f"{email}.p12",
        "Password": p12_password,
    }

    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{ident_base}.profile.{profile_uuid}",
        "PayloadUUID": profile_uuid,
        "PayloadDisplayName": f"{operator_name} - {email}",
        "PayloadDescription": f"Installs the {operator_name} certificate for {email}.",
        "PayloadOrganization": operator_name,
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [ca_payload, subca_payload, p12_payload],
    }

    return plistlib.dumps(profile, fmt=plistlib.FMT_XML)


@router.get("/mobileconfig")
async def download_mobileconfig(token: str):
    """
    One-time download of .mobileconfig profile for iOS.
    Contains Root CA + User Sub-CA + client P12 — all in one install.
    """
    if not token or not token.strip():
        raise HTTPException(status_code=400, detail="Missing download token")

    from state.redis import redis_state
    value = await redis_state.client.get(f"cert_download:{token.strip()}")
    if not value:
        raise HTTPException(status_code=410, detail="Download link expired or already used")

    serial_hex, email = value.split(":", 1)

    p12_path = ca.data_dir / "users" / f"{serial_hex}.p12"
    if not p12_path.exists():
        raise HTTPException(status_code=404, detail="Certificate file not found")
    p12_data = p12_path.read_bytes()

    pwd_path = ca.data_dir / "users" / f"{serial_hex}.pwd"
    if pwd_path.exists():
        p12_password = pwd_path.read_text().strip()
    else:
        raise HTTPException(status_code=404, detail="Certificate password not found")

    root_ca_pem = (ca.data_dir / "root-ca.crt").read_bytes()
    user_sub_ca_pem = (ca.data_dir / "user-sub-ca.crt").read_bytes()

    mobileconfig = _generate_mobileconfig(
        root_ca_pem=root_ca_pem,
        user_sub_ca_pem=user_sub_ca_pem,
        p12_data=p12_data,
        p12_password=p12_password,
        email=email,
        operator_name=_OPERATOR_NAME,
    )

    # Consume download token
    await redis_state.client.delete(f"cert_download:{token.strip()}")

    logger.info("Mobileconfig downloaded: email=%s serial=%s", email, serial_hex)

    operator_slug = _OPERATOR_NAME.lower().replace(" ", "-")

    return Response(
        content=mobileconfig,
        media_type="application/x-apple-aspen-config",
        headers={
            "Content-Disposition": f'attachment; filename="{operator_slug}-{email}.mobileconfig"',
            "Cache-Control": "no-store, no-cache",
        },
    )
