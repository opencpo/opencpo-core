"""
Event Bus — Redis Streams producer and consumer.

Producers publish events. Consumers subscribe via consumer groups.
Each consumer group reads independently — no message loss on downtime.
"""
import asyncio
import logging
from typing import AsyncIterator, Callable, Awaitable

import redis.asyncio as aioredis

from config import config
from events.types import Event, EventType

logger = logging.getLogger(__name__)


class EventBus:
    """Redis Streams-based event bus for real-time data distribution."""

    def __init__(self, redis_client: aioredis.Redis | None = None):
        self._redis: aioredis.Redis | None = redis_client
        self._prefix = config.events.stream_prefix
        self._max_len = config.events.max_stream_length
        self._running = False

    async def connect(self) -> None:
        if self._redis is None:
            self._redis = aioredis.from_url(
                config.redis.url,
                decode_responses=False,
            )
        # Verify connection
        await self._redis.ping()
        logger.info("Event bus connected to Redis", extra={"url": config.redis.url})

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    @property
    def stream_name(self) -> str:
        return f"{self._prefix}:events"

    # ── Producer ─────────────────────────────────────────────────────────

    async def publish(self, event: Event) -> str:
        """
        Publish an event to the stream.
        Returns the stream message ID.
        """
        assert self._redis is not None, "Event bus not connected"

        msg_id = await self._redis.xadd(
            self.stream_name,
            event.to_stream(),
            maxlen=self._max_len,
            approximate=True,
        )
        logger.debug(
            "Event published",
            extra={
                "event_type": event.type.value,
                "charge_point": event.charge_point,
                "stream_id": msg_id,
            },
        )
        return msg_id

    # ── Consumer ─────────────────────────────────────────────────────────

    async def ensure_group(self, group: str) -> None:
        """Create consumer group if it doesn't exist."""
        try:
            await self._redis.xgroup_create(
                self.stream_name, group, id="0", mkstream=True
            )
            logger.info(f"Consumer group '{group}' created")
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            # Group already exists — fine

    async def consume(
        self,
        group: str,
        consumer: str,
        types: set[EventType] | None = None,
        charge_points: set[str] | None = None,
        sites: set[str] | None = None,
        batch_size: int = 10,
        block_ms: int | None = None,
    ) -> AsyncIterator[Event]:
        """
        Consume events from the stream with optional filters.

        Filters are applied server-side for efficiency:
        - types: only these event types
        - charge_points: only these charge point IDs
        - sites: only these site IDs
        """
        assert self._redis is not None, "Event bus not connected"

        if block_ms is None:
            block_ms = config.events.consumer_block_ms

        await self.ensure_group(group)
        self._running = True

        while self._running:
            try:
                results = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={self.stream_name: ">"},
                    count=batch_size,
                    block=block_ms,
                )

                if not results:
                    continue

                for stream_name, messages in results:
                    for msg_id, msg_data in messages:
                        try:
                            event = Event.from_stream(msg_data)

                            # Apply filters
                            if types and event.type not in types:
                                await self._redis.xack(self.stream_name, group, msg_id)
                                continue
                            if charge_points and event.charge_point not in charge_points:
                                await self._redis.xack(self.stream_name, group, msg_id)
                                continue
                            if sites and event.site not in sites:
                                await self._redis.xack(self.stream_name, group, msg_id)
                                continue

                            yield event

                            # Acknowledge after successful processing
                            await self._redis.xack(self.stream_name, group, msg_id)

                        except Exception as e:
                            logger.error(
                                f"Error processing event {msg_id}: {e}",
                                exc_info=True,
                            )
                            # Don't ack — will be retried via XPENDING

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Consumer error: {e}", exc_info=True)
                await asyncio.sleep(1)

    def stop(self) -> None:
        self._running = False

    # ── History / Replay ─────────────────────────────────────────────────

    async def history(
        self,
        since: str = "-",
        until: str = "+",
        count: int = 100,
    ) -> list[Event]:
        """Read historical events from the stream."""
        assert self._redis is not None

        results = await self._redis.xrange(
            self.stream_name, min=since, max=until, count=count
        )
        events = []
        for msg_id, msg_data in results:
            try:
                events.append(Event.from_stream(msg_data))
            except Exception as e:
                logger.warning(f"Skipping malformed event {msg_id}: {e}")
        return events

    # ── Stream Info ──────────────────────────────────────────────────────

    async def info(self) -> dict:
        """Get stream info (length, groups, consumers)."""
        assert self._redis is not None

        try:
            stream_info = await self._redis.xinfo_stream(self.stream_name)
            groups_info = await self._redis.xinfo_groups(self.stream_name)
            return {
                "stream": self.stream_name,
                "length": stream_info.get("length", 0),
                "groups": [
                    {
                        "name": g.get("name", b"").decode() if isinstance(g.get("name"), bytes) else g.get("name", ""),
                        "consumers": g.get("consumers", 0),
                        "pending": g.get("pending", 0),
                        "last_delivered": g.get("last-delivered-id", ""),
                    }
                    for g in groups_info
                ],
            }
        except aioredis.ResponseError:
            return {"stream": self.stream_name, "length": 0, "groups": []}
