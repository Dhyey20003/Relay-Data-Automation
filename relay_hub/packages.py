"""Headshot package generation: per-location ZIP files plus manifests.

A prepared package is an immutable snapshot: its ZIP and manifest are written
once, marked read-only, and their SHA-256 hashes stored. Regenerating creates
a new revision; earlier revisions are kept (superseded) for history.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import zipfile
from collections import defaultdict
from pathlib import Path

from . import db, photos, timeutil
from .errors import ValidationFailed

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
JPEG_EXTS = {".jpg", ".jpeg", ".jfif"}
PLACEHOLDERS = ["original_filename", "original_stem", "rhc_id", "full_name", "first_name", "last_name", "location",
                "ext"]


class PackageError(ValidationFailed):
    pass


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    stem, ext = os.path.splitext(name)
    if stem.upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    stem = stem[:150] or "photo"
    return stem + ext


def render_name(template: str, emp: dict, photo: dict, location: str, out_ext: str) -> str:
    original = photo["original_filename"] or f"photo{out_ext}"
    parts = (emp["full_name"] or "").split()
    values = {
        "original_filename": original,
        "original_stem": Path(original).stem,
        "rhc_id": emp["rhc_id"] or "",
        "full_name": emp["full_name"] or "",
        "first_name": parts[0] if parts else "",
        "last_name": parts[-1] if len(parts) > 1 else "",
        "location": location,
        "ext": out_ext,
    }
    try:
        name = (template or "{original_filename}").format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise PackageError(f"Naming template is invalid ({exc}). Allowed: "
                           + ", ".join("{" + p + "}" for p in PLACEHOLDERS)) from None
    name = sanitize_filename(name)
    suffix = Path(name).suffix.lower()
    same_family = suffix == out_ext.lower() or (suffix in JPEG_EXTS and out_ext.lower() in JPEG_EXTS)
    if not same_family:
        if suffix in photos.IMAGE_EXTS:
            name = name[: -len(suffix)]
        name = name + out_ext
    return name


def resolve_duplicates(entries: list[dict]) -> None:
    """Deterministic duplicate handling: colliding names get _<RHC ID>; then _2, _3 ..."""
    entries.sort(key=lambda e: (e["rhc_id"] or "", e["employee_id"]))
    groups = defaultdict(list)
    for e in entries:
        groups[e["exported_filename"].lower()].append(e)
    for group in groups.values():
        if len(group) > 1:
            for e in group:
                stem, ext = os.path.splitext(e["exported_filename"])
                e["renamed_from"] = e["exported_filename"]
                e["exported_filename"] = f"{stem}_{e['rhc_id']}{ext}"
    used = set()
    for e in entries:
        name = e["exported_filename"]
        stem, ext = os.path.splitext(name)
        n = 2
        while name.lower() in used:
            e.setdefault("renamed_from", e["exported_filename"])
            name = f"{stem}_{n}{ext}"
            n += 1
        e["exported_filename"] = name
        used.add(name.lower())


def zip_name(task: dict, period: dict, location: str) -> str:
    safe_loc = re.sub(r"[^A-Za-z0-9]+", "_", location)
    return f"{safe_loc}_{task['kind']}_Headshots_{period['period_key']}.zip"


def collect(conn, paths, task: dict, location: str) -> dict:
    """Select employees for one location and validate IDs, locations and photos."""
    inclusion = task["inclusion"]
    kind = task["kind"]
    baseline = {r["employee_id"]: r for r in db.rows(conn, "SELECT * FROM export_baseline WHERE task_id=?",
                                                     (task["id"],))}
    emps = db.rows(conn, "SELECT e.*, p.id AS p_id, p.stored_path, p.original_filename, p.sha256, p.format "
                         "FROM employees e LEFT JOIN photos p ON p.id=e.current_photo_id "
                         "WHERE e.record_type='directory' AND e.employment_status IN ('Active','Pending Review') "
                         "AND (e.location=? OR e.location IS NULL OR e.location='') ORDER BY e.rhc_id, e.id",
                   (location,))
    included, omitted, unchanged = [], [], 0
    for e in emps:
        who = {"employee_id": e["id"], "rhc_id": e["rhc_id"], "full_name": e["full_name"]}
        if not e["location"]:
            omitted.append({**who, "reason": "No location recorded, so the employee is not in any city package"})
            continue
        if e["employment_status"] != "Active":
            omitted.append({**who, "reason": "Employment status is Pending Review"})
            continue
        if not e["rhc_id"]:
            omitted.append({**who, "reason": "Missing RHC ID"})
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", e["rhc_id"]):
            omitted.append({**who, "reason": "RHC ID contains characters that are not safe in file names"})
            continue
        if not e["p_id"]:
            omitted.append({**who, "reason": "No photo on file"})
            continue
        src = paths.images / e["stored_path"]
        if not src.exists():
            omitted.append({**who, "reason": "Photo file is missing from storage"})
            continue
        if photos.sha256_file(src) != e["sha256"]:
            omitted.append({**who, "reason": "Photo file is damaged (checksum mismatch)"})
            continue
        if inclusion == "changed":
            b = baseline.get(e["id"])
            if b and b["photo_id"] == e["p_id"] and b["location"] == location and (b["rhc_id"] or "") == e["rhc_id"]:
                unchanged += 1
                continue
        fmt_ext = photos.FORMAT_EXT.get(e["format"], ".jpg")
        convert = kind == "HR" or task["export_format"] == "jpg"
        out_ext = ".jpg" if convert else fmt_ext
        template = "{rhc_id}" if kind == "HR" else (task["naming_template"] or "{original_filename}")
        photo = {"original_filename": e["original_filename"]}
        included.append({**who, "photo_id": e["p_id"], "photo_sha256": e["sha256"], "source": src,
                         "original_filename": e["original_filename"], # JPEG originals are copied byte-for-byte; other formats are re-encoded.
                         "convert_to_jpg": convert and e["format"] not in ("JPEG", "MPO"),
                         "exported_filename": render_name(template, e, photo, location, out_ext),
                         "reason": "new or changed" if inclusion == "changed" else "active employee"})
    resolve_duplicates(included)
    return {"included": included, "omitted": omitted, "unchanged": unchanged}


def build(conn, paths, task: dict, period: dict, location: str, actor: str) -> dict:
    """Write one location package (new revision). Must run inside a transaction."""
    data = collect(conn, paths, task, location)
    rev = (db.scalar(conn, "SELECT MAX(revision) FROM packages WHERE period_id=? AND location=?",
                     (period["id"], location)) or 0) + 1
    safe_loc = re.sub(r"[^A-Za-z0-9]+", "_", location)
    folder = paths.exports / "packages" / task["kind"] / period["period_key"] / safe_loc / f"r{rev:02d}"
    if folder.exists():
        _force_rmtree(folder)  # leftover from an interrupted attempt that never committed
    folder.mkdir(parents=True)
    zname = zip_name(task, period, location)
    generated = timeutil.iso_utc()
    zip_path = None
    zip_sha = None
    zip_size = None
    if data["included"]:
        tmp_zip = folder / (zname + ".partial")
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            for item in data["included"]:
                if item["convert_to_jpg"]:
                    converted = folder / "_convert.jpg"
                    photos.to_jpeg(item["source"], converted)
                    zf.write(converted, item["exported_filename"])
                    item["exported_sha256"] = photos.sha256_file(converted)
                    converted.unlink()
                else:
                    zf.write(item["source"], item["exported_filename"])
                    item["exported_sha256"] = item["photo_sha256"]
        zip_path = folder / zname
        os.replace(tmp_zip, zip_path)
        zip_sha = photos.sha256_file(zip_path)
        zip_size = zip_path.stat().st_size
    manifest = {
        "application": "Relay Employee Hub",
        "task": task["name"], "task_kind": task["kind"],
        "reporting_period": period["period_label"], "period_key": period["period_key"],
        "location": location, "revision": rev, "generated_at": generated, "generated_by": actor,
        "timezone": timeutil.tz_name(),
        "inclusion": "All active employees" if task["inclusion"] == "all"
        else "Only new or changed headshots since the last completed submission",
        "naming": "RHC ID (JPG)" if task["kind"] == "HR" else task["naming_template"],
        "export_format": "JPG (converted)" if (task["kind"] == "HR" or task["export_format"] == "jpg")
        else "Original format",
        "zip_file": zname if zip_path else None, "zip_sha256": zip_sha,
        "complete": not data["omitted"],
        "counts": {"included": len(data["included"]), "omitted": len(data["omitted"]),
                   "unchanged_not_included": data["unchanged"]},
        "employees": [{k: v for k, v in it.items() if k not in ("source", "convert_to_jpg")}
                      for it in data["included"]],
        "exclusions": data["omitted"],
    }
    manifest_path = folder / f"{safe_loc}_{task['kind']}_{period['period_key']}_manifest.json"
    manifest_path.write_text(_pretty(manifest), encoding="utf-8")
    csv_rows = [[e["rhc_id"], e["full_name"], e["photo_id"], e["original_filename"], e["exported_filename"],
                 e.get("renamed_from", ""), "included"] for e in manifest["employees"]]
    csv_rows += [[o["rhc_id"], o["full_name"], "", "", "", "", "EXCLUDED: " + o["reason"]] for o in data["omitted"]]
    from . import sheets
    sheets.write_csv(folder / f"{safe_loc}_{task['kind']}_{period['period_key']}_manifest.csv",
                     ["RHC ID", "Full name", "Photo version", "Original filename", "Exported filename",
                      "Renamed from (duplicate)", "Status"], csv_rows)
    for f in folder.iterdir():
        _make_readonly(f)
    manifest_sha = photos.sha256_file(manifest_path)
    now = generated
    conn.execute("UPDATE packages SET superseded_at=? WHERE period_id=? AND location=? AND superseded_at IS NULL",
                 (now, period["id"], location))
    pid = conn.execute(
        "INSERT INTO packages(period_id, task_id, location, revision, zip_name, zip_path, zip_sha256, zip_size, "
        "manifest_path, manifest_sha256, inclusion, included_count, omitted_count, is_complete, created_at, "
        "created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (period["id"], task["id"], location, rev, zname, str(zip_path) if zip_path else None, zip_sha, zip_size,
         str(manifest_path), manifest_sha, task["inclusion"], len(data["included"]), len(data["omitted"]),
         0 if data["omitted"] else 1, now, actor)).lastrowid
    for it in data["included"]:
        conn.execute("INSERT INTO package_items(package_id, employee_id, rhc_id, photo_id, location, exported_filename, "
                     "original_filename) VALUES(?,?,?,?,?,?,?)",
                     (pid, it["employee_id"], it["rhc_id"], it["photo_id"], location, it["exported_filename"],
                      it["original_filename"]))
    conn.execute("INSERT INTO period_locations(period_id, location, current_package_id, status) VALUES(?,?,?,?) "
                 "ON CONFLICT(period_id, location) DO UPDATE SET current_package_id=excluded.current_package_id, "
                 "status=excluded.status", (period["id"], location, pid, "prepared"))
    return {"package_id": pid, "revision": rev, "included": len(data["included"]), "omitted": len(data["omitted"]),
            "unchanged": data["unchanged"], "zip": str(zip_path) if zip_path else None}


def _pretty(obj) -> str:
    import json
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _make_readonly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


def _force_rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def verify(pkg: dict) -> str | None:
    """Return a problem description if a stored package no longer matches its hashes."""
    if pkg["zip_path"]:
        p = Path(pkg["zip_path"])
        if not p.exists():
            return f"{pkg['zip_name']} is missing from the exports folder"
        if photos.sha256_file(p) != pkg["zip_sha256"]:
            return f"{pkg['zip_name']} has been modified since it was prepared"
    m = Path(pkg["manifest_path"])
    if not m.exists() or photos.sha256_file(m) != pkg["manifest_sha256"]:
        return f"The manifest for {pkg['location']} is missing or modified"
    return None


def read_manifest(pkg: dict) -> dict:
    import json
    try:
        return json.loads(Path(pkg["manifest_path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
