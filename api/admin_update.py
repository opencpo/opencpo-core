"""Admin Update — one-click update mechanism.

Runs opencpo/update.sh to fetch latest release, rebuild Docker images,
and restart. Requires the install directory mounted at /app/opencpo
with Docker socket access.

Endpoints:
  GET  /api/v1/admin/update/status  — check current vs latest version
  POST /api/v1/admin/update/run     — trigger update (with automatic backup)
"""
import json
import logging
import os
import subprocess

from fastapi import APIRouter, HTTPException

from state.postgres import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/update", tags=["Admin Update"])

UPDATE_SCRIPT = os.getenv("UPDATE_SCRIPT", "/app/opencpo/update.sh")
BACKUP_SCRIPT = os.getenv("BACKUP_SCRIPT", "/app/scripts/db-backup.sh")
BACKUP_DIR = os.getenv("BACKUP_DIR", "/app/backups")
CHANGELOG_URL = "https://github.com/opencpo/opencpo-core/releases"


def _run_update(args: list[str], timeout: int = 120) -> dict:
    """Run update.sh with given args and return parsed stdout."""
    if not os.path.exists(UPDATE_SCRIPT):
        raise HTTPException(
            status_code=503,
            detail=f"Update script not found at {UPDATE_SCRIPT}. "
                   "Ensure the install directory is mounted and Docker socket is available.",
        )

    env = {**os.environ, "AUTO_CONFIRM": "1", "NO_COLOR": "1", "JSON_OUTPUT": "1"}
    try:
        result = subprocess.run(
            [UPDATE_SCRIPT] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        # Try to parse JSON from the script's --json output
        data = {"exit_code": result.returncode, "stdout": stdout, "stderr": stderr}
        if stdout:
            try:
                parsed = json.loads(stdout)
                data.update(parsed)
            except json.JSONDecodeError:
                pass
        return data

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Update script timed out")
    except subprocess.CalledProcessError as e:
        return {"exit_code": e.returncode, "stdout": e.stdout or "", "stderr": e.stderr or ""}


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


async def _get_file_size(filename: str) -> int:
    """Get the size of a backup file on disk."""
    filepath = os.path.join(BACKUP_DIR, filename)
    try:
        return os.path.getsize(filepath)
    except (FileNotFoundError, OSError):
        return 0


@router.get("/status")
async def update_status():
    """Check current installed version vs latest GitHub release.

    Enhanced response includes changelog_url, backup_count, and last_update.
    """
    try:
        data = _run_update(["--status", "--json"], timeout=30)
    except HTTPException:
        # Script not available — return partial status
        result = {
            "current": "unknown",
            "latest": "unknown",
            "needs_update": False,
            "script_available": False,
        }
    else:
        # Try the json fields, fall back to parsing stdout
        result = {
            "current": data.get("current", "unknown"),
            "latest": data.get("latest", "unknown"),
            "needs_update": data.get("needs_update", False),
            "script_available": True,
            "exit_code": data.get("exit_code", 0),
        }

        # If exit_code == 1, update is available
        if data.get("exit_code") == 1 and result["latest"] != "unknown":
            result["needs_update"] = True

    # Enrich with additional fields
    result["changelog_url"] = CHANGELOG_URL

    try:
        async with db.read() as conn:
            # Count active backups
            backup_count = await conn.fetchval(
                "SELECT COUNT(*) FROM ocpp.backup_records WHERE status = 'active'"
            )
            result["backup_count"] = backup_count or 0

            # Last update event
            last_row = await conn.fetchrow(
                """
                SELECT event_type, status, to_version, details, created_at
                FROM ocpp.update_history
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            if last_row:
                result["last_update"] = {
                    "event_type": last_row["event_type"],
                    "status": last_row["status"],
                    "to_version": last_row["to_version"],
                    "details": last_row["details"] if isinstance(last_row["details"], dict) else
                               (json.loads(last_row["details"]) if last_row["details"] else {}),
                    "created_at": last_row["created_at"].isoformat() if hasattr(last_row["created_at"], "isoformat") else str(last_row["created_at"]),
                }
            else:
                result["last_update"] = None
    except Exception as e:
        logger.warning("Could not fetch backup count / last update: %s", e)
        result["backup_count"] = 0
        result["last_update"] = None

    return result


@router.post("/run")
async def update_run():
    """Trigger an update to the latest version.

    Runs a database backup FIRST, then triggers update.sh which:
    1. Backs up .env
    2. Downloads latest release from GitHub
    3. Extracts and copies files
    4. Restores .env
    5. docker compose build + docker compose up -d
    """
    logger.info("Update triggered via admin API")

    # ── Step 1: Create a backup before updating ──────────────────────────
    backup_info = None
    try:
        logger.info("Running pre-update database backup...")
        bk_data = _run_backup_script(["backup"], timeout=600)
        if bk_data.get("exit_code") == 0:
            stdout = bk_data.get("stdout", "")
            lines = stdout.strip().split("\n")
            filename = lines[-1].strip() if lines else ""
            if filename and filename.endswith(".dump"):
                size_bytes = await _get_file_size(filename)
                async with db.write() as conn:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO ocpp.backup_records (filename, size_bytes, notes)
                        VALUES ($1, $2, 'pre-update backup')
                        RETURNING id, filename, size_bytes
                        """,
                        filename, size_bytes,
                    )
                    if row:
                        backup_info = {
                            "id": row["id"],
                            "filename": row["filename"],
                            "size_bytes": row["size_bytes"],
                        }
                        logger.info("Pre-update backup created: %s (id=%s)", filename, row["id"])
        else:
            logger.warning("Pre-update backup failed (exit=%s): %s",
                          bk_data.get("exit_code"), bk_data.get("stderr", "")[:300])
    except Exception as e:
        logger.warning("Pre-update backup skipped due to error: %s", e)

    # ── Step 2: Run the actual update ────────────────────────────────────
    data = _run_update([], timeout=600)  # 10 min timeout for build+up

    log_msg = data.get("stdout", "")
    err_msg = data.get("stderr", "")
    if err_msg:
        logger.warning("Update stderr: %s", err_msg[:500])

    exit_code = data.get("exit_code", 1)
    success = exit_code == 0

    # ── Step 3: Record in update_history ──────────────────────────────────
    try:
        to_version = data.get("latest") or data.get("version") or data.get("current") or "unknown"
        async with db.write() as conn:
            await conn.execute(
                """
                INSERT INTO ocpp.update_history (event_type, to_version, status, details)
                VALUES ('update', $1, $2, $3)
                """,
                to_version,
                "completed" if success else "failed",
                json.dumps({
                    "exit_code": exit_code,
                    "backup_id": backup_info["id"] if backup_info else None,
                    "backup_filename": backup_info["filename"] if backup_info else None,
                    "log_preview": log_msg[:500],
                }),
            )
    except Exception as e:
        logger.warning("Failed to record update in history: %s", e)

    if not success:
        logger.error("Update failed (exit=%s)", exit_code)
        return {
            "ok": False,
            "exit_code": exit_code,
            "log": log_msg[:2000],
            "error": err_msg[:500],
            "backup": backup_info,
        }

    logger.info("Update completed successfully")
    return {
        "ok": True,
        "log": log_msg[:2000],
        "backup": backup_info,
    }
