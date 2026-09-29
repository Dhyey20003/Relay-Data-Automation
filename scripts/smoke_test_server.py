"""Smoke-test a running Relay Employee Hub (source or packaged) with synthetic data.

    python scripts/smoke_test_server.py http://127.0.0.1:8765

Uses a brand-new data folder only (it performs first-run setup). Switches email
to dry-run mode, so nothing is ever sent.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def main(base: str) -> int:
    c = httpx.Client(base_url=base, timeout=60)
    for _ in range(60):
        try:
            if c.get("/api/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    h = {"X-Requested-With": "RelayHub"}
    health = c.get("/api/health").json()
    assert health["setup_required"], "use an empty data folder for the smoke test"
    c.post("/api/setup", json={"email": "smoke@example.invalid", "display_name": "Smoke", "password": "SmokeTest123!"},
           headers=h).raise_for_status()
    h["X-CSRF-Token"] = c.get("/api/me").json()["csrf"]
    print("outlook status:", c.get("/api/outlook/status").json()["message"])
    c.patch("/api/settings", json={"mail_adapter": "dry_run"}, headers=h).raise_for_status()
    up = c.post("/api/imports/upload", headers=h,
                files={"file": ("roster.xlsx", (SAMPLES / "sample_current_roster.xlsx").read_bytes())}).json()
    ins = c.get(f"/api/imports/{up['batch_id']}/inspect?header_row=4").json()
    cfg = {"purpose": "roster", "sheet": ins["sheet"], "header_row": 4, "mapping": ins["suggested_mapping"],
           "options": {}, "scope": []}
    prev = c.post(f"/api/imports/{up['batch_id']}/preview", json={"config": cfg, "decisions": {}}, headers=h).json()
    assert prev["summary"]["new"] == 50, prev["summary"]
    c.post(f"/api/imports/{up['batch_id']}/commit", json={"config": cfg}, headers=h).raise_for_status()
    bid = c.post("/api/photo-batches", headers=h).json()["id"]
    files = [("files", ("photos.zip", (SAMPLES / "sample_photos.zip").read_bytes())),
             ("files", ("sheet.pdf", (SAMPLES / "sample_headshots.pdf").read_bytes()))]
    rep = c.post(f"/api/photo-batches/{bid}/upload", files=files, data={"relpaths": ["photos.zip", "sheet.pdf"]},
                 headers=h).json()
    assert rep["pdf_pages"] == 3 and rep["added"] >= 15, rep
    saved = c.post(f"/api/photo-batches/{bid}/commit", headers=h).json()
    assert saved["saved"] >= 6, saved
    tid = [t for t in c.get("/api/tasks").json()["tasks"] if t["kind"] == "HR"][0]["id"]
    c.patch(f"/api/tasks/{tid}", json={"inclusion": "all", "email": {"to": "hr@example.invalid"}},
            headers=h).raise_for_status()
    pid = c.post(f"/api/tasks/{tid}/start-period", headers=h).json()["period_id"]
    c.post(f"/api/periods/{pid}/prepare", headers=h).raise_for_status()
    sent = c.post(f"/api/periods/{pid}/send", json={"confirm": True, "allow_incomplete": True}, headers=h).json()
    assert sent["status"] == "submitted", sent
    bk = c.post("/api/backups", headers=h).json()
    assert bk["status"] == "verified", bk
    print(f"SMOKE TEST PASSED: imported 50, saved {saved['saved']} photos, dry-run send {sent['dispatch_ref']}, "
          f"backup {bk['filename']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"))
