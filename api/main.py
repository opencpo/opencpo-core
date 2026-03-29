"""
CPO REST API — FastAPI application.

The public interface to OCPP Core. All writes go through here.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import config
from api.ratelimit import RateLimitMiddleware

app = FastAPI(
    title="OCPP Core API",
    description="Charge Point Operator REST API — OCPP 1.6j + 2.0.1",
    version="1.0.0",
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

# Rate limiting (replaces Kong rate-limiting plugin)
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
# These will be added as we build each module:

from api.pki import router as pki_router
from api.chargers import router as chargers_router
from api.sessions import router as sessions_router
from api.tariffs import router as tariffs_router
from api.auth import router as auth_router
from api.tokens import router as tokens_router
from api.groups import router as groups_router
from api.events import router as events_router
from api.public import router as public_router, webhook_router
from api.users import router as users_router
from api.features import router as features_router
from api.accounts import router as accounts_router
from api.favorites import router as favorites_router
from api.push import router as push_router
from api.invoices import router as invoices_router
from api.sepa import router as sepa_router

app.include_router(chargers_router, prefix="/api/v1/chargers", tags=["Chargers"])
app.include_router(sessions_router, prefix="/api/v1/sessions", tags=["Sessions"])
app.include_router(tariffs_router, prefix="/api/v1/tariffs", tags=["Tariffs"])
app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authorization"])
app.include_router(tokens_router, prefix="/api/v1/tokens", tags=["Tokens"])
app.include_router(groups_router, prefix="/api/v1/groups", tags=["Groups"])
app.include_router(pki_router, prefix="/api/v1/pki", tags=["PKI"])
app.include_router(events_router, prefix="/api/v1/events", tags=["Events"])
app.include_router(users_router, prefix="/api/v1/users", tags=["Users"])
app.include_router(features_router)
app.include_router(accounts_router)   # prefix built-in: /api/v1/public/account
app.include_router(favorites_router)  # prefix built-in: /api/v1/public/account/favorites

# Compatibility layer — maps old dashboard endpoints to new data
# This lets the existing 20K-line dashboard frontend work with OCPP Core
app.include_router(public_router)   # prefix built-in: /api/v1/public
app.include_router(push_router)     # prefix built-in: /api/v1/public/push
app.include_router(webhook_router)  # Mollie webhook: POST /api/payments/webhook
app.include_router(invoices_router)  # Invoices: /api/v1/invoices + /api/v1/groups/{id}/invoices
app.include_router(sepa_router)      # SEPA: /api/v1/groups/{id}/mandate + /api/v1/invoices/{id}/collect + webhook

from api.lago_webhook import router as lago_webhook_router
app.include_router(lago_webhook_router)  # Lago billing webhooks: POST /api/v1/webhooks/lago

from api.profiles import router as profiles_router
app.include_router(profiles_router, prefix="/api/v1/profiles", tags=["Charger Profiles"])

from api.incidents import router as incidents_router
from api.vehicles import router as vehicles_router
from api.clients import router as clients_router

app.include_router(incidents_router, prefix="/api/v1/incidents", tags=["Incidents"])
app.include_router(vehicles_router, prefix="/api/v1/fleet/vehicles", tags=["Fleet"])
app.include_router(clients_router, prefix="/api/v1/clients", tags=["Clients"])
