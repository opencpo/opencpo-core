# OCPP Core

Open source OCPP 1.6j + 2.0.1 Central System (CSMS) with profile-driven charger compatibility, an event bus architecture, and built-in V2G PKI.

**OCPP 1.6j + 2.0.1 · Built-in PKI/CA · Plug & Charge (ISO 15118) · OCPI 2.2.1 Roaming · Redis Event Bus · PostgreSQL State**

---

## Features

- **OCPP 1.6j & 2.0.1** — Full WebSocket server (Central System / CSMS)
- **Profile-driven charger compatibility** — Per-vendor/model behavior adapters, no if/else spaghetti
- **Built-in PKI** — Own Root CA, CPO Sub-CA, MO Sub-CA. Sign SECC certs and contract certs with zero external dependencies
- **ISO 15118-2 Plug & Charge** — Out of the box
- **OCPI 2.2.1** — CPO-side roaming with EMSPs and other networks
- **Event Bus** — Redis Streams for real-time data to billing, CRM, dashboards, or any consumer
- **REST API** — Charger management, session queries, PKI operations
- **Smart Charging** — SetChargingProfile support for EMS integration

---

## Quick Start

```bash
# Clone
git clone https://github.com/opencpo/opencpo-core.git
cd opencpo-core

# Start dependencies (Postgres + Redis)
docker compose up -d postgres redis

# Install
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — at minimum set PG_PASSWORD

# Run migrations
alembic upgrade head

# Start
python main.py
```

Or run everything with Docker:

```bash
cp .env.example .env
# Edit .env as needed
docker compose up
```

Chargers connect to:
- **OCPP 1.6j**: `ws://your-host:9100/ocpp/{charger-id}`
- **OCPP 2.0.1**: `ws://your-host:9201/ocpp/{charger-id}`
- **REST API**: `http://your-host:8000`

---

## Architecture

```
Charger
  │
  │  WebSocket (OCPP 1.6j / 2.0.1)
  ▼
┌─────────────────────────────────────────┐
│             OCPP Handler                │
│   ocpp16/   ·   ocpp201/               │
│   BootNotification, MeterValues,        │
│   RemoteStart, SmartCharging, ...       │
└───────────────┬─────────────────────────┘
                │
                │  Profile lookup (vendor/model/firmware → behavior)
                ▼
┌─────────────────────────────────────────┐
│          Charger Profile Registry       │
│   charger_profiles/                     │
│   conservative defaults, per-vendor     │
│   quirks, compliance flags              │
└───────────────┬─────────────────────────┘
                │
        ┌───────┴───────┐
        │               │
        ▼               ▼
  ┌──────────┐    ┌───────────┐
  │  State   │    │ Event Bus │
  │  Redis   │    │  Redis    │
  │ Postgres │    │  Streams  │
  └──────────┘    └─────┬─────┘
                        │
               ┌────────┴────────┐
               ▼                 ▼
         Consumer A         Consumer B
       (billing, CRM)    (dashboard, push)
```

**Key design principle:** The OCPP handler is charger-agnostic. Vendor-specific behavior lives in `ChargerProfile` objects — the handler consults the profile instead of branching on vendor/model.

---

## Charger Profiles

Chargers vary wildly in protocol compliance and behavior quirks. This system lets you describe a charger once and handle it correctly everywhere.

### How it works

On `BootNotification`, the handler calls `resolve_profile(vendor, model, firmware)` which returns the best matching `ChargerProfile`. This profile is stored in Redis and consulted for all subsequent decisions.

### Built-in profiles

`charger_profiles/examples.py` contains profiles built from real-world field testing:

| Profile ID | Vendor | Notes |
|---|---|---|
| `maxpower-ccs2-v3` | MAXPOWER | 60kW DC CCS2, firmware V3.x — with compliance quirks |
| `hongjiali-120kw` | HONGJIALI | 120kW DC, OCPP 2.0.1 (TBD) |
| `hongjiali-30kw-portable` | HONGJIALI | 30kW portable DC (TBD) |
| `generic-ocpp16` | * | Conservative fallback for unknown chargers |

### Adding your own profiles

**Option 1: Register at startup (Python)**

