"""
In-process settings cache.

Runtime-configurable settings stored in ocpp.settings (DB).
Cached in-process for 60s. Invalidated immediately on PUT.

Usage:
    from state.settings import get_setting, put_setting, mask_secrets

    sms = await get_setting("sms")       # {"provider": "bird", "api_key": "..."}
    await put_setting("sms", new_value)  # writes to DB + updates cache
    safe = mask_secrets(sms)             # {"provider": "bird", "api_key": "****"}
"""
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# In-process cache: {key: (value_dict, monotonic_timestamp)}
_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 60.0  # seconds

# Fields treated as secrets — masked to "****" in API responses
_SECRET_FIELDS = {"api_key", "password", "auth_token", "client_secret", "access_token"}


async def get_setting(key: str) -> dict:
    """Return setting dict for key. Reads DB on first access or cache expiry."""
    cached = _cache.get(key)
    if cached is not None and (time.monotonic() - cached[1]) < _CACHE_TTL:
        return cached[0]
    return await _load_from_db(key)


async def put_setting(key: str, value: dict) -> None:
    """Write setting to DB and refresh cache immediately."""
    from state.postgres import db
    async with db.write() as conn:
        await conn.execute(
            """
            INSERT INTO ocpp.settings (key, value, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    updated_at = NOW()
            """,
            key,
            json.dumps(value),
        )
    _cache[key] = (value, time.monotonic())
    logger.info("Setting '%s' updated and cache refreshed", key)


async def get_all_settings() -> dict[str, dict]:
    """Return all settings as {key: value_dict}. Refreshes each entry."""
    from state.postgres import db
    async with db.read() as conn:
        rows = await conn.fetch("SELECT key, value FROM ocpp.settings ORDER BY key")

    result: dict[str, dict] = {}
    now = time.monotonic()
    for row in rows:
        val = row["value"] if isinstance(row["value"], dict) else json.loads(row["value"])
        _cache[row["key"]] = (val, now)
        result[row["key"]] = val
    return result


def invalidate(key: str) -> None:
    """Evict key from in-process cache (next read hits DB)."""
    _cache.pop(key, None)


def mask_secrets(data: dict) -> dict:
    """Return copy of data with secret field values replaced by '****'."""
    return {
        k: ("****" if k in _SECRET_FIELDS and v else v)
        for k, v in data.items()
    }


async def _load_from_db(key: str) -> dict:
    """Load single setting from DB into cache. Returns {} if missing."""
    from state.postgres import db
    try:
        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM ocpp.settings WHERE key = $1", key
            )
        if row:
            val = row["value"] if isinstance(row["value"], dict) else json.loads(row["value"])
            _cache[key] = (val, time.monotonic())
            return val
    except Exception as exc:
        logger.warning("Failed to load setting '%s' from DB: %s", key, exc)
    return {}
