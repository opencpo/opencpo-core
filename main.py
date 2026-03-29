"""
OCPP Core — Entry point.

Starts all services concurrently:
- OCPP 1.6j WebSocket server (port 9100)
- OCPP 2.0.1 WebSocket server (port 9201)
- REST API (port 8000)
- Event Bus (Redis Streams)
- PKI CA initialization
"""
import asyncio
import logging
import os
import signal
import sys

import structlog
import uvicorn
from dotenv import load_dotenv

from config import config
from state.postgres import db
from state.redis import redis_state
from events.bus import EventBus
from ocpp16.server import OCPP16Server
from ocpp16.handler import ChargePointHandler
from ocpp201.server import OCPP201Server
from ocpp201.handler import ChargePointHandler201
from pki.ca import ca

load_dotenv()


# ── Structured Logging ───────────────────────────────────────────────────

def setup_logging():
    """Configure structured logging (JSON or text)."""
    if config.log_format == "json":
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                logging.getLevelName(config.log_level)
            ),
        )
    else:
        structlog.configure(
            processors=[
                structlog.dev.ConsoleRenderer(),
            ],
        )

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(message)s",
        stream=sys.stdout,
    )


# ── Globals ──────────────────────────────────────────────────────────────

logger = structlog.get_logger()
event_bus = EventBus()

# Track active charge point handlers
_handlers_16: dict[str, ChargePointHandler] = {}
_handlers_201: dict[str, ChargePointHandler201] = {}


# ── OCPP 1.6j Connection Callbacks ──────────────────────────────────────

async def on_connect_16(cp_id: str, websocket) -> None:
    """New OCPP 1.6j charger connected."""
    handler = ChargePointHandler(cp_id, event_bus)
    _handlers_16[cp_id] = handler

    # Route messages through handler
    async for raw_message in websocket:
        if "MeterValues" in raw_message:
            logger.info(f"[{cp_id}] ← METER: {raw_message[:500]}")
        else:
            logger.debug(f"[{cp_id}] ← RAW: {raw_message[:200]}")
        response = await handler.handle_message(raw_message)
        if response:
            logger.debug(f"[{cp_id}] → RAW: {response[:200]}")
            await websocket.send(response)


async def on_disconnect_16(cp_id: str) -> None:
    """OCPP 1.6j charger disconnected."""
    handler = _handlers_16.pop(cp_id, None)
    if handler:
        await handler.on_disconnect()


# ── OCPP 2.0.1 Connection Callbacks ─────────────────────────────────────

async def on_connect_201(cp_id: str, websocket, client_cert=None) -> None:
    """New OCPP 2.0.1 charger connected."""
    handler = ChargePointHandler201(cp_id, event_bus, client_cert)
    _handlers_201[cp_id] = handler

    async for raw_message in websocket:
        response = await handler.handle_message(raw_message)
        if response:
            await websocket.send(response)


async def on_disconnect_201(cp_id: str) -> None:
    """OCPP 2.0.1 charger disconnected."""
    handler = _handlers_201.pop(cp_id, None)
    if handler:
        await handler.on_disconnect()


# ── Startup / Shutdown ───────────────────────────────────────────────────

async def startup():
    """Initialize all connections and services."""
    logger.info("Starting OCPP Core...")

    # Database
    await db.connect()
    logger.info("Database connected")

    # Redis
    await redis_state.connect()
    logger.info("Redis state connected")

    # Event bus
    await event_bus.connect()
    logger.info("Event bus connected")

    # Wire event bus into API events module
    from api.events import set_event_bus
    set_event_bus(event_bus)

    # PKI — initialize CA (load or generate)
    await ca.initialize()
    logger.info("PKI initialized")

    # Seed Redis with connector states from DB (survives restarts)
    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT c.charge_point, c.connector_id, c.status, c.error_code
              FROM ocpp.connectors c
              JOIN ocpp.charge_points cp ON cp.id = c.charge_point
             WHERE c.connector_id > 0
        """)
    for row in rows:
        await redis_state.set_charger(row["charge_point"], {
            f"connector_{row['connector_id']}_status": row["status"],
            f"connector_{row['connector_id']}_error": row["error_code"] or "NoError",
        })
    logger.info("Redis seeded with %d connector states from DB", len(rows))

    logger.info(
        "OCPP Core ready",
        ocpp16=f"ws://:{config.ocpp.ocpp16_port}",
        ocpp201=f"ws://:{config.ocpp.ocpp201_port}",
        api=f"http://:{config.api.port}",
    )


async def shutdown():
    """Clean shutdown of all services."""
    logger.info("Shutting down OCPP Core...")
    event_bus.stop()
    await event_bus.close()
    await redis_state.close()
    await db.close()
    logger.info("OCPP Core stopped. Goodbye.")


# ── Main ─────────────────────────────────────────────────────────────────

async def main():
    """Main entry point — runs all services concurrently."""
    setup_logging()
    await startup()

    # Create OCPP servers
    ocpp16_server = OCPP16Server(on_connect_16, on_disconnect_16)
    ocpp201_server = OCPP201Server(on_connect_201, on_disconnect_201)

    # Register charger registry backends
    from state.charger_registry import set_ocpp16_server, set_redis_state, set_handlers_16
    set_ocpp16_server(ocpp16_server)
    set_redis_state(redis_state)
    set_handlers_16(_handlers_16)

    # Create tasks for all services
    tasks = [
        asyncio.create_task(ocpp16_server.serve(), name="ocpp-1.6j"),
        asyncio.create_task(ocpp201_server.serve(), name="ocpp-2.0.1"),
    ]

    # Event consumers (optional — only start if configured)
    if os.getenv("LAGO_API_KEY"):
        from events.consumers import lago_billing
        tasks.append(asyncio.create_task(lago_billing.run(event_bus), name="lago-billing"))

    # REST API (uvicorn)
    api_config = uvicorn.Config(
        "api.main:app",
        host=config.api.host,
        port=config.api.port,
        log_level=config.log_level.lower(),
        access_log=False,
    )
    api_server = uvicorn.Server(api_config)
    tasks.append(asyncio.create_task(api_server.serve(), name="rest-api"))

    # Handle shutdown signals
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(tasks)))

    logger.info(f"All services running. {len(tasks)} tasks active.")

    # Wait for all tasks
    await asyncio.gather(*tasks, return_exceptions=True)
    await shutdown()


async def _shutdown(tasks: list[asyncio.Task]):
    """Cancel all tasks on signal."""
    for task in tasks:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
