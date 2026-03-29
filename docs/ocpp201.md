# OCPP 2.0.1 Implementation

## Connection

Chargers connect to:
```
ws://your-host:9201/ocpp/{ChargePointIdentity}
```

OCPP 2.0.1 uses the same OCPP-J JSON-over-WebSocket framing as 1.6j. The WebSocket subprotocol is `ocpp2.0.1`.

## Key Differences from OCPP 1.6

| Aspect | OCPP 1.6 | OCPP 2.0.1 |
|---|---|---|
| Session events | `StartTransaction` / `StopTransaction` / `MeterValues` | Single `TransactionEvent` with `eventType` field |
| Configuration | `ChangeConfiguration` / `GetConfiguration` | `SetVariables` / `GetVariables` (Component/Variable model) |
| Security | Basic auth or TLS | Defined security profiles (0–3) |
| Certificates | Manual management | `SignCertificate` for automated Plug & Charge |
| EVSE model | Connector IDs | EVSE + Connector hierarchy |

## Supported Actions (Charger → Server)

| Action | Description |
|---|---|
| `BootNotification` | Charger startup (payload uses `chargingStation` wrapper) |
| `Heartbeat` | Liveness check |
| `StatusNotification` | Connector/EVSE status (uses `connectorStatus` field) |
| `Authorize` | Token authorization (`idToken` object) |
| `TransactionEvent` | All session events: Started, Updated, Ended |
| `MeterValues` | Standalone meter readings (outside a transaction) |
| `SecurityEventNotification` | Security audit events |
| `SignCertificate` | CSR for automated certificate provisioning (PnC) |
| `Get15118EVCertificate` | Vehicle contract certificate request |
| `NotifyEvent` | Component/variable event reports |
| `NotifyReport` | Response to GetReport |
| `DataTransfer` | Vendor-specific data |
| `FirmwareStatusNotification` | Firmware update progress |
| `LogStatusNotification` | Log upload status |
| `NotifyChargingLimit` | EV-side charging limit reported |
| `NotifyEVChargingNeeds` | Vehicle's requested energy and departure time |
| `ReportChargingProfiles` | Response to GetChargingProfiles |

## TransactionEvent

`TransactionEvent` replaces the three separate 1.6 messages. The `eventType` field distinguishes phases:

```json
{
    "eventType": "Started",          // or "Updated" or "Ended"
    "timestamp": "2024-01-15T10:30:00Z",
    "triggerReason": "Authorized",
    "seqNo": 0,
    "transactionInfo": {
        "transactionId": "TX-20240115-001",
        "chargingState": "Charging"
    },
    "evse": {"id": 1, "connectorId": 1},
    "idToken": {"idToken": "DEADBEEF", "type": "ISO14443"},
    "meterValue": [...]               // Present in Updated/Ended
}
```

- `Started` → equivalent to 1.6 `StartTransaction`
- `Updated` → equivalent to 1.6 `MeterValues` (during a transaction)
- `Ended` → equivalent to 1.6 `StopTransaction`

## Security Profiles

OCPP 2.0.1 defines four security profiles:

| Profile | Transport | Authentication |
|---|---|---|
| 0 | Plain WS (`ws://`) | None |
| 1 | Plain WS (`ws://`) | HTTP Basic auth |
| 2 | TLS (`wss://`) | HTTP Basic auth |
| 3 | TLS (`wss://`) | TLS client certificate (mTLS) |

Profile 3 uses the built-in PKI: the charger generates a key pair, sends a CSR via `SignCertificate`, and the CPO Sub-CA signs it. The charger then uses the issued certificate for mTLS on reconnect.

For Profile 3, the TLS client certificate is passed to `ChargePointHandler201` as `client_cert` and can be used to verify the charger's identity independent of the WebSocket URL.

## Device Model

OCPP 2.0.1 replaces the flat key/value configuration (`ChangeConfiguration`) with a hierarchical Component/Variable model.

- **Component** — a named part of the charger (e.g., `ChargingStation`, `EVSE`, `Connector`, `SmartChargingCtrlr`)
- **Variable** — a named attribute of a component (e.g., `HeartbeatInterval`, `OperatingVoltage`)

Use `GetVariables` / `SetVariables` to read/write configuration:

```json
// GetVariables request
{
    "getVariableData": [
        {
            "component": {"name": "ChargingStation"},
            "variable": {"name": "HeartbeatInterval"}
        }
    ]
}
```

## Plug & Charge (ISO 15118)

OCPP 2.0.1 supports automated Plug & Charge via `SignCertificate` and `Get15118EVCertificate`:

1. Charger generates RSA key pair and sends CSR via `SignCertificate`
2. `SecurityMixin._on_sign_certificate()` routes to the PKI CA
3. CA signs the CSR with the CPO Sub-CA (see [pki.md](pki.md))
4. Signed certificate returned to charger

For vehicle-side contracts:
1. Vehicle presents EMAID via ISO 15118 handshake
2. Charger requests contract certificate via `Get15118EVCertificate`
3. `SecurityMixin._on_get_15118_ev_certificate()` looks up or provisions the contract cert

## Code Structure

The 2.0.1 handler is split across multiple files to keep each under 500 lines:

```
ocpp201/
├── handler.py        # ChargePointHandler201 — routing, boot, heartbeat, status
├── transaction.py    # TransactionMixin — TransactionEvent (Started/Updated/Ended)
├── meter.py          # MeterMixin — standalone MeterValues
├── security.py       # SecurityMixin — SignCertificate, Get15118EV, SecurityEvent
├── protocol.py       # Message framing, action constants
└── server.py         # WebSocket server
```

`ChargePointHandler201` uses Python multiple inheritance to compose the mixins:

```python
class ChargePointHandler201(TransactionMixin, MeterMixin, SecurityMixin):
    ...
```
