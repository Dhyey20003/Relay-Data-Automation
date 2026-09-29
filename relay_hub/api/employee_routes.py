"""Employee directory, profile, edits, paste and Excel export."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import audit, db, employees, fields, sheets, timeutil
from ..errors import ValidationFailed
from ..security import Principal
from .deps import get_conn, get_ctx, require

router = APIRouter()


class EmployeeCreate(BaseModel):
    values: dict


class EmployeePatch(BaseModel):
    version: int
    changes: dict


class ArchiveBody(BaseModel):
    version: int
    last_working_date: str | None = None


class PasteBody(BaseModel):
    rows: list[dict]


class ExportBody(BaseModel):
    query: dict = {}
    columns: list[str] = []


def _query(view, search, location, employment_status, assignment_status, department, has_photo, filters, sort,
           direction, page, page_size) -> employees.ListQuery:
    try:
        flt = json.loads(filters) if filters else []
    except ValueError:
        raise ValidationFailed("Filters are not valid") from None
    return employees.ListQuery(view=view, search=search, location=location, employment_status=employment_status,
                               assignment_status=assignment_status, department=department, has_photo=has_photo,
                               filters=flt, sort=sort, direction=direction, page=page, page_size=page_size)


@router.get("/api/employees")
def list_employees(view: str = "active", search: str = "", location: str = "", employment_status: str = "",
                   assignment_status: str = "", department: str = "", has_photo: str = "", filters: str = "",
                   sort: str = "full_name", direction: str = "asc", page: int = 1, page_size: int = 50,
                   principal: Principal = Depends(require("employees.view")), conn=Depends(get_conn)):
    q = _query(view, search, location, employment_status, assignment_status, department, has_photo, filters, sort,
               direction, page, page_size)
    return employees.list_employees(conn, principal, q)


@router.get("/api/employees/lookup")
def lookup(q: str = Query("", max_length=100), include_left: bool = False,
           principal: Principal = Depends(require("employees.view")), conn=Depends(get_conn)):
    s = f"%{q.strip()}%"
    where = "(full_name LIKE ? OR rhc_id LIKE ? OR official_email LIKE ?)"
    if not include_left:
        where += " AND record_type='directory' AND employment_status != 'Left'"
    items = db.rows(conn, f"SELECT id, rhc_id, full_name, location, employment_status, record_type, current_photo_id "
                          f"FROM employees WHERE {where} ORDER BY full_name LIMIT 25", (s, s, s))
    return [i for i in items if principal.location_ok(i["location"])]


@router.post("/api/employees")
def create(body: EmployeeCreate, principal: Principal = Depends(require("employees.edit")), conn=Depends(get_conn)):
    emp = employees.create_employee(conn, principal, body.values)
    return employees.serialize(emp, principal, fields.all_fields(conn))


@router.get("/api/employees/{emp_id}")
def get_profile(emp_id: int, principal: Principal = Depends(require("employees.view")), conn=Depends(get_conn)):
    return employees.profile(conn, principal, emp_id)


@router.patch("/api/employees/{emp_id}")
def patch(emp_id: int, body: EmployeePatch, principal: Principal = Depends(require("employees.edit")),
          conn=Depends(get_conn)):
    emp = employees.update_employee(conn, principal, emp_id, body.changes, body.version)
    return employees.serialize(emp, principal, fields.all_fields(conn))


@router.post("/api/employees/{emp_id}/archive")
def archive(emp_id: int, body: ArchiveBody, principal: Principal = Depends(require("employees.archive")),
            conn=Depends(get_conn)):
    emp = employees.archive_employee(conn, principal, emp_id, body.version, body.last_working_date)
    return employees.serialize(emp, principal, fields.all_fields(conn))


@router.post("/api/employees/{emp_id}/reactivate")
def reactivate(emp_id: int, body: ArchiveBody, principal: Principal = Depends(require("employees.archive")),
               conn=Depends(get_conn)):
    emp = employees.restore_employee(conn, principal, emp_id, body.version)
    return employees.serialize(emp, principal, fields.all_fields(conn))


@router.post("/api/employees/paste/validate")
def paste_validate(body: PasteBody, principal: Principal = Depends(require("employees.edit")), conn=Depends(get_conn)):
    res = employees.bulk_validate(conn, principal, body.rows)
    for r in res["rows"]:
        r.pop("cleaned", None)
    return res


@router.post("/api/employees/paste/apply")
def paste_apply(body: PasteBody, principal: Principal = Depends(require("employees.edit")), conn=Depends(get_conn)):
    return employees.bulk_apply(conn, principal, body.rows)


@router.post("/api/employees/export")
def export(body: ExportBody, request_ctx=Depends(get_ctx), principal: Principal = Depends(require("employees.export")),
           conn=Depends(get_conn)):
    qd = body.query or {}
    q = employees.ListQuery(view=qd.get("view", "active"), search=qd.get("search", ""),
                            location=qd.get("location", ""), employment_status=qd.get("employment_status", ""),
                            assignment_status=qd.get("assignment_status", ""), department=qd.get("department", ""),
                            has_photo=qd.get("has_photo", ""), filters=qd.get("filters") or [],
                            sort=qd.get("sort", "full_name"), direction=qd.get("direction", "asc"))
    data = employees.list_employees(conn, principal, q, paginate=False)
    fdefs = [f for f in fields.all_fields(conn) if principal.field_visible(f.key) and f.key != "photo"]
    fm = {f.key: f for f in fdefs}
    cols = [c for c in (body.columns or [f.key for f in fdefs]) if c in fm]
    if not cols:
        raise ValidationFailed("Choose at least one column to export.")
    rows = []
    import datetime as dt
    for item in data["items"]:
        row = []
        for c in cols:
            v = item.get(c)
            if fm[c].type == "date" and isinstance(v, str) and len(v) == 10 and not v.startswith("--"):
                v = dt.date.fromisoformat(v)
            row.append(v)
        rows.append(row)
    stamp = timeutil.now_local().strftime("%Y%m%d_%H%M%S")
    out = request_ctx.paths.exports / "spreadsheets" / f"employees_{principal.id}_{stamp}.xlsx"
    sheets.write_xlsx(out, [("Employees", [fm[c].label for c in cols], rows)])
    with db.transaction(conn):
        audit.log(conn, principal.label, "export.employees", "employee", None,
                  f"Exported {len(rows)} employee rows to Excel", {"columns": cols, "query": qd})
    return FileResponse(out, filename=f"Relay_Employees_{stamp}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
