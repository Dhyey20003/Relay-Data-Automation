import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from conftest import ADMIN, upload_photo
from relay_hub import backup, db
from relay_hub.config import resource_dir


def test_backup_restore_with_photos(env):
    e = env.employee(rhc_id="BK1", full_name="Backed Up")
    pid = upload_photo(env, e["id"], (5, 5, 5))
    env.api.post("/api/custom-fields", {"label": "Team", "field_type": "text"})
    env.api.post("/api/import-templates", {"name": "Tpl", "purpose": "partial", "mapping": {"RHC ID": "rhc_id"}})
    tid = env.api.get("/api/tasks").json()["tasks"][0]["id"]
    env.api.patch(f"/api/tasks/{tid}", {"inclusion": "all", "email": {"to": "t@example.invalid"}})
    env.api.post(f"/api/tasks/{tid}/auto-send", {"enable": True, "confirm": True})
    b = env.api.post("/api/backups").json()
    assert b["status"] == "verified" and Path(b["path"]).exists()
    assert not env.ctx.paths.inside(Path(b["path"])) or env.ctx.paths.inside(Path(b["path"]), env.ctx.paths.backups)
    with zipfile.ZipFile(b["path"]) as zf:
        names = zf.namelist()
        assert "employees.db" in names and any(n.startswith("images/") for n in names)
        assert not any(n.startswith("backups/") for n in names), "backups never include themselves"
    # Damage the live data after the backup.
    env.employee(rhc_id="BK2", full_name="After Backup")
    photo_file = next((env.ctx.paths.images / str(e["id"])).iterdir())
    photo_file.unlink()
    r = env.api.post("/api/backups/restore", {"backup_id": b["id"], "confirm_text": "nope"})
    assert r.status_code == 422
    r = env.api.post("/api/backups/restore", {"backup_id": b["id"], "confirm_text": "RESTORE"})
    assert r.status_code == 200, r.text
    assert r.json()["pre_restore_backup"].startswith("RelayEmployeeHub_backup_pre-restore_")
    # Everyone is signed out after a restore.
    assert env.api.get("/api/employees").status_code == 401
    env.api.login(*ADMIN)
    rhcs = {i["rhc_id"] for i in env.api.get("/api/employees").json()["items"]}
    assert rhcs == {"BK1"}
    assert env.api.get(f"/api/photos/{pid}/file").status_code == 200, "photo restored"
    assert env.api.get("/api/custom-fields").json()[0]["label"] == "Team"
    assert env.api.get("/api/import-templates").json()[0]["name"] == "Tpl"
    me = env.api.get("/api/me").json()
    assert me["revalidation_required"], "automatic sending waits for revalidation"
    t = env.api.get("/api/tasks").json()["tasks"][0]
    assert t["auto_send_blockers"]
    assert env.api.post("/api/settings/revalidate", {"task_ids": [tid], "confirm": True}).status_code == 200
    assert env.api.get("/api/tasks").json()["tasks"][0]["auto_send_blockers"] == []
    kinds = {x["kind"] for x in env.api.get("/api/backups").json()["backups"]}
    assert {"manual", "pre-restore"} <= kinds


def _tamper(src: Path, dst: Path, mutate):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            name, data = mutate(item.filename, data)
            if name:
                zout.writestr(name, data)
    return dst


def test_restore_rejects_unsafe_or_damaged_archives(env, tmp_path):
    env.employee(rhc_id="SAFE", full_name="Safe")
    b = env.api.post("/api/backups").json()
    src = Path(b["path"])
    slip = _tamper(src, tmp_path / "slip.zip", lambda n, d: (n, d))
    with zipfile.ZipFile(slip, "a") as zf:
        zf.writestr("../../outside.txt", "x")
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup.verify_backup(slip, env.ctx.paths)
    damaged = _tamper(src, tmp_path / "damaged.zip",
                      lambda n, d: (n, d + b"x" if n == "employees.db" else d))
    with pytest.raises(backup.BackupError, match="Integrity"):
        backup.verify_backup(damaged, env.ctx.paths)
    nomanifest = _tamper(src, tmp_path / "nom.zip", lambda n, d: (None if n == backup.MANIFEST else n, d))
    with pytest.raises(backup.BackupError, match="manifest"):
        backup.verify_backup(nomanifest, env.ctx.paths)
    r = env.api.post("/api/backups/restore-upload?confirm_text=RESTORE",
                     files={"file": ("slip.zip", slip.read_bytes(), "application/zip")})
    assert r.status_code == 422
    assert env.api.get("/api/employees").json()["total"] == 1, "live data untouched"


def test_backup_destination_rules_and_retention(env, tmp_path):
    r = env.api.patch("/api/settings", {"backup": {"destination": str(env.ctx.paths.images / "bk")}})
    assert r.status_code == 422, "cannot back up into the data being backed up"
    ext = tmp_path / "external_drive"
    r = env.api.patch("/api/settings", {"backup": {"destination": str(ext), "retention_manual": 2}})
    assert r.status_code == 200
    for _ in range(3):
        env.api.post("/api/backups")
    files = sorted(ext.glob("RelayEmployeeHub_backup_manual_*.zip"))
    assert len(files) == 2
    with env.conn() as conn:
        assert db.scalar(conn, "SELECT COUNT(*) FROM backups WHERE status='deleted'") == 1


def test_all_frontend_assets_are_local(env):
    static = resource_dir() / "static"
    external = re.compile(r"""(src|href)\s*=\s*["']https?://|@import\s+url\(\s*["']?https?://|from\s+["']https?://""")
    for f in list(static.rglob("*.html")) + list(static.rglob("*.js")) + list(static.rglob("*.css")):
        text = f.read_text(encoding="utf-8")
        assert not external.search(text), f"{f.name} loads a remote resource"
    html = env.api.get("/").text
    for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert ref.startswith("/static/"), ref
        assert env.api.get(ref).status_code == 200
    for js in ["api.js", "ui.js", "main.js", "views/employees.js", "views/imports.js", "views/photos.js",
               "views/tasks.js", "views/settings.js"]:
        assert env.api.get(f"/static/js/{js}").status_code == 200
    csp = env.api.get("/").headers["content-security-policy"]
    assert "default-src 'self'" in csp and "connect-src 'self'" in csp


def test_licenses_and_samples_available(env):
    text = env.api.get("/api/licenses").text
    for name in ("FastAPI", "openpyxl", "Pillow", "pypdfium2", "PDFium"):
        assert name in text
    samples = {s["name"] for s in env.api.get("/api/samples").json()}
    assert "sample_current_roster.xlsx" in samples
    r = env.api.get("/api/samples/sample_current_roster.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert env.api.get("/api/samples/..%2Fconfig.py").status_code == 404
    _ = io, json
