import datetime as dt
import io
import json
import os
import threading
import zipfile
from pathlib import Path

from PIL import Image

from conftest import image_bytes, upload_photo
from relay_hub import db, dispatch, tasks, timeutil

IST = timeutil.tz


def task_id(env, kind):
    return [t for t in env.api.get("/api/tasks").json()["tasks"] if t["kind"] == kind][0]["id"]


def configure(env, kind, **extra):
    tid = task_id(env, kind)
    body = {"inclusion": "all", "email": {"to": "team@example.invalid", "cc": "", "sender": ""}, **extra}
    r = env.api.patch(f"/api/tasks/{tid}", body)
    assert r.status_code == 200, r.text
    return tid


def start(env, tid):
    r = env.api.post(f"/api/tasks/{tid}/start-period")
    assert r.status_code == 200, r.text
    return r.json()["period_id"]


def prepare(env, pid):
    r = env.api.post(f"/api/periods/{pid}/prepare")
    assert r.status_code == 200, r.text
    return env.api.get(f"/api/periods/{pid}").json()


def zip_names(env, package_id):
    r = env.api.get(f"/api/packages/{package_id}/zip")
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content))


def people(env):
    a1 = env.employee(rhc_id="RHC1", full_name="Amd One", location="Ahmedabad")
    a2 = env.employee(rhc_id="RHC2", full_name="Amd Two", location="Ahmedabad")
    v1 = env.employee(rhc_id="RHC3", full_name="Vad One", location="Vadodara")
    nophoto = env.employee(rhc_id="RHC4", full_name="Vad No Photo", location="Vadodara")
    upload_photo(env, a1["id"], (1, 2, 3), name="headshot.jpg")
    upload_photo(env, a2["id"], (4, 5, 6), name="headshot.jpg")  # same original name: duplicate
    upload_photo(env, v1["id"], (7, 8, 9), fmt="PNG", name="vad.png")
    return a1, a2, v1, nophoto


def test_city_zips_names_and_missing_photos(env):
    a1, a2, v1, nophoto = people(env)
    tid = configure(env, "IT")
    d = prepare(env, start(env, tid))
    pk = {p["location"]: p for p in d["packages"] if p["current"]}
    assert pk["Ahmedabad"]["zip_name"].startswith("Ahmedabad_IT_Headshots_") and pk["Ahmedabad"]["zip_name"].endswith(".zip")
    assert sorted(zip_names(env, pk["Ahmedabad"]["id"]).namelist()) == ["headshot_RHC1.jpg", "headshot_RHC2.jpg"]
    assert zip_names(env, pk["Vadodara"]["id"]).namelist() == ["vad.png"]
    assert pk["Vadodara"]["is_complete"] == 0 and pk["Ahmedabad"]["is_complete"] == 1
    assert pk["Vadodara"]["exclusions"] == [{"employee_id": nophoto["id"], "rhc_id": "RHC4",
                                             "full_name": "Vad No Photo", "reason": "No photo on file"}]
    assert d["period"]["status"] == "Needs Attention", "incomplete packages are never labelled complete"
    assert {r["renamed_from"] for r in pk["Ahmedabad"]["renamed"]} == {"headshot.jpg"}
    m = json.loads(env.api.get(f"/api/packages/{pk['Ahmedabad']['id']}/manifest").content)
    assert m["reporting_period"] and m["generated_at"] and len(m["employees"]) == 2
    assert {e["photo_id"] for e in m["employees"]} and m["exclusions"] == []


def test_hr_package_uses_rhc_ids_and_real_jpeg(env):
    a1, a2, v1, _ = people(env)
    tid = configure(env, "HR")
    d = prepare(env, start(env, tid))
    pk = {p["location"]: p for p in d["packages"] if p["current"]}
    assert "_HR_Headshots_" in pk["Vadodara"]["zip_name"] and len(d["period"]["period_key"]) == 7
    zf = zip_names(env, pk["Vadodara"]["id"])
    assert zf.namelist() == ["RHC3.jpg"]
    data = zf.read("RHC3.jpg")
    assert data[:3] == b"\xff\xd8\xff", "PNG source genuinely converted to JPEG"
    assert Image.open(io.BytesIO(data)).format == "JPEG"
    original = (env.ctx.paths.images / f"{v1['id']}").glob("*.png")
    assert list(original), "original upload untouched"


