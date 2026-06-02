"""Admin Update — one-click update mechanism.

Runs opencpo/update.sh to fetch latest release, rebuild Docker images,
and restart. Requires the install directory mounted at /app/opencpo
with Docker socket access.

Endpoints:
  GET  /api/v1/admin/update/status  — check current vs latest version
  POST /api/v1/admin/update/run     — trigger update
"""
import asyncio
import json
import logging
import os
import subprocess

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/update", tags=["Admin Update"])

UPDATE_SCRIPT = os.getenv("UPDATE_SCRIPT", "/app/opencpo/update.sh")


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


@router.get("/status")
async def update_status():
    """Check current installed version vs latest GitHub release."""
    try:
        data = _run_update(["--status", "--json"], timeout=30)
    except HTTPException:
        # Script not available — return unknown status
        return {
            "current": "unknown",
            "latest": "unknown",
            "needs_update": False,
            "script_available": False,
        }

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

    return result


@router.post("/run")
async def update_run():
    """Trigger an update to the latest version.

    Runs update.sh which:
    1. Backs up .env
    2. Downloads latest release from GitHub
    3. Extracts and copies files
    4. Restores .env
    5. docker compose build + docker compose up -d
    """
    logger.info("Update triggered via admin API")

    data = _run_update([], timeout=600)  # 10 min timeout for build+up

    log_msg = data.get("stdout", "")
    err_msg = data.get("stderr", "")
    if err_msg:
        logger.warning("Update stderr: %s", err_msg[:500])

    if data.get("exit_code", 1) != 0:
        logger.error("Update failed (exit=%s)", data.get("exit_code"))
        return {
            "ok": False,
            "exit_code": data.get("exit_code"),
            "log": log_msg[:2000],
            "error": err_msg[:500],
        }

    logger.info("Update completed successfully")
    return {
        "ok": True,
        "log": log_msg[:2000],
    }
