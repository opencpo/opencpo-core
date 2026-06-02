-- ============================================================================
-- OpenCPO Core — Database Schema
-- ============================================================================
-- PostgreSQL 15+ with TimescaleDB extension (optional, for meter_values).
-- All tables live in the `ocpp` schema.
--
-- Usage:
--   createdb opencpo
--   psql -d opencpo -f db/schema.sql
--
-- This file is the canonical schema definition. Keep it in sync with the
-- running database. Migrations are applied incrementally; this file represents
-- the final desired state.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS ocpp;

-- ── Helper Functions ─────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION ocpp.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- Charge Points & Connectors
-- ============================================================================

-- Charge points registered via OCPP BootNotification or manual creation.
-- The `metadata` JSONB column stores operator-defined fields like
-- display_name, address, city, latitude, longitude, tariff_kwh, access_type.
CREATE TABLE IF NOT EXISTS ocpp.charge_points (
    id               TEXT        PRIMARY KEY,
    vendor           TEXT        NOT NULL DEFAULT '',
    model            TEXT        NOT NULL DEFAULT '',
    serial_number    TEXT,
    firmware_version TEXT,
    ocpp_version     TEXT        NOT NULL DEFAULT '1.6',
    site             TEXT,
    status           TEXT        NOT NULL DEFAULT 'offline',
    simulated        BOOLEAN     NOT NULL DEFAULT FALSE,
    last_boot        TIMESTAMPTZ,
    last_heartbeat   TIMESTAMPTZ,
    registered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    config           JSONB       NOT NULL DEFAULT '{}',
    metadata         JSONB       NOT NULL DEFAULT '{}'
);

-- Physical connectors (ports) on a charge point.
CREATE TABLE IF NOT EXISTS ocpp.connectors (
    charge_point TEXT     NOT NULL REFERENCES ocpp.charge_points(id) ON DELETE CASCADE,
    connector_id SMALLINT NOT NULL,
    status       TEXT     NOT NULL DEFAULT 'Available',
    error_code   TEXT     NOT NULL DEFAULT 'NoError',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (charge_point, connector_id)
);


-- ============================================================================
-- Sessions & Metering
-- ============================================================================

-- Charging sessions created by StartTransaction, closed by StopTransaction.
CREATE TABLE IF NOT EXISTS ocpp.sessions (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    charge_point     TEXT        NOT NULL REFERENCES ocpp.charge_points(id),
    connector_id     SMALLINT    NOT NULL,
    transaction_id   INTEGER,
    status           TEXT        NOT NULL DEFAULT 'active',
    auth_method      TEXT,
    auth_id          TEXT,
    start_time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stop_time        TIMESTAMPTZ,
    stop_reason      TEXT,
    energy_kwh       REAL        NOT NULL DEFAULT 0,
    peak_power_kw    REAL        NOT NULL DEFAULT 0,
    start_soc        SMALLINT,
    end_soc          SMALLINT,
    simulated        BOOLEAN     NOT NULL DEFAULT FALSE,
    metadata         JSONB       NOT NULL DEFAULT '{}',
    billing_type     TEXT        DEFAULT 'prepaid',
    meter_start      REAL,
    meter_stop       REAL,
    billing_group_id UUID
);