def test_it_jpg_option_and_naming_template(env):
    people(env)
    tid = configure(env, "IT", naming_template="{rhc_id}_{last_name}", export_format="jpg")
    d = prepare(env, start(env, tid))
    pk = {p["location"]: p for p in d["packages"] if p["current"]}
    zf = zip_names(env, pk["Vadodara"]["id"])
    assert zf.namelist() == ["RHC3_One.jpg"] and zf.read("RHC3_One.jpg")[:2] == b"\xff\xd8"
    r = env.api.patch(f"/api/tasks/{tid}", {"naming_template": "{bogus}"})
    assert r.status_code == 422


def test_packages_are_immutable_snapshots_with_revisions(env):
    people(env)
    tid = configure(env, "IT")
    pid = start(env, tid)
    d1 = prepare(env, pid)
    first = [p for p in d1["packages"] if p["location"] == "Ahmedabad"][0]
    with env.conn() as conn:
        path = conn.execute("SELECT zip_path FROM packages WHERE id=?", (first["id"],)).fetchone()[0]
    assert not os.access(path, os.W_OK) or os.name == "nt" or os.getuid() == 0
    d2 = prepare(env, pid)
    revs = sorted((p["revision"], p["current"]) for p in d2["packages"] if p["location"] == "Ahmedabad")
    assert revs == [(1, False), (2, True)]
    assert Path(path).exists(), "earlier revision kept"


def full_send(env, pid, allow=True):
    r = env.api.post(f"/api/periods/{pid}/send", {"confirm": True, "allow_incomplete": allow})
    assert r.status_code == 200, r.text
    return r.json()


def test_send_then_reconcile_advances_baseline_and_changed_mode(env):
    a1, a2, v1, nophoto = people(env)
    tid = configure(env, "IT", inclusion="changed")
    pid = start(env, tid)
    prepare(env, pid)
    d = full_send(env, pid)
    assert d["status"] == "submitted" and env.mail.calls[0]["send"] is True
    assert sorted(env.mail.calls[0]["attachments"]) == sorted(
        [p["zip_name"] for p in env.api.get(f"/api/periods/{pid}").json()["packages"] if p["zip_size"]])
    assert d["dispatch_ref"] in env.mail.calls[0]["body"]
    assert env.api.get(f"/api/periods/{pid}").json()["period"]["status"] == "Submitted to Outlook"
    with env.conn() as conn:
        assert db.scalar(conn, "SELECT COUNT(*) FROM export_baseline") == 0, "no baseline before confirmation"
    env.api.post(f"/api/dispatches/{d['id']}/reconcile")
    assert env.api.get(f"/api/periods/{pid}").json()["period"]["status"] == "Confirmed in Sent Items"
    with env.conn() as conn:
        assert db.scalar(conn, "SELECT COUNT(*) FROM export_baseline") == 3
    # Next period: nothing changed -> nothing included.
    upload_photo(env, nophoto["id"], (9, 9, 9), name="new.jpg")  # new headshot
    env.api.patch(f"/api/employees/{a2['id']}", {"version": a2["version"], "changes": {"location": "Vadodara"}})
    with env.conn() as conn:
        task = tasks.load_task(conn, tid)
        p2, _ = tasks.create_period(conn, task, {"period_key": "2099-01-01", "period_label": "next",
                                                 "due_at": timeutil.iso_utc()}, origin="manual")
    d2 = prepare(env, p2)
    pk = {p["location"]: p for p in d2["packages"] if p["current"]}
    assert pk["Ahmedabad"]["included_count"] == 0 and pk["Ahmedabad"]["unchanged"] == 1
    assert sorted(zip_names(env, pk["Vadodara"]["id"]).namelist()) == ["headshot.jpg", "new.jpg"], \
        "new photo and location change are included"


