"""Photo files, replace-photo, upload batches, review, PDF pages and cropping."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import audit, db, photos, settings_store, sheets, timeutil
from ..errors import Forbidden, NotFound, ValidationFailed
from ..security import Principal
from .deps import cleanup, get_conn, get_ctx, require, save_upload

router = APIRouter()
NO_CACHE = {"Cache-Control": "private, max-age=300"}


class ItemAction(BaseModel):
    action: str
    employee_id: int | None = None


class SequenceBody(BaseModel):
    item_ids: list[int]
    rhc_ids: list[str]
    confirmed: bool = False


class CropBody(BaseModel):
    rect: list[float]


def _media(path: Path) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
            ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff", ".gif": "image/gif"}.get(
        path.suffix.lower(), "application/octet-stream")


@router.get("/api/photos/{photo_id}/thumb")
def photo_thumb(photo_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(require("photos.view")),
                conn=Depends(get_conn)):
    p = photos.photo_record(conn, principal, photo_id)
    path = ctx.paths.thumbnails / p["thumb_path"]
    if not path.exists():
        photos.make_thumbnail(ctx.paths.images / p["stored_path"], path)
    return FileResponse(path, media_type="image/jpeg", headers=NO_CACHE)


@router.get("/api/photos/{photo_id}/file")
def photo_file(photo_id: int, download: bool = False, ctx=Depends(get_ctx),
               principal: Principal = Depends(require("photos.view")), conn=Depends(get_conn)):
    p = photos.photo_record(conn, principal, photo_id)
    path = ctx.paths.images / p["stored_path"]
    if not path.exists():
        raise NotFound("The photo file is missing from storage.")
    if download:
        with db.transaction(conn):
            audit.log(conn, principal.label, "export.photo", "photo", photo_id, f"Downloaded photo {photo_id}")
        return FileResponse(path, media_type=_media(path), filename=p["original_filename"])
    return FileResponse(path, media_type=_media(path), headers=NO_CACHE)


@router.post("/api/employees/{emp_id}/photo")
def replace_photo(emp_id: int, file: UploadFile = File(...), ctx=Depends(get_ctx),
                  principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    limits = settings_store.get(conn, "photo_limits")
    tmp = save_upload(file, ctx.paths.staging / "uploads", limits["max_file_mb"] * 1024 * 1024)
    try:
        return photos.upload_single(conn, ctx.paths, principal, emp_id, file.filename or "photo", tmp)
    finally:
        cleanup([tmp])


@router.post("/api/photos/{photo_id}/make-current")
def make_current(photo_id: int, principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    photos.set_current(conn, principal, photo_id)
    return {"ok": True}


@router.get("/api/photos/missing")
def missing(principal: Principal = Depends(require("photos.view")), conn=Depends(get_conn)):
    return photos.missing_photos(conn, principal)


@router.get("/api/photos/missing.xlsx")
def missing_xlsx(ctx=Depends(get_ctx), principal: Principal = Depends(require("employees.export")),
                 conn=Depends(get_conn)):
    items = photos.missing_photos(conn, principal)
    stamp = timeutil.now_local().strftime("%Y%m%d_%H%M%S")
    out = ctx.paths.exports / "reports" / f"missing_photos_{stamp}.xlsx"
    sheets.write_xlsx(out, [("Missing photos", ["RHC ID", "Full name", "Location", "Designation", "Department"],
                             [[i["rhc_id"], i["full_name"], i["location"], i["designation"], i["department"]]
                              for i in items])])
    with db.transaction(conn):
        audit.log(conn, principal.label, "export.missing_photos", None, None, f"Exported {len(items)} missing photos")
    return FileResponse(out, filename=f"Missing_Photos_{stamp}.xlsx")


# ------------------------------------------------------------------ batches

@router.post("/api/photo-batches")
def create_batch(ctx=Depends(get_ctx), principal: Principal = Depends(require("photos.manage")),
                 conn=Depends(get_conn)):
    b = photos.create_batch(conn, ctx.paths, principal)
    return {"id": b["id"]}


@router.get("/api/photo-batches")
def list_batches(principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    return photos.list_batches(conn)


@router.get("/api/photo-batches/{batch_id}")
def batch_detail(batch_id: int, principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    return photos.batch_detail(conn, principal, batch_id)


@router.post("/api/photo-batches/{batch_id}/upload")
def upload(batch_id: int, files: list[UploadFile] = File(...), relpaths: list[str] = Form(default=[]),
           ctx=Depends(get_ctx), principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    limits = settings_store.get(conn, "photo_limits")
    max_bytes = max(limits["max_zip_mb"], limits["max_file_mb"]) * 1024 * 1024
    if len(files) > limits["max_files_per_batch"]:
        raise ValidationFailed(f"Upload at most {limits['max_files_per_batch']} files at once.")
    saved = []
    try:
        for i, f in enumerate(files):
            name = (relpaths[i] if i < len(relpaths) and relpaths[i] else f.filename) or f"file{i}"
            saved.append((name.replace("\\", "/"), save_upload(f, ctx.paths.staging / "uploads", max_bytes)))
        report = photos.add_uploads(conn, ctx.paths, principal, batch_id, saved)
    finally:
        cleanup([p for _, p in saved])
    return report


def _item(conn, principal, item_id):
    it = db.one(conn, "SELECT i.*, b.status AS batch_status FROM photo_batch_items i JOIN photo_batches b "
                      "ON b.id=i.batch_id WHERE i.id=?", (item_id,))
    if not it:
        raise NotFound("Item not found")
    return it


@router.get("/api/photo-batches/items/{item_id}/thumb")
def item_thumb(item_id: int, principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    it = _item(conn, principal, item_id)
    if not it["thumb_path"] or not Path(it["thumb_path"]).exists():
        raise NotFound("No preview for this item")
    return FileResponse(it["thumb_path"], media_type="image/jpeg")


@router.get("/api/photo-batches/items/{item_id}/image")
def item_image(item_id: int, principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    it = _item(conn, principal, item_id)
    if not it["staged_path"] or not Path(it["staged_path"]).exists():
        raise NotFound("No image for this item")
    return FileResponse(it["staged_path"], media_type=_media(Path(it["staged_path"])))


@router.post("/api/photo-batches/items/{item_id}")
def item_action(item_id: int, body: ItemAction, principal: Principal = Depends(require("photos.manage")),
                conn=Depends(get_conn)):
    photos.update_item(conn, principal, item_id, body.action, body.employee_id)
    return {"ok": True}


@router.post("/api/photo-batches/{batch_id}/sequence/preview")
def sequence_preview(batch_id: int, body: SequenceBody, principal: Principal = Depends(require("photos.manage")),
                     conn=Depends(get_conn)):
    return photos.sequence_preview(conn, principal, batch_id, body.item_ids, body.rhc_ids)


@router.post("/api/photo-batches/{batch_id}/sequence/apply")
def sequence_apply(batch_id: int, body: SequenceBody, principal: Principal = Depends(require("photos.manage")),
                   conn=Depends(get_conn)):
    return photos.sequence_apply(conn, principal, batch_id, body.item_ids, body.rhc_ids, body.confirmed)


@router.get("/api/photo-batches/pages/{page_id}/image")
def page_image(page_id: int, principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    page = db.one(conn, "SELECT render_path FROM pdf_pages WHERE id=?", (page_id,))
    if not page or not page["render_path"] or not Path(page["render_path"]).exists():
        raise NotFound("Page preview not found")
    return FileResponse(page["render_path"], media_type="image/png")


@router.get("/api/photo-batches/pages/{page_id}")
def page_detail(page_id: int, principal: Principal = Depends(require("photos.manage")), conn=Depends(get_conn)):
    page = db.one(conn, "SELECT id, batch_id, source_name, page_no, width_px, height_px, text, image_count, flags "
                        "FROM pdf_pages WHERE id=?", (page_id,))
    if not page:
        raise NotFound("Page not found")
    page["flags"] = db.jload(page["flags"], [])
    page["items"] = db.rows(conn, "SELECT id, bbox, status, source_kind FROM photo_batch_items WHERE batch_id=? AND "
                                  "page_no=? AND source_name LIKE ?", (page["batch_id"], page["page_no"],
                                                                        page["source_name"] + " > page%"))
    for it in page["items"]:
        it["bbox"] = db.jload(it["bbox"])
    return page


@router.post("/api/photo-batches/pages/{page_id}/crop")
def crop(page_id: int, body: CropBody, principal: Principal = Depends(require("photos.manage")),
         conn=Depends(get_conn)):
    if len(body.rect) != 4:
        raise ValidationFailed("Crop rectangle must have x, y, width and height.")
    return photos.crop_pdf_page(conn, principal, page_id, body.rect)


@router.post("/api/photo-batches/{batch_id}/commit")
def commit(batch_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(require("photos.manage")),
           conn=Depends(get_conn)):
    return photos.commit_batch(conn, ctx.paths, principal, batch_id)


@router.post("/api/photo-batches/{batch_id}/close")
def close(batch_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(require("photos.manage")),
          conn=Depends(get_conn)):
    b = photos.get_batch(conn, batch_id)
    if b["status"] != "open":
        raise Forbidden("This batch is already closed.")
    photos.close_batch(conn, ctx.paths, principal, batch_id)
    return {"ok": True}
