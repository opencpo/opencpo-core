"""
Charger registry — single interface for charger state + commands.

State: reads from Redis (set by OCPP handlers on BootNotification/StatusNotification).
Commands: sends OCPP messages via the registered server instance.

Populated by main.py after server creation. Read by api/public.py and api/chargers.py.
"""
import json
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Set by main.py after OCPP16Server is created
_ocpp16_server = None
_redis_state = None  # state.redis.RedisState
_handlers_16 = None  # dict[str, ChargePointHandler] — set by main.py


def set_ocpp16_server(server) -> None:
    """Register the OCPP 1.6j server instance."""
    global _ocpp16_server
    _ocpp16_server = server


def set_redis_state(redis_state) -> None:
    """Register the Redis state backend."""
    global _redis_state
    _redis_state = redis_state


def set_handlers_16(handlers: dict) -> None:
    """Register the OCPP 1.6j handler dict."""
    global _handlers_16
    _handlers_16 = handlers


async def get_charger_live(cp_id: str) -> dict | None:
    """Get live charger state from Redis."""
    if _redis_state is None:
        return None
    return await _redis_state.get_charger(cp_id)


async def list_chargers_live() -> list[str]:
    """List all charger IDs with live state in Redis."""
    if _redis_state is None:
        return []
    return await _redis_state.list_chargers()


async def send_remote_start(cp_id: str, connector_id: int, id_tag: str) -> Optional[str]:
    """
    Send RemoteStartTransaction to a charger.
    Returns the unique_id of the sent message, or None if not connected.
    """
    if _ocpp16_server is None:
        logger.warning("OCPP 1.6j server not registered in charger_registry")
        return None

    import time
    msg_id = str(uuid.uuid4())
    payload = json.dumps([2, msg_id, "RemoteStartTransaction", {
        "connectorId": connector_id,
        "idTag": id_tag,
    }])

    # Register pending call so handler tracks the response
    if _handlers_16 and cp_id in _handlers_16:
        _handlers_16[cp_id]._pending_calls[msg_id] = ("RemoteStartTransaction", time.monotonic())

    ok = await _ocpp16_server.send_to(cp_id, payload)
    if ok:
        logger.info(f"RemoteStartTransaction sent to {cp_id} connector={connector_id} id_tag={id_tag} msg_id={msg_id}")
        return msg_id
    else:
        logger.warning(f"RemoteStartTransaction: {cp_id} not connected")
        return None


def is_connected(cp_id: str) -> bool:
    """Check if a charger is currently connected."""
    if _ocpp16_server is None:
        return False
    return _ocpp16_server.is_connected(cp_id)


async def send_reset(cp_id: str, reset_type: str = "Hard") -> Optional[str]:
    """Send Reset command to a charger."""
    if _ocpp16_server is None:
        return None
    msg_id = str(uuid.uuid4())
    payload = json.dumps([2, msg_id, "Reset", {"type": reset_type}])
    ok = await _ocpp16_server.send_to(cp_id, payload)
    if ok:
        logger.info(f"Reset ({reset_type}) sent to {cp_id}")
        return msg_id
    logger.warning(f"Reset: {cp_id} not connected")
    return None


async def send_command(cp_id: str, action: str, command_payload: dict) -> Optional[str]:
    """Send any OCPP command to a charger."""
    if _ocpp16_server is None:
        return None
    import time
    msg_id = str(uuid.uuid4())
    payload = json.dumps([2, msg_id, action, command_payload])
    
    if _handlers_16 and cp_id in _handlers_16:
        _handlers_16[cp_id]._pending_calls[msg_id] = (action, time.monotonic())
    
    ok = await _ocpp16_server.send_to(cp_id, payload)
    if ok:
        logger.info(f"{action} sent to {cp_id} msg_id={msg_id}")
        return msg_id
    logger.warning(f"{action}: {cp_id} not connected")
    return None
