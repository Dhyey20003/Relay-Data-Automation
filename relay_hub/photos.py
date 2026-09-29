"""Managed photo storage, upload batches, matching and review.

Originals are copied into images/<employee id>/<employee id>_p<photo id>.<ext>
so names never collide; the uploaded file name is kept as metadata only.
Thumbnails are generated separately in thumbnails/. Earlier versions are kept
for audit and package history.

Matching order (never facial recognition):
  1. explicit RHC ID mapping (a CSV of file name -> RHC ID) or an RHC ID in
     the file name: ready to save, still shown for review;
  2. unique name suggestion: needs the operator's confirmation;
  3. explicit sequence mapping: operator supplies an ordered RHC ID list,
     previews every pair, and confirms.
File, ZIP or PDF order is never assumed to match employee rows.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError

from . import audit, db, pdf_util, settings_store, timeutil
from .errors import Conflict, Forbidden, NotFound, ValidationFailed
from .security import Principal

FORMAT_EXT = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tif",
              "GIF": ".gif"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".jfif"}
THUMB_SIZE = 240


class ImageRejected(ValueError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_image(path: Path, max_megapixels: float) -> tuple[str, int, int]:
    """Open with Pillow, verify structure and size; returns (format, width, height)."""
    Image.MAX_IMAGE_PIXELS = int(max_megapixels * 1_000_000)
    try:
        with Image.open(path) as im:
            fmt = im.format
            im.verify()
        with Image.open(path) as im:
            im.load()
            w, h = im.size
    except Image.DecompressionBombError:
        raise ImageRejected(f"The image is larger than {max_megapixels} megapixels.") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageRejected(f"Not a readable image ({exc.__class__.__name__}).") from None
    if fmt not in FORMAT_EXT:
        raise ImageRejected(f"{fmt} images are not supported. Use JPG, PNG, WEBP, BMP, TIFF or GIF.")
    if w < 32 or h < 32:
        raise ImageRejected("The image is too small to be a headshot.")
    return fmt, w, h


def make_thumbnail(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((THUMB_SIZE, THUMB_SIZE))
        im.save(dest, "JPEG", quality=85)
    return dest


def to_jpeg(src: Path, dest: Path, quality: int = 92) -> Path:
    """Genuine format conversion (not just renaming): EXIF orientation applied, alpha on white."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.save(dest, "JPEG", quality=quality, optimize=True)
    return dest


# ---------------------------------------------------------------- storage

