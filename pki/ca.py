"""
Built-in Certificate Authority — zero external dependencies.

Handles the full PKI lifecycle:
- Root CA (self-signed, offline-capable)
- CPO Sub-CA (signs SECC certs for chargers)
- MO Sub-CA (signs contract certs for drivers/vehicles)
- Certificate issuance, renewal, revocation, CRL generation

Split into three modules:
  ca.py       — this file: CA class, init, hierarchy generation, load/save
  ca_certs.py — CertificateIssuanceMixin: sign_secc_csr, sign_contract_cert, issue_user_cert
  ca_ops.py   — CertificateOpsMixin: revoke_certificate, generate_crl, validate_cert_chain
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, BestAvailableEncryption, NoEncryption,
)

from config import config
from pki.ca_certs import CertificateIssuanceMixin
from pki.ca_ops import CertificateOpsMixin

logger = logging.getLogger(__name__)


class CertificateAuthority(CertificateIssuanceMixin, CertificateOpsMixin):
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

        # User Sub-CA generated on first use (may not exist on old installs)
        if self._user_sub_ca_cert is None:
            await self._init_user_sub_ca()

    # ── CA Hierarchy Generation ──────────────────────────────────────────

    async def _generate_hierarchy(self) -> None:
        """Generate Root CA → CPO Sub-CA → MO Sub-CA → User Sub-CA."""
        self._root_ca_key, self._root_ca_cert = self._generate_root_ca()
        self._save_key_cert("root-ca", self._root_ca_key, self._root_ca_cert,
                            password=config.pki.root_ca_key_password)

        self._cpo_sub_ca_key, self._cpo_sub_ca_cert = self._generate_sub_ca(
            cn="CPO Sub-CA", parent_key=self._root_ca_key, parent_cert=self._root_ca_cert,
        )
        self._save_key_cert("cpo-sub-ca", self._cpo_sub_ca_key, self._cpo_sub_ca_cert,
                            password=config.pki.sub_ca_key_password)

        self._mo_sub_ca_key, self._mo_sub_ca_cert = self._generate_sub_ca(
            cn="MO Sub-CA", parent_key=self._root_ca_key, parent_cert=self._root_ca_cert,
        )
        self._save_key_cert("mo-sub-ca", self._mo_sub_ca_key, self._mo_sub_ca_cert,
                            password=config.pki.sub_ca_key_password)

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
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1825))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True,
                    content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ), critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._root_ca_key.public_key()),
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
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True,
                    content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ), critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False,
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
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1825))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True,
                    content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ), critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(parent_key.public_key()),
                critical=False,
            )
            .sign(parent_key, hashes.SHA256())
        )
        return key, cert

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
        # User Sub-CA loaded lazily via initialize() → _init_user_sub_ca()
        logger.info("PKI loaded: Root CA + CPO Sub-CA + MO Sub-CA")

    def _save_key_cert(self, name: str, key, cert, password: str = "") -> None:
        """Save key + cert to disk."""
        encryption = BestAvailableEncryption(password.encode()) if password else NoEncryption()
        (self.data_dir / f"{name}.key").write_bytes(
            key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, encryption)
        )
        (self.data_dir / f"{name}.crt").write_bytes(cert.public_bytes(Encoding.PEM))

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
