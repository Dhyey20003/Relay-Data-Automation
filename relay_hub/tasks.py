"""Recurring headshot tasks: definitions, schedules, periods and package preparation.

IT task: every N (default 14) days from an anchor date, at a set time.
HR task: monthly on a set day and time; a day past the end of a month uses
that month's last day (e.g. day 31 -> 30 April, 28/29 February).
Times are in the application timezone (default Asia/Kolkata).
"""
from __future__ import annotations

import datetime as dt
import re

from . import audit, db, packages, settings_store, timeutil
from .errors import Conflict, Forbidden, NotFound, ValidationFailed
from .fields import EMAIL_RE
from .security import Principal

DUE = "Due"
PREPARING = "Preparing"
READY = "Ready"
NEEDS_ATTENTION = "Needs Attention"
SUBMITTED = "Submitted to Outlook"
CONFIRMED = "Confirmed in Sent Items"
FAILED = "Failed"
UNCERTAIN = "Delivery Uncertain"
MANUAL = "Manually Completed"
STATUSES = [DUE, PREPARING, READY, NEEDS_ATTENTION, SUBMITTED, CONFIRMED, FAILED, UNCERTAIN, MANUAL]
CLOSED = {CONFIRMED, MANUAL}
ACTIVE_DISPATCH = ("sending", "draft_created", "submitted", "confirmed", "uncertain", "manual")

IT_SUBJECT = "IT Headshots - {period_label}"
HR_SUBJECT = "HR Headshots - {period_label}"
DEFAULT_BODY = """Hello,

Please find attached the {task_kind} headshot packages for {period_label}.

{location_summary}

Included: {inclusion}
{omission_summary}
Regards,
Relay Human Cloud

Reference: {dispatch_ref}
"""
TEMPLATE_FIELDS = ["task_name", "task_kind", "period_label", "period_key", "location_summary", "inclusion",
                   "omission_summary", "dispatch_ref", "attachment_list", "total_photos"]


def ensure_default_tasks(conn) -> None:
    now = timeutil.iso_utc()
    locs = settings_store.locations(conn)
    today = timeutil.today().isoformat()
    defaults = [
        ("IT", "IT headshots (every 14 days)", {"type": "interval", "anchor_date": today, "interval_days": 14,
                                                "time": "10:00"}, "{original_filename}", "original", IT_SUBJECT),
        ("HR", "HR headshots (monthly)", {"type": "monthly", "day": 1, "time": "10:00"}, "{rhc_id}", "jpg",
         HR_SUBJECT),
    ]
    for kind, name, sched, naming, fmt, subject in defaults:
        if db.one(conn, "SELECT id FROM tasks WHERE kind=?", (kind,)):
            continue
        conn.execute("INSERT INTO tasks(kind, name, enabled, schedule, inclusion, naming_template, export_format, "
                     "locations, email, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (kind, name, 0, db.jdump(sched), None, naming, fmt, db.jdump(locs),
                      db.jdump({"to": [], "cc": [], "sender": "", "subject_template": subject,
                                "body_template": DEFAULT_BODY}), now, now))


