"""Audit trail for changes, imports, exports, account changes and task actions."""
from __future__ import annotations

from . import db, timeutil


def log(conn, actor: str | None, action: str, entity_type: str | None = None, entity_id=None,
        summary: str | None = None, details=None, ip: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, entity_type, entity_id, summary, details, ip) VALUES(?,?,?,?,?,?,?,?)",
        (timeutil.iso_utc(), actor, action, entity_type, None if entity_id is None else str(entity_id), summary,
         db.jdump(details) if details is not None else None, ip))


def query(conn, action: str | None = None, actor: str | None = None, entity_type: str | None = None,
          entity_id: str | None = None, search: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    where, params = [], []
    if action:
        where.append("action LIKE ?")
        params.append(action + "%")
    if actor:
        where.append("actor LIKE ?")
        params.append(f"%{actor}%")
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    if entity_id:
        where.append("entity_id = ?")
        params.append(str(entity_id))
    if search:
        where.append("(summary LIKE ? OR details LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.scalar(conn, f"SELECT COUNT(*) FROM audit_log {clause}", params)
    items = db.rows(conn, f"SELECT * FROM audit_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                    params + [min(limit, 1000), offset])
    for it in items:
        it["details"] = db.jload(it["details"])
    return {"total": total, "items": items}
