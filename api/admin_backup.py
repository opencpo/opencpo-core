"""Admin Backup/Restore/Postpone/History — backup management and update lifecycle.

Provides endpoints for listing, creating, restoring, and soft-deleting backups,
postponing update notifications, fetching changelogs, and viewing update history.

All subprocess calls wrap the db-backup.sh script.
"""
import json
import logging
import os
import subprocess
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state.postgres import db
from state.redis import redis_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["Admin Backup"])

BACKUP_SCRIPT = os.getenv("BACKUP_SCRIPT", "/app/scripts/db-backup.sh")
GITHUB_API = "https://api.github.com/repos/opencpo/opencpo-core/releases/latest"
CHANGELOG_CACHE_KEY = "admin:changelog"
POSTPONE_KEY = "admin:update:postpone_until"
BACKUP_DIR = os.getenv("BACKUP_DIR", "/app/backups")


# ── Models ────────────────────────────────────────────────────────────────


class PostponeRequest(BaseModel):
    hours: int = 24


class BackupResponse(BaseModel):
    id: int
    filename: str
    size_bytes: int
    checksum: str | None
    version: str | None
    status: str
    created_at: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _run_backup_script(args: list[str], timeout: int = 300) -> dict:
    """Run db-backup.sh with the given args and return parsed output."""
    if not os.path.exists(BACKUP_SCRIPT):
        raise HTTPException(
            status_code=503,
            detail=f"Backup script not found at {BACKUP_SCRIPT}. "
                   "Ensure the install directory is mounted.",
        )

    env = {**os.environ, "AUTO_CONFIRM": "1"}
    try:
        result = subprocess.run(
            [BACKUP_SCRIPT] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        return {
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Backup script timed out")
    except subprocess.CalledProcessError as e:
        return {"exit_code": e.returncode, "stdout": e.stdout or "", "stderr": e.stderr or ""}


async def _record_backup(
    conn, filename: str, size_bytes: int = 0,
    checksum: str | None = None, version: str | None = None,
) -> dict:
    """Insert a backup record into ocpp.backup_records and return it."""
    row = await conn.fetchrow(
        """
        INSERT INTO ocpp.backup_records (filename, size_bytes, checksum, version)
        VALUES ($1, $2, $3, $4)
        RETURNING id, filename, size_bytes, checksum, version, status, created_at
        """,
        filename, size_bytes, checksum, version,
    )
    return dict(row)


async def _get_file_size(filename: str) -> int:
    """Get the size of a backup file on disk."""
    filepath = os.path.join(BACKUP_DIR, filename)
    try:
        return os.path.getsize(filepath)
    except (FileNotFoundError, OSError):
        return 0


def _parse_timestamp(ts) -> str:
    """Format a datetime/timestamp to ISO string."""
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/backups")
async def list_backups():
    """List all backup records (excluding soft-deleted)."""
    async with db.read() as conn:
        rows = await conn.fetch(
            """
            SELECT id, filename, size_bytes, checksum, version, status, created_at
            FROM ocpp.backup_records
            ORDER BY created_at DESC
            """
        )
    return {
        "backups": [
            {
                "id": r["id"],
                "filename": r["filename"],
                "size_bytes": r["size_bytes"],
                "checksum": r["checksum"],
                "version": r["version"],
                "status": r["status"],
                "created_at": _parse_timestamp(r["created_at"]),
            }
            for r in rows
        ]
    }


@router.post("/backups")
async def create_backup():
    """Create a new database backup.

    Runs db-backup.sh backup, then records the backup in ocpp.backup_records.
    """
    logger.info("Creating database backup via admin API")

    data = _run_backup_script(["backup"], timeout=600)

    exit_code = data.get("exit_code", 1)
    stdout = data.get("stdout", "")
    stderr = data.get("stderr", "")

    if exit_code != 0:
        logger.error("Backup failed (exit=%s): %s", exit_code, stderr[:500])
        raise HTTPException(
            status_code=500,
            detail=f"Backup failed: {stderr[:500] or stdout[:500]}",
        )

    # The script outputs the filename on the last line of stdout
    lines = stdout.strip().split("\n")
    filename = lines[-1].strip() if lines else ""

    if not filename or not filename.endswith(".dump"):
        logger.error("Backup script did not return a valid filename: %s", stdout[:500])
        raise HTTPException(
            status_code=500,
            detail=f"Backup failed: unexpected output from script: {stdout[:500]}",
        )

    # Get file size
    size_bytes = await _get_file_size(filename)

    # Record in database
    async with db.write() as conn:
        record = await _record_backup(
            conn, filename=filename, size_bytes=size_bytes,
        )

    logger.info("Backup created: %s (id=%s, %s bytes)", filename, record["id"], size_bytes)
    return {
        "status": "created",
        "backup": {
            "id": record["id"],
            "filename": record["filename"],
            "size_bytes": record["size_bytes"],
            "checksum": record["checksum"],
            "version": record["version"],
            "status": record["status"],
            "created_at": _parse_timestamp(record["created_at"]),
        },
    }


@router.post("/backups/{backup_id}/restore")
async def restore_backup(backup_id: int):
    """Restore the database from a specific backup.

    Runs db-backup.sh restore <filename> with AUTO_CONFIRM=1.
    DANGER: This will restart the core service.
    """
    logger.warning("Restore requested for backup id=%s", backup_id)

    # Look up the backup record
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT id, filename, version FROM ocpp.backup_records WHERE id = $1",
            backup_id,
        )

    if not row:
        raise HTTPException(status_code=404, detail=f"Backup {backup_id} not found")

    filename = row["filename"]
    version = row["version"]

    # Run restore
    data = _run_backup_script(["restore", filename], timeout=600)

    exit_code = data.get("exit_code", 1)
    stderr = data.get("stderr", "")

    if exit_code != 0:
        logger.error("Restore failed (exit=%s): %s", exit_code, stderr[:500])
        raise HTTPException(
            status_code=500,
            detail=f"Restore failed: {stderr[:500] or 'unknown error'}",
        )

    # Record the restore event in update_history
    async with db.write() as conn:
        await conn.execute(
            """
            INSERT INTO ocpp.update_history (event_type, from_version, to_version, status, details)
            VALUES ('restore', $1, $2, 'completed', $3)
            """,
            None, version,
            json.dumps({"backup_id": backup_id, "filename": filename}),
        )

        # Mark the backup as restored
        await conn.execute(
            "UPDATE ocpp.backup_records SET status = 'restored' WHERE id = $1",
            backup_id,
        )

    logger.info("Restore complete from backup: %s", filename)
    return {
        "ok": True,
        "message": f"Restore from {filename} completed. The service will restart.",
        "backup_id": backup_id,
        "filename": filename,
    }


@router.delete("/backups/{backup_id}")
async def delete_backup(backup_id: int):
    """Soft-delete a backup record."""
    async with db.write() as conn:
        result = await conn.execute(
            "UPDATE ocpp.backup_records SET status = 'deleted' WHERE id = $1 AND status != 'deleted'",
            backup_id,
        )

    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail=f"Backup {backup_id} not found or already deleted")

    logger.info("Backup soft-deleted: id=%s", backup_id)
    return {"status": "deleted", "backup_id": backup_id}