def test_failed_and_uncertain_sends(env):
    people(env)
    tid = configure(env, "IT")
    pid = start(env, tid)
    prepare(env, pid)
    env.mail.result = "failed"
    d = full_send(env, pid)
    assert d["status"] == "failed"
    assert env.api.get(f"/api/periods/{pid}").json()["period"]["status"] == "Failed"
    env.mail.result = "uncertain"
    d = full_send(env, pid)
    assert d["status"] == "uncertain"
    assert env.api.get(f"/api/periods/{pid}").json()["period"]["status"] == "Delivery Uncertain"
    r = env.api.post(f"/api/periods/{pid}/send", {"confirm": True, "allow_incomplete": True})
    assert r.status_code == 409, "an uncertain send blocks any new send"
    with env.conn() as conn:
        assert db.scalar(conn, "SELECT COUNT(*) FROM export_baseline") == 0
    env.mail.found = "not_found"
    env.api.post(f"/api/dispatches/{d['id']}/reconcile")
    assert env.api.get(f"/api/periods/{pid}").json()["period"]["status"] == "Delivery Uncertain"
    r = env.api.post(f"/api/dispatches/{d['id']}/resolve", {"action": "not_sent", "note": "Checked Outbox and Sent Items"})
    assert r.status_code == 200 and r.json()["status"] == "abandoned"
    env.mail.result = "normal"
    assert full_send(env, pid)["status"] == "submitted"
    assert len(env.mail.calls) == 3


def test_duplicate_send_prevention_under_concurrency(env):
    people(env)
    tid = configure(env, "IT")
    pid = start(env, tid)
    prepare(env, pid)
    env.mail.delay = 0.5
    results = []

    def go():
        c = env.new_client()
        c.c.cookies = env.api.c.cookies
        c.csrf = env.api.csrf
        results.append(c.post(f"/api/periods/{pid}/send", {"confirm": True, "allow_incomplete": True}).status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409, 409, 409]
    assert len(env.mail.calls) == 1


def test_draft_discard_manual_completion_and_ready_folder(env):
    people(env)
    tid = configure(env, "HR")
    pid = start(env, tid)
    prepare(env, pid)
    r = env.api.post(f"/api/periods/{pid}/draft", {"allow_incomplete": True})
    assert r.json()["status"] == "draft_created" and env.mail.calls[0]["send"] is False
    assert env.api.post(f"/api/periods/{pid}/send", {"confirm": True, "allow_incomplete": True}).status_code == 409
    env.api.post(f"/api/dispatches/{r.json()['id']}/resolve", {"action": "discard_draft"})
    rf = env.api.post(f"/api/periods/{pid}/ready-folder").json()
    folder = Path(rf["folder"])
    assert sorted(p.name for p in folder.glob("*.zip")) == sorted(rf["files"])
    assert (folder / "email_body.txt").read_text().strip()
    r = env.api.post(f"/api/periods/{pid}/manual-complete", {"note": "Sent from Outlook on the web", "packages_sent": True})
    assert r.status_code == 200 and r.json()["status"] == "Manually Completed"
    with env.conn() as conn:
        assert db.scalar(conn, "SELECT COUNT(*) FROM export_baseline") == 3
    actions = {a["action"] for a in env.api.get("/api/audit?action=task").json()["items"]}
    assert "task.manual_complete" in actions


def test_send_requires_recipients_and_size_limit(env):
    people(env)
    tid = task_id(env, "IT")
    env.api.patch(f"/api/tasks/{tid}", {"inclusion": "all"})
    pid = start(env, tid)
    prepare(env, pid)
    r = env.api.post(f"/api/periods/{pid}/send", {"confirm": True, "allow_incomplete": True})
    assert r.status_code == 422 and "recipients" in r.json()["error"]
    configure(env, "IT", max_attachment_mb=1)
    with env.conn() as conn:
        conn.execute("UPDATE packages SET zip_size=5000000")
    r = env.api.post(f"/api/periods/{pid}/send", {"confirm": True, "allow_incomplete": True})
    assert r.status_code == 422 and "limit" in r.json()["error"]
    assert env.mail.calls == []