def store_photo(conn, paths, employee_id: int, src: Path, original_filename: str, actor: str, source: str,
                source_detail: str | None = None) -> int:
    """Copy a validated image into managed storage and make it current. Runs inside a transaction."""
    limits = settings_store.get(conn, "photo_limits")
    fmt, w, h = validate_image(src, limits["max_image_megapixels"])
    digest = sha256_file(src)
    now = timeutil.iso_utc()
    cur = conn.execute(
        "INSERT INTO photos(employee_id, stored_path, original_filename, sha256, width, height, format, file_size, "
        "source, source_detail, is_current, uploaded_at, uploaded_by) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (employee_id, "", original_filename, digest, w, h, fmt, src.stat().st_size, source, source_detail, now, actor))
    photo_id = cur.lastrowid
    rel = Path(str(employee_id)) / f"{employee_id}_p{photo_id}{FORMAT_EXT[fmt]}"
    dest = paths.images / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if sha256_file(dest) != digest:
        raise OSError("Photo copy verification failed")
    thumb_rel = Path(str(employee_id)) / f"{photo_id}.jpg"
    make_thumbnail(dest, paths.thumbnails / thumb_rel)
    prev = db.scalar(conn, "SELECT current_photo_id FROM employees WHERE id=?", (employee_id,))
    conn.execute("UPDATE photos SET is_current=0, replaced_at=? WHERE employee_id=? AND is_current=1",
                 (now, employee_id))
    conn.execute("UPDATE photos SET stored_path=?, thumb_path=?, is_current=1 WHERE id=?",
                 (rel.as_posix(), thumb_rel.as_posix(), photo_id))
    conn.execute("UPDATE employees SET current_photo_id=?, updated_at=?, updated_by=? WHERE id=?",
                 (photo_id, now, actor, employee_id))
    conn.execute("INSERT INTO field_changes(employee_id, field, old_value, new_value, changed_at, changed_by, source) "
                 "VALUES(?,?,?,?,?,?,?)", (employee_id, "photo", None if prev is None else str(prev), str(photo_id),
                                           now, actor, source))
    return photo_id


def photo_record(conn, principal: Principal, photo_id: int) -> dict:
    p = db.one(conn, "SELECT p.*, e.location FROM photos p JOIN employees e ON e.id=p.employee_id WHERE p.id=?",
               (photo_id,))
    if not p or not principal.location_ok(p["location"]):
        raise NotFound("Photo not found")
    return p


def upload_single(conn, paths, principal: Principal, employee_id: int, filename: str, tmp: Path) -> dict:
    """Replace-photo action from an employee profile."""
    if not principal.can("photos.manage"):
        raise Forbidden("You do not have permission to change photos")
    emp = db.one(conn, "SELECT * FROM employees WHERE id=?", (employee_id,))
    if not emp or not principal.location_ok(emp["location"]):
        raise NotFound("Employee not found")
    limits = settings_store.get(conn, "photo_limits")
    if tmp.stat().st_size > limits["max_file_mb"] * 1024 * 1024:
        raise ValidationFailed(f"The image is larger than {limits['max_file_mb']} MB.")
    try:
        validate_image(tmp, limits["max_image_megapixels"])
    except ImageRejected as exc:
        raise ValidationFailed(str(exc)) from None
    digest = sha256_file(tmp)
    if emp["current_photo_id"]:
        cur_sha = db.scalar(conn, "SELECT sha256 FROM photos WHERE id=?", (emp["current_photo_id"],))
        if cur_sha == digest:
            raise ValidationFailed("This image is identical to the current photo.")
    with db.transaction(conn):
        pid = store_photo(conn, paths, employee_id, tmp, Path(filename).name, principal.label, "profile")
        audit.log(conn, principal.label, "photo.replace", "employee", employee_id,
                  f"New photo for {emp['full_name']} ({emp['rhc_id']})", {"photo_id": pid, "filename": filename})
    return {"photo_id": pid}


def set_current(conn, principal: Principal, photo_id: int) -> None:
    if not principal.can("photos.manage"):
        raise Forbidden("You do not have permission to change photos")
    with db.transaction(conn):
        p = photo_record(conn, principal, photo_id)
        now = timeutil.iso_utc()
        prev = db.scalar(conn, "SELECT current_photo_id FROM employees WHERE id=?", (p["employee_id"],))
        conn.execute("UPDATE photos SET is_current=0, replaced_at=? WHERE employee_id=? AND is_current=1",
                     (now, p["employee_id"]))
        conn.execute("UPDATE photos SET is_current=1, replaced_at=NULL WHERE id=?", (photo_id,))
        conn.execute("UPDATE employees SET current_photo_id=?, updated_at=?, updated_by=? WHERE id=?",
                     (photo_id, now, principal.label, p["employee_id"]))
        conn.execute("INSERT INTO field_changes(employee_id, field, old_value, new_value, changed_at, changed_by, "
                     "source) VALUES(?,?,?,?,?,?,?)",
                     (p["employee_id"], "photo", str(prev) if prev else None, str(photo_id), now, principal.label,
                      "restore_version"))
        audit.log(conn, principal.label, "photo.set_current", "employee", p["employee_id"],
                  f"Restored photo version {photo_id}")


# ---------------------------------------------------------------- batches

def create_batch(conn, paths, principal: Principal) -> dict:
    if not principal.can("photos.manage"):
        raise Forbidden("You do not have permission to upload photos")
    with db.transaction(conn):
        cur = conn.execute("INSERT INTO photo_batches(created_at, created_by, status, staging_dir) VALUES(?,?,?,?)",
                           (timeutil.iso_utc(), principal.label, "open", ""))
        bid = cur.lastrowid
        folder = paths.staging / "photos" / f"batch_{bid:06d}"
        folder.mkdir(parents=True, exist_ok=True)
        conn.execute("UPDATE photo_batches SET staging_dir=? WHERE id=?", (str(folder), bid))
    return get_batch(conn, bid)


def get_batch(conn, batch_id: int) -> dict:
    b = db.one(conn, "SELECT * FROM photo_batches WHERE id=?", (batch_id,))
    if not b:
        raise NotFound("Photo batch not found")
    return b


def _natural_key(s: str) -> str:
    return re.sub(r"\d+", lambda m: m.group(0).zfill(8), (s or "").lower())


def _safe_member(name: str) -> str | None:
    """Return a safe relative path for a ZIP member, or None if unsafe."""
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or "\x00" in n:
        return None
    parts = PurePosixPath(n).parts
    if any(p in ("..",) for p in parts):
        return None
    return "/".join(p for p in parts if p not in ("", "."))


def add_uploads(conn, paths, principal: Principal, batch_id: int, files: list[tuple[str, Path]]) -> dict:
    """files: [(display name incl. folder path, temp path)]."""
    if not principal.can("photos.manage"):
        raise Forbidden("You do not have permission to upload photos")
    batch = get_batch(conn, batch_id)
    if batch["status"] != "open":
        raise Conflict("This photo batch is closed.")
    limits = settings_store.get(conn, "photo_limits")
    folder = Path(batch["staging_dir"])
    report = {"added": 0, "invalid": 0, "unsupported": [], "mappings": 0, "pdf_pages": 0, "messages": []}
    mapping_files = []
    for display_name, tmp in files:
        ext = Path(display_name).suffix.lower()
        size = tmp.stat().st_size
        if ext == ".csv":
            mapping_files.append((display_name, tmp))
            continue
        if ext == ".zip":
            if size > limits["max_zip_mb"] * 1024 * 1024:
                report["messages"].append(f"{display_name}: ZIP is larger than {limits['max_zip_mb']} MB; skipped.")
                continue
            _add_zip(conn, batch_id, folder, display_name, tmp, limits, report)
        elif ext == ".pdf":
            if size > limits["max_zip_mb"] * 1024 * 1024:
                report["messages"].append(f"{display_name}: PDF is too large; skipped.")
                continue
            _add_pdf(conn, batch_id, folder, display_name, tmp, limits, report)
        elif ext in IMAGE_EXTS:
            if size > limits["max_file_mb"] * 1024 * 1024:
                _stage_invalid(conn, batch_id, "image", display_name, f"Larger than {limits['max_file_mb']} MB")
                report["invalid"] += 1
                continue
            _stage_image(conn, batch_id, folder, "image", display_name, Path(display_name).name, tmp, limits,
                         report, sort_key=_natural_key(display_name))
        else:
            report["unsupported"].append(display_name)
            _stage_invalid(conn, batch_id, "other", display_name,
                           "Unsupported file type (use images, ZIP, PDF or a mapping CSV)")
            report["invalid"] += 1
        count = db.scalar(conn, "SELECT COUNT(*) FROM photo_batch_items WHERE batch_id=?", (batch_id,))
        if count > limits["max_files_per_batch"]:
            report["messages"].append(f"Batch limit of {limits['max_files_per_batch']} files reached; "
                                      "remaining files were not added.")
            break
    for display_name, tmp in mapping_files:
        report["mappings"] += _apply_mapping_csv(conn, batch_id, display_name, tmp, report)
    suggest_matches(conn, batch_id)
    return report


def _stage_invalid(conn, batch_id, kind, name, error):
    conn.execute("INSERT INTO photo_batch_items(batch_id, source_kind, source_name, original_filename, status, error, "
                 "sort_key, created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (batch_id, kind, name, Path(name).name, "invalid", error, _natural_key(name), timeutil.iso_utc()))


def _stage_image(conn, batch_id, folder: Path, kind, source_name, original_filename, src: Path, limits, report,
                 sort_key, page_no=None, bbox=None, context_text=None, flags=None) -> int | None:
    try:
        fmt, w, h = validate_image(src, limits["max_image_megapixels"])
    except ImageRejected as exc:
        _stage_invalid(conn, batch_id, kind, source_name, str(exc))
        report["invalid"] += 1
        return None
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO photo_batch_items(batch_id, source_kind, source_name, original_filename, sha256, width, "
            "height, format, page_no, bbox, context_text, flags, status, sort_key, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, kind, source_name, original_filename, sha256_file(src), w, h, fmt, page_no,
             db.jdump(bbox) if bbox else None, context_text, db.jdump(flags or []), "pending", sort_key,
             timeutil.iso_utc()))
        item_id = cur.lastrowid
        staged = folder / "items" / f"{item_id:06d}{FORMAT_EXT[fmt]}"
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, staged)
        thumb = folder / "thumbs" / f"{item_id:06d}.jpg"
        make_thumbnail(staged, thumb)
        conn.execute("UPDATE photo_batch_items SET staged_path=?, thumb_path=? WHERE id=?",
                     (str(staged), str(thumb), item_id))
    report["added"] += 1
    return item_id


