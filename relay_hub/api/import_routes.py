"""Import wizard endpoints, history, templates, error reports and undo."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import db, imports, settings_store
from ..config import resource_dir
from ..errors import NotFound
from ..security import Principal
from .deps import cleanup, get_conn, get_ctx, require, save_upload

router = APIRouter()


class PreviewBody(BaseModel):
    config: dict
    decisions: dict = {}


class CommitBody(BaseModel):
    config: dict
    decisions: dict = {}
    deactivate_ids: list[int] = []
    confirm_deactivation: bool = False
    acknowledge_repeat: bool = False
    last_working_date: str | None = None


class TemplateBody(BaseModel):
    id: int | None = None
    name: str
    purpose: str
    sheet: str | None = None
    header_row: int = 1
    mapping: dict = {}
    options: dict = {}
    scope: list[str] = []
    overwrite: bool = False


@router.get("/api/imports/meta")
def meta(principal: Principal = Depends(require("imports.run")), conn=Depends(get_conn)):
    return {"purposes": imports.PURPOSES, "targets": imports.mapping_targets(conn),
            "locations": settings_store.locations(conn),
            "historical_enrich_fields": settings_store.get(conn, "historical_enrich_fields"),
            "can_roster": principal.can("imports.roster"), "can_undo": principal.can("imports.undo")}


@router.post("/api/imports/upload")
def upload(file: UploadFile = File(...), ctx=Depends(get_ctx), principal: Principal = Depends(require("imports.run")),
           conn=Depends(get_conn)):
    limits = settings_store.get(conn, "import_limits")
    tmp = save_upload(file, ctx.paths.staging / "uploads", limits["max_file_mb"] * 1024 * 1024)
    try:
        return imports.upload(conn, ctx.paths, principal, file.filename or "upload.xlsx", tmp.read_bytes())
    finally:
        cleanup([tmp])


@router.get("/api/imports")
def history(principal: Principal = Depends(require("imports.run")), conn=Depends(get_conn)):
    return imports.list_batches(conn)


@router.get("/api/imports/{batch_id}")
def detail(batch_id: int, principal: Principal = Depends(require("imports.run")), conn=Depends(get_conn)):
    b = imports.get_batch(conn, batch_id)
    out = {k: b[k] for k in ("id", "filename", "file_hash", "purpose", "status", "operator", "created_at",
                             "committed_at", "committed_by", "undone_at", "undone_by", "sheet_name", "header_row",
                             "template_id")}
    out["summary"] = db.jload(b["summary"], {})
    out["preview"] = db.jload(b["preview"], {})
    out["mapping"] = db.jload(b["mapping"], {})
    out["options"] = db.jload(b["options"], {})
    out["scope"] = db.jload(b["scope"], [])
    out["purpose_label"] = imports.PURPOSES.get(b["purpose"] or "", "")
    return out


@router.get("/api/imports/{batch_id}/inspect")
def inspect(batch_id: int, sheet: str | None = None, header_row: int = 1,
            principal: Principal = Depends(require("imports.run")), conn=Depends(get_conn)):
    return imports.inspect(conn, batch_id, sheet, header_row)


@router.post("/api/imports/{batch_id}/preview")
def preview(batch_id: int, body: PreviewBody, principal: Principal = Depends(require("imports.run")),
            conn=Depends(get_conn)):
    return imports.run_preview(conn, principal, batch_id, body.config, body.decisions)


@router.post("/api/imports/{batch_id}/commit")
def commit(batch_id: int, body: CommitBody, principal: Principal = Depends(require("imports.run")),
           conn=Depends(get_conn)):
    return imports.commit(conn, principal, batch_id, body.config, body.decisions, body.deactivate_ids,
                          body.acknowledge_repeat, body.last_working_date, body.confirm_deactivation)


@router.post("/api/imports/{batch_id}/cancel")
def cancel(batch_id: int, principal: Principal = Depends(require("imports.run")), conn=Depends(get_conn)):
    imports.cancel(conn, principal, batch_id)
    return {"ok": True}


@router.get("/api/imports/{batch_id}/report")
def report(batch_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(require("imports.run")),
           conn=Depends(get_conn)):
    path = imports.error_report(conn, ctx.paths, batch_id)
    return FileResponse(path, filename=f"Import_{batch_id}_issues.xlsx")


@router.get("/api/imports/{batch_id}/undo-check")
def undo_check(batch_id: int, principal: Principal = Depends(require("imports.undo")), conn=Depends(get_conn)):
    return {"conflicts": imports.undo_conflicts(conn, batch_id),
            "changes": db.scalar(conn, "SELECT COUNT(*) FROM import_changes WHERE batch_id=?", (batch_id,))}


@router.post("/api/imports/{batch_id}/undo")
def undo(batch_id: int, principal: Principal = Depends(require("imports.undo")), conn=Depends(get_conn)):
    return imports.undo(conn, principal, batch_id)


@router.get("/api/import-templates")
def templates(principal: Principal = Depends(require("imports.run")), conn=Depends(get_conn)):
    return imports.list_templates(conn)


@router.post("/api/import-templates")
def save_template(body: TemplateBody, principal: Principal = Depends(require("imports.run")), conn=Depends(get_conn)):
    return imports.save_template(conn, principal, body.model_dump())


@router.delete("/api/import-templates/{tid}")
def delete_template(tid: int, principal: Principal = Depends(require("imports.run")), conn=Depends(get_conn)):
    imports.delete_template(conn, principal, tid)
    return {"ok": True}


SAMPLES = {
    "employee_import_template.xlsx": "Blank import template with all standard columns",
    "sample_historical_employees.xlsx": "Synthetic historical data",
    "sample_current_roster.xlsx": "Synthetic complete current roster",
    "sample_partial_update.csv": "Synthetic partial update (CSV)",
    "sample_designation_update.csv": "Synthetic designation update (CSV)",
    "photo_mapping_template.csv": "Photo file name to RHC ID mapping",
}


@router.get("/api/samples")
def samples(principal: Principal = Depends(require("employees.view"))):
    folder = resource_dir() / "static" / "samples"
    return [{"name": k, "description": v} for k, v in SAMPLES.items() if (folder / k).exists()]


@router.get("/api/samples/{name}")
def sample(name: str, principal: Principal = Depends(require("employees.view"))):
    if name not in SAMPLES:
        raise NotFound("Sample not found")
    path = resource_dir() / "static" / "samples" / name
    if not path.exists():
        raise NotFound("Sample not found")
    return FileResponse(path, filename=name)