# ------------------------------------------------------------ scheduling

def test_schedule_math():
    it = {"kind": "IT", "schedule": {"type": "interval", "anchor_date": "2026-10-01", "interval_days": 14, "time": "10:00"}}
    days = tasks.occurrence_dates(it, dt.date(2026, 9, 1), dt.date(2026, 11, 30))
    assert days == [dt.date(2026, 10, 1), dt.date(2026, 10, 15), dt.date(2026, 10, 29), dt.date(2026, 11, 12),
                    dt.date(2026, 11, 26)]
    hr = {"kind": "HR", "schedule": {"type": "monthly", "day": 31, "time": "09:30"}}
    days = tasks.occurrence_dates(hr, dt.date(2027, 1, 1), dt.date(2028, 4, 30))
    assert dt.date(2027, 2, 28) in days and dt.date(2028, 2, 29) in days and dt.date(2027, 4, 30) in days
    p = tasks.period_for(hr, dt.date(2027, 2, 28))
    assert p["period_key"] == "2027-02" and p["due_at"] == "2027-02-28T04:00:00Z", "09:30 IST == 04:00 UTC"
    assert tasks.period_for(it, dt.date(2026, 10, 1))["period_key"] == "2026-10-01"


def _enable(env, tid, since):
    """Enable via the API, then pretend the schedule started tracking at `since`."""
    r = env.api.patch(f"/api/tasks/{tid}", {"enabled": True})
    assert r.status_code == 200, r.text
    with env.conn() as conn:
        conn.execute("UPDATE tasks SET tracking_start=?, scheduled_through=? WHERE id=?",
                     (timeutil.iso_utc(since), timeutil.iso_utc(since), tid))


def test_scheduler_auto_sends_once_enabled_and_is_restart_safe(env):
    people(env)
    tid = configure(env, "IT", schedule={"anchor_date": "2026-10-01", "interval_days": 14, "time": "10:00"})
    _enable(env, tid, dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc))
    r = env.api.post(f"/api/tasks/{tid}/auto-send", {"enable": True, "confirm": False})
    assert r.status_code == 422, "explicit confirmation required"
    from relay_hub.context import Scheduler
    sched = Scheduler(env.ctx)
    timeutil.freeze(dt.datetime(2026, 10, 1, 5, 0, tzinfo=dt.timezone.utc))  # 10:30 IST
    sched.tick()
    with env.conn() as conn:
        per = db.rows(conn, "SELECT * FROM task_periods WHERE task_id=?", (tid,))
    assert len(per) == 1 and per[0]["period_key"] == "2026-10-01"
    assert per[0]["status"] == "Needs Attention", "incomplete package: not sent automatically"
    assert env.mail.calls == []
    sched.tick()
    with env.conn() as conn:
        assert db.scalar(conn, "SELECT COUNT(*) FROM task_periods") == 1, "no duplicate period on a second tick"
    # Complete package + automatic sending explicitly enabled -> sends exactly once.
    timeutil.freeze(None)
    r = env.api.post(f"/api/tasks/{tid}/auto-send", {"enable": True, "confirm": True})
    assert r.status_code == 200 and not r.json()["auto_send_blockers"], r.text
    with env.conn() as conn:
        conn.execute("DELETE FROM employees WHERE rhc_id='RHC4'")
    timeutil.freeze(dt.datetime(2026, 10, 15, 5, 0, tzinfo=dt.timezone.utc))
    sched.tick()
    sched.tick()
    with env.conn() as conn:
        dbg = db.rows(conn, "SELECT period_key, status, status_note FROM task_periods")
    assert len(env.mail.calls) == 1 and env.mail.calls[0]["send"], dbg
    with env.conn() as conn:
        st = db.one(conn, "SELECT status FROM task_periods WHERE period_key='2026-10-15'")["status"]
    assert st == "Submitted to Outlook"
    # Revalidation required (e.g. after restore) pauses automatic sending.
    with env.conn() as conn:
        from relay_hub import settings_store
        settings_store.set(conn, "revalidation_required", {"reason": "test"})
    timeutil.freeze(dt.datetime(2026, 10, 29, 5, 0, tzinfo=dt.timezone.utc))
    sched.tick()
    assert len(env.mail.calls) == 1
    with env.conn() as conn:
        note = db.one(conn, "SELECT status_note FROM task_periods WHERE period_key='2026-10-29'")["status_note"]
    assert "revalidate" in note


