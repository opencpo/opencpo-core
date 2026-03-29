# Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and edit.

```bash
cp .env.example .env
```

## Database (PostgreSQL)

| Variable | Default | Description |
|---|---|---|
| `PG_HOST` | `127.0.0.1` | PostgreSQL host |
| `PG_PORT` | `5432` | PostgreSQL port |
| `PG_NAME` | `ocpp` | Database name |
| `PG_USER` | `ocpp` | Database user |
| `PG_PASSWORD` | _(empty)_ | **Required in production** |
| `PG_POOL_MIN` | `5` | Minimum connection pool size |
| `PG_POOL_MAX` | `20` | Maximum connection pool size |
| `PG_REPLICA_HOST` | _(empty)_ | Optional read replica host |
| `PG_REPLICA_PORT` | `5432` | Read replica port |

Requires **PostgreSQL 16+** with the **TimescaleDB** extension for time-series meter data.

## Redis

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `127.0.0.1` | Redis host |
| `REDIS_PORT` | `6380` | Redis port (non-standard default to avoid conflicts) |
| `REDIS_DB` | `0` | Redis database index |
| `REDIS_PASSWORD` | _(empty)_ | Redis password (leave empty if no auth) |

Redis is used for live charger state, session state, event streams, and queued commands.

## OCPP WebSocket Servers

| Variable | Default | Description |
|---|---|---|
| `OCPP16_HOST` | `0.0.0.0` | Bind address for OCPP 1.6j server |
| `OCPP16_PORT` | `9100` | Port for OCPP 1.6j server |
| `OCPP201_HOST` | `0.0.0.0` | Bind address for OCPP 2.0.1 server |
| `OCPP201_PORT` | `9201` | Port for OCPP 2.0.1 server |
| `OCPP_HEARTBEAT_INTERVAL` | `60` | Heartbeat interval sent to chargers (seconds) |
| `OCPP_METER_BATCH_INTERVAL` | `2.0` | Seconds between MeterValues DB flushes |
| `OCPP_METER_BATCH_SIZE` | `100` | Max MeterValues records per flush |

Chargers connect to:
- OCPP 1.6j: `ws://your-host:9100/ocpp/{charger-id}`
- OCPP 2.0.1: `ws://your-host:9201/ocpp/{charger-id}`

## REST API

| Variable | Default | Description |
|---|---|---|
| `API_HOST` | `0.0.0.0` | API bind address |
| `API_PORT` | `8000` | API port |
| `API_KEY` | _(empty)_ | Static API key for protected endpoints. Empty = no auth |
| `CORS_ORIGINS` | `*` | Comma-separated CORS origins. `*` = allow all |

## PKI (Built-in Certificate Authority)

| Variable | Default | Description |
|---|---|---|
| `PKI_DATA_DIR` | `./data/pki` | Directory for CA keys and issued certificates |
| `PKI_ORG_NAME` | `OCPP Core` | Organization name in CA certificates |
| `PKI_ROOT_CA_CN` | `OCPP Core Root CA` | Root CA common name |
| `PKI_USER_CA_CN` | `OCPP Core User CA` | User Sub-CA common name |
| `PKI_ROOT_CA_PASSWORD` | _(empty)_ | Password to encrypt Root CA private key |
| `PKI_SUB_CA_PASSWORD` | _(empty)_ | Password to encrypt Sub-CA private keys |
| `PKI_CERT_VALIDITY_DAYS` | `365` | Validity period for issued leaf certificates |
| `PKI_OCSP_PORT` | `8099` | OCSP responder port |

**Production note:** Set `PKI_ROOT_CA_PASSWORD` and `PKI_SUB_CA_PASSWORD` to encrypt private keys at rest. Back up the `PKI_DATA_DIR` directory — losing the Root CA key means you cannot issue new certificates.

## OCPI

| Variable | Default | Description |
|---|---|---|
| `OCPI_BASE_URL` | `http://localhost:8000` | Public base URL for OCPI endpoints |
| `OCPI_OPERATOR_NAME` | `OCPP Core CPO` | CPO name shown to roaming partners |
| `OCPI_OPERATOR_WEBSITE` | _(empty)_ | CPO website URL |
| `OCPI_PARTY_ID` | `OCP` | 3-letter party identifier (must be unique per network) |
| `OCPI_COUNTRY_CODE` | `NL` | 2-letter ISO country code |

## Charger Profiles

| Variable | Default | Description |
|---|---|---|
| `CHARGER_PROFILES_YAML` | _(empty)_ | Path to custom profiles YAML file |
| `CHARGER_PROFILES_NO_EXAMPLES` | `false` | Set `true` to disable built-in example profiles |

See [charger-profiles.md](charger-profiles.md) for the YAML format.

## Event Bus

| Variable | Default | Description |
|---|---|---|
| `EVENTS_STREAM_PREFIX` | `ocpp` | Redis key prefix for event streams |
| `EVENTS_MAX_STREAM_LENGTH` | `100000` | Maximum events retained per stream |
| `EVENTS_CONSUMER_BLOCK_MS` | `5000` | How long consumers wait for new events (ms) |

## Logging

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | `json` | `json` for structured logs (log aggregators), `text` for humans |

## Minimal Production Config

```env
# Database
PG_HOST=your-db-host
PG_NAME=ocpp
PG_USER=ocpp
PG_PASSWORD=strong-password-here

# Redis
REDIS_HOST=your-redis-host
REDIS_PASSWORD=redis-password

# API
API_KEY=your-api-key
CORS_ORIGINS=https://your-dashboard.example.com

# PKI
PKI_ROOT_CA_PASSWORD=root-ca-key-password
PKI_SUB_CA_PASSWORD=sub-ca-key-password
PKI_ORG_NAME=Your Organization

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json
```
