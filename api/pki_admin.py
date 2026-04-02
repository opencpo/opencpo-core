"""
PKI admin endpoints - audit log and CA hierarchy.
Mounted under /api/v1/pki alongside the main pki router.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/audit-log")
async def audit_log(
    type: Optional[str] = Query(None, description="secc, contract, user"),
    date_from: Optional[str] = Query(None, description="ISO date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="ISO date YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=200),
):
    """Certificate lifecycle audit log."""
    from state.postgres import db

    conditions = ["1=1"]
    args = []
    idx = 1

    if type:
        conditions.append(f"c.type = \${idx}")
        args.append(type)
        idx += 1
    if date_from:
        conditions.append(f"event_time >= \${idx}::timestamptz")
        args.append(date_from)
        idx += 1
    if date_to:
        conditions.append(f"event_time <= \${idx}::timestamptz + INTERVAL '1 day'")
        args.append(date_to)
        idx += 1

    where = " AND ".join(conditions)

    async with db.read() as conn:
        rows = await conn.fetch(f"""
            SELECT serial, type, subject, charge_point, issued_at AS event_time, 'issued' AS event
            FROM ocpp.pki_certificates c
            WHERE {where}
            UNION ALL
            SELECT serial, type, subject, charge_point, revoked_at AS event_time,
                   COALESCE('revoked:' || revocation_reason, 'revoked') AS event
            FROM ocpp.pki_certificates c
            WHERE status = 'revoked' AND revoked_at IS NOT NULL AND {where}
            ORDER BY event_time DESC
            LIMIT {limit}
        """, *args)

    events = []
    for r in rows:
        d = dict(r)
        if d.get("event_time") and hasattr(d["event_time"], "isoformat"):
            d["event_time"] = d["event_time"].isoformat()
        events.append(d)

    return {"events": events, "total": len(events)}


@router.get("/ca-hierarchy")
async def ca_hierarchy():
    """Return CA chain info: root CA - sub-CAs with expiry and fingerprints."""
    from pki.ca import ca
    from cryptography.hazmat.primitives import hashes

    def cert_info(cert, role: str) -> dict:
        if cert is None:
            return {"role": role, "available": False}
        now = datetime.now(timezone.utc)
        expiry = cert.not_valid_after_utc
        days_left = (expiry - now).days
        status = "active"
        if days_left < 0:
            status = "expired"
        elif days_left < 30:
            status = "expiring"

        return {
            "role": role,
            "cn": cert.subject.get_attributes_for_oid(
                __import__("cryptography.x509.oid", fromlist=["NameOID"]).NameOID.COMMON_NAME
            )[0].value,
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "serial": format(cert.serial_number, "x"),
            "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
            "not_before": cert.not_valid_before_utc.isoformat(),
            "not_after": expiry.isoformat(),
            "days_left": days_left,
            "status": status,
            "available": True,
        }

    return {
        "root": cert_info(ca._root_ca_cert, "Root CA"),
        "sub_cas": [
            cert_info(ca._cpo_sub_ca_cert, "CPO Sub-CA (SECC)"),
            cert_info(ca._mo_sub_ca_cert, "MO Sub-CA (Contracts)"),
            cert_info(ca._user_sub_ca_cert, "User Sub-CA"),
        ],
    }
