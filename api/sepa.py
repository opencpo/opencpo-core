"""
SEPA Direct Debit endpoints — mandate setup, collection, and Mollie webhook.

  POST /api/v1/groups/{group_id}/mandate      — create Mollie customer + first payment
  GET  /api/v1/groups/{group_id}/mandate      — return mandate status
  POST /api/v1/invoices/{invoice_id}/collect  — trigger SEPA collection
  POST /api/payments/mandate-webhook          — Mollie webhook handler

All money as Decimal. Every mutation writes to billing_events.
Mandate always verified from Mollie before collection (never trust DB alone).
"""
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.mollie import (
    collect_invoice_payment,
    create_mandate_payment,
    create_mollie_customer,
    fetch_mollie_mandate,
    fetch_mollie_payment,
)
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()

_MANDATE_WEBHOOK = os.getenv("SEPA_MANDATE_WEBHOOK_URL", "http://localhost:8000/api/payments/mandate-webhook")


# ── Models ────────────────────────────────────────────────────────────────


class MandateRequest(BaseModel):
    redirect_url: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _parse_uuid(val: str, field: str = "id") -> UUID:
    try:
        return UUID(val)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field} format")


async def _billing_event(conn, entity_type, entity_id, action, actor, details, group_id=None):
    """Write audit entry to ocpp.billing_events. Best-effort."""
    try:
        await conn.execute(
            "INSERT INTO ocpp.billing_events "
            "(entity_type, entity_id, action, actor, details, group_id) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            entity_type, entity_id, action, actor,
            json.dumps(details),
            UUID(group_id) if group_id else None,
        )
    except Exception as exc:
        logger.error(f"billing_event write failed: {exc}")


# ── POST /api/v1/groups/{group_id}/mandate ───────────────────────────────


@router.post("/api/v1/groups/{group_id}/mandate", tags=["SEPA"], status_code=201)
async def setup_mandate(group_id: str, body: MandateRequest):
    """Create a Mollie customer (if needed) and initiate a SEPA mandate first-payment.

    Returns the Mollie checkout URL so the fleet manager can complete IBAN authorisation.
    Updates token_groups: mollie_customer_id, mandate_status='pending'.
    """
    group_uuid = _parse_uuid(group_id, "group_id")
    try:
        async with db.write() as conn:
            group = await conn.fetchrow(
                "SELECT id, name, billing_email, mollie_customer_id, billing_method "
                "FROM ocpp.token_groups WHERE id = $1", group_uuid
            )
            if not group:
                raise HTTPException(status_code=404, detail="Group not found")
            if group["billing_method"] != "direct_debit":
                raise HTTPException(status_code=422,
                    detail="Group billing_method must be 'direct_debit' to set up a mandate")

            # Create Mollie customer if not already on record
            customer_id = group["mollie_customer_id"]
            if not customer_id:
                customer_id = await create_mollie_customer(
                    name=group["name"] or os.getenv("OPERATOR_NAME", "Your CPO"),
                    email=group["billing_email"] or "",
                )
                await conn.execute(
                    "UPDATE ocpp.token_groups SET mollie_customer_id=$1 WHERE id=$2",
                    customer_id, group_uuid,
                )

            payment = await create_mandate_payment(customer_id, body.redirect_url, _MANDATE_WEBHOOK)

            await conn.execute(
                "UPDATE ocpp.token_groups SET mandate_status='pending' WHERE id=$1", group_uuid
            )

            await _billing_event(conn, "mandate", customer_id, "created", "api", {
                "mollie_customer_id": customer_id,
                "mollie_payment_id": payment.get("id"),
                "redirect_url": body.redirect_url,
            }, group_id=group_id)

        checkout_url = (
            payment.get("_links", {}).get("checkout", {}).get("href")
            or payment.get("_links", {}).get("paymentUrl", {}).get("href")
        )
        return {
            "mollie_customer_id": customer_id,
            "mollie_payment_id": payment.get("id"),
            "checkout_url": checkout_url,
            "mandate_status": "pending",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"setup_mandate group={group_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Mandate setup failed: {exc}")


# ── GET /api/v1/groups/{group_id}/mandate ────────────────────────────────


