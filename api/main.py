"""
CPO REST API — FastAPI application.

The public interface to OCPP Core. All writes go through here.
"""
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import config
from api.ratelimit import RateLimitMiddleware
from api.api_key_auth import management_auth

app = FastAPI(
    title="OCPP Core API",
    description="Charge Point Operator REST API — OCPP 1.6j + 2.0.1",
    version="1.0.0",
    # NOTE: /docs and /redoc are intentionally public on the demo VPS for easy API exploration.
    # In production, set docs_url=None and redoc_url=None or guard behind VPN/auth.
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.api.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting
app.add_middleware(
    RateLimitMiddleware,
    limits={
        "/ocpi": 60,          # OCPI roaming: 60/min
        "/api/public": 60,    # Public charge endpoints: 60/min
        "/api/payments": 60,  # Payment webhooks: 60/min
        "/api/v1/auth": 30,   # Auth endpoints: 30/min (brute force protection)
    },
    default_limit=120,        # Everything else: 120/min
)


# ── Health Endpoints ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Basic liveness check."""
    return {
        "status": "ok",
        "service": "ocpp-core",
        "version": "1.0.0",
    }


@app.get("/health/ready")
async def readiness():
    """Readiness check — verifies all dependencies."""
    from state.postgres import db
    from state.redis import redis_state

    db_health = await db.health()
    redis_health = await redis_state.health()

    all_ok = db_health.get("primary") == "ok" and redis_health.get("status") == "ok"

    return {
        "status": "ok" if all_ok else "degraded",
        "database": db_health,
        "redis": redis_health,
    }


# ── API Routers ──────────────────────────────────────────────────────────

from api.pki import router as pki_router
from api.pki_admin import router as pki_admin_router
from api.chargers import router as chargers_router
from api.charger_commands import router as charger_commands_router
from api.sessions import router as sessions_router
from api.tariffs import router as tariffs_router
from api.auth import router as auth_router
from api.tokens import router as tokens_router
from api.groups import router as groups_router
from api.events import router as events_router
from api.public import router as public_router, webhook_router, mgmt_public_router
from api.public_auth import router as public_auth_router
from api.public_sessions import router as public_sessions_router
from api.users import router as users_router
from api.features import router as features_router
from api.accounts import router as accounts_router, mgmt_router as driver_accounts_mgmt_router
from api.favorites import router as favorites_router
from api.push import router as push_router
from api.vehicles import router as vehicles_router
from api.profiles import router as profiles_router
from api.pricing import router as pricing_router
from api.cert_setup import router as cert_setup_router
from api.settings import router as settings_router
from api.ocpi_management import router as ocpi_mgmt_router
from api.network import router as network_router
from api.api_key_auth import management_auth

_mgmt = [Depends(management_auth)]  # shorthand for management-only routes

# ── Management endpoints (require MANAGEMENT_API_KEY) ────────────────────
# Chargers + Sessions: read endpoints are public, write endpoints have per-route auth
app.include_router(chargers_router, prefix="/api/v1/chargers", tags=["Chargers"])
app.include_router(charger_commands_router, prefix="/api/v1/chargers", tags=["Charger Commands"])
app.include_router(sessions_router, prefix="/api/v1/sessions", tags=["Sessions"])
app.include_router(tariffs_router, prefix="/api/v1/tariffs", tags=["Tariffs"], dependencies=_mgmt)
app.include_router(tokens_router, prefix="/api/v1/tokens", tags=["Tokens"], dependencies=_mgmt)
app.include_router(groups_router, prefix="/api/v1/groups", tags=["Groups"], dependencies=_mgmt)
app.include_router(pki_router, prefix="/api/v1/pki", tags=["PKI"], dependencies=_mgmt)
app.include_router(pki_admin_router, prefix="/api/v1/pki", tags=["PKI Admin"], dependencies=_mgmt)
app.include_router(events_router, prefix="/api/v1/events", tags=["Events"])  # No auth — read-only SSE
app.include_router(users_router, prefix="/api/v1/users", tags=["Users"], dependencies=_mgmt)
app.include_router(vehicles_router, prefix="/api/v1/fleet/vehicles", tags=["Fleet"], dependencies=_mgmt)
app.include_router(driver_accounts_mgmt_router, prefix="/api/v1/driver-accounts", tags=["Driver Accounts"], dependencies=_mgmt)
app.include_router(mgmt_public_router, prefix="/api/v1/public-sessions", tags=["Public Sessions"], dependencies=_mgmt)
app.include_router(features_router, dependencies=_mgmt)

# Auth router: protected by API key (admin creates RFID tokens etc.)
app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authorization"], dependencies=_mgmt)

# ── Public endpoints (no auth — charge app, webhooks) ─────────────────────
app.include_router(accounts_router)   # prefix built-in: /api/v1/public/account
app.include_router(favorites_router)  # prefix built-in: /api/v1/public/account/favorites
app.include_router(public_router)          # prefix built-in: /api/v1/public
app.include_router(public_auth_router)     # prefix built-in: /api/v1/public/auth
app.include_router(public_sessions_router) # prefix built-in: /api/v1/public/sessions
app.include_router(push_router)            # prefix built-in: /api/v1/public/push
app.include_router(webhook_router)         # Payment webhook: POST /api/payments/webhook

app.include_router(profiles_router, prefix="/api/v1/profiles", tags=["Charger Profiles"], dependencies=_mgmt)
app.include_router(settings_router, dependencies=_mgmt)
app.include_router(ocpi_mgmt_router, prefix="/api/v1/ocpi", tags=["OCPI Management"], dependencies=_mgmt)
app.include_router(network_router, dependencies=_mgmt)

# Pricing — /current is public; /config and /tiers management auth is handled per-route in pricing.py
app.include_router(pricing_router)

# Cert setup — driver certificate install wizard (prefix built-in: /api/v1/public/cert-setup)
# create-token requires API key (admin), other endpoints are public (driver-facing)
app.include_router(cert_setup_router)
