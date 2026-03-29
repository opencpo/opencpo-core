"""
OCPP 2.0.1 Security & ISO 15118 / Plug & Charge handlers.

Mixin for ChargePointHandler201 — covers:
- SignCertificate (charger SECC cert CSR)
- Get15118EVCertificate (contract cert provisioning via charger relay)
- SecurityEventNotification (tamper, cert, firmware audit events)
- _validate_contract_cert helper (P&C eMAID auth)
"""
import logging
from datetime import datetime, timezone

from events.types import Event, EventType
from state.postgres import db
from ocpp201.protocol import AuthorizationStatus

logger = logging.getLogger(__name__)


class SecurityMixin:
    """Mixin providing security, PKI, and ISO 15118 handlers."""

    async def _on_security_event(self, payload: dict) -> dict:
        """Security event from charger — log + publish."""
        event_type = payload.get("type", "")
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
        tech_info = payload.get("techInfo", "")

        severity = "critical" if "Tamper" in event_type or "InvalidCertificate" in event_type else "warning"

        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.security_events (time, charge_point, event_type, severity, details)
                VALUES ($1, $2, $3, $4, $5)
            """, timestamp, self.cp_id, event_type, severity, {"tech_info": tech_info})

        await self.event_bus.publish(Event(
            type=EventType.OPS_ALERT,
            charge_point=self.cp_id,
            simulated=self._simulated,
            data={"security_event": event_type, "severity": severity, "tech_info": tech_info},
        ))

        logger.warning(f"[{self.cp_id}] Security event: {event_type} ({severity})")
        return {}

    async def _on_sign_certificate(self, payload: dict) -> dict:
        """
        Charger sends a CSR — we sign it with our Sub-CA.
        This is the core P&C flow for charger SECC certs.
        """
        csr_pem = payload.get("csr", "")
        cert_type = payload.get("certificateType", "ChargingStationCertificate")

        # Log the CSR
        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.pki_csr_log (charge_point, csr_pem, status)
                VALUES ($1, $2, 'pending')
            """, self.cp_id, csr_pem)

        # Sign with our PKI (will be implemented in pki/ module)
        # For now: accept the CSR, actual signing happens async
        # Server sends CertificateSigned message back with the signed cert

        logger.info(f"[{self.cp_id}] SignCertificate request: type={cert_type}")

        await self.event_bus.publish(Event(
            type=EventType.PKI_CERT_ISSUED,
            charge_point=self.cp_id,
            simulated=self._simulated,
            data={"type": cert_type, "status": "pending"},
        ))

        return {"status": "Accepted"}

    async def _on_get_15118_ev_certificate(self, payload: dict) -> dict:
        """
        Vehicle requests a contract cert via the charger.
        Charger relays the ISO 15118 CertificateInstallation/Update request.
        """
        action = payload.get("action", "Install")  # Install or Update
        iso15118_schema = payload.get("iso15118SchemaVersion", "")
        exi_request = payload.get("exiRequest", "")

        logger.info(f"[{self.cp_id}] Get15118EVCertificate: action={action} schema={iso15118_schema}")

        # This will be handled by the iso15118/ module
        # For now: log and return pending
        # In production: decode EXI, validate, provision contract cert

        return {
            "status": "Accepted",
            "exiResponse": "",  # Will contain the signed contract cert in EXI
        }

    async def _validate_contract_cert(self, emaid: str, cert_hash_data: list) -> str:
        """Validate a contract certificate for P&C authorization."""
        # Will be implemented in pki/ module
        # For now: check if the eMAID is in our auth cache
        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM ocpp.authorization_cache WHERE token = $1", emaid
            )
        return row["status"] if row else AuthorizationStatus.INVALID
