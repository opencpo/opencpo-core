"""
Lago Billing Consumer — sends charging events to Lago for invoicing.

Listens for SESSION_CDR events on the event bus and forwards the kWh
delivered to Lago as billable events. Lago handles invoice generation,
tax calculation, and payment collection.

Consumer group: "lago-billing"
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

from events.bus import EventBus
from events.types import EventType

logger = logging.getLogger(__name__)

# Configuration
LAGO_API_URL = os.getenv("LAGO_API_URL", "http://127.0.0.1:3100")
LAGO_API_KEY = os.getenv("LAGO_API_KEY", "")
CONSUMER_GROUP = "lago-billing"
CONSUMER_NAME = "lago-billing-worker-1"


async def _resolve_subscription(charge_point: str) -> str | None:
    """
    Resolve a charge point to its Lago external_subscription_id.

    Lookup chain:
    1. Check Redis for charger → group mapping
    2. Use group external_id as the subscription prefix

    Returns external_subscription_id or None if not billable.
    """
    # For now, use a simple mapping via DB
    # TODO: cache in Redis for performance
    from state.postgres import db

    async with db.read() as c:
        row = await c.fetchrow("""
            SELECT g.external_id
            FROM ocpp.charge_points cp
            JOIN ocpp.groups g ON cp.group_id = g.id
            WHERE cp.id = $1 AND g.billing_method IS NOT NULL
        """, charge_point)

        if row and row["external_id"]:
            return f"{row['external_id']}_fleet_001"

    return None


async def _send_to_lago(
    transaction_id: str,
    subscription_id: str,
    kwh: float,
    timestamp: str,
    charge_point: str,
) -> bool:
    """Send a billable event to Lago. Returns True on success."""
    if not LAGO_API_KEY:
        logger.error("LAGO_API_KEY not configured — skipping billing event")
        return False

    payload = {
        "event": {
            "transaction_id": transaction_id,
            "external_subscription_id": subscription_id,
            "code": "kwh_delivered",
            "timestamp": int(datetime.fromisoformat(timestamp).timestamp())
            if timestamp
            else int(datetime.now(timezone.utc).timestamp()),
            "properties": {
                "kwh": round(kwh, 3),
            },
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{LAGO_API_URL}/api/v1/events",
                headers={
                    "Authorization": f"Bearer {LAGO_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if resp.status_code == 200:
            lago_event = resp.json().get("event", {})
            logger.info(
                f"Billing event sent to Lago: {transaction_id} | "
                f"{kwh:.3f} kWh | sub={subscription_id} | "
                f"lago_id={lago_event.get('lago_id', '?')}"
            )
            return True
        else:
            logger.error(
                f"Lago API error {resp.status_code}: {resp.text} | "
                f"transaction_id={transaction_id}"
            )
            return False

    except httpx.TimeoutException:
        logger.error(f"Lago API timeout for {transaction_id}")
        return False
    except Exception as e:
        logger.error(f"Lago API error for {transaction_id}: {e}")
        return False


async def run(event_bus: EventBus) -> None:
    """
    Main consumer loop. Listens for SESSION_CDR events and sends
    billing events to Lago.
    """
    logger.info(
        f"Lago billing consumer starting | "
        f"api={LAGO_API_URL} | group={CONSUMER_GROUP}"
    )

    if not LAGO_API_KEY:
        logger.warning(
            "LAGO_API_KEY not set — billing consumer will log events "
            "but NOT send to Lago. Set LAGO_API_KEY env var to enable."
        )

    async for event in event_bus.consume(
        group=CONSUMER_GROUP,
        consumer=CONSUMER_NAME,
        types={EventType.SESSION_CDR},
    ):
        session_id = event.session_id
        charge_point = event.charge_point
        kwh = event.data.get("energy_kwh", 0)

        if event.simulated:
            logger.debug(f"Skipping simulated CDR: {session_id}")
            continue

        if kwh <= 0:
            logger.debug(f"Skipping zero-energy CDR: {session_id}")
            continue

        # Resolve charge point → subscription
        subscription_id = await _resolve_subscription(charge_point)
        if not subscription_id:
            logger.info(
                f"No billable subscription for {charge_point} — "
                f"session {session_id} ({kwh:.3f} kWh) not billed"
            )
            continue

        # Transaction ID = session_id (idempotent — Lago deduplicates)
        success = await _send_to_lago(
            transaction_id=session_id,
            subscription_id=subscription_id,
            kwh=kwh,
            timestamp=event.timestamp,
            charge_point=charge_point,
        )

        if not success:
            logger.warning(
                f"Failed to bill session {session_id} — "
                f"will be retried via Redis XPENDING"
            )
            # Don't ack — the event bus consumer loop handles this
            # by not acking failed events (they stay in pending)