def _add_zip(conn, batch_id, folder, zip_name, tmp: Path, limits, report) -> None:
    if not zipfile.is_zipfile(tmp):
        _stage_invalid(conn, batch_id, "zip", zip_name, "Not a valid ZIP file")
        report["invalid"] += 1
        return
    extract_dir = folder / "zip_tmp"
    extract_dir.mkdir(parents=True, exist_ok=True)
    max_entry = limits["max_file_mb"] * 1024 * 1024
    max_total = limits["max_zip_uncompressed_mb"] * 1024 * 1024
    total = 0
    with zipfile.ZipFile(tmp) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > limits["max_zip_entries"]:
            report["messages"].append(f"{zip_name}: more than {limits['max_zip_entries']} entries; rejected.")
            return
        for info in infos:
            rel = _safe_member(info.filename)
            label = f"{zip_name} > {info.filename}"
            if rel is None:
                _stage_invalid(conn, batch_id, "zip", label, "Unsafe path inside ZIP (rejected)")
                report["invalid"] += 1
                continue
            base = PurePosixPath(rel).name
            if rel.startswith("__MACOSX/") or base.startswith("._") or base.lower() in ("thumbs.db", ".ds_store"):
                continue
            ext = PurePosixPath(rel).suffix.lower()
            if ext not in IMAGE_EXTS:
                _stage_invalid(conn, batch_id, "zip", label, "Not an image (nested ZIPs and documents are not "
                                                             "extracted)")
                report["invalid"] += 1
                continue
            if info.flag_bits & 0x1:
                _stage_invalid(conn, batch_id, "zip", label, "Encrypted ZIP entries are not supported")
                report["invalid"] += 1
                continue
            if info.file_size > max_entry or (info.compress_size and info.file_size / info.compress_size > 200):
                _stage_invalid(conn, batch_id, "zip", label, "Entry too large or suspicious compression ratio")
                report["invalid"] += 1
                continue
            out = extract_dir / f"entry{ext}"
            written = 0
            ok = True
            with zf.open(info) as src, open(out, "wb") as dst:
                while True:
                    chunk = src.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > max_entry or total > max_total:
                        ok = False
                        break
                    dst.write(chunk)
            if not ok:
                _stage_invalid(conn, batch_id, "zip", label, "Size limit exceeded while extracting")
                report["invalid"] += 1
                if total > max_total:
                    report["messages"].append(f"{zip_name}: total extracted size limit reached; stopped.")
                    break
                continue
            _stage_image(conn, batch_id, folder, "zip", label, base, out, limits, report,
                         sort_key=_natural_key(zip_name + "/" + rel))
    shutil.rmtree(extract_dir, ignore_errors=True)