def load_task(conn, task_id: int) -> dict:
    t = db.one(conn, "SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise NotFound("Task not found")
    return decode_task(t)


def decode_task(t: dict) -> dict:
    t = dict(t)
    t["schedule"] = db.jload(t["schedule"], {})
    t["locations"] = db.jload(t["locations"], [])
    t["email"] = db.jload(t["email"], {})
    return t


# ---------------------------------------------------------------- schedule

def occurrence_dates(task: dict, start_day: dt.date, end_day: dt.date) -> list[dt.date]:
    s = task["schedule"]
    out = []
    if s.get("type") == "interval":
        anchor = dt.date.fromisoformat(s["anchor_date"])
        step = int(s.get("interval_days") or 14)
        if end_day < anchor:
            return []
        k = max(0, (start_day - anchor).days // step)
        d = anchor + dt.timedelta(days=k * step)
        while d <= end_day:
            if d >= start_day:
                out.append(d)
            d += dt.timedelta(days=step)
    else:
        day = int(s.get("day") or 1)
        y, m = start_day.year, start_day.month
        while True:
            d = timeutil.clamp_day(y, m, day)
            if d > end_day:
                break
            if d >= start_day:
                out.append(d)
            m += 1
            if m > 12:
                y, m = y + 1, 1
    return out


def period_for(task: dict, day: dt.date) -> dict:
    due = timeutil.local_datetime(day, task["schedule"].get("time", "10:00"))
    if task["kind"] == "HR" or task["schedule"].get("type") == "monthly":
        key, label = day.strftime("%Y-%m"), day.strftime("%B %Y")
    else:
        key, label = day.isoformat(), f"{day.day} {day.strftime('%b %Y')}"
    return {"period_key": key, "period_label": label, "due_at": timeutil.iso_utc(due), "due_local": due.isoformat()}


def occurrences_between(task: dict, after: dt.datetime, until: dt.datetime) -> list[dict]:
    """Occurrences with after < due_at <= until."""
    start_day = after.astimezone(timeutil.tz()).date() - dt.timedelta(days=1)
    end_day = until.astimezone(timeutil.tz()).date() + dt.timedelta(days=1)
    out = []
    for d in occurrence_dates(task, start_day, end_day):
        p = period_for(task, d)
        due = timeutil.parse_iso_utc(p["due_at"])
        if after < due <= until:
            out.append(p)
    return out


def upcoming(task: dict, n: int = 3) -> list[dict]:
    now = timeutil.now_utc()
    local_today = timeutil.today()
    dates = occurrence_dates(task, local_today, local_today + dt.timedelta(days=400))
    out = []
    for d in dates:
        p = period_for(task, d)
        if timeutil.parse_iso_utc(p["due_at"]) > now:
            out.append(p)
        if len(out) >= n:
            break
    return out


def latest_due(task: dict) -> dict | None:
    now = timeutil.now_utc()
    local_today = timeutil.today()
    dates = occurrence_dates(task, local_today - dt.timedelta(days=400), local_today)
    past = [period_for(task, d) for d in dates]
    past = [p for p in past if timeutil.parse_iso_utc(p["due_at"]) <= now]
    return past[-1] if past else None


def create_period(conn, task: dict, p: dict, status: str = DUE, note: str | None = None,
                  origin: str = "schedule") -> tuple[int, bool]:
    now = timeutil.iso_utc()
    cur = conn.execute("INSERT OR IGNORE INTO task_periods(task_id, period_key, period_label, due_at, status, "
                       "status_note, origin, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (task["id"], p["period_key"], p["period_label"], p["due_at"], status, note, origin, now, now))
    created = cur.rowcount == 1
    pid = db.scalar(conn, "SELECT id FROM task_periods WHERE task_id=? AND period_key=?", (task["id"], p["period_key"]))
    if created:
        for loc in task["locations"]:
            conn.execute("INSERT OR IGNORE INTO period_locations(period_id, location, status) VALUES(?,?,?)",
                         (pid, loc, "pending"))
    return pid, created


def set_status(conn, period_id: int, status: str, note: str | None = None, actor: str | None = None) -> None:
    fields = ["status=?", "status_note=?", "updated_at=?"]
    params = [status, note, timeutil.iso_utc()]
    if status in CLOSED:
        fields += ["completed_at=?", "completed_by=?"]
        params += [timeutil.iso_utc(), actor]
    conn.execute(f"UPDATE task_periods SET {', '.join(fields)} WHERE id=?", params + [period_id])


# ------------------------------------------------------------- configuring

def validate_emails(values, label: str) -> list[str]:
    if isinstance(values, str):
        values = [v for v in re.split(r"[;,\s]+", values) if v]
    out = []
    for v in values or []:
        v = v.strip()
        if not v:
            continue
        if not EMAIL_RE.match(v):
            raise ValidationFailed(f"{label}: '{v}' is not a valid email address.")
        out.append(v)
    return out


def update_task(conn, principal: Principal, task_id: int, data: dict) -> dict:
    if not principal.can("tasks.configure"):
        raise Forbidden("You do not have permission to configure tasks")
    with db.transaction(conn):
        task = load_task(conn, task_id)
        if data.get("version") is not None and int(data["version"]) != task["version"]:
            raise Conflict("The task was changed by someone else. Reload and try again.")
        changes = {}
        if "name" in data:
            name = (data["name"] or "").strip()
            if not name:
                raise ValidationFailed("Task name is required.")
            changes["name"] = name
        if "schedule" in data:
            changes["schedule"] = db.jdump(_validate_schedule(task["kind"], data["schedule"]))
        if "inclusion" in data:
            if data["inclusion"] not in ("all", "changed"):
                raise ValidationFailed("Choose whether to include all active employees or only new/changed headshots.")
            changes["inclusion"] = data["inclusion"]
        if "naming_template" in data and task["kind"] == "IT":
            tpl = (data["naming_template"] or "").strip() or "{original_filename}"
            packages.render_name(tpl, {"full_name": "Asha Patel", "rhc_id": "RHC0001"},
                                 {"original_filename": "photo.jpg"}, "Ahmedabad", ".jpg")
            changes["naming_template"] = tpl
        if "export_format" in data and task["kind"] == "IT":
            if data["export_format"] not in ("original", "jpg"):
                raise ValidationFailed("Export format must be original or JPG.")
            changes["export_format"] = data["export_format"]
        if "locations" in data:
            allowed = settings_store.locations(conn)
            locs = [l for l in data["locations"] if l in allowed]
            if not locs:
                raise ValidationFailed("Choose at least one location.")
            changes["locations"] = db.jdump(locs)
        for key, lo, hi in (("max_attachment_mb", 1, 150), ("catchup_grace_hours", 1, 720)):
            if key in data:
                try:
                    v = float(data[key])
                except (TypeError, ValueError):
                    raise ValidationFailed(f"{key} must be a number.") from None
                if not lo <= v <= hi:
                    raise ValidationFailed(f"{key} must be between {lo} and {hi}.")
                changes[key] = v
        for key in ("skip_when_empty", "auto_send_with_omissions"):
            if key in data:
                changes[key] = 1 if data[key] else 0
        reset_validation = False
        if "email" in data:
            e = dict(task["email"])
            new = data["email"] or {}
            to = validate_emails(new.get("to", e.get("to")), "To")
            cc = validate_emails(new.get("cc", e.get("cc")), "CC")
            sender = (new.get("sender", e.get("sender")) or "").strip()
            if to != e.get("to") or cc != e.get("cc") or sender != (e.get("sender") or ""):
                reset_validation = True
            subject = (new.get("subject_template", e.get("subject_template")) or "").strip()
            body = new.get("body_template", e.get("body_template")) or ""
            for tpl, label in ((subject, "Subject"), (body, "Body")):
                try:
                    tpl.format(**{k: "" for k in TEMPLATE_FIELDS})
                except (KeyError, IndexError, ValueError) as exc:
                    raise ValidationFailed(f"{label} template has an unknown placeholder ({exc}). Allowed: "
                                           + ", ".join("{" + f + "}" for f in TEMPLATE_FIELDS)) from None
            if not subject:
                raise ValidationFailed("Subject template is required.")
            changes["email"] = db.jdump({"to": to, "cc": cc, "sender": sender, "subject_template": subject,
                                         "body_template": body})
        if "enabled" in data:
            enabled = bool(data["enabled"])
            inclusion = changes.get("inclusion", task["inclusion"])
            if enabled and not inclusion:
                raise ValidationFailed("Save an inclusion choice before enabling the task.")
            if enabled and not task["enabled"]:
                changes["tracking_start"] = timeutil.iso_utc()
                changes["scheduled_through"] = timeutil.iso_utc()
                changes["owner_user_id"] = principal.id
            changes["enabled"] = 1 if enabled else 0
        if reset_validation and task["auto_send_enabled"]:
            changes["auto_send_validated_at"] = None
            changes["auto_send_validated_by"] = None
        if changes:
            changes["updated_at"] = timeutil.iso_utc()
            changes["updated_by"] = principal.label
            sets = ", ".join(f"{k}=?" for k in changes)
            conn.execute(f"UPDATE tasks SET {sets}, version=version+1 WHERE id=?", list(changes.values()) + [task_id])
            audit.log(conn, principal.label, "task.configure", "task", task_id, f"Updated {task['name']}",
                      {k: v for k, v in changes.items() if k not in ("updated_at", "updated_by")})
    return load_task(conn, task_id)


def _validate_schedule(kind: str, s: dict) -> dict:
    time_str = (s.get("time") or "10:00").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", time_str):
        raise ValidationFailed("Time must be HH:MM (24-hour).")
    if kind == "IT":
        try:
            anchor = timeutil.parse_date(s.get("anchor_date")).isoformat()
        except ValueError as exc:
            raise ValidationFailed(f"Anchor date: {exc}") from None
        interval = int(s.get("interval_days") or 14)
        if not 1 <= interval <= 365:
            raise ValidationFailed("Interval must be between 1 and 365 days.")
        return {"type": "interval", "anchor_date": anchor, "interval_days": interval, "time": time_str}
    day = int(s.get("day") or 1)
    if not 1 <= day <= 31:
        raise ValidationFailed("Day of month must be between 1 and 31.")
    return {"type": "monthly", "day": day, "time": time_str}


def set_auto_send(conn, principal: Principal, task_id: int, enable: bool, confirm: bool) -> dict:
    if not principal.can("tasks.configure"):
        raise Forbidden("You do not have permission to configure automatic sending")
    with db.transaction(conn):
        task = load_task(conn, task_id)
        if enable:
            if not confirm:
                raise ValidationFailed("Confirm that routine emails may be sent automatically without asking.")
            if not task["email"].get("to"):
                raise ValidationFailed("Add at least one To recipient before enabling automatic sending.")
            if not task["inclusion"]:
                raise ValidationFailed("Save an inclusion choice first.")
            conn.execute("UPDATE tasks SET auto_send_enabled=1, auto_send_validated_at=?, auto_send_validated_by=?, "
                         "owner_user_id=?, version=version+1 WHERE id=?",
                         (timeutil.iso_utc(), principal.label, principal.id, task_id))
            audit.log(conn, principal.label, "task.auto_send_enable", "task", task_id,
                      f"Enabled automatic sending for {task['name']}",
                      {"to": task["email"].get("to"), "cc": task["email"].get("cc"),
                       "sender": task["email"].get("sender")})
        else:
            conn.execute("UPDATE tasks SET auto_send_enabled=0, version=version+1 WHERE id=?", (task_id,))
            audit.log(conn, principal.label, "task.auto_send_disable", "task", task_id,
                      f"Disabled automatic sending for {task['name']}")
    return load_task(conn, task_id)


def auto_send_blockers(conn, task: dict) -> list[str]:
    out = []
    if not task["auto_send_enabled"]:
        out.append("Automatic sending is off for this task.")
    elif not task["auto_send_validated_at"]:
        out.append("Automatic sending needs to be re-confirmed (recipients or sender changed, or data was restored "
                   "or moved).")
    if settings_store.get(conn, "revalidation_required"):
        out.append("An administrator must revalidate sender, recipients and task ownership after a restore or "
                   "computer transfer.")
    if not task["email"].get("to"):
        out.append("No To recipients are configured.")
    return out


# ----------------------------------------------------------------- periods

def start_period_now(conn, principal: Principal, task_id: int) -> int:
    if not principal.can("tasks.operate"):
        raise Forbidden("You do not have permission to operate tasks")
    task = load_task(conn, task_id)
    if not task["inclusion"]:
        raise ValidationFailed("Save an inclusion choice for this task first.")
    p = latest_due(task) or (upcoming(task, 1) or [None])[0]
    if not p:
        raise ValidationFailed("The schedule has no occurrences; check the schedule settings.")
    with db.transaction(conn):
        pid, created = create_period(conn, task, p, status=NEEDS_ATTENTION,
                                     note="Started manually: prepare the packages, then send.", origin="manual")
        if created:
            audit.log(conn, principal.label, "task.period_start", "period", pid,
                      f"Started {task['name']} period {p['period_label']} manually")
    return pid


def get_period(conn, period_id: int) -> dict:
    p = db.one(conn, "SELECT * FROM task_periods WHERE id=?", (period_id,))
    if not p:
        raise NotFound("Task period not found")
    return p


def current_packages(conn, period_id: int) -> list[dict]:
    return db.rows(conn, "SELECT p.* FROM period_locations pl JOIN packages p ON p.id=pl.current_package_id "
                         "WHERE pl.period_id=? ORDER BY p.location", (period_id,))


def active_dispatch(conn, period_id: int) -> dict | None:
    return db.one(conn, f"SELECT * FROM dispatches WHERE period_id=? AND status IN "
                        f"({','.join('?' * len(ACTIVE_DISPATCH))})", (period_id, *ACTIVE_DISPATCH))


def require_scope(principal: Principal, task: dict) -> None:
    if principal.locations is not None and set(task["locations"]) - set(principal.locations):
        raise Forbidden("This task covers locations outside your permitted scope.")


def prepare(conn, paths, principal_label: str, period_id: int, principal: Principal | None = None,
            automatic: bool = False) -> dict:
    """Build a new revision of every location package for the period."""
    if principal is not None and not principal.can("tasks.operate"):
        raise Forbidden("You do not have permission to prepare packages")
    with db.transaction(conn):
        period = get_period(conn, period_id)
        task = load_task(conn, period["task_id"])
        if principal is not None:
            require_scope(principal, task)
        if not task["inclusion"]:
            raise ValidationFailed("Save an inclusion choice for this task first.")
        if period["status"] in CLOSED or period["status"] in (SUBMITTED, UNCERTAIN):
            raise Conflict(f"This period is '{period['status']}', so packages can no longer be regenerated.")
        if period["status"] == PREPARING:
            raise Conflict("Packages are already being prepared.")
        if active_dispatch(conn, period_id):
            raise Conflict("An email for this period is in progress or was already created. Resolve it first.")
        set_status(conn, period_id, PREPARING, "Preparing packages...")
    try:
        with db.transaction(conn):
            results = {}
            for loc in task["locations"]:
                results[loc] = packages.build(conn, paths, task, period, loc, principal_label)
            status, note = evaluate(conn, task, period_id, automatic)
            set_status(conn, period_id, status, note, principal_label if status in CLOSED else None)
            audit.log(conn, principal_label, "task.prepare", "period", period_id,
                      f"Prepared {task['name']} {period['period_label']}: " +
                      ", ".join(f"{k} {v['included']} photos (rev {v['revision']})" for k, v in results.items()),
                      {"results": results, "status": status})
    except Exception as exc:
        with db.transaction(conn):
            set_status(conn, period_id, NEEDS_ATTENTION, f"Preparation failed: {exc}")
        raise
    return {"status": status, "note": note, "results": results}


def evaluate(conn, task: dict, period_id: int, automatic: bool = False) -> tuple[str, str | None]:
    pkgs = current_packages(conn, period_id)
    if len(pkgs) < len(task["locations"]):
        return NEEDS_ATTENTION, "Not every location has a prepared package."
    problems = [packages.verify(p) for p in pkgs]
    problems = [p for p in problems if p]
    if problems:
        return NEEDS_ATTENTION, " ".join(problems)
    total = sum(p["included_count"] for p in pkgs)
    omitted = sum(p["omitted_count"] for p in pkgs)
    size = sum(p["zip_size"] or 0 for p in pkgs)
    notes = []
    if total == 0:
        if task["inclusion"] == "changed":
            if task["skip_when_empty"] and automatic and not omitted:
                return MANUAL, "Closed automatically: no new or changed headshots, so nothing was sent."
            return NEEDS_ATTENTION, ("No new or changed headshots since the last completed submission. Mark the period "
                                     "complete if nothing needs to be sent.")
        return NEEDS_ATTENTION, "No employee photos could be included; see the exclusions."
    if omitted:
        notes.append(f"Incomplete: {omitted} employee(s) excluded (see manifest). Review before sending.")
    if size > task["max_attachment_mb"] * 1024 * 1024:
        notes.append(f"Attachments total {size / 1048576:.1f} MB, above the {task['max_attachment_mb']:g} MB limit.")
    if notes:
        return NEEDS_ATTENTION, " ".join(notes)
    return READY, f"{total} photo(s) ready in {sum(1 for p in pkgs if p['zip_path'])} ZIP file(s)."


def mark_manual(conn, principal: Principal, period_id: int, note: str, packages_sent: bool) -> dict:
    """Operator completed the period outside the application (audited)."""
    if not principal.can("tasks.operate"):
        raise Forbidden("You do not have permission to complete tasks")
    note = (note or "").strip()
    if not note:
        raise ValidationFailed("Add a short note explaining how the task was completed.")
    from . import dispatch as dispatch_mod
    with db.transaction(conn):
        period = get_period(conn, period_id)
        task = load_task(conn, period["task_id"])
        require_scope(principal, task)
        if period["status"] in CLOSED:
            raise Conflict("This period is already complete.")
        if active_dispatch(conn, period_id):
            raise Conflict("Resolve the existing email record for this period first (for example mark an uncertain "
                           "send as sent or not sent).")
        pkgs = current_packages(conn, period_id) if packages_sent else []
        if packages_sent and not pkgs:
            raise ValidationFailed("No packages have been prepared for this period.")
        did = dispatch_mod.record_manual(conn, task, period, pkgs, principal.label, note)
        set_status(conn, period_id, MANUAL, f"Manually completed: {note}", principal.label)
        audit.log(conn, principal.label, "task.manual_complete", "period", period_id,
                  f"Marked {task['name']} {period['period_label']} as manually completed",
                  {"note": note, "packages_sent": packages_sent, "dispatch_id": did})
    return get_period(conn, period_id)


def recover_after_restart(conn) -> None:
    with db.transaction(conn):
        for p in db.rows(conn, "SELECT id FROM task_periods WHERE status=?", (PREPARING,)):
            set_status(conn, p["id"], NEEDS_ATTENTION, "Preparation was interrupted when the application stopped. "
                                                       "Prepare the packages again.")
        for d in db.rows(conn, "SELECT id, period_id FROM dispatches WHERE status='sending'"):
            conn.execute("UPDATE dispatches SET status='uncertain', error=? WHERE id=?",
                         ("The application stopped while this email was being handed to Outlook. Check Outlook's "
                          "Outbox and Sent Items; it will not be resent automatically.", d["id"]))
            set_status(conn, d["period_id"], UNCERTAIN, "The application stopped during sending; review required.")
            audit.log(conn, "system", "task.recover_uncertain", "dispatch", d["id"],
                      "Send interrupted by restart marked Delivery Uncertain")


def schedule_tick(conn, paths, now: dt.datetime | None = None) -> list[dict]:
    """Create due periods and decide what may run automatically. Returns actions to perform."""
    now = now or timeutil.now_utc()
    actions = []
    for t in db.rows(conn, "SELECT * FROM tasks WHERE enabled=1"):
        task = decode_task(t)
        through = timeutil.parse_iso_utc(task["scheduled_through"]) or now
        with db.transaction(conn):
            for p in occurrences_between(task, through, now):
                pid, created = create_period(conn, task, p)
                if created:
                    audit.log(conn, "system", "task.period_due", "period", pid,
                              f"{task['name']} period {p['period_label']} is due")
            conn.execute("UPDATE tasks SET scheduled_through=? WHERE id=?", (timeutil.iso_utc(now), task["id"]))
        due = db.rows(conn, "SELECT * FROM task_periods WHERE task_id=? AND status=? AND due_at<=? AND origin='schedule' "
                      "ORDER BY due_at",
                      (task["id"], DUE, timeutil.iso_utc(now)))
        if len(due) > 1:
            with db.transaction(conn):
                for p in due:
                    set_status(conn, p["id"], NEEDS_ATTENTION,
                               f"Backlog: {len(due)} periods became due while the application was not running. "
                               "Nothing was prepared or sent automatically; review each period (skip older ones "
                               "or send them manually).")
                audit.log(conn, "system", "task.backlog", "task", task["id"],
                          f"{len(due)} overdue periods held for review")
            continue
        for p in due:
            age_h = (now - timeutil.parse_iso_utc(p["due_at"])).total_seconds() / 3600
            if age_h > task["catchup_grace_hours"]:
                with db.transaction(conn):
                    set_status(conn, p["id"], NEEDS_ATTENTION,
                               f"Overdue by {int(age_h)} hours when the application started; automatic processing "
                               "was skipped. Review and send manually.")
                continue
            actions.append({"task": task, "period": p})
    return actions
