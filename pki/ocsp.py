"""
OCSP Responder — Online Certificate Status Protocol.

Provides real-time certificate validity checks.
Vehicles and chargers query this to verify certs haven't been revoked.
"""
import logging
from datetime import datetime, timezone

from cryptography import x509
from cryptography.x509 import ocsp
from cryptography.hazmat.primitives import hashes, serialization

from pki.ca import ca

logger = logging.getLogger(__name__)


class OCSPResponder:
    """Built-in OCSP responder for cert validity checks."""

    async def handle_request(self, ocsp_request_der: bytes) -> bytes:
        """
        Process an OCSP request, return OCSP response.
        
        Checks both charger/contract certs (pki_certificates) and user certs.
        Returns DER-encoded OCSP response.
        """
        try:
            req = ocsp.load_der_ocsp_request(ocsp_request_der)
        except Exception as e:
            logger.error(f"Invalid OCSP request: {e}")
            return self._build_error_response()

        serial_hex = format(req.serial_number, 'x')

        # Look up certificate status — checks all cert types including user certs
        from state.postgres import db
        async with db.read() as conn:
            cert_row = await conn.fetchrow("""
                SELECT status, revoked_at, revocation_reason, not_after, type
                FROM ocpp.pki_certificates WHERE serial = $1
            """, serial_hex)

            # Also check users table directly (belt + suspenders)
            if cert_row is None:
                user_row = await conn.fetchrow("""
                    SELECT cert_status, cert_revoked_at, cert_expires_at
                    FROM ocpp.users WHERE cert_serial = $1
                """, serial_hex)
                if user_row:
                    # Synthesize a cert_row-like dict from users table
                    cert_row = {
                        "status": user_row["cert_status"],
                        "revoked_at": user_row["cert_revoked_at"],
                        "revocation_reason": "unspecified",
                        "not_after": user_row["cert_expires_at"],
                        "type": "user",
                    }

        now = datetime.now(timezone.utc)

        if not cert_row:
            cert_status = ocsp.OCSPCertStatus.UNKNOWN
            revocation_time = None
            revocation_reason = None
        elif cert_row["status"] == "revoked":
            cert_status = ocsp.OCSPCertStatus.REVOKED
            revocation_time = cert_row["revoked_at"]
            revocation_reason = None
        elif cert_row["not_after"] and cert_row["not_after"] < now:
            cert_status = ocsp.OCSPCertStatus.REVOKED  # Expired = effectively revoked
            revocation_time = cert_row["not_after"]
            revocation_reason = None
        else:
            cert_status = ocsp.OCSPCertStatus.GOOD
            revocation_time = None
            revocation_reason = None

        # Pick the right responder cert/key based on cert type
        cert_type = cert_row["type"] if cert_row else "secc"
        if cert_type == "user":
            responder_cert = ca._user_sub_ca_cert
            responder_key = ca._user_sub_ca_key
        else:
            responder_cert = ca._cpo_sub_ca_cert
            responder_key = ca._cpo_sub_ca_key

        # Build response
        builder = ocsp.OCSPResponseBuilder()
        builder = builder.add_response(
            cert=responder_cert,
            issuer=ca._root_ca_cert,
            algorithm=hashes.SHA256(),
            cert_status=cert_status,
            this_update=now,
            next_update=now,
            revocation_time=revocation_time,
            revocation_reason=revocation_reason,
        )
        builder = builder.responder_id(
            ocsp.OCSPResponderEncoding.HASH, responder_cert
        )

        response = builder.sign(responder_key, hashes.SHA256())

        logger.debug(f"OCSP response: serial={serial_hex} type={cert_type} status={cert_status.name}")
        return response.public_bytes(serialization.Encoding.DER)

    def _build_error_response(self) -> bytes:
        """Build a malformed request OCSP error response."""
        response = ocsp.OCSPResponseBuilder.build_unsuccessful(
            ocsp.OCSPResponseStatus.MALFORMED_REQUEST
        )
        return response.public_bytes(serialization.Encoding.DER)


# Singleton
ocsp_responder = OCSPResponder()
