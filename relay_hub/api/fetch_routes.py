"""Read-only local integration API for fetch tokens (no interactive UI access).

Authentication: "Authorization: Bearer rhcf_..." with a token created by an
administrator. Tokens are stored hashed, can be revoked, are limited to their
locations and fields, and never receive restricted fields (such as date of birth).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from .. import db, employees, fields
from ..errors import Forbidden, NotFound
from ..security import Principal
from .deps import get_conn, get_ctx, get_token_principal

router = APIRouter()


@router.get("/api/fetch/v1/employees")
def fetch_employees(location: str = "", updated_since: str = "", principal: Principal = Depends(get_token_principal),
                    conn=Depends(get_conn)):
    where = ["record_type='directory'"]
    params: list = []
    if not principal.include_left:
        where.append("employment_status != 'Left'")
    if principal.locations is not None:
        where.append("location IN (%s)" % ",".join("?" * len(principal.locations)))
        params += principal.locations
    if location:
        where.append("location = ?")
        params.append(location)
    if updated_since:
        where.append("updated_at >= ?")
        params.append(updated_since)
    fdefs = fields.all_fields(conn)
    items = db.rows(conn, f"SELECT * FROM employees WHERE {' AND '.join(where)} ORDER BY rhc_id", params)
    out = []
    for e in items:
        rec = employees.serialize(e, principal, fdefs)
        rec.pop("version", None)
        if principal.include_photos and e["current_photo_id"]:
            rec["photo_url"] = f"/api/fetch/v1/photos/{e['current_photo_id']}"
        rec.pop("photo", None)
        out.append(rec)
    return {"count": len(out), "fields": sorted(principal.allowed_fields or []), "employees": out}


@router.get("/api/fetch/v1/photos/{photo_id}")
def fetch_photo(photo_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(get_token_principal),
                conn=Depends(get_conn)):
    if not principal.include_photos:
        raise Forbidden("This token cannot read photos.")
    p = db.one(conn, "SELECT p.*, e.location, e.employment_status FROM photos p JOIN employees e ON "
                     "e.id=p.employee_id WHERE p.id=? AND p.is_current=1", (photo_id,))
    if not p or not principal.location_ok(p["location"]) or \
            (p["employment_status"] == "Left" and not principal.include_left):
        raise NotFound("Photo not found")
    return FileResponse(ctx.paths.images / p["stored_path"])
