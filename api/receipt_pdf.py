"""
receipt_pdf.py — Branded PDF receipt generator for OCPP Core.

Generates a professional, VAT-compliant Dutch charging receipt.
Design: premium tech invoice (think Stripe / Fastned / Tesla).

Usage:
    pdf_bytes = generate_receipt_pdf(session_data)
    # session_data keys: id, cp_id, connector_id, kwh_delivered,
    #   rate_kwh, started_at, stopped_at, mollie_payment_id,
    #   address, city, display_name
"""

from __future__ import annotations

import io
import math
import os
from datetime import datetime, timezone
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

# ── Brand colours ────────────────────────────────────────────────────────────
BLUE        = (0 / 255,    176 / 255,  228 / 255)   # #00B0E4
GREEN       = (132 / 255,  189 / 255,   0 / 255)    # #84BD00
DARK_NAVY   = (12 / 255,   35 / 255,   64 / 255)    # #0C2340
WHITE       = (1.0, 1.0, 1.0)
BLACK       = (0.0, 0.0, 0.0)
GRAY_LIGHT  = (0.92, 0.92, 0.94)                    # subtle divider
GRAY_TEXT   = (0.45, 0.45, 0.50)                    # secondary text
BODY_BG     = (0.975, 0.975, 0.98)                  # very faint cool tint

# ── Typography ────────────────────────────────────────────────────────────────
FONT_REGULAR = "Helvetica"
FONT_BOLD    = "Helvetica-Bold"

# ── Logo path ─────────────────────────────────────────────────────────────────
_LOGO_PATH = os.path.join(
    os.path.dirname(__file__),
    "static/logo.png",
)
# Normalise (works even if __file__ is relative)
_LOGO_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "static/logo.png")
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rgb(c: canvas_type) -> None:  # type: ignore[name-defined]
    """Unused sentinel — real helper below."""


def _set_fill(c: pdf_canvas.Canvas, colour: tuple) -> None:
    c.setFillColorRGB(*colour)


def _set_stroke(c: pdf_canvas.Canvas, colour: tuple) -> None:
    c.setStrokeColorRGB(*colour)


def _fmt_euro(amount: float) -> str:
    """Format as Dutch euro: € 12,50"""
    return f"€ {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_kwh(val: float) -> str:
    return f"{val:.3f}".replace(".", ",")


def _fmt_rate(val: float) -> str:
    return f"€ {val:.4f}".replace(".", ",")


def _duration_str(start: datetime, stop: datetime) -> str:
    total_sec = int((stop - start).total_seconds())
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    if h:
        return f"{h} uur {m:02d} min"
    return f"{m} min"


# ── Decorative header helpers ─────────────────────────────────────────────────

def _draw_dot_pattern(c: pdf_canvas.Canvas, x: float, y: float,
                      w: float, h: float, rows: int = 6, cols: int = 14) -> None:
    """Subtle dot grid accent — white dots at low opacity in header."""
    dx = w / (cols + 1)
    dy = h / (rows + 1)
    radius = 0.9
    c.saveState()
    c.setFillColorRGB(1, 1, 1, alpha=0.10)
    for row in range(1, rows + 1):
        for col in range(1, cols + 1):
            cx = x + col * dx
            cy = y + row * dy
            c.circle(cx, cy, radius, fill=1, stroke=0)
    c.restoreState()


