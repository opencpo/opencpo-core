"""
OCPP 2.0.1 WebSocket Server.

Same pattern as 1.6j but with:
- ocpp2.0.1 subprotocol
- Security profile support (TLS with client certs for P&C)
"""
import asyncio
import logging
import ssl
from pathlib import Path
from typing import Callable

import websockets
from websockets.asyncio.server import ServerConnection

from config import config

logger = logging.getLogger(__name__)


class OCPP201Server:
    """OCPP 2.0.1 WebSocket server with optional TLS for security profiles."""

    def __init__(self, on_connect: Callable, on_disconnect: Callable):
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._connections: dict[str, ServerConnection] = {}
        self._server = None

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Build TLS context for OCPP security profile 2/3."""
        pki_dir = Path(config.pki.data_dir)
        server_cert = pki_dir / "server.crt"
        server_key = pki_dir / "server.key"
        ca_cert = pki_dir / "root-ca.crt"

        if not server_cert.exists():
            logger.info("No TLS certs found — running without TLS (security profile 1)")
            return None

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(server_cert), str(server_key))

        if ca_cert.exists():
            # Security profile 3: mutual TLS (verify client cert)
            ctx.load_verify_locations(str(ca_cert))
            ctx.verify_mode = ssl.CERT_OPTIONAL  # Optional — not all chargers have certs yet
            logger.info("TLS enabled with client cert verification (security profile 3)")
        else:
            logger.info("TLS enabled without client cert verification (security profile 2)")

        return ctx

    async def serve(self) -> None:
        """Start the WebSocket server."""
        ssl_context = self._build_ssl_context()

        self._server = await websockets.serve(
            self._handler,
            config.ocpp.ocpp201_host,
            config.ocpp.ocpp201_port,
            subprotocols=["ocpp2.0.1"],
            ssl=ssl_context,
            ping_interval=None,  # Disable WS pings — use OCPP heartbeats
            ping_timeout=None,
            max_size=65536,
            logger=logger,
        )

        scheme = "wss" if ssl_context else "ws"
        logger.info(
            f"OCPP 2.0.1 server listening on "
            f"{scheme}://{config.ocpp.ocpp201_host}:{config.ocpp.ocpp201_port}"
        )
        await self._server.wait_closed()

    async def _handler(self, websocket: ServerConnection) -> None:
        """Handle a new charger connection."""
        path = websocket.request.path if websocket.request else ""
        cp_id = path.strip("/").split("/")[-1] if path else "unknown"

        if websocket.subprotocol != "ocpp2.0.1":
            logger.warning(f"Rejected {cp_id}: wrong subprotocol {websocket.subprotocol}")
            await websocket.close(1002, "Subprotocol not supported")
            return

        # Extract client cert info if available (for P&C / security profile 3)
        client_cert = None
        if hasattr(websocket, 'transport'):
            ssl_object = websocket.transport.get_extra_info('ssl_object')
            if ssl_object:
                client_cert = ssl_object.getpeercert()

        logger.info(
            f"Charger connected: {cp_id} (OCPP 2.0.1)"
            + (f" [client cert: {client_cert.get('subject', 'unknown')}]" if client_cert else "")
        )
        self._connections[cp_id] = websocket

        try:
            # on_connect handles the message loop
            await self._on_connect(cp_id, websocket, client_cert)

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"Charger disconnected: {cp_id} (code={e.code})")
        except Exception as e:
            logger.error(f"Error handling {cp_id}: {e}", exc_info=True)
        finally:
            self._connections.pop(cp_id, None)
            await self._on_disconnect(cp_id)

    async def send_to(self, cp_id: str, message: str) -> bool:
        ws = self._connections.get(cp_id)
        if ws is None:
            return False
        try:
            await ws.send(message)
            return True
        except Exception:
            return False

    def is_connected(self, cp_id: str) -> bool:
        return cp_id in self._connections

    @property
    def connected_count(self) -> int:
        return len(self._connections)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
