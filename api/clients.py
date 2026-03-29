"""
Clients API endpoints.

Read-only client profile data from commercial.clients.
Replaces direct-DB access in the client portal (routes/account.py).

Table: commercial.clients
Columns inferred from portal usage (SELECT * FROM commercial.clients):
  id                  SERIAL / UUID PRIMARY KEY
  name                TEXT
  contact_name        TEXT
  contact_email       TEXT
  contact_phone       TEXT
  address             TEXT
  city                TEXT
  postal_code         TEXT
  country             TEXT DEFAULT 'NL'
  kvk_number          TEXT
  vat_number          TEXT
  contract_type       TEXT
  contract_start      DATE
  contract_end        DATE (nullable)
  payment_terms_days  INT
  notes               TEXT
  created_at          TIMESTAMPTZ DEFAULT NOW()
  updated_at          TIMESTAMPTZ DEFAULT NOW()

  (Any additional columns are passed through as-is.)
"""
import logging

from fastapi import APIRouter, HTTPException

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/{client_id}")
async def get_client(client_id: str):
    """
    Full client profile including contact, address, and contract details.

    client_id can be an integer primary key or UUID — both TEXT-cast lookups are tried.
    """
    async with db.read() as conn:
        # Try by primary key (works for both INT and UUID stored as text)
        client = await conn.fetchrow(
            "SELECT * FROM commercial.clients WHERE id::text = $1", client_id
        )
        if not client:
            raise HTTPException(404, f"Client {client_id} not found")

    result = dict(client)

    # Serialise date/datetime fields to ISO strings
    for key, val in result.items():
        if hasattr(val, "isoformat"):
            result[key] = val.isoformat()

    return result


@router.get("")
async def list_clients(
    limit: int = 100,
    offset: int = 0,
):
    """List all clients (admin use). Returns id, name, contact basics."""
    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT id, name, contact_name, contact_email, city, contract_type, created_at
            FROM commercial.clients
            ORDER BY name ASC
            OFFSET $1 LIMIT $2
        """, offset, limit)
        total = await conn.fetchval("SELECT COUNT(*) FROM commercial.clients")

    clients = []
    for r in rows:
        item = dict(r)
        item["id"] = str(item["id"])
        if item.get("created_at"):
            item["created_at"] = item["created_at"].isoformat()
        clients.append(item)

    return {"clients": clients, "total": total, "offset": offset, "limit": limit}
