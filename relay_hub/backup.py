"""Full backups, verification, retention and restore.

A backup is one ZIP file containing:
  employees.db   consistent snapshot taken with SQLite's online backup API
                 (never a raw copy of the live file)
  images/, thumbnails/   every photo version and thumbnail
  exports/packages/      package manifests (and ZIPs if configured)
  config.json            server settings, kept for reference
  backup_manifest.json   SHA-256 and size of every file

The backup destination is never inside the material being backed up: the
default is <data>/backups, which is excluded from every backup, and any other
destination inside the data folder is rejected.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__, audit, db, settings_store, timeutil

FORMAT = "relay-employee-hub-backup"
MANIFEST = "backup_manifest.json"
INCLUDED_DIRS = ("images", "thumbnails", "exports/packages")


class BackupError(ValueError):
    pass


def destination(conn, paths) -> Path:
    cfg = settings_store.get(conn, "backup")
    dest = Path(cfg.get("destination") or paths.backups)
    return dest


def validate_destination(paths, dest: Path) -> Path:
    dest = Path(dest).expanduser()
    if not dest.is_absolute():
        raise BackupError("Enter a full folder path, for example E:\\RelayBackups.")
    resolved = dest.resolve()
    if paths.inside(resolved) and not paths.inside(resolved, paths.backups):
        raise BackupError("The backup folder cannot be inside the data folder (except the built-in backups folder), "
                          "because backups would then include themselves.")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / ".write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise BackupError(f"The backup folder is not writable: {exc}") from None
    return resolved


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_db(db_path: Path, out: Path) -> None:
    src = sqlite3.connect(str(db_path), timeout=30)
    dst = sqlite3.connect(str(out))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create_backup(conn, paths, kind: str, actor: str, dest: Path | None = None) -> dict:
    """Create and verify a full backup. Returns the backups-table row."""
    cfg = settings_store.get(conn, "backup")
    dest = validate_destination(paths, Path(dest or cfg.get("destination") or paths.backups))
    stamp = timeutil.now_local().strftime("%Y%m%d_%H%M%S")
    name = f"RelayEmployeeHub_backup_{kind}_{stamp}.zip"
    final = dest / name
    n = 1
    while final.exists():
        n += 1
        final = dest / f"RelayEmployeeHub_backup_{kind}_{stamp}_{n}.zip"
    with db.transaction(conn):
        bid = conn.execute("INSERT INTO backups(filename, path, kind, status, created_at, created_by) "
                           "VALUES(?,?,?,?,?,?)", (final.name, str(final), kind, "running", timeutil.iso_utc(),
                                                  actor)).lastrowid
    partial = final.with_suffix(".zip.partial")
    try:
        with tempfile.TemporaryDirectory(dir=paths.staging) as tmpd:
            snap = Path(tmpd) / "employees.db"
            snapshot_db(paths.db, snap)
            files: dict[str, dict] = {}
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                                 allowZip64=True) as zf:
                def add(src: Path, arc: str, compress=True):
                    files[arc] = {"sha256": _sha256(src), "size": src.stat().st_size}
                    zf.write(src, arc, compress_type=zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED)

                add(snap, "employees.db")
                if paths.config.exists():
                    add(paths.config, "config.json")
                include_zips = bool(cfg.get("include_package_zips"))
                for sub in INCLUDED_DIRS:
                    base = paths.root / sub
                    if not base.exists():
                        continue
                    for f in sorted(base.rglob("*")):
                        if not f.is_file() or f.name.endswith(".partial"):
                            continue
                        if sub == "exports/packages" and f.suffix.lower() == ".zip" and not include_zips:
                            continue
                        arc = f.relative_to(paths.root).as_posix()
                        add(f, arc, compress=f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".zip"))
                # Every photo referenced by the snapshot must be in the archive.
                sconn = sqlite3.connect(str(snap))
                try:
                    referenced = [r[0] for r in sconn.execute("SELECT stored_path FROM photos WHERE stored_path != ''")]
                    schema = sconn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                    instance = sconn.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()
                finally:
                    sconn.close()
                missing = [p for p in referenced if f"images/{p}" not in files]
                if missing:
                    raise BackupError(f"{len(missing)} photo file(s) referenced by the database are missing from "
                                      f"storage (for example images/{missing[0]}).")
                manifest = {"format": FORMAT, "format_version": 1, "app_version": __version__,
                            "schema_version": int(schema[0]) if schema else None,
                            "instance_id": instance[0] if instance else None, "kind": kind,
                            "created_at": timeutil.iso_utc(), "created_by": actor, "hostname": socket.gethostname(),
                            "file_count": len(files), "files": files}
                zf.writestr(MANIFEST, json.dumps(manifest, indent=1))
        os.replace(partial, final)
        info = verify_backup(final, paths)
        size = final.stat().st_size
        digest = _sha256(final)
        with db.transaction(conn):
            conn.execute("UPDATE backups SET status='verified', size=?, sha256=?, file_count=?, verified_at=? "
                         "WHERE id=?", (size, digest, info["file_count"], timeutil.iso_utc(), bid))
            audit.log(conn, actor, "backup.create", "backup", bid, f"{kind.capitalize()} backup {final.name} created "
                      f"and verified ({size / 1048576:.1f} MB)", {"path": str(final)})
        apply_retention(conn, paths)
    except Exception as exc:
        try:
            if partial.exists():
                partial.unlink()
        except OSError:
            pass
        with db.transaction(conn):
            conn.execute("UPDATE backups SET status='failed', error=? WHERE id=?", (str(exc), bid))
            audit.log(conn, actor, "backup.failed", "backup", bid, f"Backup failed: {exc}")
        raise BackupError(f"Backup failed: {exc}") from exc
    return db.one(conn, "SELECT * FROM backups WHERE id=?", (bid,))


def _safe_name(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\") or "\\" in name or "\x00" in name:
        return False
    if len(name) > 1 and name[1] == ":":
        return False
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return False
    top = parts[0]
    return name in ("employees.db", "config.json", MANIFEST) or top in ("images", "thumbnails", "exports")


def verify_backup(path: Path, paths=None, check_db: bool = True) -> dict:
    """Validate structure, paths, hashes and database integrity. Raises BackupError."""
    if not zipfile.is_zipfile(path):
        raise BackupError("Not a valid backup ZIP file.")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        unsafe = [n for n in names if not _safe_name(n)]
        if unsafe:
            raise BackupError(f"The backup contains unsafe or unexpected paths (for example '{unsafe[0]}') and was "
                              "rejected.")
        if MANIFEST not in names:
            raise BackupError("The backup has no manifest; it was not created by Relay Employee Hub.")
        manifest = json.loads(zf.read(MANIFEST))
        if manifest.get("format") != FORMAT:
            raise BackupError("Unrecognised backup format.")
        listed = manifest.get("files", {})
        extra = set(names) - set(listed) - {MANIFEST}
        if extra:
            raise BackupError(f"The backup contains files not listed in its manifest (for example '{sorted(extra)[0]}').")
        if "employees.db" not in listed:
            raise BackupError("The backup does not contain a database.")
        for arc, meta in listed.items():
            if arc not in names:
                raise BackupError(f"File listed in the manifest is missing: {arc}")
            h = hashlib.sha256()
            size = 0
            with zf.open(arc) as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
                    size += len(chunk)
            if h.hexdigest() != meta["sha256"] or size != meta["size"]:
                raise BackupError(f"Integrity check failed for {arc} (the backup is damaged).")
        if check_db:
            tmp_parent = paths.staging if paths else None
            with tempfile.TemporaryDirectory(dir=tmp_parent) as tmpd:
                out = Path(tmpd) / "check.db"
                with zf.open("employees.db") as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                c = sqlite3.connect(str(out))
                try:
                    res = c.execute("PRAGMA integrity_check").fetchone()[0]
                    if res != "ok":
                        raise BackupError(f"Database integrity check failed: {res}")
                    counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                              for t in ("employees", "photos", "users", "tasks", "task_periods")}
                finally:
                    c.close()
        else:
            counts = {}
    return {"manifest": {k: v for k, v in manifest.items() if k != "files"}, "file_count": len(listed),
            "counts": counts}


def apply_retention(conn, paths) -> list[str]:
    cfg = settings_store.get(conn, "backup")
    removed = []
    for kind, keep in (("daily", int(cfg.get("retention_daily") or 30)),
                       ("manual", int(cfg.get("retention_manual") or 0))):
        if keep <= 0:
            continue
        rows = db.rows(conn, "SELECT * FROM backups WHERE kind=? AND status='verified' ORDER BY id DESC", (kind,))
        for r in rows[keep:]:
            p = Path(r["path"])
            try:
                if p.exists() and p.name.startswith("RelayEmployeeHub_backup_"):
                    p.unlink()
                removed.append(p.name)
                with db.transaction(conn):
                    conn.execute("UPDATE backups SET status='deleted' WHERE id=?", (r["id"],))
            except OSError:
                continue
    return removed


def list_backups(conn, paths) -> list[dict]:
    items = db.rows(conn, "SELECT * FROM backups WHERE status != 'deleted' ORDER BY id DESC LIMIT 200")
    for it in items:
        it["exists"] = Path(it["path"]).exists()
    return items


def daily_due(conn) -> bool:
    cfg = settings_store.get(conn, "backup")
    if not cfg.get("daily_enabled"):
        return False
    now = timeutil.now_local()
    hh, mm = [int(x) for x in (cfg.get("daily_time") or "13:00").split(":")]
    if (now.hour, now.minute) < (hh, mm):
        return False
    last = db.one(conn, "SELECT created_at FROM backups WHERE kind='daily' AND status IN ('verified','deleted') "
                        "ORDER BY id DESC LIMIT 1")
    if last:
        last_local = timeutil.parse_iso_utc(last["created_at"]).astimezone(timeutil.tz()).date()
        if last_local == now.date():
            return False
    failed_today = db.scalar(conn, "SELECT COUNT(*) FROM backups WHERE kind='daily' AND status='failed' AND "
                                   "created_at >= ?", (timeutil.iso_utc(timeutil.now_utc().replace(hour=0, minute=0,
                                                                                                  second=0)),))
    return failed_today < 3


# ------------------------------------------------------------------ restore

def _writable_rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def restore(paths, backup_path: Path, actor: str, close_connections) -> dict:
    """Replace the live data with a verified backup.

    The caller must have taken the pre-restore backup and stopped background
    work. close_connections() must release every open database connection.
    """
    info = verify_backup(backup_path, paths)
    schema = info["manifest"].get("schema_version") or 0
    if schema > db.SCHEMA_VERSION:
        raise BackupError("This backup was made by a newer version of the application. Install that version first.")
    work = paths.staging / f"restore_{timeutil.now_local().strftime('%Y%m%d_%H%M%S')}"
    new_dir, old_dir = work / "new", work / "old"
    new_dir.mkdir(parents=True)
    old_dir.mkdir()
    with zipfile.ZipFile(backup_path) as zf:
        for name in zf.namelist():
            if name == MANIFEST or name == "config.json":
                continue  # server settings stay specific to this computer
            target = (new_dir / name).resolve()
            if not paths.inside(target, new_dir):
                raise BackupError(f"Unsafe path rejected: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    close_connections()
    moved = []
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(paths.db) + suffix)
            if src.exists():
                os.replace(src, old_dir / (paths.db.name + suffix))
                moved.append(("file", suffix))
        for sub in ("images", "thumbnails", "exports/packages"):
            cur = paths.root / sub
            if cur.exists():
                dest = old_dir / sub
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(cur, dest)
                moved.append(("dir", sub))
        os.replace(new_dir / "employees.db", paths.db)
        for sub in ("images", "thumbnails", "exports/packages"):
            src = new_dir / sub
            dest = paths.root / sub
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                os.replace(src, dest)
            else:
                dest.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Roll back to the previous files.
        for kind, item in reversed(moved):
            if kind == "file":
                live = Path(str(paths.db) + item)
                if live.exists():
                    live.unlink()
                os.replace(old_dir / (paths.db.name + item), live)
            else:
                live = paths.root / item
                if live.exists():
                    _writable_rmtree(live)
                os.replace(old_dir / item, live)
        raise
    _writable_rmtree(work)
    return info


def after_restore(conn, actor: str, reason: str, pre_restore: dict | None) -> None:
    """Disable automatic sending until an administrator revalidates; log out everyone."""
    with db.transaction(conn):
        settings_store.set(conn, "revalidation_required",
                           {"reason": reason, "since": timeutil.iso_utc(), "by": actor}, actor)
        conn.execute("UPDATE tasks SET auto_send_validated_at=NULL, auto_send_validated_by=NULL")
        conn.execute("DELETE FROM sessions")
        conn.execute("UPDATE dispatches SET status='uncertain', error='Interrupted by restore' WHERE status='sending'")
        if pre_restore:
            conn.execute("INSERT INTO backups(filename, path, kind, status, created_at, created_by, size, sha256, "
                         "file_count, verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (pre_restore["filename"], pre_restore["path"], pre_restore["kind"], pre_restore["status"],
                          pre_restore["created_at"], pre_restore["created_by"], pre_restore["size"],
                          pre_restore["sha256"], pre_restore["file_count"], pre_restore["verified_at"]))
        audit.log(conn, actor, "backup.restore", "backup", None, reason)
