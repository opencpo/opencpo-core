"""
ChargePointHelpersMixin — extracted async helpers for ChargePointHandler.

Provides:
- _request_connector_status: TriggerMessage after boot for chargers that don't send StatusNotification
- _process_pending_starts: Execute queued RemoteStart commands on reconnect
"""
import asyncio
import json
import logging

from state.redis import redis_state

logger = logging.getLogger(__name__)


class ChargePointHelpersMixin:
    """Async helper methods — mixed into ChargePointHandler."""

    # ── Post-Boot Connector Status Request ───────────────────────────────

    async def _request_connector_status(self) -> None:
        """Request StatusNotification via TriggerMessage for chargers that don't send it on boot."""
        from state import charger_registry

        await asyncio.sleep(5)  # Wait for charger to stabilise after boot

        try:
            msg_id = await charger_registry.send_command(
                self.cp_id, "TriggerMessage",
                {"requestedMessage": "StatusNotification"}
            )
            if msg_id:
                logger.info(f"[{self.cp_id}] Sent TriggerMessage(StatusNotification) — "
                            f"profile says charger may not send status on boot")
            else:
                logger.warning(f"[{self.cp_id}] Could not send TriggerMessage — charger not connected?")
        except Exception as e:
            logger.warning(f"[{self.cp_id}] TriggerMessage failed: {e}")

    # ── Pending Start Queue ──────────────────────────────────────────────

    async def _process_pending_starts(self) -> None:
        """Execute any queued RemoteStart commands for this charger.

        Called on BootNotification and first Heartbeat. Each pending start has
        a max of 3 retries with a 2-second stabilisation delay. On exhaustion,
        the session is marked failed and the Mollie payment is cancelled.
        """
        from api.public import _fail_pending_start
        from state import charger_registry

        keys = await redis_state.keys(f"pending_start:{self.cp_id}:*")
        if not keys:
            return

        logger.info(f"[{self.cp_id}] Found {len(keys)} pending RemoteStart(s) — processing")

        for key in keys:
            data_raw = await redis_state.get(key)
            if not data_raw:
                continue  # Already expired or consumed

            try:
                data = json.loads(data_raw)
            except Exception:
                await redis_state.delete(key)
                continue

            retries = data.get("retries", 0)
            if retries >= 3:
                logger.warning(
                    f"[{self.cp_id}] Pending RemoteStart retry limit reached "
                    f"(session={data['session_id'][:8]}) — failing"
                )
                await redis_state.delete(key)
                try:
                    await _fail_pending_start(data)
                except Exception as e:
                    logger.error(f"[{self.cp_id}] _fail_pending_start error: {e}")
                continue

            await asyncio.sleep(2)  # Stabilise after boot

            msg_id = await charger_registry.send_remote_start(
                data["cp_id"], data["connector_id"], data["id_tag"]
            )
            if msg_id:
                logger.info(
                    f"[{self.cp_id}] Pending RemoteStart executed: "
                    f"session={data['session_id'][:8]} (was retry {retries})"
                )
                await redis_state.delete(key)
            else:
                data["retries"] = retries + 1
                await redis_state.set(key, json.dumps(data), ttl=120)
                logger.warning(
                    f"[{self.cp_id}] Pending RemoteStart retry {data['retries']}/3 "
                    f"failed (session={data['session_id'][:8]})"
                )