def _draw_header(c: pdf_canvas.Canvas, page_w: float, page_h: float,
                 left: float, right: float, header_top: float,
                 header_h: float) -> None:
    """Dark navy header bar with logo, dot pattern, and 'Laadbon' label."""
    # Dark background
    c.saveState()
    _set_fill(c, DARK_NAVY)
    c.rect(0, header_top - header_h, page_w, header_h, fill=1, stroke=0)

    # Dot accent pattern (right half of header, subtle)
    _draw_dot_pattern(c,
                      x=page_w * 0.45, y=header_top - header_h,
                      w=page_w * 0.55, h=header_h,
                      rows=5, cols=12)

    # Green left-edge stripe (3 px)
    _set_fill(c, GREEN)
    c.rect(0, header_top - header_h, 3, header_h, fill=1, stroke=0)

    # Logo — constrain to left half of header
    logo_max_w = (page_w / 2) - left - 10 * mm  # max half the page
    logo_target_h = 16 * mm
    logo_aspect = 1094 / 182  # original logo aspect ratio
    logo_target_w = min(logo_target_h * logo_aspect, logo_max_w)
    logo_target_h = logo_target_w / logo_aspect  # recalc if constrained
    logo_x = left
    logo_y = header_top - header_h / 2 - logo_target_h / 2
    try:
        reader = ImageReader(_LOGO_PATH)
        c.drawImage(reader,
                    logo_x, logo_y,
                    width=logo_target_w, height=logo_target_h,
                    mask="auto",
                    preserveAspectRatio=True)
    except Exception:
        c.setFont(FONT_BOLD, 16)
        _set_fill(c, WHITE)
        c.drawString(logo_x, header_top - header_h / 2 - 5, os.getenv("OPERATOR_NAME", "Your CPO"))

    # "Laadbon" label — right-aligned, vertically centered
    c.setFont(FONT_BOLD, 20)
    _set_fill(c, WHITE)
    c.drawRightString(right, header_top - header_h / 2 - 3, "Laadbon")

    # Blue accent line below header
    _set_fill(c, BLUE)
    c.rect(0, header_top - header_h - 3, page_w, 3, fill=1, stroke=0)

    c.restoreState()


def _draw_divider(c: pdf_canvas.Canvas, y: float, left: float, right: float,
                  colour: tuple = GRAY_LIGHT) -> None:
    c.saveState()
    _set_stroke(c, colour)
    c.setLineWidth(0.5)
    c.line(left, y, right, y)
    c.restoreState()


def _section_title(c: pdf_canvas.Canvas, text: str, x: float, y: float,
                   accent: bool = False) -> float:
    """Draw a section label. Returns y after drawing."""
    if accent:
        # Small blue pill / accent dot
        c.saveState()
        _set_fill(c, BLUE)
        c.rect(x - 10 * mm, y - 1, 3, 10, fill=1, stroke=0)
        c.restoreState()
    c.saveState()
    c.setFont(FONT_BOLD, 9)
    _set_fill(c, GRAY_TEXT)
    text_upper = text.upper()
    c.drawString(x, y, text_upper)
    c.restoreState()
    return y - 5 * mm


def _data_row(c: pdf_canvas.Canvas, label: str, value: str,
              y: float, left: float, right: float,
              bold_value: bool = False, large: bool = False) -> float:
    """Render a label→value row. Returns updated y position."""
    font_size = 10 if large else 9
    c.saveState()
    c.setFont(FONT_REGULAR, font_size)
    _set_fill(c, GRAY_TEXT)
    c.drawString(left, y, label)
    font = FONT_BOLD if bold_value else FONT_REGULAR
    c.setFont(font, font_size)
    _set_fill(c, BLACK)
    c.drawRightString(right, y, value)
    c.restoreState()
    return y - 6 * mm


def _amount_row(c: pdf_canvas.Canvas, label: str, value: str,
                y: float, left: float, right: float,
                total: bool = False) -> float:
    """Render a money row (amount table). Total row is bold + larger."""
    label_font = FONT_BOLD if total else FONT_REGULAR
    value_font = FONT_BOLD
    size = 11 if total else 9.5
    label_colour = BLACK if total else GRAY_TEXT
    value_colour = DARK_NAVY if total else BLACK

    c.saveState()
    c.setFont(label_font, size)
    _set_fill(c, label_colour)
    c.drawString(left, y, label)
    c.setFont(value_font, size)
    _set_fill(c, value_colour)
    c.drawRightString(right, y, value)
    c.restoreState()
    return y - (7 * mm if total else 5.5 * mm)


# ── Main generator ────────────────────────────────────────────────────────────

