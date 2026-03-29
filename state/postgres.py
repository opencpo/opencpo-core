"""
PostgreSQL state backend — connection pool + core queries.

Primary for writes, replica for reads. asyncpg for performance.
"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from config import config

logger = logging.getLogger(__name__)


class Database:
    """Async PostgreSQL connection pool with primary/replica split."""

    def __init__(self):
        self._primary_pool: asyncpg.Pool | None = None
        self._replica_pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Initialize connection pools."""
        self._primary_pool = await asyncpg.create_pool(
            host=config.db.host,
            port=config.db.port,
            database=config.db.name,
            user=config.db.user,
            password=config.db.password,
            min_size=config.db.min_pool,
            max_size=config.db.max_pool,
            command_timeout=30,
        )
        logger.info(
            "Primary DB pool connected",
            extra={"host": config.db.host, "pool": f"{config.db.min_pool}-{config.db.max_pool}"},
        )

        # Replica pool (optional)
        if config.db.replica_host:
            self._replica_pool = await asyncpg.create_pool(
                host=config.db.replica_host,
                port=config.db.replica_port,
                database=config.db.name,
                user=config.db.user,
                password=config.db.password,
                min_size=2,
                max_size=config.db.max_pool // 2,
                command_timeout=30,
            )
            logger.info(
                "Replica DB pool connected",
                extra={"host": config.db.replica_host},
            )

    async def close(self) -> None:
        if self._primary_pool:
            await self._primary_pool.close()
        if self._replica_pool:
            await self._replica_pool.close()

    @property
    def primary(self) -> asyncpg.Pool:
        assert self._primary_pool is not None, "Database not connected"
        return self._primary_pool

    @property
    def replica(self) -> asyncpg.Pool:
        """Read replica, falls back to primary if no replica configured."""
        if self._replica_pool is not None:
            return self._replica_pool
        return self.primary

    @asynccontextmanager
    async def write(self) -> AsyncIterator[asyncpg.Connection]:
        """Get a connection from the primary pool (for writes)."""
        async with self.primary.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def read(self) -> AsyncIterator[asyncpg.Connection]:
        """Get a connection from the replica pool (for reads)."""
        async with self.replica.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """Get a connection with an active transaction."""
        async with self.primary.acquire() as conn:
            async with conn.transaction():
                yield conn

    async def health(self) -> dict:
        """Health check — returns pool stats."""
        result = {"primary": "error", "replica": "error"}
        try:
            async with self.write() as conn:
                await conn.fetchval("SELECT 1")
                result["primary"] = "ok"
                result["primary_pool"] = {
                    "size": self.primary.get_size(),
                    "free": self.primary.get_idle_size(),
                    "used": self.primary.get_size() - self.primary.get_idle_size(),
                }
        except Exception as e:
            result["primary_error"] = str(e)

        if self._replica_pool:
            try:
                async with self.read() as conn:
                    await conn.fetchval("SELECT 1")
                    result["replica"] = "ok"
                    result["replica_pool"] = {
                        "size": self.replica.get_size(),
                        "free": self.replica.get_idle_size(),
                    }
            except Exception as e:
                result["replica_error"] = str(e)
        else:
            result["replica"] = "not configured (using primary)"

        return result


# Singleton
db = Database()
