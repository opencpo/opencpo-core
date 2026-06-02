"""Alembic migrations environment for OpenCPO Core.

Uses synchronous psycopg2 (via SQLAlchemy) for schema migrations,
since Alembic's autogenerate and schema operations work best synchronously.
The app itself uses asyncpg for runtime queries.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

# Alembic Config object
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No SQLAlchemy models — pure SQL migrations
target_metadata = None


def get_url() -> str:
    """Build the database URL from environment variables (matching config.py)."""
    host = os.environ.get("PG_HOST", "127.0.0.1")
    port = os.environ.get("PG_PORT", "5432")
    name = os.environ.get("PG_NAME", "ocpp")
    user = os.environ.get("PG_USER", "ocpp")
    password = os.environ.get("PG_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using synchronous SQLAlchemy engine."""
    connectable = create_engine(get_url())

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema="ocpp",
            # Ensure the ocpp schema is in the search path
            version_table="alembic_version",
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL script)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema="ocpp",
    )

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