@router.get("/api/v1/groups/{group_id}/mandate", tags=["SEPA"])
async def get_mandate(group_id: str):
    """Return current mandate status for a group (from our DB, not Mollie)."""
    group_uuid = _parse_uuid(group_id, "group_id")
    try:
        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT mollie_customer_id, mollie_mandate_id, mandate_status "
                "FROM ocpp.token_groups WHERE id = $1", group_uuid
            )
        if not row:
            raise HTTPException(status_code=404, detail="Group not found")
        return {
            "group_id": group_id,
            "mollie_customer_id": row["mollie_customer_id"],
            "mollie_mandate_id": row["mollie_mandate_id"],
            "mandate_status": row["mandate_status"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"get_mandate group={group_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch mandate status")


# ── POST /api/v1/invoices/{invoice_id}/collect ───────────────────────────


@router.post("/api/v1/invoices/{invoice_id}/collect", tags=["SEPA"])
async def collect_invoice(invoice_id: str):
    """Trigger SEPA direct-debit collection for a sent invoice.

    Safety guards:
    - Invoice status must be 'sent'
    - Mandate verified live from Mollie (not trusted from DB)
    - 48h cooldown between collection attempts
    - Maximum 3 attempts per invoice
    """
    inv_uuid = _parse_uuid(invoice_id, "invoice_id")
    try:
        async with db.write() as conn:
            inv = await conn.fetchrow(
                "SELECT i.id, i.status, i.total, i.invoice_number, i.group_id, "
                "       g.mollie_customer_id, g.mollie_mandate_id "
                "FROM ocpp.invoices i "
                "JOIN ocpp.token_groups g ON i.group_id = g.id "
                "WHERE i.id = $1", inv_uuid
            )
            if not inv:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if inv["status"] != "sent":
                raise HTTPException(status_code=422,
                    detail=f"Invoice status is '{inv['status']}', must be 'sent' to collect")
            if not inv["mollie_customer_id"] or not inv["mollie_mandate_id"]:
                raise HTTPException(status_code=422,
                    detail="Group has no SEPA mandate configured")

            # Max 3 attempts
            attempts = await conn.fetchval(
                "SELECT COUNT(*) FROM ocpp.billing_events "
                "WHERE entity_type='invoice' AND entity_id=$1 AND action='collect_attempt'",
                invoice_id,
            )
            if attempts >= 3:
                raise HTTPException(status_code=422,
                    detail=f"Maximum 3 collection attempts reached for invoice {inv['invoice_number']}")

            # 48h cooldown
            last_attempt = await conn.fetchval(
                "SELECT MAX(ts) FROM ocpp.billing_events "
                "WHERE entity_type='invoice' AND entity_id=$1 AND action='collect_attempt'",
                invoice_id,
            )
            if last_attempt:
                elapsed = datetime.now(tz=timezone.utc) - last_attempt.replace(tzinfo=timezone.utc)
                if elapsed.total_seconds() < 48 * 3600:
                    remaining_h = int((48 * 3600 - elapsed.total_seconds()) / 3600)
                    raise HTTPException(status_code=429,
                        detail=f"Collection cooldown active — retry in ~{remaining_h}h")

            # Verify mandate from Mollie — never trust DB alone (spec 13.4)
            mandate = await fetch_mollie_mandate(
                inv["mollie_customer_id"], inv["mollie_mandate_id"]
            )
            if mandate.get("status") != "valid":
                raise HTTPException(status_code=422,
                    detail=f"Mandate is '{mandate.get('status')}' — cannot collect")

            payment = await collect_invoice_payment(
                customer_id=inv["mollie_customer_id"],
                mandate_id=inv["mollie_mandate_id"],
                amount=Decimal(str(inv["total"])),
                description=f"{os.getenv('OPERATOR_NAME', 'Your CPO')} factuur {inv['invoice_number']}",
                invoice_id=invoice_id,
                webhook_url=_MANDATE_WEBHOOK,
            )

            # sent → collecting
            await conn.execute(
                "UPDATE ocpp.invoices SET status='collecting', mollie_payment_id=$1 WHERE id=$2",
                payment["id"], inv_uuid,
            )

            await _billing_event(conn, "invoice", invoice_id, "collect_attempt", "api", {
                "invoice_number": inv["invoice_number"],
                "mollie_payment_id": payment["id"],
                "amount": float(inv["total"]),
                "attempt": int(attempts) + 1,
            }, group_id=str(inv["group_id"]))

        logger.info(
            "Collection triggered for invoice %s: Mollie payment %s",
            inv["invoice_number"], payment["id"],
        )
        return {
            "invoice_id": invoice_id,
            "invoice_number": inv["invoice_number"],
            "mollie_payment_id": payment["id"],
            "status": "collecting",
            "attempt": int(attempts) + 1,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"collect_invoice {invoice_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Collection failed: {exc}")


# ── POST /api/payments/mandate-webhook ───────────────────────────────────


@router.post("/api/payments/mandate-webhook", tags=["SEPA"])
async def mandate_webhook(request: Request):
    """Mollie webhook for mandate confirmation and recurring payment outcomes.

    Mollie sends only a payment ID. We always re-fetch from Mollie to verify.
    Returns 200 in all cases to prevent Mollie infinite retries.

    sequenceType=first + paid  → mandate_status='valid', store mandate_id
    sequenceType=recurring + paid   → invoice status='paid'
    sequenceType=recurring + failed → invoice status='failed'
    """
    try:
        # Mollie may send form-encoded (id=tr_xxx) or JSON ({"id":"tr_xxx"})
        try:
            body = await request.json()
            payment_id = body.get("id", "")
        except Exception:
            form = await request.form()
            payment_id = form.get("id", "")

        if not payment_id:
            logger.warning("mandate-webhook: no payment id in payload")
            return {"ok": True}

        # Always re-fetch from Mollie — do not trust webhook body
        payment = await fetch_mollie_payment(payment_id)
        sequence_type = payment.get("sequenceType", "")
        status = payment.get("status", "")
        customer_id = payment.get("customerId", "")
        metadata = payment.get("metadata") or {}
        invoice_id = metadata.get("invoice_id")

        async with db.write() as conn:
            if sequence_type == "first" and status == "paid":
                mandate_id = payment.get("mandateId", "")
                updated = await conn.fetchrow(
                    "UPDATE ocpp.token_groups "
                    "SET mollie_mandate_id=$1, mandate_status='valid' "
                    "WHERE mollie_customer_id=$2 RETURNING id",
                    mandate_id, customer_id,
                )
                group_id = str(updated["id"]) if updated else None
                await _billing_event(conn, "mandate", mandate_id, "valid", "mollie:webhook", {
                    "mollie_payment_id": payment_id,
                    "mollie_customer_id": customer_id,
                }, group_id=group_id)
                logger.info("Mandate %s validated for customer %s", mandate_id, customer_id)

            elif sequence_type == "recurring" and invoice_id:
                inv = await conn.fetchrow(
                    "SELECT id, group_id, invoice_number FROM ocpp.invoices WHERE id=$1",
                    UUID(invoice_id),
                )
                if not inv:
                    logger.error("mandate-webhook: invoice %s not found", invoice_id)
                    return {"ok": True}

                if status == "paid":
                    await conn.execute(
                        "UPDATE ocpp.invoices "
                        "SET status='paid', paid_at=NOW(), mollie_payment_id=$1 WHERE id=$2",
                        payment_id, UUID(invoice_id),
                    )
                    await _billing_event(conn, "invoice", invoice_id, "paid", "mollie:webhook", {
                        "mollie_payment_id": payment_id,
                        "invoice_number": inv["invoice_number"],
                    }, group_id=str(inv["group_id"]))
                    logger.info(
                        "Invoice %s paid via SEPA (payment %s)",
                        inv["invoice_number"], payment_id,
                    )

                elif status == "failed":
                    await conn.execute(
                        "UPDATE ocpp.invoices SET status='failed' WHERE id=$1", UUID(invoice_id)
                    )
                    await _billing_event(conn, "invoice", invoice_id, "failed", "mollie:webhook", {
                        "mollie_payment_id": payment_id,
                        "invoice_number": inv["invoice_number"],
                        "mollie_status": status,
                    }, group_id=str(inv["group_id"]))
                    logger.warning(
                        "Invoice %s collection FAILED (payment %s)",
                        inv["invoice_number"], payment_id,
                    )

        return {"ok": True}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"mandate_webhook: {exc}", exc_info=True)
        return {"ok": True, "error": str(exc)}
