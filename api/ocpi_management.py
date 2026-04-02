"""
OCPI Partner Management API — admin CRUD + connection testing.

These endpoints are for the CPO Admin panel. They are separate from the
OCPI protocol endpoints (ocpi/*.py), which are for roaming partners.

All routes require management_auth. No route here touches the OCPI partner
interface directly — they manage the partner records and test connectivity.
"""
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_ocpi_identity() -> dict:
    """Return OCPI identity from settings DB, falling back to env vars."""
    try:
        from state.settings import get_setting
        s = await get_setting("ocpi")
    except Exception:
        s = {}
    return {
        "country_code":      s.get("country_code")      or os.getenv("OCPI_COUNTRY_CODE", "NL"),
        "party_id":          s.get("party_id")           or os.getenv("OCPI_PARTY_ID", "OCP"),
        "role":              s.get("role")               or "CPO",
        "operator_name":     s.get("operator_name")      or os.getenv("OCPI_OPERATOR_NAME", "OpenCPO"),
        "emsp_country_code": s.get("emsp_country_code")  or os.getenv("OCPI_COUNTRY_CODE", "NL"),
        "emsp_party_id":     s.get("emsp_party_id")      or os.getenv("OCPI_PARTY_ID", "OCP"),
        "base_url":          s.get("base_url")            or os.getenv("OCPI_BASE_URL", "http://localhost:8000"),
        "versions_path":     s.get("versions_path")       or "/ocpi/versions",
    }


# ── Request / Response Models ────────────────────────────────────────────

class PartnerCreate(BaseModel):
    party_id: str
    country_code: str
    role: str = "EMSP"             # CPO, EMSP, HUB
    name: str
    url: str                       # Partner's OCPI versions URL
    token_b: Optional[str] = None  # Their token (we call them with this)
    # Roaming tariff markup
    base_tariff_id:   Optional[str]   = None
    roaming_fee_kwh:  Optional[float] = None  # Extra €/kWh on top of base tariff
    roaming_fee_flat: Optional[float] = None  # Extra flat connection fee
    roaming_fee_time: Optional[float] = None  # Extra €/min time fee


class PartnerUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    token_b: Optional[str] = None
    status: Optional[str] = None   # active, pending, suspended, disabled
    # Roaming tariff markup
    base_tariff_id:   Optional[str]   = None
    roaming_fee_kwh:  Optional[float] = None
    roaming_fee_flat: Optional[float] = None
    roaming_fee_time: Optional[float] = None


# ── Helpers ───────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """Convert asyncpg Record to dict, serialising datetime fields and expanding metadata."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    # Expand roaming markup fields from metadata jsonb
    meta = d.get("metadata") or {}
    if isinstance(meta, str):
        import json as _json
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    d["base_tariff_id"]   = meta.get("base_tariff_id")
    d["roaming_fee_kwh"]  = meta.get("roaming_fee_kwh")
    d["roaming_fee_flat"] = meta.get("roaming_fee_flat")
    d["roaming_fee_time"] = meta.get("roaming_fee_time")
    return d


def _mask_token(token: Optional[str]) -> Optional[str]:
    """Return first 8 chars + asterisks so logs / UI never expose full tokens."""
    if not token:
        return None
    visible = min(8, len(token))
    return token[:visible] + "*" * max(0, len(token) - visible)


# ── List / Get ────────────────────────────────────────────────────────────

@router.get("/partners")
async def list_partners():
    """List all OCPI partners."""
    async with db.read() as conn:
        rows = await conn.fetch(
            "SELECT *, metadata FROM ocpp.ocpi_partners ORDER BY created_at DESC"
        )
    partners = []
    for row in rows:
        p = _row_to_dict(row)
        p["token_a_masked"] = _mask_token(p.get("token_a"))
        p["token_b_masked"] = _mask_token(p.get("token_b"))
        del p["token_a"]
        del p["token_b"]
        partners.append(p)
    return {"partners": partners}


@router.get("/partners/{partner_id}")
async def get_partner(partner_id: int):
    """Get a single OCPI partner by ID."""
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT *, metadata FROM ocpp.ocpi_partners WHERE id = $1", partner_id
        )
    if not row:
        raise HTTPException(404, f"Partner {partner_id} not found")
    p = _row_to_dict(row)
    p["token_a_masked"] = _mask_token(p.get("token_a"))
    p["token_b_masked"] = _mask_token(p.get("token_b"))
    del p["token_a"]
    del p["token_b"]
    return {"partner": p}


# ── Create ────────────────────────────────────────────────────────────────

