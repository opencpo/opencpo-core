"""
PKI API endpoints — certificate management.
"""
import logging
import os
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from pki.ca import ca
from pki.ocsp import ocsp_responder

logger = logging.getLogger(__name__)
router = APIRouter()

class RevokeRequest(BaseModel):
    serial: str
    reason: str = "unspecified"

class IssueSeccRequest(BaseModel):
    charge_point_id: str
    csr_pem: Optional[str] = None

class IssueContractRequest(BaseModel):
    emaid: str
    csr_pem: Optional[str] = None

class IssueUserRequest(BaseModel):
    name: str
    email: str
    role: str = "operator"
    validity_days: int = 365
    cert_format: str = "modern"  # modern | legacy | pem

@router.get("/stats")
async def pki_stats():
    """PKI statistics — active, revoked, expiring certs."""
    return await ca.stats()

@router.get("/expiring")
async def expiring_certs(days: int = 30):
    """List certificates expiring within N days."""
    certs = await ca.get_expiring_certs(days)
    return {"expiring": certs, "days": days}

@router.get("/chain/{cert_type}")
async def cert_chain(cert_type: str):
    """Get the full certificate chain PEM."""
    if cert_type not in ("secc", "contract"):
        raise HTTPException(400, "cert_type must be 'secc' or 'contract'")
    chain = await ca.get_cert_chain(cert_type)
    return Response(content=chain, media_type="application/x-pem-file")

@router.post("/validate")
async def validate_cert(request: Request):
    """Validate a certificate against our CA chain."""
    body = await request.body()
    cert_pem = body.decode()
    result = await ca.validate_cert_chain(cert_pem)
    if not result["valid"]:
        raise HTTPException(400, result)
    return result

@router.post("/revoke")
async def revoke_cert(req: RevokeRequest):
    """Revoke a certificate by serial number."""
    success = await ca.revoke_certificate(req.serial, req.reason)
    if not success:
        raise HTTPException(404, "Certificate not found or already revoked")
    return {"status": "revoked", "serial": req.serial}

class RevokeAccountRequest(BaseModel):
    email: str
    reason: str = "account_revoked"

@router.post("/revoke-account")
async def revoke_account_certs(req: RevokeAccountRequest):
    """Revoke ALL active certificates for an account (by email/CN match)."""
    from state.postgres import db

    # Find all active certs where CN matches the email
    pattern = f"CN={req.email},%"
    async with db.read() as conn:
        certs = await conn.fetch("""
            SELECT serial FROM ocpp.pki_certificates
            WHERE status = 'active' AND subject LIKE $1
        """, pattern)

    if not certs:
        raise HTTPException(404, f"No active certificates found for {req.email}")

    revoked = []
    for cert in certs:
        success = await ca.revoke_certificate(cert["serial"], req.reason)
        if success:
            revoked.append(cert["serial"])

    logger.warning("Account revoked: %s — %d certificates", req.email, len(revoked))
    return {"status": "revoked", "email": req.email, "count": len(revoked), "serials": revoked}

@router.get("/crl")
async def get_crl():
    """Download the Certificate Revocation List."""
    crl_pem = await ca.generate_crl()
    return Response(content=crl_pem, media_type="application/x-pem-file")

@router.post("/ocsp")
async def ocsp_endpoint(request: Request):
    """OCSP responder — real-time cert status checks."""
    body = await request.body()
    response_der = await ocsp_responder.handle_request(body)
    return Response(content=response_der, media_type="application/ocsp-response")

# ── Certificate Listing & Detail ─────────────────────────────────────────

