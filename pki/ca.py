"""
Built-in Certificate Authority — zero external dependencies.

Handles the full PKI lifecycle:
- Root CA (self-signed, offline-capable)
- CPO Sub-CA (signs SECC certs for chargers)
- MO Sub-CA (signs contract certs for drivers/vehicles)
- Certificate issuance, renewal, revocation
- CRL generation
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, BestAvailableEncryption, NoEncryption,
)

from config import config

logger = logging.getLogger(__name__)


class CertificateAuthority:
    """
    Built-in PKI Certificate Authority.
    
    Certificate hierarchy:
        Root CA (self-signed)
        ├── CPO Sub-CA (signs charger SECC certs)
        ├── MO Sub-CA (signs driver/vehicle contract certs)
        └── User Sub-CA (signs client certs for browser SSO)
    """

    def __init__(self, data_dir: str | None = None):
        self.data_dir = Path(data_dir or config.pki.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "issued").mkdir(exist_ok=True)
        (self.data_dir / "users").mkdir(exist_ok=True)

        self._root_ca_key = None
        self._root_ca_cert = None
        self._cpo_sub_ca_key = None
        self._cpo_sub_ca_cert = None
        self._mo_sub_ca_key = None
        self._mo_sub_ca_cert = None
        self._user_sub_ca_key = None
        self._user_sub_ca_cert = None

    async def initialize(self) -> None:
        """Load existing certs or generate new CA hierarchy."""
        if (self.data_dir / "root-ca.crt").exists():
            await self._load_existing()
        else:
            logger.info("No existing PKI found — generating new CA hierarchy")
            await self._generate_hierarchy()

        # User Sub-CA is generated on first use (may not exist yet on old installs)
        if self._user_sub_ca_cert is None:
            await self._init_user_sub_ca()

    # ── CA Hierarchy Generation ──────────────────────────────────────────

    async def _generate_hierarchy(self) -> None:
        """Generate Root CA → CPO Sub-CA → MO Sub-CA → User Sub-CA."""
        # Root CA
        self._root_ca_key, self._root_ca_cert = self._generate_root_ca()
        self._save_key_cert("root-ca", self._root_ca_key, self._root_ca_cert,
                            password=config.pki.root_ca_key_password)

        # CPO Sub-CA (signs SECC certs)
        self._cpo_sub_ca_key, self._cpo_sub_ca_cert = self._generate_sub_ca(
            cn="CPO Sub-CA",
            parent_key=self._root_ca_key,
            parent_cert=self._root_ca_cert,
        )
        self._save_key_cert("cpo-sub-ca", self._cpo_sub_ca_key, self._cpo_sub_ca_cert,
                            password=config.pki.sub_ca_key_password)

        # MO Sub-CA (signs contract certs)
        self._mo_sub_ca_key, self._mo_sub_ca_cert = self._generate_sub_ca(
            cn="MO Sub-CA",
            parent_key=self._root_ca_key,
            parent_cert=self._root_ca_cert,
        )
        self._save_key_cert("mo-sub-ca", self._mo_sub_ca_key, self._mo_sub_ca_cert,
                            password=config.pki.sub_ca_key_password)

        # User Sub-CA (signs client certs for browser SSO)
        await self._init_user_sub_ca()

        logger.info("PKI hierarchy generated: Root CA → CPO Sub-CA + MO Sub-CA + User Sub-CA")

    async def _init_user_sub_ca(self) -> None:
        """Initialize User Sub-CA — load existing or generate new."""
        user_ca_path = self.data_dir / "user-sub-ca.crt"
        if user_ca_path.exists():
            self._user_sub_ca_key, self._user_sub_ca_cert = self._load_key_cert(
                "user-sub-ca", password=config.pki.sub_ca_key_password
            )
            logger.info("User Sub-CA loaded from disk")
        else:
            logger.info("Generating User Sub-CA...")
            self._user_sub_ca_key, self._user_sub_ca_cert = self._generate_user_sub_ca()
            self._save_key_cert("user-sub-ca", self._user_sub_ca_key, self._user_sub_ca_cert,
                                password=config.pki.sub_ca_key_password)
            logger.info("User Sub-CA generated")

    def _generate_user_sub_ca(self) -> tuple:
        """Generate User Sub-CA (RSA 2048) signed by Root CA."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        subject = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, config.pki.org_name),
            x509.NameAttribute(NameOID.COMMON_NAME, config.pki.user_ca_cn),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._root_ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1825))  # 5 years
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True,
                    content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self._root_ca_key.public_key()
                ),
                critical=False,
            )
            .sign(self._root_ca_key, hashes.SHA256())
        )

        return key, cert

    def _generate_root_ca(self) -> tuple:
        """Generate self-signed Root CA."""
        key = ec.generate_private_key(ec.SECP256R1())

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, config.pki.org_name),
            x509.NameAttribute(NameOID.COMMON_NAME, config.pki.root_ca_cn),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))  # 10 years
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True,
                    content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        return key, cert

    def _generate_sub_ca(self, cn: str, parent_key, parent_cert) -> tuple:
        """Generate a Sub-CA signed by parent."""
        key = ec.generate_private_key(ec.SECP256R1())

        subject = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, config.pki.org_name),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(parent_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1825))  # 5 years
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True,
                    content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(parent_key.public_key()),
                critical=False,
            )
            .sign(parent_key, hashes.SHA256())
        )

        return key, cert

    # ── Certificate Issuance ─────────────────────────────────────────────

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

        # Save issued cert
        (self.data_dir / "issued" / f"{serial_hex}.crt").write_text(cert_pem)

        # Store in database
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

    # ── Revocation ───────────────────────────────────────────────────────

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

    # ── Validation ───────────────────────────────────────────────────────

    async def validate_cert_chain(self, cert_pem: str) -> dict:
        """Validate a certificate against our CA chain."""
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode())
        except Exception as e:
            return {"valid": False, "error": f"Invalid PEM: {e}"}

        now = datetime.now(timezone.utc)

        # Check expiry
        if now < cert.not_valid_before_utc:
            return {"valid": False, "error": "Certificate not yet valid"}
        if now > cert.not_valid_after_utc:
            return {"valid": False, "error": "Certificate expired"}

        # Check issuer
        serial_hex = format(cert.serial_number, 'x')
        from state.postgres import db
        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM ocpp.pki_certificates WHERE serial = $1", serial_hex
            )

        if row and row["status"] == "revoked":
            return {"valid": False, "error": "Certificate revoked"}

        # Verify signature against our CAs
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

    # ── Certificate Info ─────────────────────────────────────────────────

    async def get_cert_chain(self, cert_type: str = "secc") -> str:
        """Get the full cert chain PEM (leaf CA + root)."""
        if cert_type == "secc":
            sub_ca = self._cpo_sub_ca_cert
        else:
            sub_ca = self._mo_sub_ca_cert

        chain = (
            sub_ca.public_bytes(Encoding.PEM).decode()
            + self._root_ca_cert.public_bytes(Encoding.PEM).decode()
        )
        return chain

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

        # Generate RSA 2048 key pair
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

        # Build PKCS#12 bundle with cert chain (modern AES-256 base)
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
            # modern (default) — AES-256, works on macOS 13+, Windows 10+, modern Linux
            # Use openssl to ensure broad OS compatibility (Python cryptography lib output
            # can have macOS Keychain parse issues on some versions)
            import subprocess, tempfile, os as _os
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

        # Save bundle to users/ folder (keyed by serial for later download)
        users_dir = self.data_dir / "users"
        users_dir.mkdir(exist_ok=True)
        (users_dir / f"{serial_hex}.{file_ext}").write_bytes(file_bytes)
        (users_dir / f"{serial_hex}.fmt").write_text(cert_format)

        # Save cert PEM to issued/ folder
        cert_pem = cert.public_bytes(Encoding.PEM).decode()
        (self.data_dir / "issued" / f"{serial_hex}.crt").write_text(cert_pem)

        logger.info(
            f"User cert issued: email={email} role={role} serial={serial_hex} "
            f"format={cert_format} valid={validity_days}d"
        )
        return file_bytes, serial_hex, password

    def _to_legacy_pkcs12(self, p12_modern: bytes, password: str, email: str, cert, key) -> bytes:
        """
        Build a legacy 3DES PKCS#12 via openssl pkcs12 -legacy.
        Falls back to modern format if openssl is unavailable or fails.
        """
        import subprocess, tempfile, os as _os

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
                [
                    'openssl', 'pkcs12', '-export', '-legacy',
                    '-in', tmp_pem,
                    '-passout', f'pass:{password}',
                    '-out', tmp_out,
                    '-name', email,
                ],
                capture_output=True,
                timeout=15,
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
        """
        Create a tar.gz bundle: {safe_email}-cert.pem + {safe_email}-key.pem + ca-chain.pem.
        Key is encrypted with AES-256 using the supplied password.
        """
        import io, tarfile

        cert_pem = cert.public_bytes(Encoding.PEM)
        key_pem = key.private_bytes(
            Encoding.PEM,
            PrivateFormat.PKCS8,
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

    def get_user_ca_cert_pem(self) -> str:
        """Return User Sub-CA cert as PEM (for trust store distribution)."""
        if self._user_sub_ca_cert is None:
            raise RuntimeError("User Sub-CA not initialized")
        return self._user_sub_ca_cert.public_bytes(Encoding.PEM).decode()

    # ── Load / Save ──────────────────────────────────────────────────────

    async def _load_existing(self) -> None:
        """Load existing CA certs and keys from disk."""
        self._root_ca_key, self._root_ca_cert = self._load_key_cert(
            "root-ca", password=config.pki.root_ca_key_password
        )
        self._cpo_sub_ca_key, self._cpo_sub_ca_cert = self._load_key_cert(
            "cpo-sub-ca", password=config.pki.sub_ca_key_password
        )
        self._mo_sub_ca_key, self._mo_sub_ca_cert = self._load_key_cert(
            "mo-sub-ca", password=config.pki.sub_ca_key_password
        )
        # User Sub-CA loaded lazily in initialize() via _init_user_sub_ca()
        logger.info("PKI loaded: Root CA + CPO Sub-CA + MO Sub-CA")

    def _save_key_cert(self, name: str, key, cert, password: str = "") -> None:
        """Save key + cert to disk."""
        encryption = (
            BestAvailableEncryption(password.encode()) if password
            else NoEncryption()
        )
        (self.data_dir / f"{name}.key").write_bytes(
            key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, encryption)
        )
        (self.data_dir / f"{name}.crt").write_bytes(
            cert.public_bytes(Encoding.PEM)
        )

    def _load_key_cert(self, name: str, password: str = "") -> tuple:
        """Load key + cert from disk."""
        key_data = (self.data_dir / f"{name}.key").read_bytes()
        cert_data = (self.data_dir / f"{name}.crt").read_bytes()

        pwd = password.encode() if password else None
        key = serialization.load_pem_private_key(key_data, password=pwd)
        cert = x509.load_pem_x509_certificate(cert_data)

        return key, cert


# Singleton
ca = CertificateAuthority()
