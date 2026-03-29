# Deployment

## Quick Start (Docker Compose)

The fastest way to run the full stack:

```bash
cp .env.example .env
# Edit .env — at minimum set PG_PASSWORD

docker compose up -d
```

This starts ocpp-core, PostgreSQL 16 (with TimescaleDB), and Redis 7.

**Ports exposed:**
- `9100` — OCPP 1.6j WebSocket
- `9201` — OCPP 2.0.1 WebSocket
- `8000` — REST API
- `5432` (localhost only) — PostgreSQL
- `6380` (localhost only) — Redis

## Requirements

- Python 3.11+
- PostgreSQL 16+ with TimescaleDB extension
- Redis 7+

## Manual / systemd Install

### 1. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set up PostgreSQL

```sql
CREATE USER ocpp WITH PASSWORD 'your-password';
CREATE DATABASE ocpp OWNER ocpp;
\c ocpp
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

Run migrations:
```bash
psql -U ocpp -d ocpp -f db/migrations/001_initial.sql
# Run all migration files in order
```

### 3. Set up Redis

Redis 7+ with AOF persistence recommended:

```bash
redis-server --appendonly yes --port 6380
```

Or add to `redis.conf`:
```
port 6380
appendonly yes
maxmemory 512mb
maxmemory-policy allkeys-lru
```

### 4. Configure

```bash
cp .env.example .env
# Edit .env
```

### 5. Run

```bash
python main.py
```

### 6. systemd Service

```ini
# /etc/systemd/system/ocpp-core.service
[Unit]
Description=OCPP Core
After=network.target postgresql.service redis.service
Requires=postgresql.service redis.service

[Service]
Type=simple
User=ocpp
WorkingDirectory=/opt/ocpp-core
EnvironmentFile=/opt/ocpp-core/.env
ExecStart=/opt/ocpp-core/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable ocpp-core
systemctl start ocpp-core
journalctl -u ocpp-core -f
```

## TLS / Reverse Proxy

Put ocpp-core behind nginx or Caddy for TLS termination:

### nginx

```nginx
# OCPP 1.6j WebSocket
server {
    listen 443 ssl;
    server_name ocpp.example.com;

    ssl_certificate /etc/ssl/certs/ocpp.example.com.crt;
    ssl_certificate_key /etc/ssl/private/ocpp.example.com.key;

    location /ocpp/ {
        proxy_pass http://127.0.0.1:9100;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;   # Keep WebSocket alive
        proxy_send_timeout 3600s;
    }
}
```

For OCPP 2.0.1 with mTLS (security profile 3), terminate at nginx and pass the client certificate to the app:

```nginx
ssl_client_certificate /path/to/root-ca.crt;
ssl_verify_client optional;

location /ocpp/ {
    proxy_set_header X-SSL-Client-Cert $ssl_client_escaped_cert;
    ...
}
```

### Caddy

```
ocpp.example.com {
    reverse_proxy /ocpp/* localhost:9100 {
        header_up Connection {http.upgrade}
        header_up Upgrade {http.upgrade}
    }
}
```

## Health Check

```bash
curl http://localhost:8000/health
# {"status": "ok", "db": "connected", "redis": "connected"}
```

## Production Checklist

- [ ] Set `PG_PASSWORD` to a strong password
- [ ] Set `API_KEY` for REST API authentication
- [ ] Set `PKI_ROOT_CA_PASSWORD` and `PKI_SUB_CA_PASSWORD`
- [ ] Back up `data/pki/` directory
- [ ] Set `LOG_FORMAT=json` for log aggregation
- [ ] Configure Redis with `requirepass` and AOF persistence
- [ ] Set `CORS_ORIGINS` to your dashboard domain instead of `*`
- [ ] Use TLS (wss://) for all charger connections
- [ ] Monitor with `GET /health` endpoint

## Scaling

A single process handles hundreds of concurrent charger connections (asyncio, non-blocking I/O). For larger deployments:

- Multiple instances behind a load balancer require sticky sessions (WebSocket connections must stick to the same backend) — use charger ID-based routing
- Use `PG_REPLICA_HOST` for read-only queries (metrics, reporting) to offload the primary
- Increase `PG_POOL_MAX` if you see connection wait times in logs

## Logging

With `LOG_FORMAT=json`, each log line is a JSON object:

```json
{"level":"info","event":"Charger connected: CP-001 (OCPP 1.6j)","timestamp":"2024-01-15T10:30:00Z"}
```

Ship to any log aggregator (Loki, Elasticsearch, CloudWatch) by reading stdout.