def _add_pdf(conn, batch_id, folder, pdf_name, tmp: Path, limits, report) -> None:
    pdf_dir = folder / "pdf" / f"{db.scalar(conn, 'SELECT COUNT(*) FROM pdf_pages WHERE batch_id=?', (batch_id,)) + 1:04d}_{re.sub(r'[^A-Za-z0-9._-]+', '_', Path(pdf_name).stem)[:40]}"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    stored_pdf = pdf_dir / "source.pdf"
    shutil.copyfile(tmp, stored_pdf)
    try:
        result = pdf_util.analyse(stored_pdf, pdf_dir, limits["max_pdf_pages"])
    except pdf_util.PdfError as exc:
        _stage_invalid(conn, batch_id, "pdf", pdf_name, str(exc))
        report["invalid"] += 1
        return
    with db.transaction(conn):
        for pg in result["pages"]:
            conn.execute("INSERT INTO pdf_pages(batch_id, source_name, pdf_path, page_no, render_path, render_scale, "
                         "width_px, height_px, text, image_count, flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (batch_id, pdf_name, str(stored_pdf), pg["page_no"], str(pg["render_path"]),
                          pdf_util.PREVIEW_SCALE, pg["width_px"], pg["height_px"], pg["text"], pg["image_count"],
                          db.jdump(pg["flags"])))
            report["pdf_pages"] += 1
    for img in result["images"]:
        flags = ["from_pdf"] + img["flags"]
        page_flags = next(p["flags"] for p in result["pages"] if p["page_no"] == img["page_no"])
        if "no_text" in page_flags:
            flags.append("no_text_on_page")
        # PDF coordinates: sort top-to-bottom, left-to-right for display only.
        top = img["px_bbox"][1]
        left = img["px_bbox"][0]
        _stage_image(conn, batch_id, folder, "pdf_image", f"{pdf_name} > page {img['page_no']}",
                     f"{Path(pdf_name).stem}_p{img['page_no']}.png", img["path"], limits, report,
                     sort_key=f"{_natural_key(pdf_name)}|{img['page_no']:05d}|{int(top):06d}|{int(left):06d}",
                     page_no=img["page_no"], bbox=img["px_bbox"], context_text=img["context_text"], flags=flags)
    if not result["images"]:
        report["messages"].append(f"{pdf_name}: no photos could be extracted. Open the PDF pages and crop the photos "
                                  "manually.")