CREATE INDEX IF NOT EXISTS idx_sessions_charge_point ON ocpp.sessions (charge_point, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_auth ON ocpp.sessions (auth_id, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON ocpp.sessions (status) WHERE status = 'active';

-- TimescaleDB hypertable for high-frequency meter values.
-- If TimescaleDB is not installed, this is a regular table.
CREATE TABLE IF NOT EXISTS ocpp.meter_values (
    time          TIMESTAMPTZ NOT NULL,
    charge_point  TEXT        NOT NULL,
    connector_id  SMALLINT    NOT NULL,
    session_id    UUID,
    energy_kwh    REAL,
    power_kw      REAL,
    soc_pct       SMALLINT,
    voltage_v     REAL[],
    current_a     REAL[],
    temperature_c REAL
);
-- SELECT create_hypertable('ocpp.meter_values', 'time', if_not_exists => TRUE);

-- Charge Detail Records — one per completed session, used for billing.
CREATE TABLE IF NOT EXISTS ocpp.cdrs (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id   UUID        NOT NULL REFERENCES ocpp.sessions(id),
    charge_point TEXT        NOT NULL,
    connector_id SMALLINT    NOT NULL,
    auth_method  TEXT,
    auth_id      TEXT,
    start_time   TIMESTAMPTZ NOT NULL,
    stop_time    TIMESTAMPTZ NOT NULL,
    energy_kwh   REAL        NOT NULL,
    duration_min REAL        NOT NULL,
    tariff_id    TEXT,
    cost         JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata     JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_cdrs_session ON ocpp.cdrs (session_id);
CREATE INDEX IF NOT EXISTS idx_cdrs_time ON ocpp.cdrs (start_time DESC);


-- ============================================================================
-- Public Sessions (Charge App / Payment Flow)
-- ============================================================================

-- Sessions initiated by drivers via the charge app (QR scan → payment → charge).
-- Linked to OCPP sessions via ocpp_transaction_id after RemoteStartTransaction.
CREATE TABLE IF NOT EXISTS ocpp.public_sessions (
    id                  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    cp_id               TEXT,
    connector_id        INTEGER,
    driver_account_id   UUID,
    driver_phone        TEXT,
    driver_email        TEXT,
    pricing_tier        TEXT          DEFAULT 'public',
    rate_kwh            NUMERIC(10,4),
    kwh_delivered       NUMERIC(10,2) DEFAULT 0,
    payment_status      TEXT,
    started_at          TIMESTAMPTZ,
    stopped_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ   DEFAULT NOW(),
    ocpp_transaction_id INTEGER,
    external_payment_id TEXT,
    spot_price_at_start NUMERIC(10,4),
    cost_basis_at_start NUMERIC(10,4),
    rate_kwh_at_start   NUMERIC(10,4)
);


-- ============================================================================
-- Tokens & Groups (RFID / Authorization)
-- ============================================================================

-- Token groups for fleet billing (e.g., "Company X fleet").
CREATE TABLE IF NOT EXISTS ocpp.token_groups (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT        NOT NULL,
    billing_email     TEXT,
    billing_address   TEXT,
    billing_reference TEXT,
    contact_name      TEXT,
    contact_phone     TEXT,
    notes             TEXT,
    pricing_tier      TEXT        DEFAULT 'fleet',
    billing_method    TEXT        DEFAULT 'prepaid',
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- RFID tokens / authorization identifiers with full lifecycle tracking.
CREATE TABLE IF NOT EXISTS ocpp.tokens (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    uid            TEXT        NOT NULL UNIQUE,
    type           TEXT        NOT NULL DEFAULT 'rfid',
    status         TEXT        NOT NULL DEFAULT 'active',
    group_id       UUID,
    description    TEXT,
    driver_name    TEXT,
    driver_email   TEXT,
    driver_phone   TEXT,
    label          TEXT,
    card_number    TEXT,
    valid_from     TIMESTAMPTZ,
    valid_until    TIMESTAMPTZ,
    activated_at   TIMESTAMPTZ,
    blocked_at     TIMESTAMPTZ,
    blocked_reason TEXT,
    replaces_id    UUID,
    replaced_by_id UUID,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Token lifecycle audit trail.
CREATE TABLE IF NOT EXISTS ocpp.token_events (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    token_id   UUID        NOT NULL,
    event      TEXT        NOT NULL,
    details    TEXT,
    actor      TEXT        DEFAULT 'system',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_token_events_token ON ocpp.token_events (token_id);

-- OCPP 1.6 Local Authorization Cache (for offline authorization).
CREATE TABLE IF NOT EXISTS ocpp.authorization_cache (
    token        TEXT        PRIMARY KEY,
    type         TEXT        NOT NULL DEFAULT 'rfid',
    status       TEXT        NOT NULL DEFAULT 'Accepted',
    display_name TEXT,
    group_id     TEXT,
    valid_from   TIMESTAMPTZ,
    valid_until  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata     JSONB       NOT NULL DEFAULT '{}'
);


-- ============================================================================
-- Tariffs & Pricing
-- ============================================================================

-- Named tariff plans with rate components.
CREATE TABLE IF NOT EXISTS ocpp.tariffs (
    id          TEXT        PRIMARY KEY,
    name        TEXT        NOT NULL,
    currency    TEXT        NOT NULL DEFAULT 'EUR',
    energy_rate REAL        NOT NULL DEFAULT 0,
    time_rate   REAL        NOT NULL DEFAULT 0,
    idle_rate   REAL        NOT NULL DEFAULT 0,
    flat_fee    REAL        NOT NULL DEFAULT 0,
    valid_from  TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata    JSONB       NOT NULL DEFAULT '{}'
);

-- Dynamic pricing configuration (cost components, tax rate, spot source).
CREATE TABLE IF NOT EXISTS ocpp.pricing_config (
    key         TEXT          PRIMARY KEY,
    value       NUMERIC(10,4) NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ   DEFAULT NOW(),
    updated_by  TEXT
);

-- Pricing tiers: named margin levels applied on top of cost basis.
CREATE TABLE IF NOT EXISTS ocpp.pricing_tiers (
    id          TEXT          PRIMARY KEY,
    name        TEXT          NOT NULL,
    margin_kwh  NUMERIC(10,4) NOT NULL DEFAULT 0,
    description TEXT,
    updated_at  TIMESTAMPTZ   DEFAULT NOW()
);


-- ============================================================================
-- Driver Accounts
-- ============================================================================

-- Driver accounts for the charge app (email/password auth).
CREATE TABLE IF NOT EXISTS ocpp.driver_accounts (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT        NOT NULL UNIQUE,
    phone         TEXT,
    password_hash TEXT        NOT NULL,
    name          TEXT,
    language      TEXT        DEFAULT 'nl',
    pricing_tier  TEXT        DEFAULT 'public',
    group_id      UUID,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Driver's saved/favorite chargers.
CREATE TABLE IF NOT EXISTS ocpp.driver_favorites (
    driver_account_id UUID REFERENCES ocpp.driver_accounts(id) ON DELETE CASCADE,
    charge_point_id   TEXT NOT NULL,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (driver_account_id, charge_point_id)
);


-- ============================================================================
-- Users (CPO Platform / mTLS)
-- ============================================================================

-- Platform users (operators, installers, clients) authenticated via client certificates
-- or email/password for the admin dashboard.
CREATE TABLE IF NOT EXISTS ocpp.users (
    id                     SERIAL      PRIMARY KEY,
    email                  TEXT        NOT NULL UNIQUE,
    name                   TEXT        NOT NULL,
    phone                  TEXT,
    role                   TEXT        NOT NULL DEFAULT 'admin',
    password_hash          TEXT,                -- bcrypt hash for email/password auth (nullable for cert-only users)
    cert_serial            TEXT,
    cert_issued_at         TIMESTAMPTZ,
    cert_expires_at        TIMESTAMPTZ,
    cert_revoked_at        TIMESTAMPTZ,
    cert_status            TEXT        DEFAULT 'none',
    download_token         TEXT,
    download_token_expires TIMESTAMPTZ,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    updated_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_cert_serial ON ocpp.users (cert_serial);
CREATE INDEX IF NOT EXISTS idx_users_download_token ON ocpp.users (download_token);

CREATE TRIGGER users_updated_at
    BEFORE UPDATE ON ocpp.users
    FOR EACH ROW EXECUTE FUNCTION ocpp.set_updated_at();

-- Certificate setup tokens — one-time links for driver cert installation.
CREATE TABLE IF NOT EXISTS ocpp.cert_setup_tokens (
    token       TEXT        PRIMARY KEY,
    email       TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    cert_serial TEXT
);

CREATE INDEX IF NOT EXISTS idx_cert_setup_email ON ocpp.cert_setup_tokens (email);
CREATE INDEX IF NOT EXISTS idx_cert_setup_expires ON ocpp.cert_setup_tokens (expires_at) WHERE used_at IS NULL;


-- ============================================================================
-- PKI (Public Key Infrastructure)
-- ============================================================================

-- All issued certificates (SECC, contract, user).
CREATE TABLE IF NOT EXISTS ocpp.pki_certificates (
    serial            TEXT        PRIMARY KEY,
    type              TEXT        NOT NULL,
    subject           TEXT        NOT NULL,
    issuer            TEXT        NOT NULL,
    charge_point      TEXT,
    not_before        TIMESTAMPTZ NOT NULL,
    not_after         TIMESTAMPTZ NOT NULL,
    fingerprint       TEXT        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'active',
    pem               TEXT        NOT NULL,
    issued_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at        TIMESTAMPTZ,
    revocation_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_pki_certs_cp ON ocpp.pki_certificates (charge_point);
CREATE INDEX IF NOT EXISTS idx_pki_certs_expiry ON ocpp.pki_certificates (not_after) WHERE status = 'active';

-- Certificate revocations (for CRL/OCSP).
CREATE TABLE IF NOT EXISTS ocpp.pki_revocations (
    serial     TEXT        PRIMARY KEY REFERENCES ocpp.pki_certificates(serial),
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason     TEXT        NOT NULL DEFAULT 'unspecified'
);

-- CSR (Certificate Signing Request) log.
CREATE TABLE IF NOT EXISTS ocpp.pki_csr_log (
    id           SERIAL      PRIMARY KEY,
    charge_point TEXT        NOT NULL,
    csr_pem      TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending',
    cert_serial  TEXT,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);


-- ============================================================================
-- Billing & Invoicing
-- ============================================================================

CREATE TABLE IF NOT EXISTS ocpp.invoices (
    id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id     UUID,
    period_start DATE,
    period_end   DATE,
    total_kwh    NUMERIC(10,2),
    total_amount NUMERIC(10,2),
    status       TEXT          DEFAULT 'draft',
    created_at   TIMESTAMPTZ   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ocpp.invoice_lines (
    id         UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID          REFERENCES ocpp.invoices(id),
    session_id UUID,
    kwh        NUMERIC(10,2),
    rate       NUMERIC(10,4),
    amount     NUMERIC(10,2)
);

-- Billing audit events (lifecycle tracking for billing entities).
CREATE TABLE IF NOT EXISTS ocpp.billing_events (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entity_type TEXT        NOT NULL,
    entity_id   TEXT        NOT NULL,
    action      TEXT        NOT NULL,
    actor       TEXT        NOT NULL,
    details     JSONB       DEFAULT '{}',
    group_id    UUID
);

CREATE INDEX IF NOT EXISTS idx_billing_events_entity ON ocpp.billing_events (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_billing_events_group ON ocpp.billing_events (group_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_billing_events_ts ON ocpp.billing_events (ts DESC);


-- ============================================================================
-- OCPI (Open Charge Point Interface — Roaming)
-- ============================================================================

CREATE TABLE IF NOT EXISTS ocpp.ocpi_locations (
    id            TEXT        PRIMARY KEY,
    name          TEXT        NOT NULL,
    address       TEXT        NOT NULL,
    city          TEXT        NOT NULL,
    country       TEXT        NOT NULL DEFAULT 'NLD',
    coordinates   POINT,
    charge_points TEXT[],
    published     BOOLEAN     NOT NULL DEFAULT FALSE,
    last_updated  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ocpp.ocpi_partners (
    id           SERIAL  PRIMARY KEY,
    party_id     TEXT    NOT NULL,
    country_code TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    token_a      TEXT,
    token_b      TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    last_sync    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (party_id, country_code, role)
);

CREATE TABLE IF NOT EXISTS ocpp.ocpi_tokens (
    uid          TEXT        PRIMARY KEY,
    type         TEXT        NOT NULL DEFAULT 'RFID',
    auth_id      TEXT        NOT NULL,
    party_id     TEXT        NOT NULL,
    country_code TEXT        NOT NULL,
    issuer       TEXT,
    valid        BOOLEAN     NOT NULL DEFAULT TRUE,
    whitelist    TEXT        NOT NULL DEFAULT 'ALWAYS',
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================================
-- Fleet Management
-- ============================================================================

CREATE TABLE IF NOT EXISTS ocpp.fleet_vehicles (
    id              SERIAL      PRIMARY KEY,
    license_plate   TEXT        NOT NULL UNIQUE,
    make            TEXT,
    model           TEXT,
    connector_type  TEXT        DEFAULT 'CCS2',
    status          TEXT        DEFAULT 'active',
    pnc_cert_serial TEXT,
    pnc_cert_status TEXT,
    last_session_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================================
-- Infrastructure & Operations
-- ============================================================================

-- Feature flags for runtime feature toggling.
CREATE TABLE IF NOT EXISTS ocpp.feature_flags (
    key         TEXT    PRIMARY KEY,
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    description TEXT,
    label       TEXT,
    category    TEXT,
    updated_at  TIMESTAMPTZ
);

-- Known firmware versions (for firmware management UI).
CREATE TABLE IF NOT EXISTS ocpp.firmware_versions (
    id          SERIAL      PRIMARY KEY,
    vendor      TEXT        NOT NULL,
    model       TEXT        NOT NULL,
    version     TEXT        NOT NULL,
    url         TEXT,
    checksum    TEXT,
    notes       TEXT,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (vendor, model, version)
);

-- OCPP message log (for debugging and audit).
CREATE TABLE IF NOT EXISTS ocpp.ocpp_messages (
    time         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    charge_point TEXT        NOT NULL,
    direction    TEXT        NOT NULL,
    ocpp_version TEXT        NOT NULL,
    action       TEXT        NOT NULL,
    message_id   TEXT,
    payload      JSONB,
    response     JSONB,
    latency_ms   REAL
);

-- Security events (failed auth, tamper detection, etc.).
CREATE TABLE IF NOT EXISTS ocpp.security_events (
    time         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    charge_point TEXT,
    event_type   TEXT        NOT NULL,
    severity     TEXT        NOT NULL DEFAULT 'info',
    details      JSONB       NOT NULL DEFAULT '{}',
    resolved     BOOLEAN     NOT NULL DEFAULT FALSE
);

-- Web Push notification subscriptions.
CREATE TABLE IF NOT EXISTS ocpp.push_subscriptions (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint     TEXT        NOT NULL,
    p256dh       TEXT,
    auth         TEXT,
    session_id   UUID,
    subscription JSONB,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS push_subs_session_idx ON ocpp.push_subscriptions (session_id);

-- Webhook subscriptions for event delivery.
CREATE TABLE IF NOT EXISTS ocpp.webhook_subscriptions (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    url        TEXT        NOT NULL,
    events     TEXT[]      NOT NULL DEFAULT '{}',
    secret     TEXT        DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Update history (audit log of updates/backups/restores)
CREATE TABLE IF NOT EXISTS ocpp.update_history (
    id              SERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,  -- 'update', 'backup', 'restore', 'migration'
    from_version    TEXT,
    to_version      TEXT,
    status          TEXT NOT NULL DEFAULT 'completed',  -- 'completed', 'failed', 'rolled_back'
    details         JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Backup records
CREATE TABLE IF NOT EXISTS ocpp.backup_records (
    id              SERIAL PRIMARY KEY,
    filename        TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    checksum        TEXT,
    version         TEXT,
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active', 'deleted', 'restored'
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
