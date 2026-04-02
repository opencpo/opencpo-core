"""
OCPI 2.2.1 — FastAPI application.

Implements the CPO-side OCPI interface for roaming.
EMSPs connect to us to discover locations, pull sessions/CDRs, push tokens.
"""
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from config import config

logger = logging.getLogger(__name__)

ocpi_app = FastAPI(
    title="OCPI 2.2.1 — CPO Interface",
    description="OCPP Core CPO OCPI endpoint for roaming partners",
    version="2.2.1",
)


# ── OCPI Response Helper ────────────────────────────────────────────────

def ocpi_response(data=None, status_code: int = 1000, message: str = "Success"):
    """Standard OCPI response wrapper."""
    return JSONResponse(content={
        "data": data,
        "status_code": status_code,
        "status_message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def ocpi_error(status_code: int, message: str):
    return JSONResponse(
        status_code=400 if status_code >= 2000 else 200,
        content={
            "data": None,
            "status_code": status_code,
            "status_message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


# ── Token Authentication ────────────────────────────────────────────────

async def verify_ocpi_token(request: Request) -> str:
    """Verify the OCPI partner token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Token "):
        raise HTTPException(401, "Missing or invalid Authorization header")

    token = auth[6:].strip()

    from state.postgres import db
    async with db.read() as conn:
        partner = await conn.fetchrow(
            "SELECT party_id, country_code, name FROM ocpp.ocpi_partners WHERE token_a = $1 AND status = 'active'",
            token,
        )

    if not partner:
        raise HTTPException(401, "Invalid OCPI token")

    return token


# ── Versions ─────────────────────────────────────────────────────────────

async def _get_base_url() -> str:
    """Return the OCPI base URL from settings DB, falling back to env var."""
    try:
        from state.settings import get_setting
        s = await get_setting("ocpi")
        if s.get("base_url"):
            return s["base_url"]
    except Exception:
        pass
    return _base_url()


@ocpi_app.get("/ocpi/versions")
async def versions():
    """OCPI versions endpoint — entry point for partners."""
    base = await _get_base_url()
    return ocpi_response([
        {
            "version": "2.2.1",
            "url": f"{base}/ocpi/2.2.1",
        }
    ])


@ocpi_app.get("/ocpi/2.2.1")
async def version_details():
    """OCPI 2.2.1 module endpoints."""
    base = f"{await _get_base_url()}/ocpi/2.2.1"
    return ocpi_response({
        "version": "2.2.1",
        "endpoints": [
            {"identifier": "credentials", "role": "SENDER", "url": f"{base}/credentials"},
            {"identifier": "locations", "role": "SENDER", "url": f"{base}/locations"},
            {"identifier": "sessions", "role": "SENDER", "url": f"{base}/sessions"},
            {"identifier": "cdrs", "role": "SENDER", "url": f"{base}/cdrs"},
            {"identifier": "tariffs", "role": "SENDER", "url": f"{base}/tariffs"},
            {"identifier": "tokens", "role": "RECEIVER", "url": f"{base}/tokens"},
        ],
    })


# ── Include module routers ───────────────────────────────────────────────

from ocpi.credentials import router as credentials_router
from ocpi.locations import router as locations_router
from ocpi.sessions import router as sessions_router
from ocpi.cdrs import router as cdrs_router
from ocpi.tariffs import router as tariffs_router
from ocpi.tokens import router as tokens_router

ocpi_app.include_router(credentials_router, prefix="/ocpi/2.2.1/credentials", tags=["Credentials"])
ocpi_app.include_router(locations_router, prefix="/ocpi/2.2.1/locations", tags=["Locations"])
ocpi_app.include_router(sessions_router, prefix="/ocpi/2.2.1/sessions", tags=["Sessions"])
ocpi_app.include_router(cdrs_router, prefix="/ocpi/2.2.1/cdrs", tags=["CDRs"])
ocpi_app.include_router(tariffs_router, prefix="/ocpi/2.2.1/tariffs", tags=["Tariffs"])
ocpi_app.include_router(tokens_router, prefix="/ocpi/2.2.1/tokens", tags=["Tokens"])


# ── Helpers ──────────────────────────────────────────────────────────────

def _base_url() -> str:
    """Get the public OCPI base URL."""
    import os
    return os.getenv("OCPI_BASE_URL", "http://localhost:8000")
