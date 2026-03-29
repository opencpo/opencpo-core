# PKI — Built-in Certificate Authority

ocpp-core includes a built-in Certificate Authority for issuing and managing certificates without external dependencies. This supports Plug & Charge (ISO 15118 / OCPP 2.0.1 security profile 3) and TLS client authentication.

## Certificate Hierarchy

```
Root CA (self-signed, EC P-256, 10 years)
├── CPO Sub-CA (EC P-256, 5 years)
│   └── SECC certificates — issued to chargers for mTLS (1 year each)
├── MO Sub-CA (EC P-256, 5 years)
│   └── Contract certificates — issued to vehicles/drivers for Plug & Charge
└── User Sub-CA (RSA 2048, 5 years)
    └── User client certificates — for browser/device SSO (PKCS#12)
```

**Root CA** — the trust anchor. Never exported. Kept on disk in `PKI_DATA_DIR`. Back it up.

**CPO Sub-CA** — signs SECC certificates for chargers. A charger uses its SECC certificate to authenticate to the CSMS via mTLS.

**MO Sub-CA** — signs contract certificates for drivers. Used in Plug & Charge flows where the vehicle authenticates automatically without a card or app.

**User Sub-CA** — signs client certificates for operator/admin browser SSO.

## Initialization

On first startup, if no CA exists in `PKI_DATA_DIR`, the full hierarchy is generated automatically:

```
INFO  No existing PKI found — generating new CA hierarchy
INFO  PKI hierarchy generated: Root CA → CPO Sub-CA + MO Sub-CA + User Sub-CA
```

On subsequent starts, the existing keys and certificates are loaded from disk:

```
INFO  PKI loaded: Root CA + CPO Sub-CA + MO Sub-CA
INFO  User Sub-CA loaded from disk
```

## Issuing SECC Certificates (Charger Authentication)

For OCPP 2.0.1 security profile 3, chargers send a CSR via `SignCertificate`. The handler signs it with the CPO Sub-CA:

```python
# Happens automatically in ocpp201/security.py
cert_pem, serial = await ca.sign_secc_csr(csr_pem, charge_point_id)
```

The signed certificate is:
- Returned to the charger in the `SignCertificate` response
- Stored in `ocpp.pki_certificates` with `type='secc'`
- Saved to `PKI_DATA_DIR/issued/{serial}.crt`

**Certificate properties:**
- Signed by CPO Sub-CA
- KeyUsage: `digitalSignature`, `keyEncipherment`
- ExtendedKeyUsage: `clientAuth`, `serverAuth`
- Validity: `PKI_CERT_VALIDITY_DAYS` (default 365)

## Issuing Contract Certificates (Plug & Charge)

For driver/vehicle Plug & Charge:

```python
cert_pem, serial = await ca.sign_contract_cert(csr_pem, emaid)
# emaid: e-mobility account identifier, e.g. "NLCPO000000001"
```

The contract certificate:
- Signed by MO Sub-CA
- Subject CN = EMAID
- KeyUsage: `digitalSignature`
- ExtendedKeyUsage: `clientAuth`

## Certificate Revocation

To revoke a certificate (e.g., charger decommissioned, credentials compromised):

```python
success = await ca.revoke_certificate(serial_hex, reason="keyCompromise")
```

Reasons follow RFC 5280: `unspecified`, `keyCompromise`, `affiliationChanged`, `superseded`, `cessationOfOperation`, `privilegeWithdrawn`.

Generate a CRL for distribution to chargers:

```python
crl_pem = await ca.generate_crl()
```

The CRL covers the CPO Sub-CA's issued certificates (SECC certs). Chargers should be configured to fetch the CRL from your OCSP endpoint.

## OCSP Responder

The built-in OCSP responder runs on `PKI_OCSP_PORT` (default 8099). Chargers can check certificate validity in real-time.

## Certificate Validation

To validate an incoming client certificate:

```python
result = await ca.validate_cert_chain(cert_pem)
# {
#   "valid": True,
#   "issuer": "CN=CPO Sub-CA,O=My CPO",
#   "serial": "1a2b3c4d",
#   "expires": "2025-01-15T10:30:00+00:00"
# }
```

Or if invalid:
```python
# {"valid": False, "error": "Certificate revoked"}
# {"valid": False, "error": "Certificate expired"}
# {"valid": False, "error": "Unknown issuer — not signed by our CA"}
```

## Monitoring Expiring Certificates

```python
expiring = await ca.get_expiring_certs(days=30)
# [
#   {"serial": "abc123", "type": "secc", "charge_point": "CP-001",
#    "not_after": "2024-02-01T00:00:00+00:00"},
#   ...
# ]
```

Use this to trigger renewal workflows before expiry.

## PKI Statistics

```python
stats = await ca.stats()
# {
#   "active": 42,
#   "revoked": 3,
#   "secc": 38,
#   "contract": 7,
#   "expiring_30d": 5
# }
```

## Storage

All CA keys and certificates are in `PKI_DATA_DIR` (default `./data/pki`):

```
data/pki/
├── root-ca.key        # Root CA private key (encrypt with PKI_ROOT_CA_PASSWORD)
├── root-ca.crt        # Root CA certificate
├── cpo-sub-ca.key     # CPO Sub-CA private key
├── cpo-sub-ca.crt     # CPO Sub-CA certificate
├── mo-sub-ca.key      # MO Sub-CA private key
├── mo-sub-ca.crt      # MO Sub-CA certificate
├── user-sub-ca.key    # User Sub-CA private key
├── user-sub-ca.crt    # User Sub-CA certificate
├── issued/            # All issued leaf certificates
│   ├── 1a2b3c4d.crt
│   └── ...
└── users/             # User PKCS#12 bundles (for download)
    ├── serial.p12
    └── ...
```

**Back up `data/pki/` regularly.** Losing the Root CA key means you cannot issue new certificates or sign a new Sub-CA.

## Key Algorithms

- Root CA: EC P-256 (SECP256R1)
- Sub-CAs: EC P-256 (SECP256R1)
- User Sub-CA: RSA 2048 (for broader compatibility with browsers and OS key stores)
- Leaf certificates: inherits key from CSR (charger generates its own key)
