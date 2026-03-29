"""
Session management API endpoints.
"""
import logging
from datetime import date as date_type

from fastapi import APIRouter, HTTPException, Query

from state.postgres import db
from state.redis import redis_state

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_sessions(
    status: str = Query(None, description="Filter: active/completed/failed"),
    charge_point: str = Query(None),
    auth_id: str = Query(None),
    simulated: bool = Query(None),
    date_from: str = Query(None, description="Filter from date (YYYY-MM-DD)"),
    date_to: str = Query(None, description="Filter to date (YYYY-MM-DD)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List charging sessions with filters."""
    conditions = ["1=1"]
    params = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if charge_point:
        conditions.append(f"charge_point = ${idx}")
        params.append(charge_point)
        idx += 1
    if auth_id:
        conditions.append(f"auth_id = ${idx}")
        params.append(auth_id)
        idx += 1
    if simulated is not None:
        conditions.append(f"simulated = ${idx}")
        params.append(simulated)
        idx += 1
    if date_from:
        conditions.append(f"start_time >= ${idx}::date")
        params.append(date_from)
        idx += 1
    if date_to:
        conditions.append(f"start_time < (${idx}::date + INTERVAL '1 day')")
        params.append(date_to)
        idx += 1

    params.extend([offset, limit])

    async with db.read() as conn:
        sessions = await conn.fetch(f"""
            SELECT id, charge_point, connector_id, transaction_id, status,
                   auth_method, auth_id, start_time, stop_time, stop_reason,
                   energy_kwh, peak_power_kw, start_soc, end_soc, simulated
            FROM ocpp.sessions
            WHERE {' AND '.join(conditions)}
            ORDER BY start_time DESC
            OFFSET ${idx} LIMIT ${idx + 1}
        """, *params)

        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM ocpp.sessions WHERE {' AND '.join(conditions)}
        """, *params[:-2])

    # Enrich active sessions with live data from Redis
    result = []
    for s in sessions:
        session_data = dict(s)
        session_data["start_time"] = s["start_time"].isoformat() if s["start_time"] else None
        session_data["stop_time"] = s["stop_time"].isoformat() if s["stop_time"] else None
        session_data["id"] = str(s["id"])

        if s["status"] == "active":
            live = await redis_state.get_session(str(s["id"]))
            session_data["live"] = live or {}

        result.append(session_data)

    return {"sessions": result, "total": total, "offset": offset, "limit": limit}


# ── Stats endpoints MUST be defined before /{session_id} ─────────────────
# FastAPI matches routes in order; a literal path like /stats/today must
# appear before the /{session_id} wildcard or it will be swallowed.

@router.get("/stats/today")
async def session_stats_today():
    """Today's energy (kWh) and session count."""
    today = date_type.today().isoformat()
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT
                COALESCE(SUM(energy_kwh) FILTER (WHERE status = 'completed'), 0) AS energy_kwh,
                COUNT(*) AS session_count
            FROM ocpp.sessions
            WHERE simulated = FALSE
              AND start_time >= CURRENT_DATE
              AND start_time < CURRENT_DATE + INTERVAL '1 day'
        """)
    return {
        "energy_kwh": float(row["energy_kwh"]) if row else 0.0,
        "session_count": int(row["session_count"]) if row else 0,
        "date": today,
    }


@router.get("/stats/summary")
async def session_stats():
    """Aggregate session statistics including avg_duration_min."""
    async with db.read() as conn:
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) as total_sessions,
                COUNT(*) FILTER (WHERE status = 'active') as active,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COALESCE(SUM(energy_kwh) FILTER (WHERE status = 'completed'), 0) as total_energy_kwh,
                COALESCE(AVG(energy_kwh) FILTER (WHERE status = 'completed'), 0) as avg_energy_kwh,
                COALESCE(MAX(peak_power_kw), 0) as max_power_kw,
                COUNT(DISTINCT charge_point) as chargers_used,
                COALESCE(
                    AVG(
                        EXTRACT(EPOCH FROM (stop_time - start_time)) / 60.0
                    ) FILTER (WHERE status = 'completed' AND stop_time IS NOT NULL),
                    0
                ) as avg_duration_min
            FROM ocpp.sessions
            WHERE simulated = FALSE
        """)

    result = dict(stats) if stats else {}
    # Ensure float for JSON serialisation
    for key in ("total_energy_kwh", "avg_energy_kwh", "max_power_kw", "avg_duration_min"):
        if key in result:
            result[key] = float(result[key])
    return result


@router.get("/stats/daily")
async def session_stats_daily(
    days: int = Query(7, ge=1, le=90, description="Number of days to include"),
):
    """Daily energy and session counts for the last N days."""
    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT
                TO_CHAR(DATE_TRUNC('day', start_time), 'YYYY-MM-DD') AS date,
                COALESCE(SUM(energy_kwh) FILTER (WHERE status = 'completed'), 0) AS energy_kwh,
                COUNT(*) AS sessions
            FROM ocpp.sessions
            WHERE simulated = FALSE
              AND start_time >= CURRENT_DATE - ($1 - 1) * INTERVAL '1 day'
              AND start_time < CURRENT_DATE + INTERVAL '1 day'
            GROUP BY DATE_TRUNC('day', start_time)
            ORDER BY DATE_TRUNC('day', start_time) ASC
        """, days)
    return {
        "days": [
            {
                "date": r["date"],
                "energy_kwh": float(r["energy_kwh"]),
                "sessions": int(r["sessions"]),
            }
            for r in rows
        ]
    }


# ── Per-session endpoints ─────────────────────────────────────────────────

@router.get("/{session_id}")
async def get_session(session_id: str):
    """Get a specific session with meter history."""
    async with db.read() as conn:
        session = await conn.fetchrow(
            "SELECT * FROM ocpp.sessions WHERE id::text = $1", session_id
        )
        if not session:
            raise HTTPException(404, f"Session {session_id} not found")

        # Get CDR if completed
        cdr = await conn.fetchrow(
            "SELECT * FROM ocpp.cdrs WHERE session_id::text = $1", session_id
        )

    # Live state if active
    live = None
    if session["status"] == "active":
        live = await redis_state.get_session(session_id)

    return {
        "session": dict(session),
        "cdr": dict(cdr) if cdr else None,
        "live": live,
    }


@router.get("/{session_id}/meter")
async def session_meter_values(
    session_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
):
    """Get meter values for a session (from TimescaleDB)."""
    async with db.read() as conn:
        values = await conn.fetch("""
            SELECT time, energy_kwh, power_kw, soc_pct, voltage_v, current_a, temperature_c
            FROM ocpp.meter_values
            WHERE session_id::text = $1
            ORDER BY time ASC
            OFFSET $2 LIMIT $3
        """, session_id, offset, limit)

    return {
        "meter_values": [
            {
                "time": v["time"].isoformat(),
                "energy_kwh": v["energy_kwh"],
                "power_kw": v["power_kw"],
                "soc_pct": v["soc_pct"],
                "voltage_v": v["voltage_v"],
                "current_a": v["current_a"],
                "temperature_c": v["temperature_c"],
            }
            for v in values
        ],
        "count": len(values),
    }
