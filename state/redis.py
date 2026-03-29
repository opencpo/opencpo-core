"""
Redis state backend — live charger state + active session state.

Redis holds what's happening RIGHT NOW. Postgres holds what happened.
"""
import json
import logging
from typing import Any

import redis.asyncio as aioredis

from config import config

logger = logging.getLogger(__name__)


class RedisState:
    """Live state management via Redis hashes."""

    def __init__(self, client: aioredis.Redis | None = None):
        self._redis: aioredis.Redis | None = client

    async def connect(self) -> None:
        if self._redis is None:
            self._redis = aioredis.from_url(
                config.redis.url,
                decode_responses=True,
            )
        await self._redis.ping()
        logger.info("Redis state connected", extra={"url": config.redis.url})

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    @property
    def client(self) -> aioredis.Redis:
        assert self._redis is not None, "Redis not connected"
        return self._redis

    # ── Charger State ────────────────────────────────────────────────────

    async def set_charger(self, cp_id: str, data: dict) -> None:
        """Update live charger state."""
        await self.client.hset(f"charger:{cp_id}", mapping={
            k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            for k, v in data.items()
        })

    async def get_charger(self, cp_id: str) -> dict | None:
        """Get live charger state."""
        data = await self.client.hgetall(f"charger:{cp_id}")
        return data if data else None

    async def del_charger(self, cp_id: str) -> None:
        """Remove charger from live state (offline)."""
        await self.client.delete(f"charger:{cp_id}")

    async def list_chargers(self) -> list[str]:
        """List all charger IDs with live state."""
        keys = []
        async for key in self.client.scan_iter(match="charger:*"):
            keys.append(key.replace("charger:", ""))
        return keys

    # ── Session State ────────────────────────────────────────────────────

    async def set_session(self, session_id: str, data: dict) -> None:
        """Update active session state."""
        await self.client.hset(f"session:{session_id}", mapping={
            k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            for k, v in data.items()
        })

    async def get_session(self, session_id: str) -> dict | None:
        """Get active session state."""
        data = await self.client.hgetall(f"session:{session_id}")
        return data if data else None

    async def del_session(self, session_id: str) -> None:
        """Remove session from live state (completed)."""
        await self.client.delete(f"session:{session_id}")

    async def update_session_meter(self, session_id: str, meter: dict) -> None:
        """Update latest meter reading for an active session."""
        await self.client.hset(f"session:{session_id}", mapping={
            "power_kw": str(meter.get("power_kw", 0)),
            "energy_kwh": str(meter.get("energy_kwh", 0)),
            "soc_pct": str(meter.get("soc_pct", 0)),
            "last_meter": meter.get("timestamp", ""),
        })

    async def list_active_sessions(self) -> list[str]:
        """List all active session IDs."""
        keys = []
        async for key in self.client.scan_iter(match="session:*"):
            keys.append(key.replace("session:", ""))
        return keys

    # ── Generic KV ───────────────────────────────────────────────────────

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        await self.client.set(key, str(value), ex=ttl)

    async def get(self, key: str) -> str | None:
        return await self.client.get(key)

    async def delete(self, key: str) -> None:
        """Delete a key from Redis."""
        await self.client.delete(key)

    async def keys(self, pattern: str) -> list[str]:
        """Return all keys matching a pattern (uses SCAN for safety)."""
        result = []
        async for key in self.client.scan_iter(match=pattern):
            result.append(key)
        return result

    # ── Health ───────────────────────────────────────────────────────────

    async def health(self) -> dict:
        try:
            info = await self.client.info("memory")
            return {
                "status": "ok",
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "connected_clients": (await self.client.info("clients")).get("connected_clients", 0),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


# Singleton
redis_state = RedisState()
