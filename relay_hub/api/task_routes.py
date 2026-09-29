"""Headshot tasks, periods, packages, email dispatch and Outlook status."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import audit, db, dispatch, packages, tasks, timeutil
from ..errors import Forbidden, NotFound, ValidationFailed
from ..security import Principal
from .deps import client_ip, get_conn, get_ctx, require

router = APIRouter()


class AutoSendBody(BaseModel):
    enable: bool
    confirm: bool = False


class SendBody(BaseModel):
    confirm: bool = False
    allow_incomplete: bool = False


class ManualBody(BaseModel):
    note: str
    packages_sent: bool = True


class ResolveBody(BaseModel):
    action: str
    note: str = ""


def _task_summary(conn, task: dict) -> dict:
    t = dict(task)
    t["upcoming"] = tasks.upcoming(task, 3) if task["schedule"] else []
    t["auto_send_blockers"] = tasks.auto_send_blockers(conn, task)
    t["open_periods"] = db.rows(conn, "SELECT * FROM task_periods WHERE task_id=? AND status NOT IN (?,?) "
                                      "ORDER BY due_at", (task["id"], tasks.CONFIRMED, tasks.MANUAL))
    now = timeutil.iso_utc()
    for p in t["open_periods"]:
        p["overdue"] = p["due_at"] < now
    t["recent_periods"] = db.rows(conn, "SELECT * FROM task_periods WHERE task_id=? ORDER BY due_at DESC LIMIT 12",
                                  (task["id"],))
    last = db.one(conn, "SELECT * FROM task_periods WHERE task_id=? AND status IN (?,?) ORDER BY due_at DESC LIMIT 1",
                  (task["id"], tasks.CONFIRMED, tasks.MANUAL))
    t["last_completed"] = last
    t["baseline_count"] = db.scalar(conn, "SELECT COUNT(*) FROM export_baseline WHERE task_id=?", (task["id"],))
    return t


@router.get("/api/tasks")
def list_tasks(principal: Principal = Depends(require("tasks.view")), conn=Depends(get_conn)):
    return {"tasks": [_task_summary(conn, tasks.decode_task(t)) for t in db.rows(conn, "SELECT * FROM tasks ORDER BY id")],
            "statuses": tasks.STATUSES, "template_fields": tasks.TEMPLATE_FIELDS,
            "naming_placeholders": packages.PLACEHOLDERS, "now": timeutil.iso_utc()}


@router.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: dict, principal: Principal = Depends(require("tasks.configure")),
                conn=Depends(get_conn)):
    return _task_summary(conn, tasks.update_task(conn, principal, task_id, body))


@router.post("/api/tasks/{task_id}/auto-send")
def auto_send(task_id: int, body: AutoSendBody, principal: Principal = Depends(require("tasks.configure")),
              conn=Depends(get_conn)):
    return _task_summary(conn, tasks.set_auto_send(conn, principal, task_id, body.enable, body.confirm))


@router.post("/api/tasks/{task_id}/start-period")
def start_period(task_id: int, principal: Principal = Depends(require("tasks.operate")), conn=Depends(get_conn)):
    return {"period_id": tasks.start_period_now(conn, principal, task_id)}


@router.get("/api/tasks/{task_id}/periods")
def periods(task_id: int, limit: int = 50, principal: Principal = Depends(require("tasks.view")),
            conn=Depends(get_conn)):
    return db.rows(conn, "SELECT * FROM task_periods WHERE task_id=? ORDER BY due_at DESC LIMIT ?",
                   (task_id, min(limit, 500)))


@router.get("/api/periods/{period_id}")
def period_detail(period_id: int, principal: Principal = Depends(require("tasks.view")), conn=Depends(get_conn)):
    period = tasks.get_period(conn, period_id)
    task = tasks.load_task(conn, period["task_id"])
    pkgs = db.rows(conn, "SELECT * FROM packages WHERE period_id=? ORDER BY location, revision DESC", (period_id,))
    current_ids = {p["id"] for p in tasks.current_packages(conn, period_id)}
    for p in pkgs:
        p["current"] = p["id"] in current_ids
        p["problem"] = packages.verify(p) if p["current"] else None
        m = packages.read_manifest(p)
        p["exclusions"] = m.get("exclusions", [])
        p["unchanged"] = m.get("counts", {}).get("unchanged_not_included", 0)
        p["renamed"] = [e for e in m.get("employees", []) if e.get("renamed_from")]
        p["allowed"] = principal.location_ok(p["location"])
        p.pop("zip_path", None)
        p.pop("manifest_path", None)
    ds = db.rows(conn, "SELECT id, dispatch_ref, mode, adapter, status, automatic, sender, to_addrs, cc_addrs, subject, "
                       "attachments, total_bytes, created_at, created_by, submitted_at, confirmed_at, resolved_at, "
                       "resolved_by, error, notes FROM dispatches WHERE period_id=? ORDER BY id DESC", (period_id,))
    for d in ds:
        d["to_addrs"] = db.jload(d["to_addrs"], [])
        d["cc_addrs"] = db.jload(d["cc_addrs"], [])
        d["attachments"] = [{k: v for k, v in a.items() if k != "path"} for a in db.jload(d["attachments"], [])]
    return {"period": period, "task": {k: task[k] for k in ("id", "kind", "name", "inclusion", "locations",
                                                           "max_attachment_mb")},
            "packages": pkgs, "dispatches": ds, "active_dispatch": tasks.active_dispatch(conn, period_id)}


@router.post("/api/periods/{period_id}/prepare")
def prepare(period_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(require("tasks.operate")),
            conn=Depends(get_conn)):
    return tasks.prepare(conn, ctx.paths, principal.label, period_id, principal=principal)


@router.get("/api/periods/{period_id}/email-preview")
def email_preview(period_id: int, principal: Principal = Depends(require("tasks.view")), conn=Depends(get_conn)):
    return dispatch.preview(conn, principal, period_id)


@router.post("/api/periods/{period_id}/draft")
def create_draft(period_id: int, body: SendBody, ctx=Depends(get_ctx),
                 principal: Principal = Depends(require("tasks.operate")), conn=Depends(get_conn)):
    return dispatch.send(conn, ctx.paths, ctx.adapter(conn), period_id, "draft", principal.label, principal,
                         allow_incomplete=body.allow_incomplete)


@router.post("/api/periods/{period_id}/send")
def send_now(period_id: int, body: SendBody, ctx=Depends(get_ctx),
             principal: Principal = Depends(require("tasks.operate")), conn=Depends(get_conn)):
    if not body.confirm:
        raise ValidationFailed("Confirm that the email should be sent now.")
    return dispatch.send(conn, ctx.paths, ctx.adapter(conn), period_id, "send", principal.label, principal,
                         allow_incomplete=body.allow_incomplete)


@router.post("/api/periods/{period_id}/manual-complete")
def manual_complete(period_id: int, body: ManualBody, principal: Principal = Depends(require("tasks.operate")),
                    conn=Depends(get_conn)):
    return tasks.mark_manual(conn, principal, period_id, body.note, body.packages_sent)


@router.post("/api/periods/{period_id}/ready-folder")
def ready_folder(period_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(require("tasks.operate")),
                 conn=Depends(get_conn)):
    return dispatch.ready_folder(conn, ctx.paths, principal, period_id)


@router.post("/api/periods/{period_id}/open-folder")
def open_folder(period_id: int, request: Request, ctx=Depends(get_ctx),
                principal: Principal = Depends(require("tasks.operate")), conn=Depends(get_conn)):
    if client_ip(request) not in ("127.0.0.1", "::1"):
        raise Forbidden("Folders can only be opened from the computer running the application.")
    period = tasks.get_period(conn, period_id)
    task = tasks.load_task(conn, period["task_id"])
    folder = ctx.paths.exports / "ready" / f"{task['kind']}_{period['period_key']}"
    return {"opened": dispatch.open_folder(str(folder)), "folder": str(folder)}


@router.post("/api/dispatches/{dispatch_id}/resolve")
def resolve(dispatch_id: int, body: ResolveBody, ctx=Depends(get_ctx),
            principal: Principal = Depends(require("tasks.operate")), conn=Depends(get_conn)):
    return dispatch.resolve(conn, ctx.adapter(conn), principal, dispatch_id, body.action, body.note)


@router.post("/api/dispatches/{dispatch_id}/reconcile")
def reconcile(dispatch_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(require("tasks.operate")),
              conn=Depends(get_conn)):
    return dispatch.reconcile(conn, ctx.adapter(conn), dispatch_id, principal.label)


def _package(conn, principal, package_id):
    p = db.one(conn, "SELECT * FROM packages WHERE id=?", (package_id,))
    if not p:
        raise NotFound("Package not found")
    if not principal.location_ok(p["location"]):
        raise Forbidden("This package is outside your location scope.")
    return p


@router.get("/api/packages/{package_id}/zip")
def package_zip(package_id: int, principal: Principal = Depends(require("tasks.view")), conn=Depends(get_conn)):
    if not principal.can("tasks.operate") and not principal.can("employees.export"):
        raise Forbidden("Downloading packages requires export or task permission.")
    p = _package(conn, principal, package_id)
    if not p["zip_path"] or not Path(p["zip_path"]).exists():
        raise NotFound("This package has no ZIP file (no photos were included) or the file is missing.")
    issue = packages.verify(p)
    if issue:
        raise ValidationFailed(issue)
    with db.transaction(conn):
        audit.log(conn, principal.label, "export.package", "package", package_id, f"Downloaded {p['zip_name']}")
    return FileResponse(p["zip_path"], filename=p["zip_name"], media_type="application/zip")


@router.get("/api/packages/{package_id}/manifest")
def package_manifest(package_id: int, fmt: str = "json", principal: Principal = Depends(require("tasks.view")),
                     conn=Depends(get_conn)):
    p = _package(conn, principal, package_id)
    path = Path(p["manifest_path"])
    if fmt == "csv":
        path = path.with_suffix(".csv")
    if not path.exists():
        raise NotFound("Manifest file missing")
    return FileResponse(path, filename=path.name)


@router.get("/api/outlook/status")
def outlook_status(ctx=Depends(get_ctx), principal: Principal = Depends(require("tasks.view")),
                   conn=Depends(get_conn)):
    adapter = ctx.adapter(conn)
    st = adapter.status()
    st["adapter_label"] = adapter.label
    return st


@router.get("/api/dashboard")
def dashboard(principal: Principal = Depends(require("employees.view")), conn=Depends(get_conn)):
    loc_clause, params = "", []
    if principal.locations is not None:
        loc_clause = " AND location IN (%s)" % ",".join("?" * len(principal.locations))
        params = list(principal.locations)
    active = db.scalar(conn, "SELECT COUNT(*) FROM employees WHERE record_type='directory' AND "
                             "employment_status='Active'" + loc_clause, params)
    pending = db.scalar(conn, "SELECT COUNT(*) FROM employees WHERE record_type='directory' AND "
                              "employment_status='Pending Review'" + loc_clause, params)
    no_photo = db.scalar(conn, "SELECT COUNT(*) FROM employees WHERE record_type='directory' AND "
                               "employment_status='Active' AND current_photo_id IS NULL" + loc_clause, params)
    out = {"active": active, "pending_review": pending, "missing_photos": no_photo}
    if principal.can("tasks.view"):
        now = timeutil.iso_utc()
        out["attention"] = db.rows(conn, "SELECT p.id, p.period_label, p.status, p.status_note, p.due_at, t.name, "
                                         "t.kind FROM task_periods p JOIN tasks t ON t.id=p.task_id WHERE p.status "
                                         "NOT IN (?,?) ORDER BY p.due_at", (tasks.CONFIRMED, tasks.MANUAL))
        for a in out["attention"]:
            a["overdue"] = a["due_at"] < now
    return out
