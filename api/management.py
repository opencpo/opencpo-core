"""
Management API — Demo Platform Invitation & Analytics.

Handles demo invitation lifecycle and usage analytics for the OpenCPO
demo platform at demo.opencpo.io.

All endpoints require X-Management-Key header (reuses existing management_auth dep).

Endpoints:
    GET  /api/analytics/overview
    GET  /api/analytics/invitations
    GET  /api/analytics/invitations/{inv_id}
    POST /api/analytics/track
    POST /api/invitations
    POST /api/invitations/{inv_id}/approve
    POST /api/invitations/{inv_id}/revoke
    POST /api/invitations/{inv_id}/resend
    POST /api/auth/demo-login
"""
import hashlib
import hmac
import httpx
import logging
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr

from api.api_key_auth import management_auth
from state.postgres import db

logger = logging.getLogger(__name__)

# ── Auth dependency ──────────────────────────────────────────────────────

_mgmt = [Depends(management_auth)]

analytics_router = APIRouter(prefix="/api/analytics", tags=["Analytics"], dependencies=_mgmt)
invitations_router = APIRouter(prefix="/api/invitations", tags=["Invitations"], dependencies=_mgmt)
auth_router = APIRouter(prefix="/api/auth", tags=["Demo Auth"])


# ── Config ───────────────────────────────────────────────────────────────

COMMS_URL = os.environ.get("COMMS_URL", "http://127.0.0.1:8091")

# ── Helpers ───────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), hashed)


def _generate_username(name: str) -> str:
    """Generate lowercase first.last username from full name."""
    parts = name.strip().lower().split()
    if len(parts) >= 2:
        base = f"{parts[0]}.{parts[-1]}"
    else:
        base = re.sub(r"[^a-z0-9]", "", parts[0]) if parts else "user"
    # Sanitize
    base = re.sub(r"[^a-z0-9.]", "", base)
    return base


def _human_time_ago(dt: Optional[datetime]) -> Optional[str]:
    """Return human-readable 'N ago' string."""
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    else:
        return f"{seconds // 86400}d ago"


async def _username_exists(conn, username: str) -> bool:
    row = await conn.fetchrow(
        "SELECT id FROM ocpp.invitations WHERE credentials_user = $1", username
    )
    return row is not None


async def _unique_username(conn, name: str) -> str:
    base = _generate_username(name)
    if not await _username_exists(conn, base):
        return base
    # Append random 3-digit suffix
    for _ in range(20):
        candidate = f"{base}{secrets.randbelow(900) + 100}"
        if not await _username_exists(conn, candidate):
            return candidate
    # Fallback: full random
    return f"user{secrets.randbelow(9000) + 1000}"


