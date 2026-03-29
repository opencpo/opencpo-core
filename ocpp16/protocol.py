"""
OCPP 1.6j Protocol — message framing and types.

OCPP-J 1.6 uses JSON arrays over WebSocket:
  [MessageTypeId, UniqueId, Action, Payload]      → Call (2)
  [MessageTypeId, UniqueId, Payload]               → CallResult (3)
  [MessageTypeId, UniqueId, ErrorCode, ErrorDesc, ErrorDetails]  → CallError (4)
"""
import json
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class MessageType(IntEnum):
    CALL = 2
    CALL_RESULT = 3
    CALL_ERROR = 4


# OCPP 1.6j Actions (charger → server)
class Action:
    AUTHORIZE = "Authorize"
    BOOT_NOTIFICATION = "BootNotification"
    DATA_TRANSFER = "DataTransfer"
    DIAGNOSTICS_STATUS = "DiagnosticsStatusNotification"
    FIRMWARE_STATUS = "FirmwareStatusNotification"
    HEARTBEAT = "Heartbeat"
    METER_VALUES = "MeterValues"
    START_TRANSACTION = "StartTransaction"
    STATUS_NOTIFICATION = "StatusNotification"
    STOP_TRANSACTION = "StopTransaction"


# OCPP 1.6j Actions (server → charger)
class ServerAction:
    CANCEL_RESERVATION = "CancelReservation"
    CHANGE_AVAILABILITY = "ChangeAvailability"
    CHANGE_CONFIGURATION = "ChangeConfiguration"
    CLEAR_CACHE = "ClearCache"
    CLEAR_CHARGING_PROFILE = "ClearChargingProfile"
    DATA_TRANSFER = "DataTransfer"
    GET_COMPOSITE_SCHEDULE = "GetCompositeSchedule"
    GET_CONFIGURATION = "GetConfiguration"
    GET_DIAGNOSTICS = "GetDiagnostics"
    GET_LOCAL_LIST_VERSION = "GetLocalListVersion"
    REMOTE_START_TRANSACTION = "RemoteStartTransaction"
    REMOTE_STOP_TRANSACTION = "RemoteStopTransaction"
    RESERVE_NOW = "ReserveNow"
    RESET = "Reset"
    SEND_LOCAL_LIST = "SendLocalList"
    SET_CHARGING_PROFILE = "SetChargingProfile"
    TRIGGER_MESSAGE = "TriggerMessage"
    UNLOCK_CONNECTOR = "UnlockConnector"
    UPDATE_FIRMWARE = "UpdateFirmware"


# Registration status
class RegistrationStatus:
    ACCEPTED = "Accepted"
    PENDING = "Pending"
    REJECTED = "Rejected"


# Authorization status
class AuthorizationStatus:
    ACCEPTED = "Accepted"
    BLOCKED = "Blocked"
    EXPIRED = "Expired"
    INVALID = "Invalid"
    CONCURRENT_TX = "ConcurrentTx"


@dataclass
class OCPPMessage:
    """Parsed OCPP-J message."""
    message_type: MessageType
    unique_id: str
    action: str = ""          # Only for CALL
    payload: dict = None      # CALL and CALL_RESULT
    error_code: str = ""      # Only for CALL_ERROR
    error_description: str = ""
    error_details: dict = None

    def to_json(self) -> str:
        if self.message_type == MessageType.CALL:
            return json.dumps([
                self.message_type.value,
                self.unique_id,
                self.action,
                self.payload or {},
            ])
        elif self.message_type == MessageType.CALL_RESULT:
            return json.dumps([
                self.message_type.value,
                self.unique_id,
                self.payload or {},
            ])
        elif self.message_type == MessageType.CALL_ERROR:
            return json.dumps([
                self.message_type.value,
                self.unique_id,
                self.error_code,
                self.error_description,
                self.error_details or {},
            ])

    @classmethod
    def parse(cls, raw: str) -> "OCPPMessage":
        """Parse a raw OCPP-J message."""
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) < 3:
            raise ValueError(f"Invalid OCPP message format: {raw[:100]}")

        msg_type = MessageType(data[0])

        if msg_type == MessageType.CALL:
            return cls(
                message_type=msg_type,
                unique_id=data[1],
                action=data[2],
                payload=data[3] if len(data) > 3 else {},
            )
        elif msg_type == MessageType.CALL_RESULT:
            return cls(
                message_type=msg_type,
                unique_id=data[1],
                payload=data[2] if len(data) > 2 else {},
            )
        elif msg_type == MessageType.CALL_ERROR:
            return cls(
                message_type=msg_type,
                unique_id=data[1],
                error_code=data[2] if len(data) > 2 else "",
                error_description=data[3] if len(data) > 3 else "",
                error_details=data[4] if len(data) > 4 else {},
            )

    @staticmethod
    def call(action: str, payload: dict) -> "OCPPMessage":
        """Create a CALL message (server → charger)."""
        return OCPPMessage(
            message_type=MessageType.CALL,
            unique_id=str(uuid.uuid4()),
            action=action,
            payload=payload,
        )

    @staticmethod
    def result(unique_id: str, payload: dict) -> "OCPPMessage":
        """Create a CALL_RESULT message."""
        return OCPPMessage(
            message_type=MessageType.CALL_RESULT,
            unique_id=unique_id,
            payload=payload,
        )

    @staticmethod
    def error(unique_id: str, code: str, description: str = "") -> "OCPPMessage":
        """Create a CALL_ERROR message."""
        return OCPPMessage(
            message_type=MessageType.CALL_ERROR,
            unique_id=unique_id,
            error_code=code,
            error_description=description,
        )