```python
from charger_profiles.registry import register_profile, ChargerProfile

MY_CHARGER = ChargerProfile(
    id="my-charger-v2",
    vendor="MYVENDOR",
    model_pattern=r"XDC.*",
    firmware_pattern=r"V2\..*",
    description="My 50kW DC charger, firmware V2",

    # Override only what differs from defaults
    sends_power_measurand=True,
    power_unit="kW",
    max_power_kw=50.0,
    smart_charging_safe=False,  # Known bug: drops WS on SetChargingProfile
    quirks=["Energy in kWh, not Wh"],
)

register_profile(MY_CHARGER)
```

Call this during app startup, before chargers connect.

**Option 2: YAML file**

```yaml
# my-profiles.yaml
profiles:
  - id: my-charger-v2
    vendor: MYVENDOR
    model_pattern: "XDC.*"
    firmware_pattern: "V2\\..*"
    description: "My 50kW DC charger, firmware V2"
    sends_power_measurand: true
    power_unit: kW
    max_power_kw: 50.0
    smart_charging_safe: false
    quirks:
      - "Energy in kWh, not Wh"
```

```bash
# In .env or environment:
CHARGER_PROFILES_YAML=/etc/ocpp/my-profiles.yaml
```

**Option 3: Disable built-in examples**

If you don't want the example profiles loaded:
```bash
CHARGER_PROFILES_NO_EXAMPLES=true
```

---

## Configuration

All configuration via environment variables. Copy `.env.example` to `.env`.

| Variable | Default | Description |
|---|---|---|
| `PG_HOST` | `127.0.0.1` | PostgreSQL host |
| `PG_PORT` | `5432` | PostgreSQL port |
| `PG_NAME` | `ocpp` | Database name |
| `PG_USER` | `ocpp` | Database user |
| `PG_PASSWORD` | *(required)* | Database password |
| `REDIS_HOST` | `127.0.0.1` | Redis host |
| `REDIS_PORT` | `6380` | Redis port |
| `REDIS_PASSWORD` | | Redis password (optional) |
| `OCPP16_PORT` | `9100` | OCPP 1.6j WebSocket port |
| `OCPP201_PORT` | `9201` | OCPP 2.0.1 WebSocket port |
| `API_PORT` | `8000` | REST API port |
| `API_KEY` | | API key for REST endpoints (leave empty to disable auth) |
| `PKI_ORG_NAME` | `OCPP Core` | Organization name in CA certificates |
| `PKI_ROOT_CA_CN` | `OCPP Core Root CA` | Root CA common name |
| `PKI_USER_CA_CN` | `OCPP Core User CA` | User CA common name |
| `PKI_DATA_DIR` | `./data/pki` | PKI storage directory |
| `OCPI_BASE_URL` | `http://localhost:8000` | Public OCPI base URL |
| `OCPI_OPERATOR_NAME` | `OCPP Core CPO` | Your CPO name (shown to roaming partners) |
| `OCPI_OPERATOR_WEBSITE` | | Your website URL |
| `OCPI_PARTY_ID` | `OCP` | 3-letter OCPI party ID |
| `OCPI_COUNTRY_CODE` | `NL` | 2-letter country code |
| `CHARGER_PROFILES_YAML` | | Path to YAML file with custom charger profiles |
| `CHARGER_PROFILES_NO_EXAMPLES` | `false` | Set to `true` to disable built-in example profiles |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `LOG_FORMAT` | `json` | Log format (`json` or `text`) |

See `.env.example` for all options with documentation comments.

---

## Event Bus

Events are published to Redis Streams. Consumers subscribe and process them asynchronously.

### Implementing a consumer

```python
from events.consumer import EventConsumer
from events.types import EventType

class MyConsumer(EventConsumer):
    async def handle(self, event_type: str, payload: dict) -> None:
        if event_type == EventType.SESSION_STARTED:
            charge_point_id = payload["charge_point_id"]
            # do something...
```

Register your consumer in `main.py` alongside the existing ones.

### Event types

See `events/types.py` for all event types: session lifecycle, meter values, charger status changes, alarms, auth events, and more.

---

## Requirements

- Python 3.11+
- PostgreSQL 16+ (with TimescaleDB extension for meter value hypertables)
- Redis 7+

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

---

## Contributing

Pull requests welcome. When adding support for a new charger model, the most useful contribution is a `ChargerProfile` in `charger_profiles/examples.py` with documented quirks from compliance testing.
