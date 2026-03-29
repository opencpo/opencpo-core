"""
group_invoice.py — Branded PDF invoice generator for token groups.

Operator branding (dark navy header, blue accent, logo).
Same design language as receipt_pdf.py.
"""
from __future__ import annotations

import io
import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

# ── Brand colours ─────────────────────────────────────────────────────────────
BLUE      = (0 / 255,   176 / 255, 228 / 255)
GREEN     = (132 / 255, 189 / 255,   0 / 255)
DARK_NAVY = (12 / 255,   35 / 255,  64 / 255)
WHITE     = (1.0, 1.0, 1.0)
BLACK     = (0.0, 0.0, 0.0)
GRAY_L    = (0.92, 0.92, 0.94)
GRAY_T    = (0.45, 0.45, 0.50)

FONT_R = "Helvetica"
FONT_B = "Helvetica-Bold"

_LOGO_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "static/logo.png")
)

BTW_RATE = 0.21


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _fill(c, col):
    c.setFillColorRGB(*col)


def _stroke(c, col):
    c.setStrokeColorRGB(*col)


def _euro(amount: float) -> str:
    return f"€\u202f{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _dots(c, x, y, w, h, rows=5, cols=12):
    dx, dy = w / (cols + 1), h / (rows + 1)
    c.saveState()
    c.setFillColorRGB(1, 1, 1, alpha=0.10)
    for r in range(1, rows + 1):
        for col in range(1, cols + 1):
            c.circle(x + col * dx, y + r * dy, 0.9, fill=1, stroke=0)
    c.restoreState()


def _header(c, pw, ph, left, right, htop, hh, title="Factuur"):
    c.saveState()
    _fill(c, DARK_NAVY)
    c.rect(0, htop - hh, pw, hh, fill=1, stroke=0)
    _dots(c, pw * 0.45, htop - hh, pw * 0.55, hh)
    _fill(c, GREEN)
    c.rect(0, htop - hh, 3, hh, fill=1, stroke=0)

    # Logo
    lh = 20 * mm
    lw = lh * (1094 / 182)
    ly = htop - hh / 2 - lh / 2
    try:
        c.drawImage(ImageReader(_LOGO_PATH), left, ly, width=lw, height=lh,
                    mask="auto", preserveAspectRatio=True)
    except Exception:
        c.setFont(FONT_B, 16)
        _fill(c, WHITE)
        c.drawString(left, htop - hh / 2 - 5, os.getenv("OPERATOR_NAME", "Your CPO"))

    c.setFont(FONT_B, 20)
    _fill(c, WHITE)
    c.drawRightString(right, htop - hh / 2 + 3, title)

    _fill(c, BLUE)
    c.rect(0, htop - hh - 3, pw, 3, fill=1, stroke=0)
    c.restoreState()


def _divider(c, y, left, right):
    c.saveState()
    _stroke(c, GRAY_L)
    c.setLineWidth(0.5)
    c.line(left, y, right, y)
    c.restoreState()


def _label(c, text, x, y):
    c.saveState()
    c.setFont(FONT_B, 8)
    _fill(c, GRAY_T)
    c.drawString(x, y, text.upper())
    c.restoreState()
    return y - 12


def _value(c, text, x, y, bold=False, color=BLACK):
    c.saveState()
    c.setFont(FONT_B if bold else FONT_R, 10)
    _fill(c, color)
    c.drawString(x, y, str(text))
    c.restoreState()
    return y - 14


def _row_bg(c, y, pw, left, alt=False):
    if alt:
        c.saveState()
        c.setFillColorRGB(0.96, 0.96, 0.98)
        c.rect(left, y - 4, pw - left * 2, 18, fill=1, stroke=0)
        c.restoreState()


# ── Main generator ────────────────────────────────────────────────────────────

