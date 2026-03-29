"""
OCPP 2.0.1 TransactionEvent handling.

Mixin for ChargePointHandler201 — handles Started/Updated/Ended lifecycle.
Replaces StartTransaction + StopTransaction + periodic MeterValues from 1.6j.
"""
import logging
from datetime import datetime, timezone

from events.types import Event, EventType
from state.postgres import db
from state.redis import redis_state
from ocpp201.protocol import TransactionEventType

logger = logging.getLogger(__name__)


class TransactionMixin:
    """Mixin providing TransactionEvent handlers."""

    async def _on_transaction_event(self, payload: dict) -> dict:
        """
        Unified transaction message — replaces Start/Stop/MeterValues from 1.6j.

        eventType: Started | Updated | Ended
        triggerReason: Authorized | MeterValuePeriodic | EVDeparted | RemoteStop | etc.
        """
        event_type = payload.get("eventType", "")
        trigger = payload.get("triggerReason", "")
        seq_no = payload.get("seqNo", 0)
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

        transaction = payload.get("transactionInfo", {})
        transaction_id = transaction.get("transactionId", "")
        charging_state = transaction.get("chargingState")
        stopped_reason = transaction.get("stoppedReason")
        remote_start_id = transaction.get("remoteStartId")

        evse = payload.get("evse", {})
        evse_id = evse.get("id", 0)
        connector_id = evse.get("connectorId", 0)

        id_token = payload.get("idToken", {})
        token_id = id_token.get("idToken", "")
        token_type = id_token.get("type", "")

        meter_values = payload.get("meterValue", [])

        if event_type == TransactionEventType.STARTED:
            return await self._transaction_started(
                transaction_id, evse_id, connector_id,
                token_id, token_type, timestamp, trigger,
            )
        elif event_type == TransactionEventType.UPDATED:
            return await self._transaction_updated(
                transaction_id, meter_values, timestamp,
                charging_state, trigger, seq_no,
            )
        elif event_type == TransactionEventType.ENDED:
            return await self._transaction_ended(
                transaction_id, meter_values, timestamp,
                stopped_reason, trigger,
            )

        return {}

    async def _transaction_started(
        self, txn_id: str, evse_id: int, connector_id: int,
        token_id: str, token_type: str, timestamp: str, trigger: str,
    ) -> dict:
        """New charging session."""
        auth_method = "pnc" if token_type == "eMAID" else "rfid" if token_id else "remote"

        async with db.transaction() as conn:
            session = await conn.fetchrow("""
                INSERT INTO ocpp.sessions 
                    (charge_point, connector_id, auth_method, auth_id, start_time, simulated, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id::text as session_id
            """, self.cp_id, connector_id, auth_method, token_id, timestamp,
                self._simulated, {"transaction_id": txn_id, "evse_id": evse_id, "trigger": trigger})

        session_id = session["session_id"]
        self._active_transactions[txn_id] = session_id

        await redis_state.set_session(session_id, {
            "charge_point": self.cp_id,
            "connector_id": str(connector_id),
            "evse_id": str(evse_id),
            "transaction_id": txn_id,
            "auth_id": token_id,
            "auth_method": auth_method,
            "start_time": timestamp,
            "energy_kwh": "0",
            "power_kw": "0",
            "status": "active",
        })

        await self.event_bus.publish(Event(
            type=EventType.SESSION_START,
            charge_point=self.cp_id,
            connector=connector_id,
            session_id=session_id,
            simulated=self._simulated,
            data={"transaction_id": txn_id, "auth_method": auth_method,
                  "token": token_id, "trigger": trigger, "evse_id": evse_id},
        ))

        logger.info(f"[{self.cp_id}] Session started: txn={txn_id} auth={auth_method} trigger={trigger}")
        return {}

    async def _transaction_updated(
        self, txn_id: str, meter_values: list, timestamp: str,
        charging_state: str | None, trigger: str, seq_no: int,
    ) -> dict:
        """Session update — meter values and/or state change."""
        session_id = self._active_transactions.get(txn_id)

        if not session_id:
            # Look up from DB (reconnect scenario)
            async with db.read() as conn:
                row = await conn.fetchrow("""
                    SELECT id::text as session_id FROM ocpp.sessions
                    WHERE charge_point = $1 AND metadata->>'transaction_id' = $2 AND status = 'active'
                """, self.cp_id, txn_id)
                if row:
                    session_id = row["session_id"]
                    self._active_transactions[txn_id] = session_id

        if meter_values:
            await self._process_meter_values(session_id, meter_values, timestamp)

        if charging_state and session_id:
            await redis_state.set_session(session_id, {"charging_state": charging_state})

        return {}

    async def _transaction_ended(
        self, txn_id: str, meter_values: list, timestamp: str,
        stopped_reason: str | None, trigger: str,
    ) -> dict:
        """Session ended."""
        session_id = self._active_transactions.pop(txn_id, None)

        if not session_id:
            async with db.read() as conn:
                row = await conn.fetchrow("""
                    SELECT id::text as session_id FROM ocpp.sessions
                    WHERE charge_point = $1 AND metadata->>'transaction_id' = $2 AND status = 'active'
                """, self.cp_id, txn_id)
                if row:
                    session_id = row["session_id"]

        # Process final meter values
        if meter_values:
            await self._process_meter_values(session_id, meter_values, timestamp)

        if session_id:
            # Finalize session
            async with db.transaction() as conn:
                session = await conn.fetchrow("""
                    UPDATE ocpp.sessions SET
                        status = 'completed', stop_time = $1, stop_reason = $2
                    WHERE id::text = $3 AND status = 'active'
                    RETURNING energy_kwh, start_time
                """, timestamp, stopped_reason or "Local", session_id)

            if session:
                await redis_state.del_session(session_id)
                await self._generate_cdr(session_id)

                await self.event_bus.publish(Event(
                    type=EventType.SESSION_STOP,
                    charge_point=self.cp_id,
                    session_id=session_id,
                    simulated=self._simulated,
                    data={"transaction_id": txn_id, "reason": stopped_reason,
                          "trigger": trigger, "energy_kwh": float(session["energy_kwh"])},
                ))

                logger.info(f"[{self.cp_id}] Session ended: txn={txn_id} reason={stopped_reason} energy={session['energy_kwh']:.1f}kWh")
        else:
            logger.warning(f"[{self.cp_id}] TransactionEvent Ended for unknown txn={txn_id}")

        return {}
