"""
Event Stream API — SSE + webhook endpoints.

The live data feed for all consumers:
- Charge App: connector status, session progress
- Client Portal: their sessions, their chargers
- CPO Admin: full firehose
- External systems: webhooks
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from events.bus import EventBus
from events.types import Event, EventType
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()

# Module-level event bus reference (set during startup)
_event_bus: EventBus | None = None


def set_event_bus(bus: EventBus):
    global _event_bus
    _event_bus = bus


def get_event_bus() -> EventBus:
    assert _event_bus is not None, "Event bus not initialized"
    return _event_bus


# ── SSE Live Stream ──────────────────────────────────────────────────────

# NOTE: In production, this endpoint should require management_auth or a read-only API key.
# For the demo, it's intentionally public to allow the admin dashboard to connect.
@router.get("/stream")
async def event_stream(
    request: Request,
    types: str = Query(None, description="Comma-separated event types: session,charger,auth,pki"),
    chargers: str = Query(None, description="Comma-separated charge point IDs"),
    sites: str = Query(None, description="Comma-separated site IDs"),
    since: str = Query(None, description="Replay from timestamp (ISO or Redis stream ID)"),
):
    """
    Server-Sent Events stream — filtered live events.
    
    Consumers subscribe to exactly what they need:
    - Charge App:  ?types=charger.status,session.start,session.meter,session.stop
    - Portal:      ?types=session&chargers=CP001,CP002
    - Admin:       (no filters = full firehose)
    - EMS:         ?types=session.meter,charger.status,charger.online,charger.offline
    """
    # Parse filters
    type_filter = None
    if types:
        type_filter = set()
        for t in types.split(","):
            t = t.strip()
            # Allow partial matches: "session" matches session.start, session.meter, etc.
            for et in EventType:
                if et.value == t or et.value.startswith(t + "."):
                    type_filter.add(et)

    charger_filter = set(c.strip() for c in chargers.split(",")) if chargers else None
    site_filter = set(s.strip() for s in sites.split(",")) if sites else None

    async def generate() -> AsyncGenerator[str, None]:
        bus = get_event_bus()
        # Each SSE connection gets its own ephemeral group starting from $ (no replay)
        group = f"sse-stream-{uuid.uuid4().hex[:8]}"
        consumer = "sse"
        await bus.ensure_group(group, start_id="$")

        try:
            async for event in bus.consume(
                group=group,
                consumer=consumer,
                types=type_filter,
                charge_points=charger_filter,
                sites=site_filter,
                batch_size=10,
                block_ms=30000,  # 30s long-poll
            ):
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                # SSE format
                yield f"event: {event.type.value}\n"
                yield f"data: {event.to_json()}\n"
                yield f"id: {event.event_id}\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            try:
                await bus._redis.xgroup_destroy(bus.stream_name, group)
            except Exception:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
        },
    )


# ── Event History ────────────────────────────────────────────────────────

@router.get("/history")
async def event_history(
    since: str = Query("-", description="Start (Redis stream ID or '-')"),
    until: str = Query("+", description="End (Redis stream ID or '+')"),
    count: int = Query(100, ge=1, le=1000),
    limit: int = Query(None, ge=1, le=1000, description="Alias for count"),
    charge_point: str = Query(None, description="Filter by charge point ID"),
    types: str = Query(None, description="Comma-separated event types to filter"),
):
    """Query historical events from the stream.

    Supports filtering by charge_point and event types. Both `count` and `limit`
    are accepted (limit takes precedence when provided).
    """
    bus = get_event_bus()
    effective_count = limit if limit is not None else count
    events = await bus.history(since=since, until=until, count=effective_count)

    # Post-filter by charge_point
    if charge_point:
        events = [e for e in events if e.charge_point == charge_point]

    # Post-filter by event types
    if types:
        requested = set(t.strip() for t in types.split(","))
        filtered = []
        for e in events:
            for t in requested:
                if e.type.value == t or e.type.value.startswith(t + "."):
                    filtered.append(e)
                    break
        events = filtered

    return {
        "events": [e.to_dict() for e in events],
        "count": len(events),
    }


# ── Stream Info ──────────────────────────────────────────────────────────

@router.get("/info")
async def stream_info():
    """Event bus health — stream length, consumer groups, lag."""
    bus = get_event_bus()
    return await bus.info()


# ── Single Resource Live Streams ─────────────────────────────────────────

@router.get("/chargers/{cp_id}/live")
async def charger_live(cp_id: str, request: Request):
    """Live SSE stream for a single charger — only new events, no history replay."""
    async def generate():
        bus = get_event_bus()
        # Each SSE connection gets its own ephemeral group starting from $
        group = f"sse-cp-{cp_id}-{uuid.uuid4().hex[:8]}"
        consumer = "sse"
        await bus.ensure_group(group, start_id="$")

        try:
            async for event in bus.consume(
                group=group,
                consumer=consumer,
                charge_points={cp_id},
                block_ms=30000,
            ):
                if await request.is_disconnected():
                    break
                yield f"event: {event.type.value}\ndata: {event.to_json()}\nid: {event.event_id}\n\n"
        finally:
            # Clean up ephemeral group on disconnect
            try:
                await bus._redis.xgroup_destroy(bus.stream_name, group)
            except Exception:
                pass

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/sessions/{session_id}/live")
async def session_live(session_id: str, request: Request):
    """Live SSE stream for a single session — only new events, no history replay."""
    async def generate():
        bus = get_event_bus()
        group = f"sse-sess-{uuid.uuid4().hex[:8]}"
        consumer = "sse"
        await bus.ensure_group(group, start_id="$")

        try:
            async for event in bus.consume(
                group=group,
                consumer=consumer,
                types={EventType.SESSION_METER, EventType.SESSION_STOP, EventType.SESSION_CDR},
                block_ms=30000,
            ):
                if await request.is_disconnected():
                    break
                if event.session_id == session_id:
                    yield f"event: {event.type.value}\ndata: {event.to_json()}\nid: {event.event_id}\n\n"
        finally:
            try:
                await bus._redis.xgroup_destroy(bus.stream_name, group)
            except Exception:
                pass

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Webhooks ─────────────────────────────────────────────────────────────

class WebhookCreate(BaseModel):
    url: str
    events: list[str]     # e.g., ["session.start", "session.stop"]
    secret: str = ""       # optional: HMAC signing key


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    created_at: str


@router.post("/webhooks")
async def create_webhook(webhook: WebhookCreate):
    """Register a webhook endpoint for event delivery."""
    async with db.write() as conn:
        row = await conn.fetchrow("""
            INSERT INTO ocpp.webhook_subscriptions (url, events, secret)
            VALUES ($1, $2, $3)
            RETURNING id::text AS id
        """, webhook.url, webhook.events, webhook.secret)

    webhook_id = row["id"]
    logger.info(f"Webhook registered: {webhook_id} → {webhook.url} events={webhook.events}")
    return {
        "id": webhook_id,
        "url": webhook.url,
        "events": webhook.events,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: str):
    """Remove a webhook."""
    async with db.write() as conn:
        result = await conn.execute(
            "DELETE FROM ocpp.webhook_subscriptions WHERE id = $1::uuid", webhook_id
        )
    if result == "DELETE 0":
        raise HTTPException(404, "Webhook not found")
    return {"status": "deleted", "id": webhook_id}
