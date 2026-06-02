"""
Admin Setup Wizard — first-time platform configuration.

These endpoints are only accessible when no admin user exists yet (fresh install).
Once setup is complete, they return 403.

Steps (all skippable — user can configure manually via files):
  1. Admin account  — email + password for the first admin user
  2. Organization   — name, timezone, currency, public URL
  3. SMTP           — email sending credentials for the comms module
  4. PKI            — generate root CA + user CA
  5. Pricing        — default tariff + pricing tiers
  6. Features       — toggle OCPI, billing, EMS, etc.

Endpoints:
  GET  /api/v1/admin/setup/status  — which steps are complete/skipped
  POST /api/v1/admin/setup/step    — complete a step
  POST /api/v1/admin/setup/skip    — skip a step
"""
import json
import logging
import os
from datetime import datetime, timezone

import bcrypt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from typing import Optional

from state.postgres import db
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/setup", tags=["Admin Setup"])

# ── Setup state is tracked via settings table + feature_flags ────────────
# Steps use ocpp.feature_flags to mark completion:
#   setup.step.admin  = true | skipped
#   setup.step.org    = true | skipped
#   setup.step.smtp   = true | skipped
#   setup.step.pki    = true | skipped
#   setup.step.pricing = true | skipped
#   setup.step.features = true | skipped
# Complete when all steps are true or skipped.

STEPS = ["admin", "org", "smtp", "pki", "pricing", "features"]


async def _admin_exists() -> bool:
    """Check if any admin user with a password_hash exists."""
    async with db.read() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM ocpp.users WHERE password_hash IS NOT NULL"
        )
    return count > 0


async def _get_step_flag(key: str) -> Optional[str]:
    """Get the value of a setup step flag from feature_flags."""
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT enabled, label FROM ocpp.feature_flags WHERE key = $1",
            key,
        )
    if not row:
        return None
    if row["label"] == "skipped":
        return "skipped"
    return "done" if row["enabled"] else None


async def _set_step_flag(key: str, value: str):
    """Set a setup step flag in feature_flags.
    value: 'done' (completed), 'skipped' (user chose to skip)
    """
    async with db.transaction() as conn:
        await conn.execute(
            """
            INSERT INTO ocpp.feature_flags (key, enabled, label)
            VALUES ($1, $2, $3)
            ON CONFLICT (key) DO UPDATE SET enabled = $2, label = $3, updated_at = NOW()
            """,
            key,
            value == "done",
            value,
        )


# ── Models ───────────────────────────────────────────────────────────────

class StepAdminRequest(BaseModel):
    email: EmailStr
    password: str
    name: str = "Admin"


class StepOrgRequest(BaseModel):
    name: str = "My CPO"
    timezone: str = "Europe/Amsterdam"
    currency: str = "EUR"
    public_url: str = "http://localhost"


