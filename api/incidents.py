"""
Incidents API endpoints.

CRUD for commercial.incidents and commercial.incident_events.
Replaces direct-DB access in the client portal (routes/incidents.py).

Table: commercial.incidents
Columns inferred from portal usage:
  id              SERIAL PRIMARY KEY
  title           TEXT NOT NULL
  description     TEXT
  charge_point    TEXT (nullable FK to ocpp.charge_points)
  priority        TEXT ('high'/'medium'/'low')
  status          TEXT ('open'/'in_progress'/'resolved')
  reported_by     TEXT
  created_at      TIMESTAMPTZ DEFAULT NOW()
  updated_at      TIMESTAMPTZ DEFAULT NOW()

Table: commercial.incident_events
  id              SERIAL PRIMARY KEY
  incident_id     INT REFERENCES commercial.incidents(id)
  event_type      TEXT ('created'/'status_change'/'comment')
  content         TEXT
  author          TEXT
  created_at      TIMESTAMPTZ DEFAULT NOW()
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request models ─────────────────────────────────────────────────────────

class CreateIncidentRequest(BaseModel):
    title: str
    description: str = ""
    charge_point: Optional[str] = None
    priority: str = "medium"
    reported_by: str = ""


class PatchIncidentRequest(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    note: Optional[str] = None          # if provided, a comment event is added
    author: Optional[str] = "api"


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("")
async def list_incidents(
    status: str = Query(None, description="Filter by status: open/in_progress/resolved"),
    priority: str = Query(None, description="Filter by priority: high/medium/low"),
    charge_point: str = Query(None, description="Filter by charge point ID"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List incidents with optional filters."""
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if priority:
        conditions.append(f"priority = ${idx}")
        params.append(priority)
        idx += 1
    if charge_point:
        conditions.append(f"charge_point = ${idx}")
        params.append(charge_point)
        idx += 1

    params.extend([offset, limit])

    async with db.read() as conn:
        rows = await conn.fetch(f"""
            SELECT id, title, description, charge_point, priority, status,
                   reported_by, created_at, updated_at
            FROM commercial.incidents
            WHERE {' AND '.join(conditions)}
            ORDER BY
                CASE status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                created_at DESC
            OFFSET ${idx} LIMIT ${idx + 1}
        """, *params)

        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM commercial.incidents WHERE {' AND '.join(conditions)}
        """, *params[:-2])

    incidents = []
    for r in rows:
        item = dict(r)
        item["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        item["updated_at"] = r["updated_at"].isoformat() if r["updated_at"] else None
        incidents.append(item)

    return {"incidents": incidents, "total": total, "offset": offset, "limit": limit}


@router.post("", status_code=201)
async def create_incident(req: CreateIncidentRequest):
    """Create a new incident and log a creation event."""
    async with db.write() as conn:
        row = await conn.fetchrow("""
            INSERT INTO commercial.incidents
                (title, description, charge_point, priority, reported_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, title, status, priority, created_at
        """, req.title, req.description, req.charge_point or None,
            req.priority, req.reported_by)

        await conn.execute("""
            INSERT INTO commercial.incident_events
                (incident_id, event_type, content, author)
            VALUES ($1, 'created', $2, $3)
        """, row["id"], f"Incident aangemaakt: {req.title}", req.reported_by or "api")

    logger.info("Created incident id=%s title=%r", row["id"], req.title)
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "priority": row["priority"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/{incident_id}")
async def get_incident(incident_id: int):
    """Get incident detail including event timeline."""
    async with db.read() as conn:
        incident = await conn.fetchrow(
            "SELECT * FROM commercial.incidents WHERE id = $1", incident_id
        )
        if not incident:
            raise HTTPException(404, f"Incident {incident_id} not found")

        events = await conn.fetch("""
            SELECT id, event_type, content, author, created_at
            FROM commercial.incident_events
            WHERE incident_id = $1
            ORDER BY created_at ASC
        """, incident_id)

    result = dict(incident)
    result["created_at"] = incident["created_at"].isoformat() if incident["created_at"] else None
    result["updated_at"] = incident["updated_at"].isoformat() if incident["updated_at"] else None
    result["events"] = [
        {
            **dict(e),
            "created_at": e["created_at"].isoformat() if e["created_at"] else None,
        }
        for e in events
    ]
    return result


@router.patch("/{incident_id}")
async def patch_incident(incident_id: int, req: PatchIncidentRequest):
    """Update incident status/priority/title/description and optionally add a note."""
    async with db.write() as conn:
        incident = await conn.fetchrow(
            "SELECT id, status FROM commercial.incidents WHERE id = $1", incident_id
        )
        if not incident:
            raise HTTPException(404, f"Incident {incident_id} not found")

        # Build dynamic SET clause
        sets: list[str] = ["updated_at = NOW()"]
        params: list = []
        idx = 1

        col_map = {
            "status": req.status,
            "priority": req.priority,
            "title": req.title,
            "description": req.description,
        }
        for col, val in col_map.items():
            if val is not None:
                sets.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1

        params.append(incident_id)
        await conn.execute(
            f"UPDATE commercial.incidents SET {', '.join(sets)} WHERE id = ${idx}",
            *params,
        )

        # Log status-change event
        if req.status and req.status != incident["status"]:
            status_labels = {
                "open": "Open",
                "in_progress": "In behandeling",
                "resolved": "Opgelost",
            }
            content = req.note or f"Status gewijzigd naar: {status_labels.get(req.status, req.status)}"
            await conn.execute("""
                INSERT INTO commercial.incident_events
                    (incident_id, event_type, content, author)
                VALUES ($1, 'status_change', $2, $3)
            """, incident_id, content, req.author or "api")
        elif req.note:
            # Plain comment note without status change
            await conn.execute("""
                INSERT INTO commercial.incident_events
                    (incident_id, event_type, content, author)
                VALUES ($1, 'comment', $2, $3)
            """, incident_id, req.note, req.author or "api")

    logger.info("Patched incident id=%s", incident_id)
    return {"id": incident_id, "status": "updated"}
