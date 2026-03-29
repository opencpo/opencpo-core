"""Mollie payment helpers — iDEAL (public) + SEPA Direct Debit (fleet).

Public/iDEAL flow:   create_mollie_payment / fetch_mollie_payment
SEPA mandate flow:   create_mollie_customer → create_mandate_payment →
                     fetch_mollie_mandate → collect_invoice_payment
"""
import logging
import os
from datetime import datetime
from decimal import Decimal

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

MOLLIE_KEY      = os.environ.get("MOLLIE_API_KEY", "")
CHARGE_BASE_URL = os.getenv("CHARGE_APP_URL", "http://localhost:8080")
WEBHOOK_URL     = os.getenv("PAYMENT_WEBHOOK_URL", "http://localhost:8000/api/payments/webhook")
PREAUTH_AMOUNT  = 25.00

# ── Environment safety check (section 13.4) ──────────────────────────────

IS_SANDBOX = os.environ.get("BILLING_ENV", "sandbox") == "sandbox"

if MOLLIE_KEY.startswith("test_") and not IS_SANDBOX:
    logger.critical("TEST Mollie key in PRODUCTION — billing disabled")
if MOLLIE_KEY.startswith("live_") and IS_SANDBOX:
    logger.critical("LIVE Mollie key in SANDBOX — billing disabled")

BILLING_ENABLED = (
    not (MOLLIE_KEY.startswith("test_") and not IS_SANDBOX)
    and not (MOLLIE_KEY.startswith("live_") and IS_SANDBOX)
)

# ── Internal helpers ──────────────────────────────────────────────────────

_MOLLIE_BASE = "https://api.mollie.com/v2"


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {MOLLIE_KEY}", "Content-Type": "application/json"}


def _guard() -> None:
    """Raise 503 if Mollie key is missing or environment is mis-configured."""
    if not MOLLIE_KEY:
        raise HTTPException(503, "Betaalprovider niet geconfigureerd")
    if not BILLING_ENABLED:
        raise HTTPException(503, "Billing disabled — environment/key mismatch")


async def _post(path: str, payload: dict) -> dict:
    """POST to Mollie with 15s timeout; logs full response on error."""
    _guard()
    async with httpx.AsyncClient(timeout=15.0) as c:
        resp = await c.post(f"{_MOLLIE_BASE}{path}", json=payload, headers=_auth_headers())
    if resp.status_code not in (200, 201):
        logger.error(
            "Mollie POST %s error %d: %s — payload: %s",
            path, resp.status_code, resp.text[:500], str(payload)[:300],
        )
        raise HTTPException(502, f"Betaalprovider fout: {resp.text[:200]}")
    return resp.json()


async def _get(path: str) -> dict:
    """GET from Mollie with 15s timeout; logs response on error."""
    _guard()
    async with httpx.AsyncClient(timeout=15.0) as c:
        resp = await c.get(f"{_MOLLIE_BASE}{path}", headers=_auth_headers())
    if resp.status_code != 200:
        logger.error("Mollie GET %s error %d: %s", path, resp.status_code, resp.text[:500])
        raise HTTPException(502, f"Betaalprovider fout: {resp.text[:200]}")
    return resp.json()


# ── iDEAL (public sessions) — unchanged ──────────────────────────────────


async def create_mollie_payment(
    session_id: str, cp_id: str, connector_id: int, amount: float = PREAUTH_AMOUNT
) -> dict:
    """Create an iDEAL pre-auth payment (public drivers). Returns full payment object."""
    return await _post(
        "/payments",
        {
            "amount":      {"currency": "EUR", "value": f"{amount:.2f}"},
            "description": f"{os.getenv('OPERATOR_NAME', 'Your CPO')} Laden - {cp_id} Port {connector_id}",
            "redirectUrl": f"{CHARGE_BASE_URL}/session/{session_id}",
            "webhookUrl":  WEBHOOK_URL,
            "method":      "ideal",
            "metadata":    {"session_id": session_id, "cp_id": cp_id, "connector_id": connector_id},
        },
    )


async def fetch_mollie_payment(payment_id: str) -> dict:
    """Fetch current payment status from Mollie (iDEAL + recurring)."""
    return await _get(f"/payments/{payment_id}")


