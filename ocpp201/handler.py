"""
OCPP 2.0.1 Message Handler.

Key differences from 1.6j:
- TransactionEvent replaces StartTransaction/StopTransaction/MeterValues
  Single message with eventType: Started/Updated/Ended
- Component/Variable model replaces ChangeConfiguration
- SecurityEventNotification for security audit
- SignCertificate for P&C (ISO 15118)
- Get15118EVCertificate for vehicle contract cert provisioning

Structure (split per architecture v2):
- handler.py    — ChargePointHandler201 class, routing, boot/heartbeat/status
- transaction.py — TransactionMixin: Started/Updated/Ended + CDR
- meter.py       — MeterMixin: MeterValues + parsing helpers
- security.py    — SecurityMixin: SignCertificate, Get15118EV, SecurityEvent
"""
import logging
import os
import time
from datetime import datetime, timezone

from config import config
from events.bus import EventBus
from events.types import Event, EventType
from state.postgres import db
from state.redis import redis_state
from ocpp201.protocol import (
    OCPPMessage, MessageType, Action,
    RegistrationStatus, AuthorizationStatus,
    ConnectorStatus, ChargingState,
)
from ocpp201.transaction import TransactionMixin
from ocpp201.meter import MeterMixin
from ocpp201.security import SecurityMixin

logger = logging.getLogger(__name__)