@router.get("/certificates")
async def list_certificates(
    status: Optional[str] = Query(None, description="Filter: active, revoked, expired"),
    type: Optional[str] = Query(None, description="Filter: secc, contract, user"),
    charge_point: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Search in serial, subject"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
):
    """List all certificates with optional filtering and pagination."""
    from state.postgres import db

    conditions = []
    args = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        args.append(status)
        idx += 1
    if type:
        conditions.append(f"type = ${idx}")
        args.append(type)
        idx += 1
    if charge_point:
        conditions.append(f"charge_point ILIKE ${idx}")
        args.append(f"%{charge_point}%")
        idx += 1
    if search:
        conditions.append(f"(serial ILIKE ${idx} OR subject ILIKE ${idx})")
        args.append(f"%{search}%")
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * limit

    async with db.read() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM ocpp.pki_certificates {where}", *args
        )
        rows = await conn.fetch(
            f"""SELECT serial, type, subject, issuer, charge_point,
                       not_before, not_after, fingerprint, status,
                       issued_at, revoked_at, revocation_reason
                FROM ocpp.pki_certificates {where}
                ORDER BY issued_at DESC
                LIMIT {limit} OFFSET {offset}""",
            *args
        )

    certs = []
    now = datetime.now(timezone.utc)
    for r in rows:
        d = dict(r)
        # Convert datetimes to ISO strings
        for k in ("not_before", "not_after", "issued_at", "revoked_at"):
            if d.get(k) and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        # Computed status: mark expired
        if d["status"] == "active" and d.get("not_after"):
            expiry = datetime.fromisoformat(d["not_after"])
            if expiry < now:
                d["status"] = "expired"
        certs.append(d)

    return {
        "certificates": certs,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if total else 0,
    }

@router.get("/certificates/{serial}/download")
async def download_cert(serial: str):
    """Download certificate bundle — P12/PFX from users/ dir, or PEM fallback from DB."""
    from pathlib import Path

    pki_dir = Path(ca.data_dir)

    # Check for user cert bundle (P12/PEM saved at issue time)
    for ext in ("p12", "pfx", "pem"):
        bundle_path = pki_dir / "users" / f"{serial}.{ext}"
        if bundle_path.exists():
            mime = {
                "p12": "application/x-pkcs12",
                "pfx": "application/x-pkcs12",
                "pem": "application/x-pem-file",
            }[ext]
            return Response(
                content=bundle_path.read_bytes(),
                media_type=mime,
                headers={"Content-Disposition": f'attachment; filename="operator-cert.{ext}"'},
            )

    # Fallback: serve PEM from DB (SECC/contract certs — no private key)
    from state.postgres import db
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT pem, type FROM ocpp.pki_certificates WHERE serial = $1", serial
        )
    if not row or not row["pem"]:
        raise HTTPException(404, "Certificate not found")

    return Response(
        content=row["pem"],
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f'attachment; filename="cert-{serial[:16]}.pem"'},
    )

@router.get("/certificates/{serial}")
async def get_certificate(serial: str):
    """Get full details for a single certificate."""
    from state.postgres import db

    async with db.read() as conn:
        row = await conn.fetchrow(
            """SELECT serial, type, subject, issuer, charge_point,
                      not_before, not_after, fingerprint, status, pem,
                      issued_at, revoked_at, revocation_reason
               FROM ocpp.pki_certificates WHERE serial = $1""",
            serial
        )

    if not row:
        raise HTTPException(404, "Certificate not found")

    d = dict(row)
    for k in ("not_before", "not_after", "issued_at", "revoked_at"):
        if d.get(k) and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()

    now = datetime.now(timezone.utc)
    if d["status"] == "active" and d.get("not_after"):
        if datetime.fromisoformat(d["not_after"]) < now:
            d["status"] = "expired"

    return d

# ── Certificate Issuance ─────────────────────────────────────────────────

