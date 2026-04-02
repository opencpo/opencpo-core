"""
OCPP 1.6j Message Handler.

Routes incoming OCPP messages to the appropriate handler function.
Each handler processes the message, updates state, publishes events.

Energy data model:
- Charger sends Energy.Active.Import.Register = CUMULATIVE lifetime Wh counter
- StartTransaction.meterStart = register value at session start (Wh)
- StopTransaction.meterStop = register value at session end (Wh)
- Session energy = (meterStop - meterStart) / 1000 (kWh)
- TimescaleDB meter_values.energy_kwh stores RAW register (kWh, for audit)
- ocpp.sessions.energy_kwh stores SESSION DELTA (kWh, for billing)
- Redis session state stores SESSION DELTA (kWh, for live display)
- session_energy() in ocpp16.meter is the SINGLE function that computes the delta

Known Hongjiali MAXPOWER quirks:
- Sends StopTransaction with reason="Other" on reconnect (kills active session)
- BootNotification vendor="MAXPOWER", model varies by config
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from config import config
from events.bus import EventBus
from events.types import Event, EventType
from state.postgres import db
from state.redis import redis_state
from ocpp16.protocol import (
    OCPPMessage, MessageType, Action,
    RegistrationStatus, AuthorizationStatus,
)
from ocpp16.session import handle_start_transaction, handle_stop_transaction
from ocpp16.meter import handle_meter_values, session_energy, parse_sampled_values
from charger_profiles.registry import resolve_profile, ChargerProfile, GENERIC_OCPP16
from ocpp16.handler_helpers import ChargePointHelpersMixin

logger = logging.getLogger(__name__)


class ChargePointHandler(ChargePointHelpersMixin):
    """Handles all OCPP 1.6j messages for a single charge point."""

    def __init__(self, cp_id: str, event_bus: EventBus):
        self.cp_id = cp_id
        self.event_bus = event_bus
        self._pending_calls: dict[str, tuple[str, float]] = {}
        self._simulated = False
        self.profile: ChargerProfile = GENERIC_OCPP16

    async def handle_message(self, raw: str) -> str | None:
        """Parse and route an OCPP message. Returns response to send back."""
        start = time.monotonic()
        try:
            msg = OCPPMessage.parse(raw)
        except Exception as e:
            logger.error(f"[{self.cp_id}] Failed to parse message: {e}")
            return None

        if msg.message_type == MessageType.CALL:
            response = await self._handle_call(msg)
            await self._log_message(msg, response, (time.monotonic() - start) * 1000)
            return response.to_json() if response else None

        elif msg.message_type == MessageType.CALL_RESULT:
            pending = self._pending_calls.pop(msg.unique_id, None)
            if pending:
                action, sent_at = pending
                latency = (time.monotonic() - sent_at) * 1000
                status = msg.payload.get("status", "unknown") if isinstance(msg.payload, dict) else str(msg.payload)
                logger.info(f"[{self.cp_id}] ← {action} response: {status} ({latency:.0f}ms)")
            else:
                logger.info(f"[{self.cp_id}] ← Untracked CALL_RESULT: {msg.unique_id} {msg.payload}")

        elif msg.message_type == MessageType.CALL_ERROR:
            pending = self._pending_calls.pop(msg.unique_id, None)
            if pending:
                action, _ = pending
                logger.warning(f"[{self.cp_id}] Error response to {action}: {msg.error_code} {msg.error_description}")
        return None

    async def _handle_call(self, msg: OCPPMessage) -> OCPPMessage:
        """Route a CALL message to the appropriate handler."""
        handlers = {
            Action.BOOT_NOTIFICATION: self._on_boot_notification,
            Action.HEARTBEAT: self._on_heartbeat,
            Action.STATUS_NOTIFICATION: self._on_status_notification,
            Action.AUTHORIZE: self._on_authorize,
            Action.START_TRANSACTION: self._on_start_transaction,
            Action.STOP_TRANSACTION: self._on_stop_transaction,
            Action.METER_VALUES: self._on_meter_values,
            Action.DATA_TRANSFER: self._on_data_transfer,
            Action.FIRMWARE_STATUS: self._on_firmware_status,
            Action.DIAGNOSTICS_STATUS: self._on_diagnostics_status,
        }
        handler = handlers.get(msg.action)
        if handler is None:
            logger.warning(f"[{self.cp_id}] Unknown action: {msg.action}")
            return OCPPMessage.error(msg.unique_id, "NotImplemented", f"Action {msg.action} not supported")
        try:
            payload = await handler(msg.payload)
            return OCPPMessage.result(msg.unique_id, payload)
        except Exception as e:
            logger.error(f"[{self.cp_id}] Error handling {msg.action}: {e}", exc_info=True)
            return OCPPMessage.error(msg.unique_id, "InternalError", str(e))

    # ── Message Handlers ─────────────────────────────────────────────────

    async def _on_boot_notification(self, payload: dict) -> dict:
        """Charger is booting up. Register/update it."""
        vendor = payload.get("chargePointVendor", "")
        model = payload.get("chargePointModel", "")
        serial = payload.get("chargePointSerialNumber", "")
        firmware = payload.get("firmwareVersion", "")

        VIRTUAL_PREFIXES = ("STRESS-", "SIM-", "CHAOS-", "VAL-", "LOAD-", "FUZZ-", "FARM-", "PNC-")
        self._simulated = (
            vendor == os.getenv("SIMULATED_VENDOR", "VIRTUAL_CHARGER")
            or any(self.cp_id.startswith(p) for p in VIRTUAL_PREFIXES)
        )

        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.charge_points (id, vendor, model, serial_number, firmware_version,
                                                ocpp_version, status, simulated, last_boot)
                VALUES ($1, $2, $3, $4, $5, '1.6', 'online', $6, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    vendor = $2, model = $3, serial_number = $4, firmware_version = $5,
                    status = 'online', simulated = $6, last_boot = NOW()
            """, self.cp_id, vendor, model, serial, firmware, self._simulated)

        await redis_state.set_charger(self.cp_id, {
            "status": "online", "vendor": vendor, "model": model,
            "serial": serial, "firmware": firmware, "ocpp_version": "1.6",
            "simulated": str(self._simulated),
            "last_boot": datetime.now(timezone.utc).isoformat(),
        })

        await self.event_bus.publish(Event(
            type=EventType.CHARGER_BOOT, charge_point=self.cp_id, simulated=self._simulated,
            data={"vendor": vendor, "model": model, "serial": serial, "firmware": firmware},
        ))

        self.profile = resolve_profile(vendor, model, firmware)
        await redis_state.set_charger(self.cp_id, {"profile": self.profile.id})

        logger.info(f"[{self.cp_id}] Boot: {vendor} {model} (fw: {firmware}) "
                    f"profile={self.profile.id}" + (" [SIMULATED]" if self._simulated else ""))

        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(self._process_pending_starts())
        )
        if not self.profile.sends_status_on_boot:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self._request_connector_status())
            )

        return {
            "currentTime": datetime.now(timezone.utc).isoformat(),
            "interval": config.ocpp.heartbeat_interval,
            "status": RegistrationStatus.ACCEPTED,
        }

    async def _on_heartbeat(self, payload: dict) -> dict:
        """Charger heartbeat — update last seen."""
        now = datetime.now(timezone.utc)

        if self.profile.id == "generic-ocpp16":
            charger_data = await redis_state.get_charger(self.cp_id)
            if charger_data:
                vendor = charger_data.get("vendor", "")
                model = charger_data.get("model", "")
                firmware = charger_data.get("firmware", "")
                if vendor:
                    self.profile = resolve_profile(vendor, model, firmware)
                    await redis_state.set_charger(self.cp_id, {"profile": self.profile.id})
                    logger.info(f"[{self.cp_id}] Profile resolved on reconnect: {self.profile.id}")

        await redis_state.set_charger(self.cp_id, {"status": "online", "last_heartbeat": now.isoformat()})
        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.charge_points (id, ocpp_version, status, last_heartbeat)
                VALUES ($1, '1.6', 'online', $2)
                ON CONFLICT (id) DO UPDATE SET status = 'online', last_heartbeat = $2
            """, self.cp_id, now)

        await self.event_bus.publish(Event(
            type=EventType.CHARGER_HEARTBEAT, charge_point=self.cp_id,
            simulated=self._simulated, data={"timestamp": now.isoformat()},
        ))
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(self._process_pending_starts())
        )
        return {"currentTime": now.isoformat()}

    async def _on_status_notification(self, payload: dict) -> dict:
        """Connector status changed."""
        connector_id = payload.get("connectorId", 0)
        status = payload.get("status", "Unknown")
        error_code = payload.get("errorCode", "NoError")

        # Connector 0 = whole charger status, not a physical port
        if connector_id > 0:
            async with db.write() as conn:
                await conn.execute("""
                    INSERT INTO ocpp.connectors (charge_point, connector_id, status, error_code, updated_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (charge_point, connector_id) DO UPDATE SET
                        status = $3, error_code = $4, updated_at = NOW()
                """, self.cp_id, connector_id, status, error_code)

        await redis_state.set_charger(self.cp_id, {
            f"connector_{connector_id}_status": status,
            f"connector_{connector_id}_error": error_code,
        })
        await self.event_bus.publish(Event(
            type=EventType.CHARGER_STATUS, charge_point=self.cp_id,
            connector=connector_id, simulated=self._simulated,
            data={"status": status, "error_code": error_code},
        ))
        if error_code != "NoError":
            logger.warning(f"[{self.cp_id}] Connector {connector_id}: {status} ({error_code})")
        else:
            logger.info(f"[{self.cp_id}] Connector {connector_id}: {status}")
        return {}

    async def _on_authorize(self, payload: dict) -> dict:
        """Authorization request — queries ocpp.tokens."""
        id_tag = payload.get("idTag", "")

        if id_tag.startswith("APP_") or id_tag == "REMOTE":
            self._last_auth_billing = {"type": "prepaid", "group_id": None}
            status = AuthorizationStatus.ACCEPTED
        else:
            async with db.read() as conn:
                token = await conn.fetchrow(
                    "SELECT t.id, t.uid, t.status, t.valid_from, t.valid_until, t.group_id, g.billing_method "
                    "FROM ocpp.tokens t LEFT JOIN ocpp.token_groups g ON t.group_id = g.id "
                    "WHERE t.uid = $1", id_tag,
                )
            if not token:
                status = AuthorizationStatus.INVALID
            elif token["valid_until"] and token["valid_until"] < datetime.now(timezone.utc):
                status = AuthorizationStatus.EXPIRED
            elif token["valid_from"] and token["valid_from"] > datetime.now(timezone.utc):
                status = AuthorizationStatus.INVALID
            elif token["status"] == "active":
                billing_method = token["billing_method"] or "prepaid"
                self._last_auth_billing = {
                    "type": "postpaid" if billing_method in ("invoice", "direct_debit") else "prepaid",
                    "group_id": str(token["group_id"]) if token.get("group_id") else None,
                }
                status = AuthorizationStatus.ACCEPTED
                try:
                    async with db.write() as conn:
                        await conn.execute(
                            "INSERT INTO ocpp.token_events (token_id, event, details, actor)"
                            " VALUES ($1, 'used', $2, 'charger')",
                            token["id"], f"Authorized at {self.cp_id}",
                        )
                except Exception as exc:
                    logger.warning(f"[{self.cp_id}] Failed to log token usage: {exc}")
            elif token["status"] == "blocked":
                status = AuthorizationStatus.BLOCKED
            else:
                status = AuthorizationStatus.INVALID

        await self.event_bus.publish(Event(
            type=EventType.AUTH_RESULT, charge_point=self.cp_id,
            simulated=self._simulated, data={"id_tag": id_tag, "status": status},
        ))
        logger.info(f"[{self.cp_id}] Authorize {id_tag}: {status}")
        return {"idTagInfo": {"status": status}}

    async def _on_start_transaction(self, payload: dict) -> dict:
        """Delegate to session module."""
        billing_ctx = getattr(self, '_last_auth_billing', None)
        return await handle_start_transaction(
            self.cp_id, self.event_bus, self._simulated, payload, billing_context=billing_ctx,
        )

    async def _on_stop_transaction(self, payload: dict) -> dict:
        """Delegate to session module."""
        return await handle_stop_transaction(self.cp_id, self.event_bus, self._simulated, payload)

    async def _on_meter_values(self, payload: dict) -> dict:
        """Delegate to meter module."""
        return await handle_meter_values(self.cp_id, self.event_bus, self._simulated, payload)

    async def _on_data_transfer(self, payload: dict) -> dict:
        """Vendor-specific data transfer."""
        vendor_id = payload.get("vendorId", "")
        message_id = payload.get("messageId", "")
        logger.info(f"[{self.cp_id}] DataTransfer: vendor={vendor_id} msg={message_id}")
        return {"status": "Accepted"}

    async def _on_firmware_status(self, payload: dict) -> dict:
        """Firmware update progress."""
        status = payload.get("status", "")
        logger.info(f"[{self.cp_id}] Firmware status: {status}")
        await self.event_bus.publish(Event(
            type=EventType.CHARGER_FIRMWARE, charge_point=self.cp_id,
            simulated=self._simulated, data={"status": status},
        ))
        return {}

    async def _on_diagnostics_status(self, payload: dict) -> dict:
        """Diagnostics upload status."""
        status = payload.get("status", "")
        logger.info(f"[{self.cp_id}] Diagnostics status: {status}")
        return {}

    # ── Private Helpers ───────────────────────────────────────────────────

    async def _log_message(self, msg: OCPPMessage, response: OCPPMessage, latency_ms: float) -> None:
        """Log OCPP message to database."""
        import json as _json
        try:
            payload = _json.dumps(msg.payload) if isinstance(msg.payload, dict) else msg.payload
            resp = _json.dumps(response.payload) if response and isinstance(response.payload, dict) else None
            async with db.write() as conn:
                await conn.execute("""
                    INSERT INTO ocpp.ocpp_messages
                        (charge_point, direction, ocpp_version, action, message_id, payload, response, latency_ms)
                    VALUES ($1, 'in', '1.6', $2, $3, $4::jsonb, $5::jsonb, $6)
                """, self.cp_id, msg.action, msg.unique_id, payload, resp, latency_ms)
        except Exception as e:
            logger.error(f"Failed to log message: {e}")

    # ── Disconnect Handler ───────────────────────────────────────────────

    async def on_disconnect(self) -> None:
        """Handle charger disconnection."""
        await redis_state.set_charger(self.cp_id, {"status": "offline"})
        async with db.write() as conn:
            await conn.execute(
                "UPDATE ocpp.charge_points SET status = 'offline' WHERE id = $1", self.cp_id,
            )
        await self.event_bus.publish(Event(
            type=EventType.CHARGER_OFFLINE, charge_point=self.cp_id,
            simulated=self._simulated, data={},
        ))
        logger.info(f"[{self.cp_id}] Offline")
