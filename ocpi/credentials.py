"""
OCPI 2.2.1 Credentials module — partner registration + token exchange.
"""
import logging
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends

from ocpi.main import ocpi_response, ocpi_error, verify_ocpi_token
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


def _cpo_role_info() -> dict:
    """Build our CPO role information from environment variables."""
    return {
        "role": "CPO",
        "business_details": {
            "name": os.getenv("OCPI_OPERATOR_NAME", "OCPP Core CPO"),
            "website": os.getenv("OCPI_OPERATOR_WEBSITE", ""),
        },
        "party_id": os.getenv("OCPI_PARTY_ID", "OCP"),
        "country_code": os.getenv("OCPI_COUNTRY_CODE", "NL"),
    }


@router.get("")
async def get_credentials(token: str = Depends(verify_ocpi_token)):
    """Return our credentials to the partner."""
    return ocpi_response({
        "token": token,
        "url": f"{_base_url()}/ocpi/versions",
        "roles": [_cpo_role_info()],
    })


@router.post("")
async def post_credentials(request: Request):
    """
    Partner sends their credentials — we register them and return ours.
    This is the OCPI handshake.
    """
    body = await request.json()

    partner_token = body.get("token", "")
    partner_url = body.get("url", "")
    roles = body.get("roles", [])

    if not partner_token or not partner_url or not roles:
        return ocpi_error(2001, "Missing required fields: token, url, roles")

    role = roles[0]
    party_id = role.get("party_id", "")
    country_code = role.get("country_code", "")
    partner_role = role.get("role", "EMSP")
    name = role.get("business_details", {}).get("name", "Unknown")

    # Generate our token for them
    our_token = secrets.token_urlsafe(32)

    async with db.write() as conn:
        await conn.execute("""
            INSERT INTO ocpp.ocpi_partners (party_id, country_code, role, name, url, token_a, token_b, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')
            ON CONFLICT (party_id, country_code, role) DO UPDATE SET
                name = $4, url = $5, token_a = $6, token_b = $7, status = 'active'
        """, party_id, country_code, partner_role, name, partner_url,
            our_token, partner_token)

    logger.info(f"OCPI credentials exchanged: {country_code}*{party_id} ({name}) role={partner_role}")

    return ocpi_response({
        "token": our_token,
        "url": f"{_base_url()}/ocpi/versions",
        "roles": [_cpo_role_info()],
    })


@router.put("")
async def put_credentials(request: Request, token: str = Depends(verify_ocpi_token)):
    """Update partner credentials (re-registration)."""
    body = await request.json()
    roles = body.get("roles", [{}])
    role = roles[0]

    new_token = body.get("token", "")
    new_url = body.get("url", "")

    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.ocpi_partners SET
                token_b = $1, url = $2, last_sync = NOW()
            WHERE token_a = $3
        """, new_token, new_url, token)

    return ocpi_response({
        "token": token,
        "url": f"{_base_url()}/ocpi/versions",
        "roles": [_cpo_role_info()],
    })


@router.delete("")
async def delete_credentials(token: str = Depends(verify_ocpi_token)):
    """Partner unregisters."""
    async with db.write() as conn:
        await conn.execute(
            "UPDATE ocpp.ocpi_partners SET status = 'suspended' WHERE token_a = $1", token
        )
    logger.info("OCPI partner unregistered")
    return ocpi_response()


def _base_url() -> str:
    return os.getenv("OCPI_BASE_URL", "http://localhost:8000")
