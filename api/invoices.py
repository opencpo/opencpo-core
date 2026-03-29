"""
Invoice generation and management API.

  GET  /api/v1/invoices                       — list invoices
  GET  /api/v1/invoices/{invoice_id}          — single invoice with lines
  POST /api/v1/groups/{group_id}/invoices     — generate invoice for period
  POST /api/v1/invoices/{invoice_id}/send     — mark as sent
  POST /api/v1/invoices/{invoice_id}/mark-paid — mark as paid (manual)

SEPA mandate + collection endpoints are in api/sepa.py.

Money rules: Decimal only, triple verification, billing_events on every mutation.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Models ───────────────────────────────────────────────────────────────


class GenerateInvoiceRequest(BaseModel):
    period_start: date
    period_end: date


# ── Helpers ──────────────────────────────────────────────────────────────


def _invoice_dict(row) -> dict:
    return {
        "id": str(row["id"]),
        "group_id": str(row["group_id"]),
        "invoice_number": row["invoice_number"],
        "invoice_type": row["invoice_type"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "subtotal": float(row["subtotal"]),
        "vat_rate": float(row["vat_rate"]),
        "vat_amount": float(row["vat_amount"]),
        "total": float(row["total"]),
        "currency": row["currency"],
        "status": row["status"],
        "due_date": row["due_date"],
        "paid_at": row["paid_at"],
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
    }


def _line_dict(row) -> dict:
    return {
        "id": str(row["id"]),
        "invoice_id": str(row["invoice_id"]),
        "cdr_id": str(row["cdr_id"]) if row["cdr_id"] else None,
        "session_id": str(row["session_id"]) if row["session_id"] else None,
        "token_uid": row["token_uid"],
        "driver_name": row["driver_name"],
        "description": row["description"],
        "kwh": float(row["kwh"]) if row["kwh"] is not None else None,
        "rate_kwh": float(row["rate_kwh"]) if row["rate_kwh"] is not None else None,
        "amount": float(row["amount"]),
    }


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


def _parse_uuid(val: str, field: str = "id") -> UUID:
    try:
        return UUID(val)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field} format")


# ── GET /api/v1/invoices ─────────────────────────────────────────────────


@router.get("/api/v1/invoices", tags=["Invoices"])
async def list_invoices(
    group_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List invoices with optional filters."""
    try:
        clauses, params, idx = [], [], 1
        if group_id:
            clauses.append(f"group_id = ${idx}")
            params.append(_parse_uuid(group_id, "group_id"))
            idx += 1
        if status:
            clauses.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with db.read() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM ocpp.invoices {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
                *params, limit, offset,
            )
            total = await conn.fetchval(f"SELECT COUNT(*) FROM ocpp.invoices {where}", *params)
        return {"invoices": [_invoice_dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"list_invoices: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list invoices")


# ── GET /api/v1/invoices/{invoice_id} ────────────────────────────────────


@router.get("/api/v1/invoices/{invoice_id}", tags=["Invoices"])
async def get_invoice(invoice_id: str):
    """Fetch a single invoice with its line items."""
    inv_uuid = _parse_uuid(invoice_id, "invoice_id")
    try:
        async with db.read() as conn:
            inv = await conn.fetchrow("SELECT * FROM ocpp.invoices WHERE id = $1", inv_uuid)
            if not inv:
                raise HTTPException(status_code=404, detail="Invoice not found")
            lines = await conn.fetch(
                "SELECT * FROM ocpp.invoice_lines WHERE invoice_id = $1 ORDER BY id", inv_uuid
            )
        result = _invoice_dict(inv)
        result["lines"] = [_line_dict(r) for r in lines]
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"get_invoice: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch invoice")


# ── POST /api/v1/groups/{group_id}/invoices ──────────────────────────────


