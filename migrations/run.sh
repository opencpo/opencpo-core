#!/usr/bin/env bash
# run.sh -- Run Alembic migrations inside the core container
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/app/opencpo}"
cd /app

echo "Running database migrations..."

# Check current migration state
alembic current 2>/dev/null || echo "No migrations applied yet"

# Run all pending migrations
alembic upgrade head

# Verify
echo "Migration complete. Current head:"
alembic current
