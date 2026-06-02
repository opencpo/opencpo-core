"""v0.3.0 Baseline — Create all base tables

Creates the ocpp schema and all base tables for OpenCPO Core.

This migration reads from db/schema.sql and applies all CREATE TABLE IF NOT EXISTS
statements. It is the foundational migration that sets up the entire database schema
in a single revision.

Tables created:
- ocpp.charge_points, ocpp.connectors
- ocpp.sessions, ocpp.meter_values, ocpp.cdrs
- ocpp.public_sessions
- ocpp.token_groups, ocpp.tokens, ocpp.token_events
- ocpp.authorization_cache
- ocpp.tariffs, ocpp.pricing_config, ocpp.pricing_tiers
- ocpp.driver_accounts, ocpp.driver_favorites
- ocpp.users, ocpp.cert_setup_tokens
- ocpp.pki_certificates, ocpp.pki_revocations, ocpp.pki_csr_log
- ocpp.invoices, ocpp.invoice_lines, ocpp.billing_events
- ocpp.ocpi_locations, ocpp.ocpi_partners, ocpp.ocpi_tokens
- ocpp.fleet_vehicles
- ocpp.feature_flags, ocpp.firmware_versions
- ocpp.ocpp_messages, ocpp.security_events
- ocpp.push_subscriptions, ocpp.webhook_subscriptions
- ocpp.update_history, ocpp.backup_records

Revision ID: v0_3_0_baseline
Revises:
Create Date: 2026-06-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "v0_3_0_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ============================================================================
    # Schema
    # ============================================================================
    op.execute("CREATE SCHEMA IF NOT EXISTS ocpp")

    # ============================================================================
    # Helper Functions
    # ============================================================================
    op.execute("""
        CREATE OR REPLACE FUNCTION ocpp.set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    # ============================================================================
    # Charge Points & Connectors
    # ============================================================================
    op.create_table(
        "charge_points",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("vendor", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.Text(), nullable=False, server_default=""),
        sa.Column("serial_number", sa.Text(), nullable=True),
        sa.Column("firmware_version", sa.Text(), nullable=True),
        sa.Column("ocpp_version", sa.Text(), nullable=False, server_default="1.6"),
        sa.Column("site", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="offline"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("last_boot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    op.create_table(
        "connectors",
        sa.Column("charge_point", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="Available"),
        sa.Column("error_code", sa.Text(), nullable=False, server_default="NoError"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["charge_point"], ["ocpp.charge_points.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("charge_point", "connector_id"),
        schema="ocpp",
    )

    # ============================================================================
    # Sessions & Metering
    # ============================================================================
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("charge_point", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.SmallInteger(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("auth_method", sa.Text(), nullable=True),
        sa.Column("auth_id", sa.Text(), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("stop_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("energy_kwh", sa.Real(), nullable=False, server_default=sa.text("0")),
        sa.Column("peak_power_kw", sa.Real(), nullable=False, server_default=sa.text("0")),
        sa.Column("start_soc", sa.SmallInteger(), nullable=True),
        sa.Column("end_soc", sa.SmallInteger(), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("billing_type", sa.Text(), server_default="prepaid"),
        sa.Column("meter_start", sa.Real(), nullable=True),
        sa.Column("meter_stop", sa.Real(), nullable=True),
        sa.Column("billing_group_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["charge_point"], ["ocpp.charge_points.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )
    op.create_index("idx_sessions_charge_point", "sessions", ["charge_point", sa.text("start_time DESC")], schema="ocpp")
    op.create_index("idx_sessions_auth", "sessions", ["auth_id", sa.text("start_time DESC")], schema="ocpp")
    op.create_index("idx_sessions_status", "sessions", ["status"], schema="ocpp", postgresql_where=sa.text("status = 'active'"))

    op.create_table(
        "meter_values",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("charge_point", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.SmallInteger(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("energy_kwh", sa.Real(), nullable=True),
        sa.Column("power_kw", sa.Real(), nullable=True),
        sa.Column("soc_pct", sa.SmallInteger(), nullable=True),
        sa.Column("voltage_v", postgresql.ARRAY(sa.Real()), nullable=True),
        sa.Column("current_a", postgresql.ARRAY(sa.Real()), nullable=True),
        sa.Column("temperature_c", sa.Real(), nullable=True),
        schema="ocpp",
    )

    op.create_table(
        "cdrs",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("charge_point", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.SmallInteger(), nullable=False),
        sa.Column("auth_method", sa.Text(), nullable=True),
        sa.Column("auth_id", sa.Text(), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stop_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("energy_kwh", sa.Real(), nullable=False),
        sa.Column("duration_min", sa.Real(), nullable=False),
        sa.Column("tariff_id", sa.Text(), nullable=True),
        sa.Column("cost", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(["session_id"], ["ocpp.sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )
    op.create_index("idx_cdrs_session", "cdrs", ["session_id"], schema="ocpp")
    op.create_index("idx_cdrs_time", "cdrs", [sa.text("start_time DESC")], schema="ocpp")

    # ============================================================================
    # Public Sessions
    # ============================================================================
    op.create_table(
        "public_sessions",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cp_id", sa.Text(), nullable=True),
        sa.Column("connector_id", sa.Integer(), nullable=True),
        sa.Column("driver_account_id", sa.Uuid(), nullable=True),
        sa.Column("driver_phone", sa.Text(), nullable=True),
        sa.Column("driver_email", sa.Text(), nullable=True),
        sa.Column("pricing_tier", sa.Text(), server_default="public"),
        sa.Column("rate_kwh", sa.Numeric(10, 4), nullable=True),
        sa.Column("kwh_delivered", sa.Numeric(10, 2), server_default=sa.text("0")),
        sa.Column("payment_status", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("ocpp_transaction_id", sa.Integer(), nullable=True),
        sa.Column("external_payment_id", sa.Text(), nullable=True),
        sa.Column("spot_price_at_start", sa.Numeric(10, 4), nullable=True),
        sa.Column("cost_basis_at_start", sa.Numeric(10, 4), nullable=True),
        sa.Column("rate_kwh_at_start", sa.Numeric(10, 4), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    # ============================================================================
    # Tokens & Groups
    # ============================================================================
    op.create_table(
        "token_groups",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("billing_email", sa.Text(), nullable=True),
        sa.Column("billing_address", sa.Text(), nullable=True),
        sa.Column("billing_reference", sa.Text(), nullable=True),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("contact_phone", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("pricing_tier", sa.Text(), server_default="fleet"),
        sa.Column("billing_method", sa.Text(), server_default="prepaid"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    op.create_table(
        "tokens",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("uid", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False, server_default="rfid"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("driver_name", sa.Text(), nullable=True),
        sa.Column("driver_email", sa.Text(), nullable=True),
        sa.Column("driver_phone", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("replaces_id", sa.Uuid(), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
        schema="ocpp",
    )

    op.create_table(
        "token_events",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )
    op.create_index("idx_token_events_token", "token_events", ["token_id"], schema="ocpp")

    op.create_table(
        "authorization_cache",
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False, server_default="rfid"),
        sa.Column("status", sa.Text(), nullable=False, server_default="Accepted"),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("group_id", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("token"),
        schema="ocpp",
    )

    # ============================================================================
    # Tariffs & Pricing
    # ============================================================================
    op.create_table(
        "tariffs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="EUR"),
        sa.Column("energy_rate", sa.Real(), nullable=False, server_default=sa.text("0")),
        sa.Column("time_rate", sa.Real(), nullable=False, server_default=sa.text("0")),
        sa.Column("idle_rate", sa.Real(), nullable=False, server_default=sa.text("0")),
        sa.Column("flat_fee", sa.Real(), nullable=False, server_default=sa.text("0")),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    op.create_table(
        "pricing_config",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(10, 4), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
        schema="ocpp",
    )

    op.create_table(
        "pricing_tiers",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("margin_kwh", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    # ============================================================================
    # Driver Accounts
    # ============================================================================
    op.create_table(
        "driver_accounts",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), server_default="nl"),
        sa.Column("pricing_tier", sa.Text(), server_default="public"),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        schema="ocpp",
    )

    op.create_table(
        "driver_favorites",
        sa.Column("driver_account_id", sa.Uuid(), nullable=False),
        sa.Column("charge_point_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["driver_account_id"], ["ocpp.driver_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("driver_account_id", "charge_point_id"),
        schema="ocpp",
    )

    # ============================================================================
    # Users (Platform / mTLS)
    # ============================================================================
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="admin"),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("cert_serial", sa.Text(), nullable=True),
        sa.Column("cert_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cert_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cert_revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cert_status", sa.Text(), server_default="none"),
        sa.Column("download_token", sa.Text(), nullable=True),
        sa.Column("download_token_expires", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        schema="ocpp",
    )
    op.create_index("idx_users_cert_serial", "users", ["cert_serial"], schema="ocpp")
    op.create_index("idx_users_download_token", "users", ["download_token"], schema="ocpp")
    op.execute("""
        CREATE TRIGGER users_updated_at
            BEFORE UPDATE ON ocpp.users
            FOR EACH ROW EXECUTE FUNCTION ocpp.set_updated_at()
    """)

    op.create_table(
        "cert_setup_tokens",
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cert_serial", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("token"),
        schema="ocpp",
    )
    op.create_index("idx_cert_setup_email", "cert_setup_tokens", ["email"], schema="ocpp")
    op.create_index(
        "idx_cert_setup_expires", "cert_setup_tokens", ["expires_at"],
        schema="ocpp", postgresql_where=sa.text("used_at IS NULL"),
    )

    # ============================================================================
    # PKI
    # ============================================================================
    op.create_table(
        "pki_certificates",
        sa.Column("serial", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("charge_point", sa.Text(), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("pem", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("serial"),
        schema="ocpp",
    )
    op.create_index("idx_pki_certs_cp", "pki_certificates", ["charge_point"], schema="ocpp")
    op.create_index(
        "idx_pki_certs_expiry", "pki_certificates", ["not_after"],
        schema="ocpp", postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "pki_revocations",
        sa.Column("serial", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("reason", sa.Text(), nullable=False, server_default="unspecified"),
        sa.ForeignKeyConstraint(["serial"], ["ocpp.pki_certificates.serial"]),
        sa.PrimaryKeyConstraint("serial"),
        schema="ocpp",
    )

    op.create_table(
        "pki_csr_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("charge_point", sa.Text(), nullable=False),
        sa.Column("csr_pem", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("cert_serial", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    # ============================================================================
    # Billing & Invoicing
    # ============================================================================
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("total_kwh", sa.Numeric(10, 2), nullable=True),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("status", sa.Text(), server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("kwh", sa.Numeric(10, 2), nullable=True),
        sa.Column("rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=True),
        sa.ForeignKeyConstraint(["invoice_id"], ["ocpp.invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    op.create_table(
        "billing_events",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(), server_default="{}"),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )
    op.create_index("idx_billing_events_entity", "billing_events", ["entity_type", "entity_id"], schema="ocpp")
    op.create_index("idx_billing_events_group", "billing_events", ["group_id", sa.text("ts DESC")], schema="ocpp")
    op.create_index("idx_billing_events_ts", "billing_events", [sa.text("ts DESC")], schema="ocpp")

    # ============================================================================
    # OCPI
    # ============================================================================
    op.create_table(
        "ocpi_locations",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False, server_default="NLD"),
        sa.Column("coordinates", postgresql.POINT(), nullable=True),
        sa.Column("charge_points", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    op.create_table(
        "ocpi_partners",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("party_id", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("token_a", sa.Text(), nullable=True),
        sa.Column("token_b", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("last_sync", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("party_id", "country_code", "role"),
        schema="ocpp",
    )

    op.create_table(
        "ocpi_tokens",
        sa.Column("uid", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False, server_default="RFID"),
        sa.Column("auth_id", sa.Text(), nullable=False),
        sa.Column("party_id", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=True),
        sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("whitelist", sa.Text(), nullable=False, server_default="ALWAYS"),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("uid"),
        schema="ocpp",
    )

    # ============================================================================
    # Fleet Management
    # ============================================================================
    op.create_table(
        "fleet_vehicles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("license_plate", sa.Text(), nullable=False),
        sa.Column("make", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("connector_type", sa.Text(), server_default="CCS2"),
        sa.Column("status", sa.Text(), server_default="active"),
        sa.Column("pnc_cert_serial", sa.Text(), nullable=True),
        sa.Column("pnc_cert_status", sa.Text(), nullable=True),
        sa.Column("last_session_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("license_plate"),
        schema="ocpp",
    )

    # ============================================================================
    # Infrastructure & Operations
    # ============================================================================
    op.create_table(
        "feature_flags",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key"),
        schema="ocpp",
    )

    op.create_table(
        "firmware_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("checksum", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vendor", "model", "version"),
        schema="ocpp",
    )

    op.create_table(
        "ocpp_messages",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("charge_point", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("ocpp_version", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        sa.Column("latency_ms", sa.Real(), nullable=True),
        schema="ocpp",
    )

    op.create_table(
        "security_events",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("charge_point", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False, server_default="info"),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        schema="ocpp",
    )

    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=True),
        sa.Column("auth", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("subscription", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )
    op.create_index("push_subs_session_idx", "push_subscriptions", ["session_id"], schema="ocpp")

    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("events", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("secret", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    # ============================================================================
    # Update History & Backup Records
    # ============================================================================
    op.create_table(
        "update_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("from_version", sa.Text(), nullable=True),
        sa.Column("to_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="completed"),
        sa.Column("details", postgresql.JSONB(), server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )

    op.create_table(
        "backup_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("checksum", sa.Text(), nullable=True),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        schema="ocpp",
    )


def downgrade() -> None:
    """Remove all tables (reverse of upgrade)."""
    tables = [
        "backup_records",
        "update_history",
        "webhook_subscriptions",
        "push_subscriptions",
        "security_events",
        "ocpp_messages",
        "firmware_versions",
        "feature_flags",
        "fleet_vehicles",
        "ocpi_tokens",
        "ocpi_partners",
        "ocpi_locations",
        "billing_events",
        "invoice_lines",
        "invoices",
        "pki_csr_log",
        "pki_revocations",
        "pki_certificates",
        "cert_setup_tokens",
        "users",
        "driver_favorites",
        "driver_accounts",
        "pricing_tiers",
        "pricing_config",
        "tariffs",
        "authorization_cache",
        "token_events",
        "tokens",
        "token_groups",
        "public_sessions",
        "cdrs",
        "meter_values",
        "sessions",
        "connectors",
        "charge_points",
    ]
    for table in tables:
        op.drop_table(table, schema="ocpp", if_exists=True)

    op.execute("DROP FUNCTION IF EXISTS ocpp.set_updated_at()")
