"""
Network API — Tailscale zero-trust network management.

All endpoints require management auth (MANAGEMENT_API_KEY).

Endpoints:
    GET  /api/v1/network/status        → Tailscale status (running, IP, hostname)
    GET  /api/v1/network/nodes         → List all nodes on the tailnet
    POST /api/v1/network/generate-key  → Generate a pre-auth key for a new site
    POST /api/v1/network/add-site      → Generate join command for a charger site
    GET  /api/v1/network/health        → Connectivity check to all known nodes
"""
import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state.settings import get_setting

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/network", tags=["Network"])

_TAILSCALE_API = "https://api.tailscale.com/api/v2"


# ── Models ────────────────────────────────────────────────────────────────

class AddSiteRequest(BaseModel):
    site_type: str = "charger"  # charger | server | proxy
    site_name: Optional[str] = None


class GenerateKeyRequest(BaseModel):
    site_type: str = "charger"
    reusable: bool = False
    expiry_seconds: int = 3600


# ── Helpers ───────────────────────────────────────────────────────────────

async def _get_tailscale_config() -> dict:
    """Return tailscale settings merged with defaults."""
    defaults = {
        "auth_key": "",
        "tailnet":  "",
        "api_key":  "",
        "enabled":  False,
    }
    stored = await get_setting("tailscale")
    merged = dict(defaults)
    merged.update(stored)
    return merged


def _local_tailscale_status() -> dict:
    """
    Query local Tailscale daemon if installed.
    Returns parsed JSON or empty dict.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {}


def _tags_for_site_type(site_type: str) -> list[str]:
    tag_map = {
        "charger": ["tag:charger-site"],
        "server":  ["tag:ocpp-server"],
        "proxy":   ["tag:edge-proxy"],
    }
    return tag_map.get(site_type, ["tag:charger-site"])




async def _ts_get(path: str, api_key: str) -> dict:
    """Make an authenticated GET request to the Tailscale API."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{_TAILSCALE_API}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        return r.json()