def crop_pdf_page(conn, principal: Principal, page_id: int, rect: list[float]) -> dict:
    if not principal.can("photos.manage"):
        raise Forbidden("You do not have permission to upload photos")
    page = db.one(conn, "SELECT * FROM pdf_pages WHERE id=?", (page_id,))
    if not page:
        raise NotFound("PDF page not found")
    batch = get_batch(conn, page["batch_id"])
    if batch["status"] != "open":
        raise Conflict("This photo batch is closed.")
    folder = Path(batch["staging_dir"])
    n = db.scalar(conn, "SELECT COUNT(*) FROM photo_batch_items WHERE batch_id=?", (batch["id"],)) + 1
    out = folder / "crops" / f"crop_{page_id}_{n}.png"
    try:
        pdf_util.crop(Path(page["pdf_path"]), page["page_no"], tuple(rect), out)
    except pdf_util.PdfError as exc:
        raise ValidationFailed(str(exc)) from None
    limits = settings_store.get(conn, "photo_limits")
    report = {"added": 0, "invalid": 0}
    item_id = _stage_image(conn, batch["id"], folder, "pdf_crop", f"{page['source_name']} > page {page['page_no']}",
                           f"{Path(page['source_name']).stem}_p{page['page_no']}_crop.png", out, limits, report,
                           sort_key=f"{_natural_key(page['source_name'])}|{page['page_no']:05d}|{int(rect[1]):06d}|{int(rect[0]):06d}",
                           page_no=page["page_no"], bbox=[rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]],
                           flags=["from_pdf", "manual_crop"])
    if item_id is None:
        raise ValidationFailed("The cropped area is not a usable image.")
    suggest_matches(conn, batch["id"])
    return {"item_id": item_id}


def _apply_mapping_csv(conn, batch_id, name, tmp: Path, report) -> int:
    """Explicit mapping file: columns filename, rhc_id (header names are flexible)."""
    text = tmp.read_bytes().decode("utf-8-sig", errors="replace")
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        return 0
    header = [h.strip().lower() for h in reader[0]]
    fi = next((i for i, h in enumerate(header) if h in ("filename", "file name", "file", "photo", "image")), None)
    ri = next((i for i, h in enumerate(header) if h.replace(" ", "") in ("rhcid", "rhc", "employeeid", "empid")), None)
    if fi is None or ri is None:
        report["messages"].append(f"{name}: a mapping CSV needs 'filename' and 'RHC ID' columns.")
        return 0
    by_rhc = {r["rhc_id"].upper(): r["id"] for r in db.rows(conn, "SELECT id, rhc_id FROM employees WHERE rhc_id "
                                                                   "IS NOT NULL")}
    items = db.rows(conn, "SELECT id, original_filename FROM photo_batch_items WHERE batch_id=? AND status IN "
                          "('pending','ready')", (batch_id,))
    by_name = defaultdict(list)
    for it in items:
        by_name[(it["original_filename"] or "").lower()].append(it["id"])
    applied = 0
    with db.transaction(conn):
        for row in reader[1:]:
            if len(row) <= max(fi, ri):
                continue
            fname, rhc = row[fi].strip(), row[ri].strip().upper()
            if not fname:
                continue
            ids = by_name.get(Path(fname).name.lower(), [])
            emp_id = by_rhc.get(rhc)
            if not ids:
                report["messages"].append(f"Mapping: file '{fname}' is not in this batch.")
                continue
            if emp_id is None:
                report["messages"].append(f"Mapping: RHC ID '{rhc}' for '{fname}' does not exist.")
                continue
            if len(ids) > 1:
                report["messages"].append(f"Mapping: more than one file is named '{fname}'; assign them manually.")
                continue
            conn.execute("UPDATE photo_batch_items SET assigned_employee_id=?, suggested_employee_id=?, "
                         "match_method='mapping', match_note=?, status='ready' WHERE id=?",
                         (emp_id, emp_id, f"Mapping file {name}", ids[0]))
            applied += 1
    return applied


def _rhc_tokens(text: str) -> list[str]:
    return [t.upper() for t in re.split(r"[^A-Za-z0-9]+", text or "") if t]