@router.post("/api/v1/groups/{group_id}/invoices", tags=["Invoices"], status_code=201)
async def generate_invoice(group_id: str, body: GenerateInvoiceRequest):
    """Generate a postpaid invoice for a billing period."""
    group_uuid = _parse_uuid(group_id, "group_id")
    if body.period_end <= body.period_start:
        raise HTTPException(status_code=400, detail="period_end must be after period_start")

    try:
        async with db.write() as conn:
            # 1. Verify group is postpaid
            group = await conn.fetchrow(
                "SELECT id, name, billing_method, max_invoice_amount, payment_terms_days "
                "FROM ocpp.token_groups WHERE id = $1", group_uuid
            )
            if not group:
                raise HTTPException(status_code=404, detail="Group not found")
            if group["billing_method"] not in ("invoice", "direct_debit"):
                raise HTTPException(status_code=422,
                    detail=f"Group billing_method '{group['billing_method']}' is not postpaid")

            # 2. Idempotency check
            existing = await conn.fetchrow(
                "SELECT * FROM ocpp.invoices WHERE group_id=$1 AND period_start=$2 AND period_end=$3",
                group_uuid, body.period_start, body.period_end,
            )
            if existing:
                result = _invoice_dict(existing)
                result["_already_existed"] = True
                return result

            # 3. Query unbilled CDRs
            period_start_dt = datetime.combine(body.period_start, datetime.min.time()).replace(tzinfo=timezone.utc)
            period_end_dt = datetime.combine(body.period_end, datetime.min.time()).replace(tzinfo=timezone.utc)
            cdrs = await conn.fetch(
                """
                SELECT c.id, c.session_id, c.energy_kwh, c.cost, c.start_time,
                       c.stop_time, c.charge_point, c.connector_id,
                       t.uid AS token_uid, t.driver_name
                FROM ocpp.cdrs c
                JOIN ocpp.sessions s ON c.session_id = s.id
                JOIN ocpp.tokens t ON s.auth_id = t.uid
                WHERE t.group_id = $1
                  AND c.start_time >= $2 AND c.start_time < $3
                  AND c.id NOT IN (
                      SELECT cdr_id FROM ocpp.invoice_lines WHERE cdr_id IS NOT NULL
                  )
                ORDER BY c.start_time
                """,
                group_uuid, period_start_dt, period_end_dt,
            )

            # 4. No CDRs → 404
            if not cdrs:
                raise HTTPException(status_code=404, detail="No unbilled sessions for this period")

            # 5. Rate sanity check
            for cdr in cdrs:
                cost = json.loads(cdr["cost"]) if isinstance(cdr["cost"], str) else cdr["cost"]
                if cost and "rate_kwh" in cost:
                    rate = cost["rate_kwh"]
                    if rate < 0.05 or rate > 2.00:
                        raise HTTPException(status_code=422,
                            detail=f"CDR {cdr['id']} rate {rate} €/kWh outside safe range (0.05–2.00)")

            # 6. Build lines with Decimal
            lines_data = []
            subtotal_dec = Decimal("0.00")
            for cdr in cdrs:
                cost = json.loads(cdr["cost"]) if isinstance(cdr["cost"], str) else (cdr["cost"] or {})
                energy = Decimal(str(cdr["energy_kwh"]))
                rate_kwh = Decimal(str(cost["rate_kwh"])) if cost.get("rate_kwh") else Decimal("0.35")
                amount = (energy * rate_kwh).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                subtotal_dec += amount
                start_s = cdr["start_time"].strftime("%d-%m-%Y %H:%M") if cdr["start_time"] else "?"
                stop_s = cdr["stop_time"].strftime("%H:%M") if cdr["stop_time"] else "?"
                desc = (f"Laden {start_s} – {stop_s} · {cdr['charge_point']}, "
                        f"Connector {cdr['connector_id']} · {float(energy):.1f} kWh")
                lines_data.append({
                    "cdr_id": cdr["id"], "session_id": cdr["session_id"],
                    "token_uid": cdr["token_uid"], "driver_name": cdr["driver_name"],
                    "description": desc, "kwh": energy, "rate_kwh": rate_kwh, "amount": amount,
                })

            # 7. Triple verify
            independent = sum(ld["amount"] for ld in lines_data)
            if abs(subtotal_dec - independent) > Decimal("0.01"):
                logger.error(f"Invoice subtotal mismatch: acc={subtotal_dec} ind={independent}")
                raise HTTPException(status_code=500, detail="Amount integrity check failed")

            vat_amount = (subtotal_dec * Decimal("0.21")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            total = subtotal_dec + vat_amount

            # 8. Max invoice amount check
            max_amount = group["max_invoice_amount"]
            if max_amount and total > Decimal(str(max_amount)):
                raise HTTPException(status_code=422,
                    detail=f"Invoice total {float(total):.2f} EUR exceeds group max {float(max_amount):.2f} EUR")

            # 9. Invoice number
            year = body.period_end.year
            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(CAST(SPLIT_PART(invoice_number,'-',3) AS INTEGER)),0)+1 "
                "FROM ocpp.invoices WHERE invoice_number LIKE $1",
                f"STM-{year}-%",
            )
            invoice_number = f"STM-{year}-{seq:04d}"
            payment_terms = group["payment_terms_days"] or 30
            due_date = body.period_end + timedelta(days=payment_terms)

            # 10. Single transaction INSERT
            inv_row = await conn.fetchrow(
                """INSERT INTO ocpp.invoices
                   (group_id, invoice_number, period_start, period_end,
                    subtotal, vat_rate, vat_amount, total, currency, status, due_date)
                   VALUES ($1,$2,$3,$4,$5,21.00,$6,$7,'EUR','draft',$8) RETURNING *""",
                group_uuid, invoice_number, body.period_start, body.period_end,
                subtotal_dec, vat_amount, total, due_date,
            )
            inv_id = inv_row["id"]
            for ld in lines_data:
                await conn.execute(
                    """INSERT INTO ocpp.invoice_lines
                       (invoice_id,cdr_id,session_id,token_uid,driver_name,description,kwh,rate_kwh,amount)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    inv_id, ld["cdr_id"], ld["session_id"], ld["token_uid"], ld["driver_name"],
                    ld["description"], ld["kwh"], ld["rate_kwh"], ld["amount"],
                )

            # 11. Billing event
            await _billing_event(conn, "invoice", str(inv_id), "created", "api", {
                "invoice_number": invoice_number, "subtotal": float(subtotal_dec),
                "vat_amount": float(vat_amount), "total": float(total), "cdr_count": len(lines_data),
                "period_start": body.period_start.isoformat(), "period_end": body.period_end.isoformat(),
            }, group_id=group_id)

        logger.info(f"Invoice {invoice_number} generated for group {group_id}: {len(lines_data)} CDRs total={float(total):.2f}")

        # 12. Return
        result = _invoice_dict(inv_row)
        result["lines"] = [
            {"cdr_id": str(ld["cdr_id"]), "session_id": str(ld["session_id"]) if ld["session_id"] else None,
             "token_uid": ld["token_uid"], "driver_name": ld["driver_name"], "description": ld["description"],
             "kwh": float(ld["kwh"]), "rate_kwh": float(ld["rate_kwh"]), "amount": float(ld["amount"])}
            for ld in lines_data
        ]
        return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"generate_invoice group={group_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Invoice generation failed: {exc}")


# ── POST /api/v1/invoices/{invoice_id}/send ──────────────────────────────


@router.post("/api/v1/invoices/{invoice_id}/send", tags=["Invoices"])
async def send_invoice(invoice_id: str):
    """Transition invoice draft → sent."""
    inv_uuid = _parse_uuid(invoice_id, "invoice_id")
    try:
        async with db.write() as conn:
            row = await conn.fetchrow(
                "SELECT id, status, group_id, invoice_number FROM ocpp.invoices WHERE id = $1", inv_uuid
            )
            if not row:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if row["status"] != "draft":
                raise HTTPException(status_code=422,
                    detail=f"Invoice is '{row['status']}', only 'draft' invoices can be sent")
            updated = await conn.fetchrow(
                "UPDATE ocpp.invoices SET status='sent', sent_at=NOW() WHERE id=$1 RETURNING *", inv_uuid
            )
            await _billing_event(conn, "invoice", invoice_id, "sent", "api",
                {"invoice_number": row["invoice_number"]}, group_id=str(row["group_id"]))
        logger.info(f"Invoice {row['invoice_number']} sent")
        return _invoice_dict(updated)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"send_invoice: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to mark invoice as sent")


# ── POST /api/v1/invoices/{invoice_id}/mark-paid ─────────────────────────


@router.post("/api/v1/invoices/{invoice_id}/mark-paid", tags=["Invoices"])
async def mark_invoice_paid(invoice_id: str):
    """Manually mark invoice as paid (bank transfer received)."""
    inv_uuid = _parse_uuid(invoice_id, "invoice_id")
    try:
        async with db.write() as conn:
            row = await conn.fetchrow(
                "SELECT id, status, group_id, invoice_number, total FROM ocpp.invoices WHERE id = $1", inv_uuid
            )
            if not row:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if row["status"] in ("paid", "cancelled"):
                raise HTTPException(status_code=422,
                    detail=f"Invoice is '{row['status']}' — cannot mark paid again")
            updated = await conn.fetchrow(
                "UPDATE ocpp.invoices SET status='paid', paid_at=NOW() WHERE id=$1 RETURNING *", inv_uuid
            )
            await _billing_event(conn, "invoice", invoice_id, "paid", "api", {
                "invoice_number": row["invoice_number"], "total": float(row["total"]), "method": "manual"
            }, group_id=str(row["group_id"]))
        logger.info(f"Invoice {row['invoice_number']} marked paid (manual)")
        return _invoice_dict(updated)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"mark_invoice_paid: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to mark invoice as paid")