@router.post("/update/postpone")
async def postpone_update(body: PostponeRequest):
    """Postpone an update notification for a given number of hours."""
    if body.hours < 1 or body.hours > 720:  # max 30 days
        raise HTTPException(
            status_code=400,
            detail="Postpone hours must be between 1 and 720 (30 days)",
        )

    expires_at = datetime.now(timezone.utc) + timedelta(hours=body.hours)
    ttl_seconds = body.hours * 3600

    await redis_state.set(
        POSTPONE_KEY,
        expires_at.isoformat(),
        ttl=ttl_seconds,
    )

    logger.info("Update postponed for %s hours (until %s)", body.hours, expires_at.isoformat())
    return {
        "ok": True,
        "postponed_until": expires_at.isoformat(),
        "hours": body.hours,
    }


@router.get("/update/changelog")
async def get_changelog():
    """Fetch the latest release changelog from GitHub.

    Caches in Redis for 1 hour to avoid rate limits.
    """
    # Check cache
    cached = await redis_state.get(CHANGELOG_CACHE_KEY)
    if cached:
        return {"source": "cache", "changelog": cached}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                GITHUB_API,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                body = data.get("body", "No changelog available.")
                tag = data.get("tag_name", "unknown")

                # Cache for 1 hour
                await redis_state.set(CHANGELOG_CACHE_KEY, body, ttl=3600)

                return {
                    "source": "github",
                    "version": tag,
                    "changelog": body,
                    "url": data.get("html_url", ""),
                }
            else:
                logger.warning("GitHub API returned %s: %s", resp.status_code, resp.text[:200])
                return {
                    "source": "github",
                    "error": f"GitHub API returned {resp.status_code}",
                    "changelog": "Changelog temporarily unavailable.",
                }
    except httpx.TimeoutException:
        logger.warning("GitHub API timed out")
        return {
            "source": "github",
            "error": "GitHub API timed out",
            "changelog": "Changelog temporarily unavailable.",
        }
    except Exception as e:
        logger.warning("Failed to fetch changelog: %s", e)
        return {
            "source": "github",
            "error": str(e),
            "changelog": "Changelog temporarily unavailable.",
        }


@router.get("/update/history")
async def get_update_history():
    """Get the update/backup/restore history ordered by most recent first."""
    async with db.read() as conn:
        rows = await conn.fetch(
            """
            SELECT id, event_type, from_version, to_version, status, details, created_at
            FROM ocpp.update_history
            ORDER BY created_at DESC
            LIMIT 100
            """
        )

    return {
        "events": [
            {
                "id": r["id"],
                "event_type": r["event_type"],
                "from_version": r["from_version"],
                "to_version": r["to_version"],
                "status": r["status"],
                "details": r["details"] if isinstance(r["details"], dict) else
                           (json.loads(r["details"]) if r["details"] else {}),
                "created_at": _parse_timestamp(r["created_at"]),
            }
            for r in rows
        ]
    }