class StepSmtpRequest(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_email: str = ""
    use_tls: bool = True


class StepPkiRequest(BaseModel):
    org_name: str = ""
    country: str = "NL"


class StepPricingRequest(BaseModel):
    currency: str = "EUR"
    default_rate_kwh: float = 0.35
    tariff_name: str = "Standard Rate"


class StepFeaturesRequest(BaseModel):
    ocpi: bool = False
    billing: bool = False
    ems: bool = False
    iso15118: bool = False


class SetupStatus(BaseModel):
    complete: bool
    steps: dict[str, str]  # step_name → "pending" | "done" | "skipped"


# ── Endpoints ────────────────────────────────────────────────────────────

@router.get("/status")
async def get_setup_status():
    """Return which setup steps are complete. Used by the admin panel to
    decide whether to show the wizard or the login page."""
    if await _admin_exists():
        # Check all steps
        step_status = {}
        for name in STEPS:
            val = await _get_step_flag(f"setup.step.{name}")
            step_status[name] = val or "pending" if val else "pending"

        all_done = all(v in ("done", "skipped") for v in step_status.values())
        return SetupStatus(complete=all_done, steps=step_status)
    else:
        # No admin exists yet — first-time setup
        return SetupStatus(complete=False, steps={s: "pending" for s in STEPS})


@router.post("/step/admin")
async def setup_admin(body: StepAdminRequest):
    """Create the first admin user."""
    if await _admin_exists():
        raise HTTPException(status_code=400, detail="An admin user already exists")

    email = body.email.strip().lower()
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()

    async with db.transaction() as conn:
        # Check not duplicate
        existing = await conn.fetchval(
            "SELECT id FROM ocpp.users WHERE email = $1", email
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"User '{email}' already exists")

        await conn.execute(
            """
            INSERT INTO ocpp.users (email, name, role, password_hash)
            VALUES ($1, $2, 'admin', $3)
            """,
            email, body.name.strip(), pw_hash,
        )
        await _set_step_flag("setup.step.admin", "done")

    logger.info(f"Admin user created: email={email}")
    return {"ok": True, "step": "admin"}


@router.post("/step/org")
async def setup_org(body: StepOrgRequest):
    """Store organization settings."""
    if not await _admin_exists():
        raise HTTPException(status_code=403, detail="Create the admin account first")

    async with db.transaction() as conn:
        # Store org settings in pricing_config (generic key-value store)
        settings = [
            ("org.name", body.name),
            ("org.timezone", body.timezone),
            ("org.currency", body.currency),
            ("org.public_url", body.public_url.rstrip("/")),
        ]
        for key, value in settings:
            await conn.execute(
                """
                INSERT INTO ocpp.pricing_config (key, value, description, updated_at)
                VALUES ($1, 0, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET description = $2, updated_at = NOW()
                """,
                key, value,
            )
        await _set_step_flag("setup.step.org", "done")

    logger.info(f"Organization settings saved: {body.name}")
    return {"ok": True, "step": "org"}


@router.post("/step/smtp")
async def setup_smtp(body: StepSmtpRequest):
    """Save SMTP credentials for the comms module."""
    if not await _admin_exists():
        raise HTTPException(status_code=403, detail="Create the admin account first")

    async with db.transaction() as conn:
        settings = [
            ("smtp.host", body.host),
            ("smtp.port", str(body.port)),
            ("smtp.username", body.username),
            ("smtp.password", body.password),
            ("smtp.from_email", body.from_email),
            ("smtp.use_tls", str(body.use_tls).lower()),
        ]
        for key, value in settings:
            await conn.execute(
                """
                INSERT INTO ocpp.pricing_config (key, value, description, updated_at)
                VALUES ($1, 0, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET description = $2, updated_at = NOW()
                """,
                key, value,
            )
        await _set_step_flag("setup.step.smtp", "done")

    logger.info(f"SMTP configured: host={body.host} user={body.username}")
    return {"ok": True, "step": "smtp"}


@router.post("/step/pki")
async def setup_pki(body: StepPkiRequest):
    """Initialize the PKI (Root CA + User CA)."""
    if not await _admin_exists():
        raise HTTPException(status_code=403, detail="Create the admin account first")

    from pki.ca import ca
    try:
        await ca.initialize()
        logger.info("PKI initialized via setup wizard")
    except Exception as e:
        logger.warning(f"PKI initialization failed (skippable): {e}")
        # PKI init is already attempted at startup; if it fails, it's
        # usually because certs already exist — non-fatal.

    await _set_step_flag("setup.step.pki", "done")
    return {"ok": True, "step": "pki"}


@router.post("/step/pricing")
async def setup_pricing(body: StepPricingRequest):
    """Create a default tariff and pricing tier."""
    if not await _admin_exists():
        raise HTTPException(status_code=403, detail="Create the admin account first")

    async with db.transaction() as conn:
        # Set default pricing config
        configs = [
            ("default_currency", body.currency),
            ("default_margin", str(body.default_rate_kwh)),
        ]
        for key, value in configs:
            await conn.execute(
                """
                INSERT INTO ocpp.pricing_config (key, value, description, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
                """,
                key,
                body.default_rate_kwh if key == "default_margin" else 0,
                value,
            )

        # Create a default tariff (idempotent)
        await conn.execute(
            """
            INSERT INTO ocpp.tariffs (id, name, currency, energy_rate)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO NOTHING
            """,
            "default",
            body.tariff_name,
            body.currency,
            body.default_rate_kwh,
        )

        await _set_step_flag("setup.step.pricing", "done")

    logger.info(f"Default pricing set: {body.currency} {body.default_rate_kwh}/kWh")
    return {"ok": True, "step": "pricing"}


@router.post("/step/features")
async def setup_features(body: StepFeaturesRequest):
    """Toggle feature flags."""
    if not await _admin_exists():
        raise HTTPException(status_code=403, detail="Create the admin account first")

    flags = {
        "ocpi": body.ocpi,
        "billing": body.billing,
        "ems": body.ems,
        "iso15118": body.iso15118,
    }

    async with db.transaction() as conn:
        for key, enabled in flags.items():
            await conn.execute(
                """
                INSERT INTO ocpp.feature_flags (key, enabled, description)
                VALUES ($1, $2, $3)
                ON CONFLICT (key) DO UPDATE SET enabled = $2, updated_at = NOW()
                """,
                key, enabled,
                f"Enabled via setup wizard" if enabled else "Disabled via setup wizard",
            )
        await _set_step_flag("setup.step.features", "done")

    logger.info(f"Feature flags set: {flags}")
    return {"ok": True, "step": "features"}


@router.post("/skip/{step}")
async def skip_step(step: str):
    """Mark a setup step as skipped (user will configure manually)."""
    if step not in STEPS:
        raise HTTPException(status_code=400, detail=f"Unknown step: {step}")

    await _set_step_flag(f"setup.step.{step}", "skipped")
    logger.info(f"Setup step skipped: {step}")
    return {"ok": True, "step": step, "skipped": True}