class ChargePointHandler201(TransactionMixin, MeterMixin, SecurityMixin):
    """Handles all OCPP 2.0.1 messages for a single charge point."""

    def __init__(self, cp_id: str, event_bus: EventBus, client_cert: dict | None = None):
        self.cp_id = cp_id
        self.event_bus = event_bus
        self.client_cert = client_cert  # TLS client cert (security profile 3)
        self._pending_calls: dict[str, tuple[str, float]] = {}
        self._simulated = False
        # Track active transactions by evse_id
        self._active_transactions: dict[str, str] = {}  # transaction_id → session_id

    async def handle_message(self, raw: str) -> str | None:
        """Parse and route an OCPP 2.0.1 message."""
        start = time.monotonic()
        try:
            msg = OCPPMessage.parse(raw)
        except Exception as e:
            logger.error(f"[{self.cp_id}] Parse error: {e}")
            return None

        if msg.message_type == MessageType.CALL:
            response = await self._handle_call(msg)
            latency = (time.monotonic() - start) * 1000
            await self._log_message(msg, response, latency)
            return response.to_json() if response else None

        elif msg.message_type == MessageType.CALL_RESULT:
            pending = self._pending_calls.pop(msg.unique_id, None)
            if pending:
                action, sent_at = pending
                latency = (time.monotonic() - sent_at) * 1000
                logger.debug(f"[{self.cp_id}] Response to {action} ({latency:.0f}ms)")
            return None

        elif msg.message_type == MessageType.CALL_ERROR:
            pending = self._pending_calls.pop(msg.unique_id, None)
            if pending:
                action, _ = pending
                logger.warning(f"[{self.cp_id}] Error for {action}: {msg.error_code}")
            return None

    async def _handle_call(self, msg: OCPPMessage) -> OCPPMessage:
        """Route CALL to handler."""
        handlers = {
            Action.BOOT_NOTIFICATION: self._on_boot_notification,
            Action.HEARTBEAT: self._on_heartbeat,
            Action.STATUS_NOTIFICATION: self._on_status_notification,
            Action.AUTHORIZE: self._on_authorize,
            Action.TRANSACTION_EVENT: self._on_transaction_event,
            Action.METER_VALUES: self._on_meter_values,
            Action.SECURITY_EVENT_NOTIFICATION: self._on_security_event,
            Action.SIGN_CERTIFICATE: self._on_sign_certificate,
            Action.GET_15118_EV_CERTIFICATE: self._on_get_15118_ev_certificate,
            Action.NOTIFY_EVENT: self._on_notify_event,
            Action.NOTIFY_REPORT: self._on_notify_report,
            Action.DATA_TRANSFER: self._on_data_transfer,
            Action.FIRMWARE_STATUS_NOTIFICATION: self._on_firmware_status,
            Action.LOG_STATUS_NOTIFICATION: self._on_log_status,
            Action.NOTIFY_CHARGING_LIMIT: self._on_notify_charging_limit,
            Action.NOTIFY_EV_CHARGING_NEEDS: self._on_notify_ev_charging_needs,
            Action.REPORT_CHARGING_PROFILES: self._on_report_charging_profiles,
        }

        handler = handlers.get(msg.action)
        if handler is None:
            logger.warning(f"[{self.cp_id}] Unknown action: {msg.action}")
            return OCPPMessage.error(msg.unique_id, "NotImplemented", f"{msg.action} not supported")

        try:
            payload = await handler(msg.payload)
            return OCPPMessage.result(msg.unique_id, payload)
        except Exception as e:
            logger.error(f"[{self.cp_id}] Error in {msg.action}: {e}", exc_info=True)
            return OCPPMessage.error(msg.unique_id, "InternalError", str(e))

    # ── Core Handlers ────────────────────────────────────────────────────

    async def _on_boot_notification(self, payload: dict) -> dict:
        """Charger boot — 2.0.1 uses chargingStation object."""
        cs = payload.get("chargingStation", {})
        vendor = cs.get("vendorName", "")
        model = cs.get("model", "")
        serial = cs.get("serialNumber", "")
        firmware = cs.get("firmwareVersion", "")
        reason = payload.get("reason", "PowerUp")

        self._simulated = vendor == os.getenv("SIMULATED_VENDOR", "VIRTUAL_CHARGER")

        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.charge_points (id, vendor, model, serial_number, firmware_version,
                                                ocpp_version, status, simulated, last_boot)
                VALUES ($1, $2, $3, $4, $5, '2.0.1', 'online', $6, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    vendor = $2, model = $3, serial_number = $4, firmware_version = $5,
                    ocpp_version = '2.0.1', status = 'online', simulated = $6, last_boot = NOW()
            """, self.cp_id, vendor, model, serial, firmware, self._simulated)

        await redis_state.set_charger(self.cp_id, {
            "status": "online", "vendor": vendor, "model": model,
            "serial": serial, "firmware": firmware,
            "ocpp_version": "2.0.1", "simulated": str(self._simulated),
            "last_boot": datetime.now(timezone.utc).isoformat(),
            "boot_reason": reason,
        })

        await self.event_bus.publish(Event(
            type=EventType.CHARGER_BOOT,
            charge_point=self.cp_id,
            simulated=self._simulated,
            data={"vendor": vendor, "model": model, "serial": serial,
                  "firmware": firmware, "reason": reason},
        ))

        logger.info(f"[{self.cp_id}] Boot 2.0.1: {vendor} {model} reason={reason}"
                     + (" [SIM]" if self._simulated else ""))

        return {
            "currentTime": datetime.now(timezone.utc).isoformat(),
            "interval": config.ocpp.heartbeat_interval,
            "status": RegistrationStatus.ACCEPTED,
        }

    async def _on_heartbeat(self, payload: dict) -> dict:
        now = datetime.now(timezone.utc)
        await redis_state.set_charger(self.cp_id, {"last_heartbeat": now.isoformat()})
        async with db.write() as conn:
            await conn.execute("UPDATE ocpp.charge_points SET last_heartbeat = $1 WHERE id = $2", now, self.cp_id)
        return {"currentTime": now.isoformat()}

    async def _on_status_notification(self, payload: dict) -> dict:
        """2.0.1 StatusNotification — per EVSE + connector."""
        evse_id = payload.get("evseId", 0)
        connector_id = payload.get("connectorId", 0)
        status = payload.get("connectorStatus", "Available")
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.connectors (charge_point, connector_id, status, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (charge_point, connector_id) DO UPDATE SET
                    status = $3, updated_at = $4
            """, self.cp_id, connector_id, status, timestamp)

        await redis_state.set_charger(self.cp_id, {
            f"evse_{evse_id}_connector_{connector_id}_status": status,
        })

        await self.event_bus.publish(Event(
            type=EventType.CHARGER_STATUS,
            charge_point=self.cp_id,
            connector=connector_id,
            simulated=self._simulated,
            data={"evse_id": evse_id, "status": status},
        ))

        logger.info(f"[{self.cp_id}] EVSE {evse_id} connector {connector_id}: {status}")
        return {}

    async def _on_authorize(self, payload: dict) -> dict:
        """Authorization — supports RFID + certificate (P&C)."""
        id_token = payload.get("idToken", {})
        token_id = id_token.get("idToken", "")
        token_type = id_token.get("type", "ISO14443")  # ISO14443=RFID, eMAID=P&C

        # Certificate-based auth (Plug & Charge)
        cert_chain = payload.get("15118CertificateHashData")

        if token_type == "eMAID" and cert_chain:
            # P&C: validate contract cert against our PKI
            status = await self._validate_contract_cert(token_id, cert_chain)
        else:
            # RFID: look up in auth cache
            async with db.read() as conn:
                row = await conn.fetchrow(
                    "SELECT status FROM ocpp.authorization_cache WHERE token = $1", token_id
                )
            status = row["status"] if row else AuthorizationStatus.INVALID

        await self.event_bus.publish(Event(
            type=EventType.AUTH_RESULT,
            charge_point=self.cp_id,
            simulated=self._simulated,
            data={"token": token_id, "type": token_type, "status": status},
        ))

        logger.info(f"[{self.cp_id}] Authorize {token_type} {token_id}: {status}")
        return {"idTokenInfo": {"status": status}}

    # ── Informational Handlers ───────────────────────────────────────────

    async def _on_notify_event(self, payload: dict) -> dict:
        """Component events (errors, warnings) from charger."""
        events = payload.get("eventData", [])
        for event in events:
            component = event.get("component", {}).get("name", "")
            variable = event.get("variable", {}).get("name", "")
            actual_value = event.get("actualValue", "")
            trigger = event.get("trigger", "")
            logger.info(f"[{self.cp_id}] Event: {component}.{variable}={actual_value} trigger={trigger}")
        return {}

    async def _on_notify_report(self, payload: dict) -> dict:
        """Response to GetReport — charger config dump."""
        report_data = payload.get("reportData", [])
        logger.info(f"[{self.cp_id}] Report: {len(report_data)} items")
        # Store config snapshot if needed
        return {}

    async def _on_data_transfer(self, payload: dict) -> dict:
        vendor = payload.get("vendorId", "")
        msg_id = payload.get("messageId", "")
        logger.info(f"[{self.cp_id}] DataTransfer: vendor={vendor} msg={msg_id}")
        return {"status": "Accepted"}

    async def _on_firmware_status(self, payload: dict) -> dict:
        status = payload.get("status", "")
        logger.info(f"[{self.cp_id}] Firmware: {status}")
        await self.event_bus.publish(Event(
            type=EventType.CHARGER_FIRMWARE, charge_point=self.cp_id,
            simulated=self._simulated, data={"status": status}))
        return {}

    async def _on_log_status(self, payload: dict) -> dict:
        status = payload.get("status", "")
        logger.info(f"[{self.cp_id}] Log upload: {status}")
        return {}

    async def _on_notify_charging_limit(self, payload: dict) -> dict:
        """Charger reports its current charging limit (from local EMS/DLB)."""
        # Important for our dual-protocol safety: this tells us the factory
        # Modbus controller's current limit
        charging_limit = payload.get("chargingLimit", {})
        source = charging_limit.get("chargingLimitSource", "Other")
        is_grid = charging_limit.get("isGridCritical", False)
        logger.info(f"[{self.cp_id}] Charging limit from {source} (grid_critical={is_grid})")
        return {}

    async def _on_notify_ev_charging_needs(self, payload: dict) -> dict:
        """EV communicates its charging needs (ISO 15118)."""
        ev_needs = payload.get("chargingNeeds", {})
        mode = ev_needs.get("requestedEnergyTransfer", "")
        logger.info(f"[{self.cp_id}] EV charging needs: mode={mode}")
        return {"status": "Accepted"}

    async def _on_report_charging_profiles(self, payload: dict) -> dict:
        """Response to GetChargingProfiles."""
        profiles = payload.get("chargingProfile", [])
        logger.info(f"[{self.cp_id}] Reported {len(profiles) if isinstance(profiles, list) else 1} charging profiles")
        return {}

    # ── CDR Generation ───────────────────────────────────────────────────

    async def _generate_cdr(self, session_id: str) -> None:
        """Generate CDR for completed session."""
        async with db.write() as conn:
            session = await conn.fetchrow("""
                SELECT charge_point, connector_id, auth_method, auth_id,
                       start_time, stop_time, energy_kwh
                FROM ocpp.sessions WHERE id::text = $1
            """, session_id)

            if not session or not session["stop_time"]:
                return

            duration_min = (session["stop_time"] - session["start_time"]).total_seconds() / 60

            await conn.execute("""
                INSERT INTO ocpp.cdrs
                    (session_id, charge_point, connector_id, auth_method, auth_id,
                     start_time, stop_time, energy_kwh, duration_min)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
                session_id, session["charge_point"], session["connector_id"],
                session["auth_method"], session["auth_id"],
                session["start_time"], session["stop_time"],
                session["energy_kwh"], duration_min)

        await self.event_bus.publish(Event(
            type=EventType.SESSION_CDR,
            charge_point=self.cp_id,
            session_id=session_id,
            simulated=self._simulated,
            data={"energy_kwh": float(session["energy_kwh"]), "duration_min": round(duration_min, 1)},
        ))

    # ── Message Log ──────────────────────────────────────────────────────

    async def _log_message(self, msg: OCPPMessage, response: OCPPMessage, latency_ms: float) -> None:
        try:
            async with db.write() as conn:
                await conn.execute("""
                    INSERT INTO ocpp.ocpp_messages
                        (charge_point, direction, ocpp_version, action, message_id, payload, response, latency_ms)
                    VALUES ($1, 'in', '2.0.1', $2, $3, $4, $5, $6)
                """, self.cp_id, msg.action, msg.unique_id, msg.payload,
                    response.payload if response else None, latency_ms)
        except Exception as e:
            logger.debug(f"Message log failed: {e}")

    # ── Disconnect ───────────────────────────────────────────────────────

    async def on_disconnect(self) -> None:
        await redis_state.set_charger(self.cp_id, {"status": "offline"})
        async with db.write() as conn:
            await conn.execute("UPDATE ocpp.charge_points SET status = 'offline' WHERE id = $1", self.cp_id)
        await self.event_bus.publish(Event(
            type=EventType.CHARGER_OFFLINE, charge_point=self.cp_id,
            simulated=self._simulated, data={}))
        logger.info(f"[{self.cp_id}] Offline")


# Backwards-compatible alias used by older imports
OCPP201Handler = ChargePointHandler201