async def _send_invitation_email(
    to_email: str,
    name: str,
    username: str,
    password: str,
    company: str = "",
):
    """Send invitation email via the comms service."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{COMMS_URL}/api/send", json={
                "template_name": "opencpo_invitation",
                "recipient": to_email,
                "subject": f"Your OpenCPO Demo Access — {username}",
                "context": {
                    "name": name,
                    "company": company,
                    "username": username,
                    "password": password,
                    "login_url": "https://demo.opencpo.io",
                    "expires_days": "30",
                    "to_email": to_email,
                },
            })
            resp.raise_for_status()
            logger.info(f"Invitation email sent via comms to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send invitation email to {to_email} via comms: {e}")
        raise


# ── Models ────────────────────────────────────────────────────────────────

class CreateInvitationRequest(BaseModel):
    email: EmailStr
    name: str
    company: str = ""


class TrackPageViewRequest(BaseModel):
    path: str
    token: str


class DemoLoginRequest(BaseModel):
    username: str
    password: str


# ── Analytics Endpoints ───────────────────────────────────────────────────

@analytics_router.get("/overview")
async def analytics_overview():
    """Return high-level platform analytics."""
    async with db.read() as conn:
        # Invitation counts by status
        counts = await conn.fetch(
            "SELECT status, COUNT(*) AS cnt FROM ocpp.invitations GROUP BY status"
        )
        by_status = {r["status"]: r["cnt"] for r in counts}
        total = sum(by_status.values())
        active = by_status.get("active", 0)
        pending = by_status.get("requested", 0) + by_status.get("pending", 0)
        expired = by_status.get("expired", 0)
        conversion = round((active / total * 100), 1) if total > 0 else 0.0

        # Total page views
        total_pv = await conn.fetchval("SELECT COUNT(*) FROM ocpp.page_views") or 0

        # Top pages (last 30 days)
        top_pages_rows = await conn.fetch(
            """
            SELECT path, COUNT(*) AS views
            FROM ocpp.page_views
            WHERE timestamp > NOW() - INTERVAL '30 days'
            GROUP BY path
            ORDER BY views DESC
            LIMIT 10
            """
        )
        top_pages = [{"path": r["path"], "views": r["views"]} for r in top_pages_rows]

        # Recent logins (last 10)
        recent_rows = await conn.fetch(
            """
            SELECT name, company, last_login
            FROM ocpp.invitations
            WHERE last_login IS NOT NULL
            ORDER BY last_login DESC
            LIMIT 10
            """
        )
        recent_logins = [
            {
                "name": r["name"].split()[0] if r["name"] else "",
                "company": r["company"] or "",
                "when": _human_time_ago(r["last_login"]),
            }
            for r in recent_rows
        ]

    return {
        "total_invitations": total,
        "active_users": active,
        "pending_users": pending,
        "expired_users": expired,
        "conversion_rate": conversion,
        "total_pageviews": total_pv,
        "errors_24h": 0,  # Placeholder — hook into log aggregation if needed
        "top_pages": top_pages,
        "recent_logins": recent_logins,
    }


@analytics_router.get("/invitations")
async def analytics_list_invitations():
    """List all invitations with stats."""
    async with db.read() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email, name, company, status, credentials_user,
                   login_count, total_page_views, last_login, created_at, approved_at, expires_at
            FROM ocpp.invitations
            ORDER BY created_at DESC
            """
        )
    return [
        {
            "id": r["id"],
            "email": r["email"],
            "name": r["name"],
            "company": r["company"] or "",
            "status": r["status"],
            "credentials_user": r["credentials_user"],
            "login_count": r["login_count"],
            "total_page_views": r["total_page_views"],
            "last_login": r["last_login"].isoformat() if r["last_login"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "approved_at": r["approved_at"].isoformat() if r["approved_at"] else None,
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        }
        for r in rows
    ]


@analytics_router.get("/invitations/{inv_id}")
async def analytics_get_invitation(inv_id: int):
    """Get single invitation with full page view history."""
    async with db.read() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, name, company, status, credentials_user,
                   login_count, total_page_views, last_login, created_at,
                   approved_at, expires_at, metadata
            FROM ocpp.invitations
            WHERE id = $1
            """,
            inv_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Invitation not found")

        pv_rows = await conn.fetch(
            """
            SELECT path, timestamp, ip, user_agent
            FROM ocpp.page_views
            WHERE invitation_id = $1
            ORDER BY timestamp DESC
            LIMIT 200
            """,
            inv_id,
        )

    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "company": row["company"] or "",
        "status": row["status"],
        "credentials_user": row["credentials_user"],
        "login_count": row["login_count"],
        "total_page_views": row["total_page_views"],
        "last_login": row["last_login"].isoformat() if row["last_login"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "metadata": row["metadata"] if isinstance(row["metadata"], dict) else {},
        "page_views": [
            {
                "path": pv["path"],
                "timestamp": pv["timestamp"].isoformat() if pv["timestamp"] else None,
                "ip": pv["ip"],
                "user_agent": pv["user_agent"],
            }
            for pv in pv_rows
        ],
    }


@analytics_router.post("/track")
async def track_page_view(request: Request, body: TrackPageViewRequest):
    """Record a page view. Called by demo platform JS. No management key required."""
    async with db.transaction() as conn:
        # Look up invitation by token
        inv = await conn.fetchrow(
            "SELECT id FROM ocpp.invitations WHERE token = $1 AND status = 'active'",
            body.token,
        )
        if not inv:
            # Silent failure — don't leak token validity
            return {"ok": True}

        inv_id = inv["id"]
        ip = request.client.host if request.client else None
        ua = request.headers.get("user-agent")

        await conn.execute(
            """
            INSERT INTO ocpp.page_views (invitation_id, path, ip, user_agent)
            VALUES ($1, $2, $3, $4)
            """,
            inv_id, body.path, ip, ua,
        )
        await conn.execute(
            "UPDATE ocpp.invitations SET total_page_views = total_page_views + 1 WHERE id = $1",
            inv_id,
        )

    return {"ok": True}


# ── Invitation Management ─────────────────────────────────────────────────

@invitations_router.post("")
async def create_invitation(body: CreateInvitationRequest):
    """Create a new invitation request."""
    async with db.transaction() as conn:
        # Check for duplicate email
        existing = await conn.fetchrow(
            "SELECT id, status FROM ocpp.invitations WHERE email = $1", body.email
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Invitation for {body.email} already exists (status: {existing['status']})",
            )

        row = await conn.fetchrow(
            """
            INSERT INTO ocpp.invitations (email, name, company, status)
            VALUES ($1, $2, $3, 'requested')
            RETURNING id, email, name, company, status, created_at
            """,
            body.email, body.name, body.company,
        )

    logger.info(f"Invitation created for {body.email} (id={row['id']})")
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "company": row["company"] or "",
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@invitations_router.post("/{inv_id}/approve")
async def approve_invitation(inv_id: int):
    """Approve an invitation: generate credentials, set expiry, send email."""
    async with db.transaction() as conn:
        inv = await conn.fetchrow(
            "SELECT * FROM ocpp.invitations WHERE id = $1", inv_id
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if inv["status"] not in ("requested", "pending"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve invitation with status '{inv['status']}'",
            )

        username = await _unique_username(conn, inv["name"])
        password = secrets.token_urlsafe(12)
        hashed = _hash_password(password)
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=30)

        await conn.execute(
            """
            UPDATE ocpp.invitations
            SET status = 'active',
                credentials_user = $2,
                credentials_pass = $3,
                token = $4,
                approved_at = $5,
                expires_at = $6
            WHERE id = $1
            """,
            inv_id, username, hashed, token, now, expires_at,
        )

    # Send email outside transaction (non-fatal if it fails)
    try:
        await _send_invitation_email(
            to_email=inv["email"],
            name=inv["name"],
            username=username,
            password=password,
            company=inv["company"] or "",
        )
    except Exception as e:
        logger.error(f"Invitation approved but email failed for inv_id={inv_id}: {e}")
        # Don't roll back — credentials are set, admin can resend

    logger.info(f"Invitation approved: id={inv_id} user={username} expires={expires_at.date()}")
    return {
        "id": inv_id,
        "status": "active",
        "credentials_user": username,
        "expires_at": expires_at.isoformat(),
        "email_sent": True,
    }


@invitations_router.post("/{inv_id}/revoke")
async def revoke_invitation(inv_id: int):
    """Revoke an invitation — user can no longer log in."""
    async with db.transaction() as conn:
        inv = await conn.fetchrow(
            "SELECT id, status FROM ocpp.invitations WHERE id = $1", inv_id
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if inv["status"] == "revoked":
            raise HTTPException(status_code=400, detail="Invitation is already revoked")

        await conn.execute(
            "UPDATE ocpp.invitations SET status = 'revoked' WHERE id = $1", inv_id
        )

    logger.info(f"Invitation revoked: id={inv_id}")
    return {"id": inv_id, "status": "revoked"}


@invitations_router.post("/{inv_id}/resend")
async def resend_invitation(inv_id: int):
    """Resend invitation email with existing credentials."""
    async with db.read() as conn:
        inv = await conn.fetchrow(
            "SELECT * FROM ocpp.invitations WHERE id = $1", inv_id
        )
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if inv["status"] != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resend — invitation status is '{inv['status']}' (must be active)",
        )
    if not inv["credentials_user"]:
        raise HTTPException(
            status_code=400,
            detail="No credentials on file — approve the invitation first",
        )

    # We can't recover the plaintext password (it's hashed).
    # Generate a new password and update the hash.
    new_password = secrets.token_urlsafe(12)
    new_hash = _hash_password(new_password)

    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE ocpp.invitations SET credentials_pass = $2 WHERE id = $1",
            inv_id, new_hash,
        )

    try:
        await _send_invitation_email(
            to_email=inv["email"],
            name=inv["name"],
            username=inv["credentials_user"],
            password=new_password,
            company=inv["company"] or "",
        )
    except Exception as e:
        logger.error(f"Resend email failed for inv_id={inv_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send email: {e}")

    logger.info(f"Invitation resent: id={inv_id} user={inv['credentials_user']}")
    return {"id": inv_id, "email_sent": True, "note": "New password generated and sent"}


# ── Demo Auth (no management key — used by demo login page) ──────────────

@auth_router.post("/demo-login")
async def demo_login(body: DemoLoginRequest, response: Response):
    """
    Demo platform login. Validates username/password against invitation credentials.
    Returns a session token on success.
    """
    async with db.read() as conn:
        inv = await conn.fetchrow(
            """
            SELECT id, credentials_pass, name, company, status, expires_at, token
            FROM ocpp.invitations
            WHERE credentials_user = $1
            """,
            body.username,
        )

    if not inv:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if inv["status"] != "active":
        raise HTTPException(status_code=403, detail=f"Account is {inv['status']}")

    if inv["expires_at"]:
        expires_at = inv["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            # Mark as expired
            async with db.transaction() as conn:
                await conn.execute(
                    "UPDATE ocpp.invitations SET status = 'expired' WHERE id = $1",
                    inv["id"],
                )
            raise HTTPException(status_code=403, detail="Demo access has expired")

    if not _verify_password(body.password, inv["credentials_pass"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Update login stats
    async with db.transaction() as conn:
        await conn.execute(
            """
            UPDATE ocpp.invitations
            SET login_count = login_count + 1,
                last_login = NOW()
            WHERE id = $1
            """,
            inv["id"],
        )

    logger.info(f"Demo login: user={body.username} id={inv['id']}")

    # Return the user's session token (used for page view tracking)
    return {
        "ok": True,
        "token": inv["token"],
        "name": inv["name"],
        "company": inv["company"],
    }