@router.post("/partners")
async def create_partner(partner: PartnerCreate):
    """
    Manually register an OCPI partner.

    Generates a token_a (the token we give them) automatically.
    token_b is their token (we use it when calling them) — optional at creation,
    they can provide it later during the OCPI handshake.
    """
    import json as _json

    token_a = secrets.token_urlsafe(32)

    meta: dict = {}
    if partner.base_tariff_id is not None:
        meta["base_tariff_id"] = partner.base_tariff_id
    if partner.roaming_fee_kwh is not None:
        meta["roaming_fee_kwh"] = partner.roaming_fee_kwh
    if partner.roaming_fee_flat is not None:
        meta["roaming_fee_flat"] = partner.roaming_fee_flat
    if partner.roaming_fee_time is not None:
        meta["roaming_fee_time"] = partner.roaming_fee_time

    async with db.write() as conn:
        row = await conn.fetchrow("""
            INSERT INTO ocpp.ocpi_partners
                (party_id, country_code, role, name, url, token_a, token_b, status, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8::jsonb)
            RETURNING id, party_id, country_code, role, name, url, status, created_at, metadata
        """,
            partner.party_id.upper(),
            partner.country_code.upper(),
            partner.role.upper(),
            partner.name,
            partner.url,
            token_a,
            partner.token_b or None,
            _json.dumps(meta),
        )

    logger.info(
        "OCPI partner registered manually: %s*%s (%s) role=%s",
        partner.country_code.upper(), partner.party_id.upper(), partner.name, partner.role.upper(),
    )
    result = _row_to_dict(row)
    # Return token_a in full exactly once — the operator needs to send it to the partner
    result["token_a"] = token_a
    return {"partner": result, "token_a": token_a}


# ── Update ────────────────────────────────────────────────────────────────

@router.put("/partners/{partner_id}")
async def update_partner(partner_id: int, update: PartnerUpdate):
    """Update partner details — name, URL, token, status, or roaming markup."""
    import json as _json

    # Separate roaming metadata fields from direct column fields
    meta_fields = {"base_tariff_id", "roaming_fee_kwh", "roaming_fee_flat", "roaming_fee_time"}
    column_updates = {}
    meta_updates = {}

    for field, value in update.model_dump(exclude_none=True).items():
        if field in meta_fields:
            meta_updates[field] = value
        else:
            column_updates[field] = value

    if not column_updates and not meta_updates:
        raise HTTPException(400, "No fields to update")

    async with db.write() as conn:
        if meta_updates:
            # Read existing metadata, merge, write back
            row = await conn.fetchrow(
                "SELECT metadata FROM ocpp.ocpi_partners WHERE id = $1", partner_id
            )
            if not row:
                raise HTTPException(404, f"Partner {partner_id} not found")
            existing_meta = row["metadata"] or {}
            if isinstance(existing_meta, str):
                try:
                    existing_meta = _json.loads(existing_meta)
                except Exception:
                    existing_meta = {}
            existing_meta.update(meta_updates)
            column_updates["metadata"] = _json.dumps(existing_meta)

        fields = []
        values = []
        idx = 1
        for field, value in column_updates.items():
            if field == "metadata":
                fields.append(f"metadata = ${idx}::jsonb")
            else:
                fields.append(f"{field} = ${idx}")
            values.append(value)
            idx += 1

        values.append(partner_id)
        result = await conn.execute(
            f"UPDATE ocpp.ocpi_partners SET {', '.join(fields)} WHERE id = ${idx}",
            *values,
        )

    if result == "UPDATE 0":
        raise HTTPException(404, f"Partner {partner_id} not found")

    logger.info("OCPI partner %d updated: columns=%s meta=%s", partner_id, list(column_updates.keys()), list(meta_updates.keys()))
    return {"status": "updated", "id": partner_id}


# ── Delete ────────────────────────────────────────────────────────────────

@router.delete("/partners/{partner_id}")
async def delete_partner(partner_id: int):
    """Remove an OCPI partner record permanently."""
    async with db.write() as conn:
        result = await conn.execute(
            "DELETE FROM ocpp.ocpi_partners WHERE id = $1", partner_id
        )
    if result == "DELETE 0":
        raise HTTPException(404, f"Partner {partner_id} not found")

    logger.info("OCPI partner %d deleted", partner_id)
    return {"status": "deleted", "id": partner_id}


# ── Test Connection ────────────────────────────────────────────────────────

