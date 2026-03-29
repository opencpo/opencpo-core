"""
Event type definitions for the OCPP Core event bus.

Every state change in the system flows through typed events.
Consumers subscribe to exactly what they need.
"""
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
import json
import uuid


class EventType(str, Enum):
    # Charger lifecycle
    CHARGER_ONLINE = "charger.online"
    CHARGER_OFFLINE = "charger.offline"
    CHARGER_STATUS = "charger.status"
    CHARGER_CONFIG = "charger.config"
    CHARGER_FIRMWARE = "charger.firmware"
    CHARGER_BOOT = "charger.boot"

    # Session lifecycle
    SESSION_START = "session.start"
    SESSION_METER = "session.meter"
    SESSION_STOP = "session.stop"
    SESSION_CDR = "session.cdr"

    # Authorization
    AUTH_RESULT = "auth.result"

    # PKI
    PKI_CERT_ISSUED = "pki.cert.issued"
    PKI_CERT_EXPIRING = "pki.cert.expiring"
    PKI_CERT_REVOKED = "pki.cert.revoked"

    # EMS (published by external EMS, consumed by OCPP Core consumers)
    EMS_SITE_UPDATE = "ems.site.update"
    EMS_PROFILE_SET = "ems.profile.set"

    # Operations
    OPS_ALERT = "ops.alert"
    OPS_HEAL = "ops.heal"


@dataclass
class Event:
    """Base event structure for the event bus."""
    type: EventType
    data: dict[str, Any]
    charge_point: str = ""
    connector: int = 0
    session_id: str = ""
    site: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    simulated: bool = False  # True for virtual charger farm events

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d["type"] = EventType(d["type"])
        return cls(**d)

    @classmethod
    def from_stream(cls, stream_data: dict[bytes, bytes]) -> "Event":
        """Parse from Redis Stream entry."""
        decoded = {k.decode(): v.decode() for k, v in stream_data.items()}
        data = json.loads(decoded.get("data", "{}"))
        return cls(
            type=EventType(decoded["type"]),
            data=data,
            charge_point=decoded.get("charge_point", ""),
            connector=int(decoded.get("connector", "0")),
            session_id=decoded.get("session_id", ""),
            site=decoded.get("site", ""),
            timestamp=decoded.get("timestamp", ""),
            event_id=decoded.get("event_id", ""),
            simulated=decoded.get("simulated", "false") == "true",
        )

    def to_stream(self) -> dict[str, str]:
        """Serialize for Redis Stream XADD."""
        return {
            "type": self.type.value,
            "charge_point": self.charge_point,
            "connector": str(self.connector),
            "session_id": self.session_id,
            "site": self.site,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "simulated": str(self.simulated).lower(),
            "data": json.dumps(self.data, default=str),
        }