async def cancel_mollie_payment(payment_id: str) -> bool:
    """Cancel an open/pending Mollie payment (iDEAL pre-auth).

    Only works for payments in 'open' or 'pending' state.
    Returns True if cancelled, False if payment couldn't be cancelled (e.g. already paid/expired).
    """
    _guard()
    async with httpx.AsyncClient(timeout=15.0) as c:
        resp = await c.delete(f"{_MOLLIE_BASE}/payments/{payment_id}", headers=_auth_headers())
    if resp.status_code == 204:
        logger.info("Mollie payment %s cancelled", payment_id)
        return True
    elif resp.status_code in (404, 422):
        # Already gone or can't be cancelled (already paid/expired)
        logger.warning("Mollie payment %s cannot be cancelled: %s %s", payment_id, resp.status_code, resp.text[:200])
        return False
    else:
        logger.error("Mollie cancel %s error %d: %s", payment_id, resp.status_code, resp.text[:200])
        return False


# ── SEPA mandate — customer setup ────────────────────────────────────────


async def create_mollie_customer(name: str, email: str) -> str:
    """Create a Mollie customer record for a fleet group.

    Returns the Mollie customer ID (cst_xxx).
    """
    data = await _post("/customers", {"name": name, "email": email})
    customer_id: str = data["id"]
    logger.info("Mollie customer created: %s (%s)", customer_id, email)
    return customer_id


async def create_mandate_payment(
    customer_id: str, redirect_url: str, webhook_url: str
) -> dict:
    """Create a €0.01 first-payment to establish a SEPA mandate.

    sequenceType=first opens the Mollie-hosted IBAN/mandate page.
    Returns the full Mollie payment object (contains _links.checkout.href).
    """
    data = await _post(
        f"/customers/{customer_id}/payments",
        {
            "amount":       {"currency": "EUR", "value": "0.01"},
            "description":  f"{os.getenv('OPERATOR_NAME', 'Your CPO')} — SEPA machtiging verificatie",
            "sequenceType": "first",
            "method":       "directdebit",
            "redirectUrl":  redirect_url,
            "webhookUrl":   webhook_url,
        },
    )
    logger.info(
        "Mandate first-payment created: %s for customer %s", data.get("id"), customer_id
    )
    return data


# ── SEPA mandate — status & collection ───────────────────────────────────


async def fetch_mollie_mandate(customer_id: str, mandate_id: str) -> dict:
    """Fetch mandate status directly from Mollie.

    Returns the mandate object; key fields: status ('valid'|'pending'|'invalid'|'revoked').
    """
    return await _get(f"/customers/{customer_id}/mandates/{mandate_id}")


async def collect_invoice_payment(
    customer_id: str,
    mandate_id: str,
    amount: Decimal,
    description: str,
    invoice_id: str,
    webhook_url: str,
) -> dict:
    """Collect an invoice amount via SEPA recurring payment.

    Verifies mandate is valid from Mollie *before* attempting collection.
    Raises HTTPException(422) if mandate is not valid.
    Returns the full Mollie payment object.
    """
    # Always re-fetch mandate from Mollie — never trust DB status alone
    mandate = await fetch_mollie_mandate(customer_id, mandate_id)
    if mandate.get("status") != "valid":
        raise HTTPException(
            422,
            f"Mandate {mandate_id} is '{mandate.get('status')}' — cannot collect",
        )

    data = await _post(
        f"/customers/{customer_id}/payments",
        {
            "amount":       {"currency": "EUR", "value": f"{amount:.2f}"},
            "description":  description,
            "sequenceType": "recurring",
            "mandateId":    mandate_id,
            "webhookUrl":   webhook_url,
            "metadata":     {"invoice_id": invoice_id},
        },
    )
    logger.info(
        "Recurring payment %s created for invoice %s (amount: %s EUR)",
        data.get("id"), invoice_id, amount,
    )
    return data


# ── Utility ───────────────────────────────────────────────────────────────


def session_row_to_dict(row) -> dict:
    """Convert asyncpg Record to JSON-serializable dict."""
    d = dict(row)
    for key, val in d.items():
        if isinstance(val, datetime):
            d[key] = val.isoformat()
    return d