@router.post("/partners/{partner_id}/test")
async def test_partner_connection(partner_id: int):
    """
    Test connectivity to a roaming partner.

    Calls their OCPI versions endpoint (GET /versions) using our stored
    token_b (their token). Returns the HTTP status, response time, and
    the versions they advertise.
    """
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT name, url, token_b, status FROM ocpp.ocpi_partners WHERE id = $1",
            partner_id,
        )
    if not row:
        raise HTTPException(404, f"Partner {partner_id} not found")

    url = row["url"]
    token_b = row["token_b"]

    if not token_b:
        return {
            "ok": False,
            "error": "No token configured for this partner (token_b is missing). "
                     "Complete the OCPI handshake first, or set token_b manually.",
        }

    start = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Token {token_b}"},
            )
        elapsed_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

        return {
            "ok": resp.status_code == 200,
            "status_code": resp.status_code,
            "elapsed_ms": elapsed_ms,
            "partner_name": row["name"],
            "url": url,
            "versions": body.get("data", []) if resp.status_code == 200 else [],
            "ocpi_status": body.get("status_code"),
            "ocpi_message": body.get("status_message"),
        }
    except httpx.ConnectTimeout:
        return {"ok": False, "error": f"Connection timed out after 10s — is {url} reachable?"}
    except httpx.ConnectError as e:
        return {"ok": False, "error": f"Could not connect: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Sync ─────────────────────────────────────────────────────────────────

@router.post("/partners/{partner_id}/sync")
async def sync_partner(partner_id: int):
    """
    Trigger a pull-sync from a roaming partner.

    Currently records the sync attempt and returns the partner's locations
    count. Full sync implementation (pull CDRs, sessions, tariffs) is
    wired in when the OCPI sync worker is active.
    """
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT name, url, token_b, status FROM ocpp.ocpi_partners WHERE id = $1",
            partner_id,
        )
    if not row:
        raise HTTPException(404, f"Partner {partner_id} not found")

    if row["status"] != "active":
        raise HTTPException(400, f"Partner is {row['status']} — only active partners can be synced")

    # Stamp last_sync
    async with db.write() as conn:
        await conn.execute(
            "UPDATE ocpp.ocpi_partners SET last_sync = NOW() WHERE id = $1", partner_id
        )

    logger.info("OCPI sync triggered for partner %d (%s)", partner_id, row["name"])
    return {
        "status": "sync_triggered",
        "partner_id": partner_id,
        "partner_name": row["name"],
        "note": "Sync worker will pull locations, sessions, and CDRs on next cycle.",
    }


# ── Status Overview ───────────────────────────────────────────────────────

@router.get("/status")
async def ocpi_status():
    """
    OCPI module status — our identity, endpoint health, and partner summary.
    """
    identity = await _get_ocpi_identity()
    versions_url = f"{identity['base_url']}{identity['versions_path']}"

    async with db.read() as conn:
        totals = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'active')    AS active,
                COUNT(*) FILTER (WHERE status = 'pending')   AS pending,
                COUNT(*) FILTER (WHERE status = 'suspended') AS suspended,
                COUNT(*) FILTER (WHERE status = 'disabled')  AS disabled,
                COUNT(*)                                      AS total,
                MAX(last_sync)                                AS last_sync
            FROM ocpp.ocpi_partners
        """)

    # Probe our own OCPI versions endpoint
    our_health = "unknown"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(versions_url)
        our_health = "ok" if r.status_code == 200 else f"http_{r.status_code}"
    except Exception:
        our_health = "unreachable"

    last_sync = totals["last_sync"]
    return {
        "identity": {
            "country_code":      identity["country_code"],
            "party_id":          identity["party_id"],
            "role":              identity["role"],
            "operator_name":     identity["operator_name"],
            "emsp_country_code": identity["emsp_country_code"],
            "emsp_party_id":     identity["emsp_party_id"],
            "base_url":          identity["base_url"],
            "versions_url":      versions_url,
            "versions_path":     identity["versions_path"],
        },
        "endpoints_health": our_health,
        "partners": {
            "total":     totals["total"],
            "active":    totals["active"],
            "pending":   totals["pending"],
            "suspended": totals["suspended"],
            "disabled":  totals["disabled"],
        },
        "last_sync": last_sync.isoformat() if last_sync else None,
    }


# ── Request Log ───────────────────────────────────────────────────────────

@router.get("/log")
async def ocpi_log(limit: int = 100, partner_id: Optional[int] = None):
    """
    Recent OCPI inbound/outbound requests.

    Reads from ocpp.ocpi_request_log if it exists. If the table has not been
    created yet (schema migration pending), returns an empty list with a note.
    """
    try:
        async with db.read() as conn:
            if partner_id:
                rows = await conn.fetch("""
                    SELECT * FROM ocpp.ocpi_request_log
                    WHERE partner_id = $1
                    ORDER BY created_at DESC LIMIT $2
                """, partner_id, limit)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM ocpp.ocpi_request_log
                    ORDER BY created_at DESC LIMIT $1
                """, limit)
        return {"entries": [_row_to_dict(r) for r in rows], "total": len(rows)}
    except Exception as e:
        # Table may not exist yet — return graceful empty state
        if "ocpi_request_log" in str(e):
            return {
                "entries": [],
                "total": 0,
                "note": "Request log table not yet created. "
                        "Run the schema migration to enable OCPI request logging.",
            }
        raise
