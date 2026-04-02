"""
Certificate issuance methods for CertificateAuthority.

Mixin: CertificateIssuanceMixin
Provides sign_secc_csr, sign_contract_cert, issue_user_cert and helpers.
"""
import logging
import subprocess
import tempfile
import os as _os
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, BestAvailableEncryption, NoEncryption,
)

from config import config

logger = logging.getLogger(__name__)


class CertificateIssuanceMixin:
    """Certificate issuance operations — mixed into CertificateAuthority."""

    # ── SECC / Contract Issuance ─────────────────────────────────────────

    async def sign_secc_csr(self, csr_pem: str, charge_point_id: str) -> tuple[str, str]:
        """
        Sign a charger's CSR with the CPO Sub-CA.
        Returns (cert_pem, serial_hex).
        """
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        if not csr.is_signature_valid:
            raise ValueError("Invalid CSR signature")

        serial = x509.random_serial_number()
        validity_days = config.pki.cert_validity_days

        cert = (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(self._cpo_sub_ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(serial)
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=validity_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=True,
                    content_commitment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([
                    ExtendedKeyUsageOID.CLIENT_AUTH,
                    ExtendedKeyUsageOID.SERVER_AUTH,
                ]),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self._cpo_sub_ca_key.public_key()
                ),
                critical=False,
            )
            .sign(self._cpo_sub_ca_key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(Encoding.PEM).decode()
        serial_hex = format(serial, 'x')
        (self.data_dir / "issued" / f"{serial_hex}.crt").write_text(cert_pem)

        from state.postgres import db
        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.pki_certificates
                    (serial, type, subject, issuer, charge_point,
                     not_before, not_after, fingerprint, status, pem)
                VALUES ($1, 'secc', $2, $3, $4, $5, $6, $7, 'active', $8)
            """,
                serial_hex,
                cert.subject.rfc4514_string(),
                cert.issuer.rfc4514_string(),
                charge_point_id,
                cert.not_valid_before_utc,
                cert.not_valid_after_utc,
                cert.fingerprint(hashes.SHA256()).hex(),
                cert_pem,
            )

        logger.info(f"SECC cert issued: serial={serial_hex} cp={charge_point_id} valid={validity_days}d")
        return cert_pem, serial_hex

    async def sign_contract_cert(self, csr_pem: str, emaid: str) -> tuple[str, str]:
        """
        Sign a contract certificate with the MO Sub-CA.
        For Plug & Charge driver/vehicle onboarding.
        Returns (cert_pem, serial_hex).
        """
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        if not csr.is_signature_valid:
            raise ValueError("Invalid CSR signature")

        serial = x509.random_serial_number()
        validity_days = config.pki.cert_validity_days

        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, config.pki.org_name),
                x509.NameAttribute(NameOID.COMMON_NAME, emaid),
            ]))
            .issuer_name(self._mo_sub_ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(serial)
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=validity_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=False,
                    content_commitment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .sign(self._mo_sub_ca_key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(Encoding.PEM).decode()
        serial_hex = format(serial, 'x')
        (self.data_dir / "issued" / f"{serial_hex}.crt").write_text(cert_pem)

        from state.postgres import db
        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.pki_certificates
                    (serial, type, subject, issuer, not_before, not_after, fingerprint, status, pem)
                VALUES ($1, 'contract', $2, $3, $4, $5, $6, 'active', $7)
            """,
                serial_hex, emaid,
                cert.issuer.rfc4514_string(),
                cert.not_valid_before_utc, cert.not_valid_after_utc,
                cert.fingerprint(hashes.SHA256()).hex(), cert_pem,
            )

        logger.info(f"Contract cert issued: serial={serial_hex} emaid={emaid}")
        return cert_pem, serial_hex

    # ── User Certificate Issuance ────────────────────────────────────────

    async def issue_user_cert(
        self,
        email: str,
        role: str,
        validity_days: int = 365,
        cert_format: str = "modern",
    ) -> tuple[bytes, str, str]:
        """
        Issue a user client certificate for browser/device SSO.

        cert_format:
            modern  — PKCS#12 with AES-256 (macOS 13+, Windows 10+, modern Linux)
            legacy  — PKCS#12 with 3DES via openssl -legacy (macOS <13, iOS, Windows <10)
            pem     — tar.gz bundle: cert.pem + encrypted key.pem + ca-chain.pem (Linux)

        Returns (file_bytes, serial_hex, password).
        """
        import secrets
        from cryptography.hazmat.primitives.serialization import pkcs12

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        serial = x509.random_serial_number()
        now = datetime.now(timezone.utc)

        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, config.pki.org_name),
                x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, role),
                x509.NameAttribute(NameOID.COMMON_NAME, email),
            ]))
            .issuer_name(self._user_sub_ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(serial)
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=validity_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=False,
                    content_commitment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.RFC822Name(email)]),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self._user_sub_ca_key.public_key()
                ),
                critical=False,
            )
            .sign(self._user_sub_ca_key, hashes.SHA256())
        )

        serial_hex = format(serial, 'x')
        password = secrets.token_urlsafe(16)

        p12_modern = pkcs12.serialize_key_and_certificates(
            name=email.encode(),
            key=key,
            cert=cert,
            cas=[self._user_sub_ca_cert, self._root_ca_cert],
            encryption_algorithm=BestAvailableEncryption(password.encode()),
        )

        if cert_format == "pem":
            file_bytes = self._to_pem_bundle(cert, key, password, email)
            file_ext = "tar.gz"
        elif cert_format == "legacy":
            file_bytes = self._to_legacy_pkcs12(p12_modern, password, email, cert, key)
            file_ext = "p12"
        else:
            # modern (default) — use openssl for broadest macOS Keychain compatibility
            tmp_pem = tempfile.mktemp(suffix='.pem')
            tmp_p12_out = tempfile.mktemp(suffix='.p12')
            try:
                key_pem_bytes = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
                cert_pem_bytes = cert.public_bytes(Encoding.PEM)
                chain_pem = (self._user_sub_ca_cert.public_bytes(Encoding.PEM)
                             + self._root_ca_cert.public_bytes(Encoding.PEM))
                with open(tmp_pem, 'wb') as f:
                    f.write(key_pem_bytes + cert_pem_bytes + chain_pem)
                subprocess.run([
                    'openssl', 'pkcs12', '-export',
                    '-in', tmp_pem, '-out', tmp_p12_out,
                    '-passout', f'pass:{password}',
                    '-certpbe', 'PBE-SHA1-3DES',
                    '-keypbe', 'PBE-SHA1-3DES',
                    '-macalg', 'SHA1',
                ], check=True, capture_output=True, timeout=10)
                with open(tmp_p12_out, 'rb') as f:
                    file_bytes = f.read()
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                logger.warning(f"OpenSSL modern P12 failed: {e}, using Python output")
                file_bytes = p12_modern
            finally:
                for fp in [tmp_pem, tmp_p12_out]:
                    if _os.path.exists(fp):
                        _os.unlink(fp)
            file_ext = "p12"

        users_dir = self.data_dir / "users"
        users_dir.mkdir(exist_ok=True)
        (users_dir / f"{serial_hex}.{file_ext}").write_bytes(file_bytes)
        (users_dir / f"{serial_hex}.fmt").write_text(cert_format)

        cert_pem = cert.public_bytes(Encoding.PEM).decode()
        (self.data_dir / "issued" / f"{serial_hex}.crt").write_text(cert_pem)

        logger.info(
            f"User cert issued: email={email} role={role} serial={serial_hex} "
            f"format={cert_format} valid={validity_days}d"
        )
        return file_bytes, serial_hex, password

    def _to_legacy_pkcs12(self, p12_modern: bytes, password: str, email: str, cert, key) -> bytes:
        """Build a legacy 3DES PKCS#12 via openssl pkcs12 -legacy. Falls back to modern."""
        key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
        cert_chain_pem = (
            cert.public_bytes(Encoding.PEM)
            + self._user_sub_ca_cert.public_bytes(Encoding.PEM)
            + self._root_ca_cert.public_bytes(Encoding.PEM)
        )

        tmp_pem = tempfile.mktemp(suffix='.pem')
        tmp_out = tempfile.mktemp(suffix='.p12')
        try:
            with open(tmp_pem, 'wb') as f:
                f.write(key_pem + cert_chain_pem)
            result = subprocess.run(
                ['openssl', 'pkcs12', '-export', '-legacy',
                 '-in', tmp_pem, '-passout', f'pass:{password}',
                 '-out', tmp_out, '-name', email],
                capture_output=True, timeout=15,
            )
            if result.returncode != 0:
                logger.warning(
                    "openssl legacy p12 failed (%s) — falling back to modern",
                    result.stderr.decode(errors="replace"),
                )
                return p12_modern
            with open(tmp_out, 'rb') as f:
                return f.read()
        except Exception as exc:
            logger.warning("Legacy PKCS#12 conversion failed: %s — using modern", exc)
            return p12_modern
        finally:
            for fp in [tmp_pem, tmp_out]:
                try:
                    _os.unlink(fp)
                except OSError:
                    pass

    def _to_pem_bundle(self, cert, key, password: str, email: str) -> bytes:
        """Create a tar.gz bundle: cert.pem + encrypted key.pem + ca-chain.pem."""
        import io, tarfile

        cert_pem = cert.public_bytes(Encoding.PEM)
        key_pem = key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8,
            BestAvailableEncryption(password.encode()),
        )
        ca_chain_pem = (
            self._user_sub_ca_cert.public_bytes(Encoding.PEM)
            + self._root_ca_cert.public_bytes(Encoding.PEM)
        )

        safe = email.replace("@", "_").replace(".", "_")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def _add(name: str, data: bytes) -> None:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            _add(f"{safe}-cert.pem", cert_pem)
            _add(f"{safe}-key.pem", key_pem)
            _add("ca-chain.pem", ca_chain_pem)

        return buf.getvalue()

    # ── Info / Stats ─────────────────────────────────────────────────────

    def get_user_ca_cert_pem(self) -> str:
        """Return User Sub-CA cert as PEM (for trust store distribution)."""
        if self._user_sub_ca_cert is None:
            raise RuntimeError("User Sub-CA not initialized")
        return self._user_sub_ca_cert.public_bytes(Encoding.PEM).decode()

    async def get_cert_chain(self, cert_type: str = "secc") -> str:
        """Get the full cert chain PEM (leaf CA + root)."""
        sub_ca = self._cpo_sub_ca_cert if cert_type == "secc" else self._mo_sub_ca_cert
        return (
            sub_ca.public_bytes(Encoding.PEM).decode()
            + self._root_ca_cert.public_bytes(Encoding.PEM).decode()
        )

    async def get_expiring_certs(self, days: int = 30) -> list[dict]:
        """Find certificates expiring within N days."""
        from state.postgres import db
        async with db.read() as conn:
            rows = await conn.fetch("""
                SELECT serial, type, subject, charge_point, not_after
                FROM ocpp.pki_certificates
                WHERE status = 'active' AND not_after < NOW() + $1 * INTERVAL '1 day'
                ORDER BY not_after
            """, days)
        return [dict(r) for r in rows]

    async def stats(self) -> dict:
        """PKI statistics."""
        from state.postgres import db
        async with db.read() as conn:
            counts = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'active') as active,
                    COUNT(*) FILTER (WHERE status = 'revoked') as revoked,
                    COUNT(*) FILTER (WHERE type = 'secc') as secc,
                    COUNT(*) FILTER (WHERE type = 'contract') as contract,
                    COUNT(*) FILTER (WHERE status = 'active' AND not_after < NOW() + INTERVAL '30 days') as expiring_30d
                FROM ocpp.pki_certificates
            """)
        return dict(counts) if counts else {}
