# Event Bus

The event bus connects internal components without tight coupling. Every significant state change publishes a typed event. External services (billing, analytics, EMS, monitoring) consume events without modifying the core.

## Implementation

Redis Streams via `XADD` / `XREADGROUP`. Consumer groups provide at-least-once delivery — if a consumer crashes mid-processing, the unacknowledged message stays pending and is redelivered on restart.

## Event Types

```python
class EventType(str, Enum):
    # Charger lifecycle
    CHARGER_ONLINE = "charger.online"       # (unused, use CHARGER_BOOT)
    CHARGER_OFFLINE = "charger.offline"     # WebSocket disconnected
    CHARGER_STATUS = "charger.status"       # StatusNotification received
    CHARGER_CONFIG = "charger.config"       # Configuration changed
    CHARGER_FIRMWARE = "charger.firmware"   # Firmware update status changed
    CHARGER_BOOT = "charger.boot"           # BootNotification received

    # Session lifecycle
    SESSION_START = "session.start"         # StartTransaction accepted
    SESSION_METER = "session.meter"         # MeterValues batch
    SESSION_STOP = "session.stop"           # StopTransaction received
    SESSION_CDR = "session.cdr"             # Charge Detail Record generated

    # Authorization
    AUTH_RESULT = "auth.result"             # Authorize result (Accepted/Invalid/etc.)

    # PKI
    PKI_CERT_ISSUED = "pki.cert.issued"     # Certificate issued
    PKI_CERT_EXPIRING = "pki.cert.expiring" # Certificate expiring soon
    PKI_CERT_REVOKED = "pki.cert.revoked"   # Certificate revoked

    # EMS (published by external EMS, consumed here)
    EMS_SITE_UPDATE = "ems.site.update"     # Site power budget changed
    EMS_PROFILE_SET = "ems.profile.set"     # Charging profile pushed

    # Operations
    OPS_ALERT = "ops.alert"                 # Operational alert
    OPS_HEAL = "ops.heal"                   # Auto-heal action taken
```

## Event Structure

Every event has the same base fields:

```python
@dataclass
class Event:
    type: EventType
    data: dict           # Event-specific payload
    charge_point: str    # Charger ID (empty for global events)
    connector: int       # Connector ID (0 if N/A)
    session_id: str      # Session UUID (empty if N/A)
    site: str            # Site ID (empty if N/A)
    timestamp: str       # ISO 8601 UTC
    event_id: str        # UUID, unique per event
    simulated: bool      # True for virtual/test charger events
```

## Writing a Consumer

Consumers use `EventBus.consume()` with a consumer group. Groups are created automatically on first use.

```python
import asyncio
from events.bus import EventBus
from events.types import EventType

async def my_consumer(event_bus: EventBus):
    async for event in event_bus.consume(
        group="my-service",          # Consumer group name (unique per service)
        consumer="worker-1",         # Consumer instance name (unique per process)
        types={EventType.SESSION_CDR, EventType.SESSION_STOP},  # Filter by type
    ):
        print(f"Session {event.session_id} on {event.charge_point}: {event.data}")
        # Event is auto-acknowledged after the loop body completes
```

### Filtering

You can filter server-side by event type, charge point, or site:

```python
async for event in event_bus.consume(
    group="analytics",
    consumer="worker-1",
    types={EventType.SESSION_METER},
    charge_points={"CP-001", "CP-002"},   # Only these chargers
    sites={"site-amsterdam"},             # Only this site
):
    ...
```

### Error Handling

If your handler raises an exception, the event is **not** acknowledged and will be redelivered. This is correct for transient errors (DB temporarily unavailable). For permanent errors (malformed data), acknowledge explicitly:

```python
from events.bus import EventBus
from events.types import Event

async def safe_consumer(event_bus: EventBus):
    async for event in event_bus.consume(group="my-service", consumer="w1"):
        try:
            await process_event(event)
        except PermanentError as e:
            # Log and skip — don't block the consumer
            print(f"Skipping event {event.event_id}: {e}")
            # Event is acknowledged by the async-for iterator after yield
            # To prevent redelivery on permanent errors, don't raise
```

## Example: Billing Consumer

```python
# events/consumers/billing.py
import asyncio
import logging
from events.bus import EventBus
from events.types import EventType, Event

logger = logging.getLogger(__name__)


async def run(event_bus: EventBus) -> None:
    """Consume SESSION_CDR events and submit to billing provider."""
    logger.info("Billing consumer started")

    async for event in event_bus.consume(
        group="billing",
        consumer="billing-worker-1",
        types={EventType.SESSION_CDR},
    ):
        session_id = event.session_id
        energy_kwh = event.data.get("energy_kwh", 0)
        cost_total = event.data.get("cost_total")

        if event.simulated:
            # Skip test/virtual charger events
            continue

        logger.info(
            f"CDR: session={session_id} energy={energy_kwh:.3f}kWh cost={cost_total}"
        )

        try:
            await submit_to_billing_api(session_id, energy_kwh, cost_total)
        except Exception as e:
            logger.error(f"Billing API error for {session_id}: {e}")
            raise  # Will cause redelivery
```

Register your consumer in `main.py`:

```python
from events.consumers import my_billing_consumer

tasks.append(asyncio.create_task(
    my_billing_consumer.run(event_bus),
    name="my-billing",
))
```

## Replaying History

To replay events from a specific point (e.g., after a consumer was down):

```python
events = await event_bus.history(
    since="-",           # Start of stream
    until="+",           # End of stream
    count=1000,
)
```

Use Redis Stream IDs (e.g., `"1711234567890-0"`) for `since`/`until` to replay from a specific timestamp.

## Stream Info

```python
info = await event_bus.info()
# {
#   "stream": "ocpp:events",
#   "length": 42031,
#   "groups": [
#     {"name": "billing", "consumers": 1, "pending": 0},
#     {"name": "analytics", "consumers": 2, "pending": 3},
#   ]
# }
```