def _norm_name(s: str) -> str:
    s = re.sub(r"[_.\-]+", " ", s or "")
    s = re.sub(r"\d+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def suggest_matches(conn, batch_id: int) -> None:
    """Compute suggestions for unassigned items. Only RHC-ID filename matches become 'ready'."""
    emps = db.rows(conn, "SELECT id, rhc_id, full_name, current_photo_id FROM employees")
    by_rhc = {e["rhc_id"].upper(): e for e in emps if e["rhc_id"]}
    by_name = defaultdict(list)
    for e in emps:
        n = _norm_name(e["full_name"])
        by_name[n].append(e)
        parts = n.split(" ")
        if len(parts) >= 2:
            by_name[" ".join([parts[-1]] + parts[:-1])].append(e)  # "Surname First"
    current_sha = {r["employee_id"]: r["sha256"] for r in
                   db.rows(conn, "SELECT employee_id, sha256 FROM photos WHERE is_current=1")}
    all_sha = defaultdict(set)
    for r in db.rows(conn, "SELECT employee_id, sha256 FROM photos"):
        all_sha[r["sha256"]].add(r["employee_id"])
    items = db.rows(conn, "SELECT * FROM photo_batch_items WHERE batch_id=? AND status IN ('pending','ready') "
                          "ORDER BY sort_key, id", (batch_id,))
    seen_sha: dict[str, int] = {}
    with db.transaction(conn):
        for it in items:
            flags = set(db.jload(it["flags"], []))
            flags -= {"duplicate_in_batch", "same_as_current", "used_by_other_employee", "ambiguous_rhc"}
            note = it["match_note"]
            status, suggested, method = it["status"], it["suggested_employee_id"], it["match_method"]
            assigned = it["assigned_employee_id"]
            if it["sha256"] in seen_sha:
                flags.add("duplicate_in_batch")
                note = f"Identical to item {seen_sha[it['sha256']]} in this batch"
            else:
                seen_sha[it["sha256"]] = it["id"]
            if assigned is None and method not in ("manual", "sequence", "mapping"):
                from_pdf = "from_pdf" in flags
                stem = Path(it["original_filename"] or "").stem
                found = {by_rhc[t]["id"]: by_rhc[t] for t in _rhc_tokens(stem) if t in by_rhc}
                if not from_pdf and len(found) == 1:
                    e = next(iter(found.values()))
                    suggested, method, status = e["id"], "rhc_filename", "ready"
                    assigned = e["id"]
                    note = f"RHC ID {e['rhc_id']} in file name"
                elif len(found) > 1:
                    flags.add("ambiguous_rhc")
                    suggested, method = None, None
                    note = "File name contains more than one RHC ID; choose the employee."
                else:
                    ctx_found = {}
                    if from_pdf:
                        ctx_found = {by_rhc[t]["id"]: by_rhc[t] for t in _rhc_tokens(it["context_text"] or "")
                                     if t in by_rhc}
                    if len(ctx_found) == 1:
                        e = next(iter(ctx_found.values()))
                        suggested, method = e["id"], "pdf_text_rhc"
                        note = f"RHC ID {e['rhc_id']} appears next to the photo in the PDF (confirm)"
                    else:
                        cand = by_name.get(_norm_name(stem), [])
                        if from_pdf and not cand:
                            ctx = _norm_name(it["context_text"] or "")
                            cand = [e for key, es in by_name.items() if key and len(key) > 4 and key in ctx
                                    for e in es]
                            cand = list({e["id"]: e for e in cand}.values())
                        if len(cand) == 1:
                            suggested, method = cand[0]["id"], "name_suggestion"
                            note = f"Name looks like {cand[0]['full_name']} (confirm)"
                        elif len(cand) > 1:
                            suggested, method = None, None
                            note = "Several employees share this name; choose the right one."
                        else:
                            suggested, method = None, None
                            note = note if "duplicate_in_batch" in flags else "No match found; choose the employee."
            target = assigned or suggested
            if target and current_sha.get(target) == it["sha256"]:
                flags.add("same_as_current")
            if it["sha256"] in all_sha and (all_sha[it["sha256"]] - {target}):
                flags.add("used_by_other_employee")
            conn.execute("UPDATE photo_batch_items SET flags=?, match_note=?, status=?, suggested_employee_id=?, "
                         "match_method=?, assigned_employee_id=? WHERE id=?",
                         (db.jdump(sorted(flags)), note, status, suggested, method, assigned, it["id"]))


def batch_detail(conn, principal: Principal, batch_id: int) -> dict:
    batch = get_batch(conn, batch_id)
    items = db.rows(conn, "SELECT i.*, se.full_name AS suggested_name, se.rhc_id AS suggested_rhc, "
                          "se.location AS suggested_location, ae.full_name AS assigned_name, ae.rhc_id AS assigned_rhc, "
                          "ae.location AS assigned_location, ae.current_photo_id AS assigned_current_photo "
                          "FROM photo_batch_items i LEFT JOIN employees se ON se.id=i.suggested_employee_id "
                          "LEFT JOIN employees ae ON ae.id=i.assigned_employee_id WHERE i.batch_id=? "
                          "ORDER BY i.sort_key, i.id", (batch_id,))
    visible = []
    for it in items:
        loc = it["assigned_location"] or it["suggested_location"]
        if loc and not principal.location_ok(loc):
            it["suggested_employee_id"] = it["assigned_employee_id"] = None
            it["suggested_name"] = it["assigned_name"] = it["suggested_rhc"] = it["assigned_rhc"] = None
        it["flags"] = db.jload(it["flags"], [])
        it["bbox"] = db.jload(it["bbox"])
        for k in ("staged_path", "thumb_path"):
            it.pop(k, None)
        visible.append(it)
    pages = db.rows(conn, "SELECT id, source_name, page_no, width_px, height_px, image_count, flags, "
                          "substr(text, 1, 500) AS text FROM pdf_pages WHERE batch_id=? ORDER BY id", (batch_id,))
    for p in pages:
        p["flags"] = db.jload(p["flags"], [])
    counts = defaultdict(int)
    for it in visible:
        counts[it["status"]] += 1
    return {"batch": batch | {"staging_dir": None}, "items": visible, "pdf_pages": pages, "counts": counts}


def update_item(conn, principal: Principal, item_id: int, action: str, employee_id: int | None = None) -> None:
    if not principal.can("photos.manage"):
        raise Forbidden("You do not have permission to manage photos")
    it = db.one(conn, "SELECT * FROM photo_batch_items WHERE id=?", (item_id,))
    if not it:
        raise NotFound("Item not found")
    if get_batch(conn, it["batch_id"])["status"] != "open":
        raise Conflict("This photo batch is closed.")
    if it["status"] in ("committed", "invalid"):
        raise Conflict("This item can no longer be changed.")
    with db.transaction(conn):
        if action == "assign":
            emp = db.one(conn, "SELECT id, location FROM employees WHERE id=?", (employee_id,))
            if not emp or not principal.location_ok(emp["location"]):
                raise NotFound("Employee not found")
            conn.execute("UPDATE photo_batch_items SET assigned_employee_id=?, match_method='manual', status='ready', "
                         "match_note='Chosen by operator' WHERE id=?", (employee_id, item_id))
        elif action == "confirm":
            if not it["suggested_employee_id"]:
                raise ValidationFailed("There is no suggestion to confirm; choose the employee.")
            conn.execute("UPDATE photo_batch_items SET assigned_employee_id=suggested_employee_id, status='ready', "
                         "match_note=COALESCE(match_note,'') || ' - confirmed' WHERE id=?", (item_id,))
        elif action == "skip":
            conn.execute("UPDATE photo_batch_items SET status='skipped' WHERE id=?", (item_id,))
        elif action == "reset":
            conn.execute("UPDATE photo_batch_items SET status='pending', assigned_employee_id=NULL, match_method=NULL "
                         "WHERE id=?", (item_id,))
        else:
            raise ValidationFailed("Unknown action")
    suggest_matches(conn, it["batch_id"])


def sequence_preview(conn, principal: Principal, batch_id: int, item_ids: list[int], rhc_ids: list[str]) -> dict:
    """Pair an explicitly ordered list of items with an explicitly ordered list of RHC IDs."""
    rhc_ids = [r.strip().upper() for r in rhc_ids if r and r.strip()]
    items = {i["id"]: i for i in db.rows(conn, "SELECT * FROM photo_batch_items WHERE batch_id=? AND status IN "
                                               "('pending','ready')", (batch_id,))}
    errors = []
    if len(item_ids) != len(rhc_ids):
        errors.append(f"{len(item_ids)} photos are selected but {len(rhc_ids)} RHC IDs were entered. "
                      "The lists must be the same length.")
    if len(set(rhc_ids)) != len(rhc_ids):
        errors.append("The RHC ID list contains duplicates.")
    pairs = []
    for pos, (iid, rhc) in enumerate(zip(item_ids, rhc_ids), start=1):
        it = items.get(int(iid))
        emp = db.one(conn, "SELECT id, full_name, rhc_id, location FROM employees WHERE rhc_id=?", (rhc,))
        problem = None
        if not it:
            problem = "Photo is not open in this batch"
        elif not emp:
            problem = f"RHC ID {rhc} does not exist"
        elif not principal.location_ok(emp["location"]):
            problem = f"RHC ID {rhc} is outside your location scope"
        if problem:
            errors.append(f"Position {pos}: {problem}")
        pairs.append({"position": pos, "item_id": iid, "original_filename": it["original_filename"] if it else None,
                      "source_name": it["source_name"] if it else None, "rhc_id": rhc,
                      "employee_id": emp["id"] if emp else None, "name": emp["full_name"] if emp else None,
                      "problem": problem})
    return {"pairs": pairs, "errors": errors}


def sequence_apply(conn, principal: Principal, batch_id: int, item_ids: list[int], rhc_ids: list[str],
                   confirmed: bool) -> dict:
    if not principal.can("photos.manage"):
        raise Forbidden("You do not have permission to manage photos")
    if not confirmed:
        raise ValidationFailed("Review the pairs and tick the confirmation first.")
    prev = sequence_preview(conn, principal, batch_id, item_ids, rhc_ids)
    if prev["errors"]:
        raise ValidationFailed("The sequence has problems.", prev)
    with db.transaction(conn):
        for p in prev["pairs"]:
            conn.execute("UPDATE photo_batch_items SET assigned_employee_id=?, suggested_employee_id=?, "
                         "match_method='sequence', status='ready', match_note=? WHERE id=?",
                         (p["employee_id"], p["employee_id"], f"Sequence position {p['position']} (confirmed)",
                          p["item_id"]))
        audit.log(conn, principal.label, "photo.sequence_map", "photo_batch", batch_id,
                  f"Confirmed sequence mapping of {len(prev['pairs'])} photos")
    suggest_matches(conn, batch_id)
    return {"assigned": len(prev["pairs"])}


def commit_batch(conn, paths, principal: Principal, batch_id: int) -> dict:
    if not principal.can("photos.manage"):
        raise Forbidden("You do not have permission to manage photos")
    batch = get_batch(conn, batch_id)
    if batch["status"] != "open":
        raise Conflict("This photo batch is closed.")
    ready = db.rows(conn, "SELECT i.*, e.location, e.full_name, e.rhc_id FROM photo_batch_items i JOIN employees e "
                          "ON e.id=i.assigned_employee_id WHERE i.batch_id=? AND i.status='ready'", (batch_id,))
    per_emp = defaultdict(list)
    for it in ready:
        per_emp[it["assigned_employee_id"]].append(it)
    clashes = [{"employee": its[0]["full_name"], "rhc_id": its[0]["rhc_id"], "items": [i["id"] for i in its]}
               for its in per_emp.values() if len(its) > 1]
    if clashes:
        raise ValidationFailed("More than one photo is assigned to the same employee. Skip the extra photos first.",
                               {"clashes": clashes})
    for it in ready:
        if not principal.location_ok(it["location"]):
            raise Forbidden(f"{it['full_name']} is outside your location scope")
    saved, unchanged = 0, 0
    with db.transaction(conn):
        for it in ready:
            if "same_as_current" in db.jload(it["flags"], []):
                conn.execute("UPDATE photo_batch_items SET status='skipped', match_note='Identical to current photo; "
                             "not saved again' WHERE id=?", (it["id"],))
                unchanged += 1
                continue
            pid = store_photo(conn, paths, it["assigned_employee_id"], Path(it["staged_path"]),
                              it["original_filename"], principal.label, f"batch:{it['source_kind']}",
                              db.jdump({"batch_id": batch_id, "item_id": it["id"], "source": it["source_name"],
                                        "page": it["page_no"], "method": it["match_method"]}))
            conn.execute("UPDATE photo_batch_items SET status='committed', committed_photo_id=? WHERE id=?",
                         (pid, it["id"]))
            saved += 1
        remaining = db.scalar(conn, "SELECT COUNT(*) FROM photo_batch_items WHERE batch_id=? AND status='pending'",
                              (batch_id,))
        summary = {"saved": saved, "unchanged": unchanged, "pending": remaining}
        conn.execute("UPDATE photo_batches SET summary=? WHERE id=?", (db.jdump(summary), batch_id))
        audit.log(conn, principal.label, "photo.batch_commit", "photo_batch", batch_id,
                  f"Saved {saved} photos from batch {batch_id}", summary)
    return summary


def close_batch(conn, paths, principal: Principal, batch_id: int) -> None:
    batch = get_batch(conn, batch_id)
    with db.transaction(conn):
        conn.execute("UPDATE photo_batches SET status='closed', committed_at=? WHERE id=?",
                     (timeutil.iso_utc(), batch_id))
        audit.log(conn, principal.label, "photo.batch_close", "photo_batch", batch_id, "Closed photo batch")
    if batch["staging_dir"] and paths.inside(Path(batch["staging_dir"]), paths.staging):
        shutil.rmtree(batch["staging_dir"], ignore_errors=True)


def list_batches(conn, limit=50) -> list[dict]:
    items = db.rows(conn, "SELECT b.id, b.created_at, b.created_by, b.status, b.summary, "
                          "(SELECT COUNT(*) FROM photo_batch_items i WHERE i.batch_id=b.id) AS item_count, "
                          "(SELECT COUNT(*) FROM photo_batch_items i WHERE i.batch_id=b.id AND i.status='pending') "
                          "AS pending FROM photo_batches b ORDER BY b.id DESC LIMIT ?", (limit,))
    for it in items:
        it["summary"] = db.jload(it["summary"], {})
    return items


def missing_photos(conn, principal: Principal) -> list[dict]:
    items = db.rows(conn, "SELECT id, rhc_id, full_name, location, designation, department FROM employees "
                          "WHERE record_type='directory' AND employment_status='Active' AND current_photo_id IS NULL "
                          "ORDER BY location, full_name")
    return [i for i in items if principal.location_ok(i["location"])]
