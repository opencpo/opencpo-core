"""
OCPI 2.2.1 Tokens module — CPO receiver interface.

EMSPs push their tokens to us so we can authorize their users at our chargers.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends

from ocpi.main import ocpi_response, ocpi_error, verify_ocpi_token
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/{country_code}/{party_id}/{token_uid}")
async def get_token(country_code: str, party_id: str, token_uid: str,
                    token: str = Depends(verify_ocpi_token)):
    """Get a specific token."""
    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ocpp.ocpi_tokens WHERE uid = $1 AND country_code = $2 AND party_id = $3",
            token_uid, country_code, party_id,
        )

    if not row:
        return ocpi_error(2003, "Unknown token")

    return ocpi_response({
        "country_code": row["country_code"],
        "party_id": row["party_id"],
        "uid": row["uid"],
        "type": row["type"],
        "auth_id": row["auth_id"],
        "issuer": row["issuer"],
        "valid": row["valid"],
        "whitelist": row["whitelist"],
        "last_updated": row["last_updated"].isoformat(),
    })


@router.put("/{country_code}/{party_id}/{token_uid}")
async def put_token(country_code: str, party_id: str, token_uid: str,
                    request: Request, token: str = Depends(verify_ocpi_token)):
    """EMSP pushes/updates a token — we store it for authorization."""
    body = await request.json()

    auth_id = body.get("contract_id", body.get("auth_id", token_uid))
    token_type = body.get("type", "RFID")
    issuer = body.get("issuer", "")
    valid = body.get("valid", True)
    whitelist = body.get("whitelist", "ALWAYS")

    async with db.write() as conn:
        # Store in OCPI tokens table
        await conn.execute("""
            INSERT INTO ocpp.ocpi_tokens (uid, type, auth_id, party_id, country_code, issuer, valid, whitelist, last_updated)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            ON CONFLICT (uid) DO UPDATE SET
                type = $2, auth_id = $3, issuer = $6, valid = $7, whitelist = $8, last_updated = NOW()
        """, token_uid, token_type, auth_id, party_id, country_code, issuer, valid, whitelist)

        # Also update authorization cache so OCPP Authorize works
        status = "Accepted" if valid else "Blocked"
        await conn.execute("""
            INSERT INTO ocpp.authorization_cache (token, type, status, display_name, group_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (token) DO UPDATE SET status = $3, display_name = $4
        """, token_uid, token_type.lower(), status, issuer, f"{country_code}*{party_id}")

    logger.info(f"OCPI token pushed: {country_code}*{party_id} uid={token_uid} valid={valid}")
    return ocpi_response()


@router.patch("/{country_code}/{party_id}/{token_uid}")
async def patch_token(country_code: str, party_id: str, token_uid: str,
                      request: Request, token: str = Depends(verify_ocpi_token)):
    """Partial update of a token."""
    body = await request.json()

    updates = []
    values = []
    idx = 1

    for field in ("valid", "whitelist", "type", "issuer"):
        if field in body:
            updates.append(f"{field} = ${idx}")
            values.append(body[field])
            idx += 1

    if not updates:
        return ocpi_error(2001, "No fields to update")

    updates.append(f"last_updated = NOW()")
    values.append(token_uid)

    async with db.write() as conn:
        await conn.execute(
            f"UPDATE ocpp.ocpi_tokens SET {', '.join(updates)} WHERE uid = ${idx}",
            *values,
        )

        # Sync auth cache
        if "valid" in body:
            status = "Accepted" if body["valid"] else "Blocked"
            await conn.execute(
                "UPDATE ocpp.authorization_cache SET status = $1 WHERE token = $2",
                status, token_uid,
            )

    logger.info(f"OCPI token patched: {token_uid}")
    return ocpi_response()