async def _ts_post(path: str, api_key: str, body: dict) -> dict:
    """Make an authenticated POST request to the Tailscale API."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{_TAILSCALE_API}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        r.raise_for_status()
        return r.json()


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/status")
async def network_status():
    """
    Return Tailscale network status.
    Combines local daemon status (if installed) with configured settings.
    """
    cfg = await _get_tailscale_config()
    local = _local_tailscale_status()

    # Extract from local daemon
    self_node = local.get("Self", {})
    hostname  = self_node.get("HostName", "")
    ts_ip     = ""
    if self_node.get("TailscaleIPs"):
        ts_ip = self_node["TailscaleIPs"][0]

    # Determine connected state
    backend_state = local.get("BackendState", "")
    connected = backend_state == "Running"

    # Count peers
    peers     = local.get("Peer", {})
    peer_count = len(peers) if peers else 0

    return {
        "enabled":         cfg.get("enabled", False),
        "configured":      bool(cfg.get("api_key")),
        "connected":       connected,
        "backend_state":   backend_state or ("not_installed" if not local else backend_state),
        "hostname":        hostname or None,
        "tailscale_ip":    ts_ip or None,
        "tailnet":         cfg.get("tailnet", "") or None,
        "peer_count":      peer_count,
        "local_available": bool(local),
    }


@router.get("/nodes")
async def list_nodes():
    """
    Return all devices on the tailnet.
    Uses Tailscale API if api_key is configured, otherwise returns empty list.
    """
    cfg = await _get_tailscale_config()
    api_key = cfg.get("api_key", "")
    tailnet = cfg.get("tailnet", "")

    if not api_key or not tailnet:
        return {
            "nodes":       [],
            "demo_mode":   False,
            "message":     "Add your Tailscale API key and tailnet name in Settings to see real devices.",
        }

    try:
        data = await _ts_get(f"/tailnet/{tailnet}/devices", api_key)
        devices = data.get("devices", [])

        nodes = []
        for d in devices:
            ts_ips = d.get("addresses", [])
            last_seen = d.get("lastSeen", "")
            online = False
            if last_seen:
                try:
                    ls = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                    delta = (datetime.now(timezone.utc) - ls).total_seconds()
                    online = delta < 180  # online if seen within 3 minutes
                except (ValueError, TypeError):
                    pass

            nodes.append({
                "id":        d.get("id", ""),
                "hostname":  d.get("hostname", ""),
                "addresses": ts_ips,
                "os":        d.get("os", ""),
                "lastSeen":  last_seen,
                "online":    online,
                "tags":      d.get("tags", []),
                "demo":      False,
            })

        return {"nodes": nodes, "demo_mode": False}

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise HTTPException(401, "Tailscale API key is invalid or expired")
        raise HTTPException(502, f"Tailscale API error: {exc.response.status_code}")
    except Exception as exc:
        logger.warning("Failed to fetch Tailscale nodes: %s", exc)
        raise HTTPException(502, "Could not reach Tailscale API")


@router.post("/generate-key")
async def generate_key(body: GenerateKeyRequest):
    """
    Generate a Tailscale pre-auth key.
    If API key not configured, returns an example command.
    """
    cfg = await _get_tailscale_config()
    api_key = cfg.get("api_key", "")
    tailnet = cfg.get("tailnet", "")

    tags = _tags_for_site_type(body.site_type)

    if not api_key or not tailnet:
        # Return example/placeholder
        return {
            "key":      "tskey-example-configure-api-key-in-settings",
            "reusable": body.reusable,
            "expires":  None,
            "tags":     tags,
            "demo_mode": True,
            "message":  "Configure your Tailscale API key in Settings to generate real keys.",
        }

    try:
        payload = {
            "capabilities": {
                "devices": {
                    "create": {
                        "reusable":      body.reusable,
                        "ephemeral":     False,
                        "preauthorized": True,
                        "tags":          tags,
                    }
                }
            },
            "expirySeconds": body.expiry_seconds,
        }
        result = await _ts_post(f"/tailnet/{tailnet}/keys", api_key, payload)
        return {
            "key":       result.get("key", ""),
            "reusable":  result.get("capabilities", {}).get("devices", {}).get("create", {}).get("reusable", False),
            "expires":   result.get("expires", ""),
            "tags":      tags,
            "demo_mode": False,
        }

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise HTTPException(401, "Tailscale API key is invalid or expired")
        raise HTTPException(502, f"Tailscale API error: {exc.response.status_code}")
    except Exception as exc:
        logger.warning("Failed to generate Tailscale key: %s", exc)
        raise HTTPException(502, "Could not generate key via Tailscale API")


@router.post("/add-site")
async def add_site(body: AddSiteRequest):
    """
    Generate a complete join command for a new charger site.
    Generates a fresh pre-auth key (or example if not configured).
    """
    cfg = await _get_tailscale_config()
    api_key = cfg.get("api_key", "")
    tailnet = cfg.get("tailnet", "")
    tags    = _tags_for_site_type(body.site_type)
    tags_str = ",".join(tags)

    if not api_key or not tailnet:
        example_key = "tskey-example-configure-api-key-in-settings"
        command = (
            f"curl -fsSL https://tailscale.com/install.sh | sh && "
            f"tailscale up --authkey={example_key} --advertise-tags={tags_str}"
        )
        return {
            "auth_key":  example_key,
            "command":   command,
            "tags":      tags,
            "demo_mode": True,
            "message":   "Configure your Tailscale API key in Settings to generate real join commands.",
            "site_type": body.site_type,
        }

    # Generate a real key
    key_resp = await generate_key(GenerateKeyRequest(
        site_type=body.site_type,
        reusable=False,
        expiry_seconds=3600,
    ))
    auth_key = key_resp["key"]
    command = (
        f"curl -fsSL https://tailscale.com/install.sh | sh && "
        f"tailscale up --authkey={auth_key} --advertise-tags={tags_str}"
    )

    return {
        "auth_key":  auth_key,
        "command":   command,
        "tags":      tags,
        "demo_mode": False,
        "site_type": body.site_type,
        "expires_in": "1 hour (single use)",
    }


@router.get("/health")
async def network_health():
    """
    Connectivity check to all known nodes.
    Pings each Tailscale peer to verify reachability.
    """
    cfg = await _get_tailscale_config()
    api_key = cfg.get("api_key", "")
    tailnet = cfg.get("tailnet", "")

    if not api_key or not tailnet:
        return {
            "overall": "unconfigured",
            "message": "Configure Tailscale in Settings to enable health checks.",
            "nodes":   [],
        }

    try:
        nodes_data = await list_nodes()
        nodes = nodes_data.get("nodes", [])
    except Exception:
        return {"overall": "error", "nodes": [], "message": "Could not fetch node list"}

    results = []
    online_count = 0

    for node in nodes:
        ts_ips = node.get("addresses", [])
        ts_ip = ts_ips[0] if ts_ips else None
        reachable = False

        if ts_ip:
            try:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "2", ts_ip],
                    capture_output=True,
                    timeout=5,
                )
                reachable = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                reachable = node.get("online", False)  # fallback to API status

        if reachable or node.get("online"):
            online_count += 1

        results.append({
            "hostname":  node.get("hostname", ""),
            "ts_ip":     ts_ip,
            "online":    node.get("online", False),
            "reachable": reachable,
        })

    total = len(results)
    if total == 0:
        overall = "no_nodes"
    elif online_count == total:
        overall = "healthy"
    elif online_count == 0:
        overall = "all_offline"
    else:
        overall = "degraded"

    return {
        "overall":       overall,
        "online_count":  online_count,
        "total_count":   total,
        "nodes":         results,
    }
