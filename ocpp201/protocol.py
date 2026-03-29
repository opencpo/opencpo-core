"""
OCPP 2.0.1 Protocol — message framing and types.

Same JSON-over-WebSocket framing as 1.6j (OCPP-J), but with:
- Different action names (TransactionEvent replaces Start/Stop)
- Security profiles
- Component/Variable model (replaces ChangeConfiguration)
- ISO 15118 certificate messages
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


# OCPP 2.0.1 Actions (charger → server)
class Action:
    AUTHORIZE = "Authorize"
    BOOT_NOTIFICATION = "BootNotification"
    CLEARED_CHARGING_LIMIT = "ClearedChargingLimit"
    DATA_TRANSFER = "DataTransfer"
    FIRMWARE_STATUS_NOTIFICATION = "FirmwareStatusNotification"
    GET_15118_EV_CERTIFICATE = "Get15118EVCertificate"
    HEARTBEAT = "Heartbeat"
    LOG_STATUS_NOTIFICATION = "LogStatusNotification"
    METER_VALUES = "MeterValues"
    NOTIFY_CHARGING_LIMIT = "NotifyChargingLimit"
    NOTIFY_CUSTOMER_INFORMATION = "NotifyCustomerInformation"
    NOTIFY_DISPLAY_MESSAGES = "NotifyDisplayMessages"
    NOTIFY_EV_CHARGING_NEEDS = "NotifyEVChargingNeeds"
    NOTIFY_EV_CHARGING_SCHEDULE = "NotifyEVChargingSchedule"
    NOTIFY_EVENT = "NotifyEvent"
    NOTIFY_MONITORING_REPORT = "NotifyMonitoringReport"
    NOTIFY_REPORT = "NotifyReport"
    PUBLISH_FIRMWARE_STATUS = "PublishFirmwareStatusNotification"
    REPORT_CHARGING_PROFILES = "ReportChargingProfiles"
    RESERVATION_STATUS_UPDATE = "ReservationStatusUpdate"
    SECURITY_EVENT_NOTIFICATION = "SecurityEventNotification"
    SIGN_CERTIFICATE = "SignCertificate"
    STATUS_NOTIFICATION = "StatusNotification"
    TRANSACTION_EVENT = "TransactionEvent"


# OCPP 2.0.1 Actions (server → charger)
class ServerAction:
    CANCEL_RESERVATION = "CancelReservation"
    CERTIFICATE_SIGNED = "CertificateSigned"
    CHANGE_AVAILABILITY = "ChangeAvailability"
    CLEAR_CACHE = "ClearCache"
    CLEAR_CHARGING_PROFILE = "ClearChargingProfile"
    CLEAR_DISPLAY_MESSAGE = "ClearDisplayMessage"
    CLEAR_VARIABLE_MONITORING = "ClearVariableMonitoring"
    COST_UPDATED = "CostUpdated"
    CUSTOMER_INFORMATION = "CustomerInformation"
    DATA_TRANSFER = "DataTransfer"
    DELETE_CERTIFICATE = "DeleteCertificate"
    GET_BASE_REPORT = "GetBaseReport"
    GET_CHARGING_PROFILES = "GetChargingProfiles"
    GET_COMPOSITE_SCHEDULE = "GetCompositeSchedule"
    GET_DISPLAY_MESSAGES = "GetDisplayMessages"
    GET_INSTALLED_CERTIFICATE_IDS = "GetInstalledCertificateIds"
    GET_LOCAL_LIST_VERSION = "GetLocalListVersion"
    GET_LOG = "GetLog"
    GET_MONITORING_REPORT = "GetMonitoringReport"
    GET_REPORT = "GetReport"
    GET_TRANSACTION_STATUS = "GetTransactionStatus"
    GET_VARIABLES = "GetVariables"
    INSTALL_CERTIFICATE = "InstallCertificate"
    PUBLISH_FIRMWARE = "PublishFirmware"
    REQUEST_START_TRANSACTION = "RequestStartTransaction"
    REQUEST_STOP_TRANSACTION = "RequestStopTransaction"
    RESERVE_NOW = "ReserveNow"
    RESET = "Reset"
    SEND_LOCAL_LIST = "SendLocalList"
    SET_CHARGING_PROFILE = "SetChargingProfile"
    SET_DISPLAY_MESSAGE = "SetDisplayMessage"
    SET_MONITORING_BASE = "SetMonitoringBase"
    SET_MONITORING_LEVEL = "SetMonitoringLevel"
    SET_NETWORK_PROFILE = "SetNetworkProfile"
    SET_VARIABLE_MONITORING = "SetVariableMonitoring"
    SET_VARIABLES = "SetVariables"
    TRIGGER_MESSAGE = "TriggerMessage"
    UNLOCK_CONNECTOR = "UnlockConnector"
    UNPUBLISH_FIRMWARE = "UnpublishFirmware"
    UPDATE_FIRMWARE = "UpdateFirmware"


# Transaction event types (replaces StartTransaction/StopTransaction)
class TransactionEventType:
    STARTED = "Started"
    UPDATED = "Updated"
    ENDED = "Ended"


# Trigger reasons for TransactionEvent
class TriggerReason:
    AUTHORIZED = "Authorized"
    CABLE_PLUGGED_IN = "CablePluggedIn"
    CHARGING_RATE_CHANGED = "ChargingRateChanged"
    CHARGING_STATE_CHANGED = "ChargingStateChanged"
    DEAUTHORIZED = "Deauthorized"
    ENERGY_LIMIT_REACHED = "EnergyLimitReached"
    EV_COMMUNICATION_LOST = "EVCommunicationLost"
    EV_CONNECT_TIMEOUT = "EVConnectTimeout"
    EV_DEPARTED = "EVDeparted"
    EV_DETECTED = "EVDetected"
    METER_VALUE_CLOCK = "MeterValueClock"
    METER_VALUE_PERIODIC = "MeterValuePeriodic"
    TIME_LIMIT_REACHED = "TimeLimitReached"
    TRIGGER = "Trigger"
    UNLOCK_COMMAND = "UnlockCommand"
    STOP_AUTHORIZED = "StopAuthorized"
    EV_COMMUNICATION_LOST_2 = "EVCommunicationLost"
    ABNORMAL_CONDITION = "AbnormalCondition"
    SIGNED_DATA_RECEIVED = "SignedDataReceived"
    RESET_COMMAND = "ResetCommand"
    REMOTE_STOP = "RemoteStop"
    REMOTE_START = "RemoteStart"


class RegistrationStatus:
    ACCEPTED = "Accepted"
    PENDING = "Pending"
    REJECTED = "Rejected"


class AuthorizationStatus:
    ACCEPTED = "Accepted"
    BLOCKED = "Blocked"
    CONCURRENT_TX = "ConcurrentTx"
    EXPIRED = "Expired"
    INVALID = "Invalid"
    NO_CREDIT = "NoCredit"
    NOT_ALLOWED_TYPE_EVSE = "NotAllowedTypeEVSE"
    NOT_AT_THIS_LOCATION = "NotAtThisLocation"
    NOT_AT_THIS_TIME = "NotAtThisTime"
    UNKNOWN = "Unknown"


class ConnectorStatus:
    AVAILABLE = "Available"
    OCCUPIED = "Occupied"
    RESERVED = "Reserved"
    UNAVAILABLE = "Unavailable"
    FAULTED = "Faulted"


class ChargingState:
    CHARGING = "Charging"
    EV_CONNECTED = "EVConnected"
    SUSPENDED_EV = "SuspendedEV"
    SUSPENDED_EVSE = "SuspendedEVSE"
    IDLE = "Idle"


# Reuse the same message framing as 1.6j
@dataclass
class OCPPMessage:
    """Parsed OCPP-J 2.0.1 message (same wire format as 1.6j)."""
    message_type: MessageType
    unique_id: str
    action: str = ""
    payload: dict = None
    error_code: str = ""
    error_description: str = ""
    error_details: dict = None

    def to_json(self) -> str:
        if self.message_type == MessageType.CALL:
            return json.dumps([self.message_type.value, self.unique_id, self.action, self.payload or {}])
        elif self.message_type == MessageType.CALL_RESULT:
            return json.dumps([self.message_type.value, self.unique_id, self.payload or {}])
        elif self.message_type == MessageType.CALL_ERROR:
            return json.dumps([self.message_type.value, self.unique_id, self.error_code, self.error_description, self.error_details or {}])

    @classmethod
    def parse(cls, raw: str) -> "OCPPMessage":
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) < 3:
            raise ValueError(f"Invalid OCPP message: {raw[:100]}")
        msg_type = MessageType(data[0])
        if msg_type == MessageType.CALL:
            return cls(message_type=msg_type, unique_id=data[1], action=data[2], payload=data[3] if len(data) > 3 else {})
        elif msg_type == MessageType.CALL_RESULT:
            return cls(message_type=msg_type, unique_id=data[1], payload=data[2] if len(data) > 2 else {})
        elif msg_type == MessageType.CALL_ERROR:
            return cls(message_type=msg_type, unique_id=data[1], error_code=data[2] if len(data) > 2 else "",
                       error_description=data[3] if len(data) > 3 else "", error_details=data[4] if len(data) > 4 else {})

    @staticmethod
    def call(action: str, payload: dict) -> "OCPPMessage":
        return OCPPMessage(message_type=MessageType.CALL, unique_id=str(uuid.uuid4()), action=action, payload=payload)

    @staticmethod
    def result(unique_id: str, payload: dict) -> "OCPPMessage":
        return OCPPMessage(message_type=MessageType.CALL_RESULT, unique_id=unique_id, payload=payload)

    @staticmethod
    def error(unique_id: str, code: str, description: str = "") -> "OCPPMessage":
        return OCPPMessage(message_type=MessageType.CALL_ERROR, unique_id=unique_id, error_code=code, error_description=description)