def test_backlog_and_overdue_are_held_for_review(env):
    people(env)
    with env.conn() as conn:
        conn.execute("DELETE FROM employees WHERE rhc_id='RHC4'")
    tid = configure(env, "IT", schedule={"anchor_date": "2026-10-01", "interval_days": 14, "time": "10:00"})
    _enable(env, tid, dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc))
    env.api.post(f"/api/tasks/{tid}/auto-send", {"enable": True, "confirm": True})
    from relay_hub.context import Scheduler
    sched = Scheduler(env.ctx)
    # Application was off for six weeks: three periods became due.
    timeutil.freeze(dt.datetime(2026, 11, 10, 5, 0, tzinfo=dt.timezone.utc))
    sched.tick()
    with env.conn() as conn:
        rows = db.rows(conn, "SELECT status, status_note FROM task_periods ORDER BY due_at")
    assert len(rows) == 3 and all(r["status"] == "Needs Attention" and "Backlog" in r["status_note"] for r in rows)
    assert env.mail.calls == [], "a backlog is never sent automatically"
    # A single period discovered long after its due time is also held.
    with env.conn() as conn:
        conn.execute("UPDATE tasks SET scheduled_through=? WHERE id=?", ("2026-11-10T05:00:00Z", tid))
    timeutil.freeze(dt.datetime(2026, 11, 14, 5, 0, tzinfo=dt.timezone.utc))  # due 12 Nov, 2+ days late
    sched.tick()
    with env.conn() as conn:
        row = db.one(conn, "SELECT status, status_note FROM task_periods WHERE period_key='2026-11-12'")
    assert row["status"] == "Needs Attention" and "Overdue" in row["status_note"]
    assert env.mail.calls == []


def test_restart_recovery(env):
    people(env)
    tid = configure(env, "IT")
    pid = start(env, tid)
    prepare(env, pid)
    with env.conn() as conn:
        conn.execute("UPDATE task_periods SET status='Preparing' WHERE id=?", (pid,))
        task = tasks.load_task(conn, tid)
        p2, _ = tasks.create_period(conn, task, {"period_key": "X2", "period_label": "x", "due_at": timeutil.iso_utc()})
        conn.execute("INSERT INTO dispatches(period_id, task_id, dispatch_ref, mode, adapter, status, created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (p2, tid, "REF-1", "send", "mock", "sending", timeutil.iso_utc()))
        tasks.recover_after_restart(conn)
        assert db.one(conn, "SELECT status FROM task_periods WHERE id=?", (pid,))["status"] == "Needs Attention"
        assert db.one(conn, "SELECT status FROM task_periods WHERE id=?", (p2,))["status"] == "Delivery Uncertain"
        assert db.one(conn, "SELECT status FROM dispatches WHERE dispatch_ref='REF-1'")["status"] == "uncertain"


def test_dry_run_adapter_never_sends(tmp_path):
    from relay_hub.config import Paths
    from relay_hub.mail.base import MailMessage, select_adapter
    paths = Paths(tmp_path)
    paths.ensure()
    a = select_adapter("dry_run", paths)
    att = tmp_path / "a.zip"
    att.write_bytes(b"PK")
    res = a.create(MailMessage("REF-9", ["x@example.invalid"], [], "s", "b", [att]), send=True, timeout=5)
    assert res.status == "submitted" and a.reconcile({"dispatch_ref": "REF-9"}) == "sent"
    assert (paths.exports / "dry_run_mail" / "REF-9" / "message.json").exists()
    manual = select_adapter("auto", paths)
    if os.name != "nt":
        assert manual.name == "manual" and not manual.supports_send
    _ = dispatch, image_bytes
