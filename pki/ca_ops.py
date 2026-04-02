"""
Certificate revocation, CRL generation, and validation for CertificateAuthority.

Mixin: CertificateOpsMixin
Provides revoke_certificate, generate_crl, validate_cert_chain.
"""
import logging
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger(__name__)


class CertificateOpsMixin:
    """Revocation, CRL, and validation operations — mixed into CertificateAuthority."""

    async def revoke_certificate(self, serial_hex: str, reason: str = "unspecified") -> bool:
        """Revoke a certificate by serial number."""
        from state.postgres import db
        async with db.transaction() as conn:
            result = await conn.execute("""
                UPDATE ocpp.pki_certificates SET status = 'revoked', revoked_at = NOW(),
                    revocation_reason = $1
                WHERE serial = $2 AND status = 'active'
            """, reason, serial_hex)

            if result == "UPDATE 0":
                return False

            await conn.execute("""
                INSERT INTO ocpp.pki_revocations (serial, reason)
                VALUES ($1, $2)
                ON CONFLICT (serial) DO NOTHING
            """, serial_hex, reason)

        logger.warning(f"Certificate revoked: serial={serial_hex} reason={reason}")
        return True

    async def generate_crl(self) -> bytes:
        """Generate a Certificate Revocation List."""
        from state.postgres import db

        builder = x509.CertificateRevocationListBuilder()
        builder = builder.issuer_name(self._cpo_sub_ca_cert.subject)
        builder = builder.last_update(datetime.now(timezone.utc))
        builder = builder.next_update(datetime.now(timezone.utc) + timedelta(hours=24))

        async with db.read() as conn:
            revocations = await conn.fetch("""
                SELECT r.serial, r.revoked_at, r.reason
                FROM ocpp.pki_revocations r
                JOIN ocpp.pki_certificates c ON r.serial = c.serial
                WHERE c.issuer = $1
            """, self._cpo_sub_ca_cert.subject.rfc4514_string())

        for rev in revocations:
            revoked = (
                x509.RevokedCertificateBuilder()
                .serial_number(int(rev["serial"], 16))
                .revocation_date(rev["revoked_at"])
                .build()
            )
            builder = builder.add_revoked_certificate(revoked)

        crl = builder.sign(self._cpo_sub_ca_key, hashes.SHA256())
        logger.info(f"CRL generated: {len(revocations)} revoked certs")
        return crl.public_bytes(Encoding.PEM)

    async def validate_cert_chain(self, cert_pem: str) -> dict:
        """Validate a certificate against our CA chain."""
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode())
        except Exception as e:
            return {"valid": False, "error": f"Invalid PEM: {e}"}

        now = datetime.now(timezone.utc)
        if now < cert.not_valid_before_utc:
            return {"valid": False, "error": "Certificate not yet valid"}
        if now > cert.not_valid_after_utc:
            return {"valid": False, "error": "Certificate expired"}

        serial_hex = format(cert.serial_number, 'x')
        from state.postgres import db
        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM ocpp.pki_certificates WHERE serial = $1", serial_hex
            )

        if row and row["status"] == "revoked":
            return {"valid": False, "error": "Certificate revoked"}

        issuers = [self._root_ca_cert, self._cpo_sub_ca_cert, self._mo_sub_ca_cert]
        for issuer in issuers:
            try:
                issuer.public_key().verify(
                    cert.signature,
                    cert.tbs_certificate_bytes,
                    ec.ECDSA(cert.signature_hash_algorithm),
                )
                return {
                    "valid": True,
                    "issuer": issuer.subject.rfc4514_string(),
                    "serial": serial_hex,
                    "expires": cert.not_valid_after_utc.isoformat(),
                }
            except Exception:
                continue

        return {"valid": False, "error": "Unknown issuer — not signed by our CA"}