@router.post("/issue/secc")
async def issue_secc_cert(req: IssueSeccRequest):
    """Issue a SECC certificate for a charge point."""
    from state.postgres import db
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import Encoding

    if req.csr_pem:
        csr_pem = req.csr_pem
    else:
        # Auto-generate key + CSR for the charge point
        key = ec.generate_private_key(ec.SECP256R1())
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, os.getenv("OCPI_COUNTRY_CODE", "XX")),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, os.getenv("OPERATOR_NAME", "Your CPO")),
                x509.NameAttribute(NameOID.COMMON_NAME, req.charge_point_id),
            ]))
            .sign(key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(Encoding.PEM).decode()

    try:
        cert_pem, serial_hex = await ca.sign_secc_csr(csr_pem, req.charge_point_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "status": "issued",
        "serial": serial_hex,
        "charge_point_id": req.charge_point_id,
        "cert_pem": cert_pem,
        "auto_generated_key": req.csr_pem is None,
    }

@router.post("/issue/contract")
async def issue_contract_cert(req: IssueContractRequest):
    """Issue a contract (Plug & Charge) certificate."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import Encoding

    if req.csr_pem:
        csr_pem = req.csr_pem
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, os.getenv("OCPI_COUNTRY_CODE", "XX")),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, os.getenv("OPERATOR_NAME", "Your CPO")),
                x509.NameAttribute(NameOID.COMMON_NAME, req.emaid),
            ]))
            .sign(key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(Encoding.PEM).decode()

    try:
        cert_pem, serial_hex = await ca.sign_contract_cert(csr_pem, req.emaid)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "status": "issued",
        "serial": serial_hex,
        "emaid": req.emaid,
        "cert_pem": cert_pem,
    }

@router.post("/issue/user")
async def issue_user_cert(req: IssueUserRequest):
    """Issue a user client certificate (PKCS#12 or PEM bundle)."""
    from state.postgres import db

    # Normalise cert_format
    cert_format = req.cert_format if req.cert_format in ("modern", "legacy", "pem") else "modern"

    try:
        file_bytes, serial_hex, password = await ca.issue_user_cert(
            email=req.email,
            role=req.role,
            validity_days=req.validity_days,
            cert_format=cert_format,
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to issue cert: {e}")

    # Get cert PEM for DB storage (cert only, no private key).
    # ca.issue_user_cert() always saves cert PEM to issued/{serial}.crt on disk.
    from cryptography.hazmat.primitives.serialization import Encoding
    cert_pem = None
    try:
        from pathlib import Path
        from config import config as _cfg
        issued_path = Path(_cfg.pki.data_dir) / "issued" / f"{serial_hex}.crt"
        if issued_path.exists():
            cert_pem = issued_path.read_text()
    except Exception:
        pass
    # Fallback: extract from p12 if the file wasn't found
    if cert_pem is None and cert_format != "pem":
        try:
            from cryptography.hazmat.primitives.serialization import pkcs12
            p12_data = pkcs12.load_pkcs12(file_bytes, password.encode())
            cert_pem = p12_data.cert.certificate.public_bytes(Encoding.PEM).decode()
        except Exception:
            cert_pem = ""  # empty string satisfies NOT NULL

    async with db.write() as conn:
        await conn.execute("""
            INSERT INTO ocpp.pki_certificates
                (serial, type, subject, issuer, not_before, not_after, fingerprint, status, pem)
            VALUES ($1, 'user', $2, $3,
                    NOW(), NOW() + ($4 * INTERVAL '1 day'),
                    'n/a', 'active', $5)
            ON CONFLICT (serial) DO NOTHING
        """,
            serial_hex,
            f"CN={req.email},OU={req.role},O={os.getenv('OPERATOR_NAME', 'Your CPO')},C=NL",
            f"CN={os.getenv('OPERATOR_NAME', 'Your CPO')} User CA,O={os.getenv('OPERATOR_NAME', 'Your CPO')},C=NL",
            req.validity_days,
            cert_pem or "",  # NOT NULL — empty string if we couldn't extract
        )

    # Determine file extension for download link
    file_ext = "tar.gz" if cert_format == "pem" else "p12"

    return {
        "status": "issued",
        "serial": serial_hex,
        "email": req.email,
        "role": req.role,
        "cert_format": cert_format,
        "file_ext": file_ext,
        "p12_password": password,
        "download_hint": f"GET /api/v1/pki/certificates/{serial_hex}/download",
    }


# Audit log and CA hierarchy moved to api/pki_admin.py
