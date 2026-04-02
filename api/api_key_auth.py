"""
API Key Authentication — management endpoint protection.

Management endpoints (chargers, tokens, tariffs, invoices, users, etc.)
require a Bearer API key or X-API-Key header matching MANAGEMENT_API_KEY.

Public endpoints (charge app, webhooks, OCPP WebSocket) are exempt.

Usage in FastAPI:
    from api.api_key_auth import management_auth

    router = APIRouter(dependencies=[Depends(management_auth)])
    # or per-route: @router.get("/", dependencies=[Depends(management_auth)])
"""
import hmac
import logging
import os

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, APIKeyQuery

logger = logging.getLogger(__name__)

_MANAGEMENT_API_KEY = os.environ.get("MANAGEMENT_API_KEY", "")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_api_key_bearer = APIKeyHeader(name="Authorization", auto_error=False)


async def management_auth(
    request: Request,
    x_api_key: str = Security(_api_key_header),
    authorization: str = Security(_api_key_bearer),
) -> None:
    """
    FastAPI dependency: verify MANAGEMENT_API_KEY.

    Accepts:
        X-API-Key: <key>
        Authorization: Bearer <key>

    Raises 401 if key is missing/wrong.
    Raises 503 if MANAGEMENT_API_KEY env var is not configured (startup misconfiguration).
    """
    if not _MANAGEMENT_API_KEY:
        logger.critical(
            "MANAGEMENT_API_KEY is not set — management API is effectively open. "
            "Set MANAGEMENT_API_KEY in your .env file."
        )
        raise HTTPException(
            status_code=503,
            detail="Management API is not configured. Set MANAGEMENT_API_KEY.",
        )

    # Extract key from header(s)
    provided = None
    if x_api_key:
        provided = x_api_key
    elif authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:]

    if not provided or not hmac.compare_digest(provided, _MANAGEMENT_API_KEY):
        logger.warning(f"Management API: unauthorized request (bad/missing key) path={request.url.path} from={request.client.host if request.client else '?'}")
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Provide X-API-Key or Authorization: Bearer <key>.",
            headers={"WWW-Authenticate": "Bearer"},
        )