def generate_receipt_pdf(session: dict) -> bytes:
    """
    Build a VAT-compliant branded PDF receipt.

    session dict must include:
        id, cp_id, connector_id, kwh_delivered, rate_kwh,
        started_at (datetime), stopped_at (datetime),
        mollie_payment_id (str|None),
        display_name (str|None), address (str|None), city (str|None)
    """
    buf = io.BytesIO()
    PAGE_W, PAGE_H = A4
    LEFT  = 22 * mm
    RIGHT = PAGE_W - 22 * mm
    BODY_W = RIGHT - LEFT

    c = pdf_canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Laadbon")
    c.setAuthor(os.getenv("OPERATOR_NAME", "Your CPO"))
    c.setCreator("OCPP Core Receipt Generator")

    # ── Very faint full-page background tint ─────────────────────────────────
    c.saveState()
    _set_fill(c, BODY_BG)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.restoreState()

    # ── Header ────────────────────────────────────────────────────────────────
    HEADER_TOP = PAGE_H - 10 * mm
    HEADER_H   = 36 * mm
    _draw_header(c, PAGE_W, PAGE_H, LEFT, RIGHT, HEADER_TOP, HEADER_H)

    # ── Company info block (right-aligned, under header) ─────────────────────
    y = HEADER_TOP - HEADER_H - 7 * mm
    c.saveState()
    c.setFont(FONT_REGULAR, 8)
    _set_fill(c, GRAY_TEXT)
    lines = [
        os.getenv("OPERATOR_NAME", "Your CPO"),
        "KVK: —",
        "BTW: —",
        f"{os.getenv('OPERATOR_EMAIL', 'info@example.com')}  ·  {os.getenv('OPERATOR_URL', 'https://example.com')}",
    ]
    for line in lines:
        c.drawRightString(RIGHT, y, line)
        y -= 4.5 * mm
    c.restoreState()

    # ── Session details column (left) ────────────────────────────────────────
    y_details_start = HEADER_TOP - HEADER_H - 7 * mm

    # Extract & format data
    session_id       = session.get("id", "")
    cp_id            = session.get("cp_id", "—")
    connector_id     = session.get("connector_id", "—")
    display_name     = session.get("display_name") or cp_id
    address          = session.get("address") or ""
    city             = session.get("city") or ""
    location_str     = ", ".join(p for p in [address, city] if p) or cp_id

    started_at: Optional[datetime] = session.get("started_at")
    stopped_at:  Optional[datetime] = session.get("stopped_at")
    mollie_id        = session.get("mollie_payment_id") or "—"

    kwh   = float(session.get("kwh_delivered") or 0)
    rate  = float(session.get("rate_kwh") or 0)
    sub   = kwh * rate
    btw   = sub * 0.21
    total = sub + btw

    date_str  = started_at.strftime("%d-%m-%Y") if started_at else "—"
    start_str = started_at.strftime("%H:%M") if started_at else "—"
    stop_str  = stopped_at.strftime("%H:%M")  if stopped_at  else "—"
    dur_str   = _duration_str(started_at, stopped_at) if started_at and stopped_at else "—"
    ref_str   = session_id[:8].upper() if session_id else "—"

    # ── White content card ───────────────────────────────────────────────────
    CARD_TOP    = y_details_start + 2 * mm
    CARD_BOTTOM = 22 * mm
    CARD_H      = CARD_TOP - CARD_BOTTOM

    c.saveState()
    _set_fill(c, WHITE)
    _set_stroke(c, GRAY_LIGHT)
    c.setLineWidth(0.5)
    c.roundRect(LEFT - 4 * mm, CARD_BOTTOM, BODY_W + 8 * mm, CARD_H,
                radius=3 * mm, fill=1, stroke=1)
    c.restoreState()

    INNER_LEFT  = LEFT + 4 * mm
    INNER_RIGHT = RIGHT - 4 * mm

    y = CARD_TOP - 10 * mm

    # ── Section: Sessie Details ───────────────────────────────────────────────
    y = _section_title(c, "Sessie Details", INNER_LEFT, y, accent=True)
    y -= 2 * mm

    y = _data_row(c, "Sessie-ID",   ref_str,       y, INNER_LEFT, INNER_RIGHT)
    y = _data_row(c, "Datum",       date_str,       y, INNER_LEFT, INNER_RIGHT)
    y = _data_row(c, "Starttijd",   start_str,      y, INNER_LEFT, INNER_RIGHT)
    y = _data_row(c, "Eindtijd",    stop_str,       y, INNER_LEFT, INNER_RIGHT)
    y = _data_row(c, "Duur",        dur_str,        y, INNER_LEFT, INNER_RIGHT)
    y = _data_row(c, "Lader",       display_name,   y, INNER_LEFT, INNER_RIGHT)
    y = _data_row(c, "Connector",   str(connector_id), y, INNER_LEFT, INNER_RIGHT)
    y = _data_row(c, "Locatie",     location_str,   y, INNER_LEFT, INNER_RIGHT)

    y -= 4 * mm
    _draw_divider(c, y, INNER_LEFT, INNER_RIGHT)
    y -= 6 * mm

    # ── Section: Kosten ───────────────────────────────────────────────────────
    y = _section_title(c, "Kosten", INNER_LEFT, y, accent=True)
    y -= 2 * mm

    y = _amount_row(c, "Geleverde energie",
                    f"{_fmt_kwh(kwh)} kWh",
                    y, INNER_LEFT, INNER_RIGHT)
    y = _amount_row(c, "Tarief per kWh",
                    _fmt_rate(rate),
                    y, INNER_LEFT, INNER_RIGHT)
    y -= 2 * mm
    _draw_divider(c, y, INNER_LEFT, INNER_RIGHT)
    y -= 5 * mm

    y = _amount_row(c, "Subtotaal (excl. BTW)",
                    _fmt_euro(sub),
                    y, INNER_LEFT, INNER_RIGHT)
    y = _amount_row(c, "BTW 21%",
                    _fmt_euro(btw),
                    y, INNER_LEFT, INNER_RIGHT)

    y -= 2 * mm
    # Stronger divider before total
    c.saveState()
    _set_stroke(c, DARK_NAVY)
    c.setLineWidth(1.0)
    c.line(INNER_LEFT, y, INNER_RIGHT, y)
    c.restoreState()
    y -= 5 * mm

    y = _amount_row(c, "Totaal betaald",
                    _fmt_euro(total),
                    y, INNER_LEFT, INNER_RIGHT,
                    total=True)

    y -= 4 * mm
    _draw_divider(c, y, INNER_LEFT, INNER_RIGHT)
    y -= 6 * mm

    # ── Section: Betaling ─────────────────────────────────────────────────────
    y = _section_title(c, "Betaling", INNER_LEFT, y, accent=True)
    y -= 2 * mm

    y = _data_row(c, "Betaalmethode",  "iDEAL",              y, INNER_LEFT, INNER_RIGHT)
    y = _data_row(c, "Status",         "Betaald",             y, INNER_LEFT, INNER_RIGHT,
                  bold_value=True)
    y = _data_row(c, "Referentie",     mollie_id,             y, INNER_LEFT, INNER_RIGHT)

    # ── Blue accent bar at bottom of card ─────────────────────────────────────
    c.saveState()
    _set_fill(c, BLUE)
    c.rect(LEFT - 4 * mm,
           CARD_BOTTOM,
           BODY_W + 8 * mm,
           2.5,
           fill=1, stroke=0)
    c.restoreState()

    # ── Footer ────────────────────────────────────────────────────────────────
    c.saveState()
    c.setFont(FONT_REGULAR, 7.5)
    _set_fill(c, GRAY_TEXT)
    footer_y = 10 * mm
    c.drawCentredString(
        PAGE_W / 2, footer_y,
        f"Dit document is automatisch gegenereerd door {os.getenv('OPERATOR_NAME', 'Your CPO')}  ·  "
        f"{os.getenv('OPERATOR_URL', 'https://example.com')}  ·  {os.getenv('OPERATOR_EMAIL', 'info@example.com')}",
    )
    c.restoreState()

    c.save()
    return buf.getvalue()