def generate_invoice_pdf(group: dict, usage: dict, month: str) -> bytes:
    """
    Generate a branded invoice PDF.

    Args:
        group: dict with token_group fields (name, billing_email, etc.)
        usage: dict from get_group_usage endpoint (cards, totals)
        month: 'YYYY-MM' string
    Returns:
        PDF bytes
    """
    buf = io.BytesIO()
    pw, ph = A4
    left = 20 * mm
    right = pw - 20 * mm
    htop = ph - 15 * mm
    hh = 38 * mm

    c = pdf_canvas.Canvas(buf, pagesize=A4)

    # ── Header ────────────────────────────────────────────────────────────────
    try:
        month_label = datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B %Y")
    except Exception:
        month_label = month

    _header(c, pw, ph, left, right, htop, hh, title="Factuur")

    # ── Invoice meta block ────────────────────────────────────────────────────
    y = htop - hh - 14 * mm

    # Left: group billing details
    c.setFont(FONT_B, 12)
    _fill(c, DARK_NAVY)
    c.drawString(left, y, group.get("name", ""))
    y -= 16

    billing_lines = []
    if group.get("billing_address"):
        billing_lines.append(group["billing_address"])
    if group.get("billing_email"):
        billing_lines.append(group["billing_email"])
    if group.get("billing_reference"):
        billing_lines.append(f"Ref: {group['billing_reference']}")
    if group.get("contact_name"):
        billing_lines.append(f"t.a.v. {group['contact_name']}")

    for line in billing_lines:
        c.setFont(FONT_R, 9)
        _fill(c, BLACK)
        c.drawString(left, y, line)
        y -= 12

    # Right: invoice date & period
    rx = pw / 2 + 10 * mm
    ry = htop - hh - 14 * mm
    _label(c, "Factuurdatum", rx, ry)
    ry -= 14
    c.setFont(FONT_R, 10)
    _fill(c, BLACK)
    c.drawString(rx, ry, datetime.now().strftime("%d-%m-%Y"))
    ry -= 18
    _label(c, "Periode", rx, ry)
    ry -= 14
    c.setFont(FONT_B, 10)
    _fill(c, DARK_NAVY)
    c.drawString(rx, ry, month_label)

    # Separator
    y = min(y, ry) - 10 * mm
    _divider(c, y, left, right)
    y -= 8 * mm

    # ── Table header ──────────────────────────────────────────────────────────
    col_uid    = left
    col_driver = left + 55 * mm
    col_sess   = left + 105 * mm
    col_kwh    = left + 125 * mm
    col_cost   = right - 2 * mm

    c.saveState()
    _fill(c, DARK_NAVY)
    c.rect(left, y - 6, right - left, 18, fill=1, stroke=0)
    c.setFont(FONT_B, 8)
    _fill(c, WHITE)
    c.drawString(col_uid + 2,    y + 3, "TOKEN UID")
    c.drawString(col_driver + 2, y + 3, "DRIVER")
    c.drawString(col_sess + 2,   y + 3, "SESSIONS")
    c.drawString(col_kwh + 2,    y + 3, "kWh")
    c.drawRightString(col_cost,  y + 3, "BEDRAG")
    c.restoreState()
    y -= 14

    # ── Table rows ────────────────────────────────────────────────────────────
    cards = usage.get("cards", [])
    for i, card in enumerate(cards):
        _row_bg(c, y, pw, left, alt=(i % 2 == 0))

        uid = str(card.get("uid", ""))
        driver = str(card.get("driver_name") or card.get("label") or "—")
        sessions = int(card.get("sessions", 0))
        kwh = float(card.get("kwh", 0))
        cost = float(card.get("cost", 0))

        c.setFont("Courier", 8)
        _fill(c, DARK_NAVY)
        c.drawString(col_uid + 2, y, uid[:18])

        c.setFont(FONT_R, 9)
        _fill(c, BLACK)
        c.drawString(col_driver + 2, y, driver[:22])

        c.setFont(FONT_R, 9)
        c.drawString(col_sess + 2, y, str(sessions))

        c.drawString(col_kwh + 2, y, f"{kwh:.3f}".replace(".", ","))

        c.setFont(FONT_R, 9)
        c.drawRightString(col_cost, y, _euro(cost))

        y -= 16

        # Page break (leave room for footer)
        if y < 60 * mm:
            c.showPage()
            y = ph - 20 * mm

    # ── Totals block ──────────────────────────────────────────────────────────
    y -= 6
    _divider(c, y, left, right)
    y -= 14

    subtotal = usage.get("total_cost", 0.0)
    btw = subtotal * BTW_RATE
    total = subtotal + btw

    totals_x = col_kwh

    def _total_line(label, value, bold=False, color=BLACK):
        nonlocal y
        c.setFont(FONT_B if bold else FONT_R, 10)
        _fill(c, GRAY_T)
        c.drawString(totals_x, y, label)
        _fill(c, color)
        c.drawRightString(col_cost, y, _euro(value))
        y -= 15

    _total_line("Subtotaal (excl. BTW)", subtotal)
    _total_line(f"BTW {int(BTW_RATE*100)}%", btw)

    y -= 2
    # Total box
    c.saveState()
    _fill(c, DARK_NAVY)
    c.rect(totals_x - 4, y - 6, right - totals_x + 6, 22, fill=1, stroke=0)
    c.setFont(FONT_B, 12)
    _fill(c, WHITE)
    c.drawString(totals_x, y + 3, "TOTAAL INCL. BTW")
    c.drawRightString(col_cost, y + 3, _euro(total))
    c.restoreState()
    y -= 30

    # ── Summary stats ─────────────────────────────────────────────────────────
    y -= 4
    _divider(c, y, left, right)
    y -= 14

    stats = [
        ("Totaal sessies", str(usage.get("total_sessions", 0))),
        ("Totaal kWh", f"{usage.get('total_kwh', 0):.3f}".replace(".", ",")),
        ("Aantal kaarten", str(len(cards))),
    ]
    sx = left
    for lbl, val in stats:
        c.setFont(FONT_B, 8)
        _fill(c, GRAY_T)
        c.drawString(sx, y, lbl.upper())
        c.setFont(FONT_B, 10)
        _fill(c, DARK_NAVY)
        c.drawString(sx, y - 12, val)
        sx += 55 * mm

    # ── Footer ────────────────────────────────────────────────────────────────
    fy = 18 * mm
    _fill(c, DARK_NAVY)
    c.rect(0, 0, pw, fy - 2 * mm, fill=1, stroke=0)
    _fill(c, BLUE)
    c.rect(0, fy - 2 * mm, pw, 2, fill=1, stroke=0)
    c.setFont(FONT_R, 7)
    _fill(c, WHITE)
    c.drawCentredString(
        pw / 2, fy / 2 - 3,
        f"{os.getenv('OPERATOR_NAME', 'Your CPO')}  ·  {os.getenv('OPERATOR_EMAIL', 'info@example.com')}  ·  {os.getenv('OPERATOR_URL', 'https://example.com')}"
    )

    c.save()
    return buf.getvalue()
