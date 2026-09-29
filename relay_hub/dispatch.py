"""Email dispatch of prepared packages: composing, locking, sending, reconciling.

Rules enforced here:
  * One active dispatch per task period (database unique index + BEGIN
    IMMEDIATE), so two workers or two clicks cannot send twice.
  * An uncertain result is never retried automatically; it must be reconciled
    with Sent Items or resolved by a person.
  * The "changed since last submission" baseline advances only when a send is
    confirmed in Sent Items or a person marks the period manually completed.
"""
from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path

from . import audit, db, packages, settings_store, tasks, timeutil
from .errors import Conflict, Forbidden, NotFound, ValidationFailed
from .mail.base import MailAdapter, MailMessage
from .security import Principal


def new_ref(task: dict, period: dict) -> str:
    return f"RHC-{task['kind']}-{period['period_key'].replace('-', '')}-{secrets.token_hex(3).upper()}"


def compose(conn, task: dict, period: dict, pkgs: list[dict], ref: str) -> dict:
    attachments = [p for p in pkgs if p["zip_path"]]
    lines, omissions = [], []
    total = 0
    for p in pkgs:
        if p["zip_path"]:
            lines.append(f"{p['location']}: {p['included_count']} photo(s) - {p['zip_name']}")
        else:
            lines.append(f"{p['location']}: no photos this period")
        total += p["included_count"]
        if p["omitted_count"]:
            omissions.append(f"{p['location']}: {p['omitted_count']} employee(s) not included (see manifest)")
    values = {
        "task_name": task["name"], "task_kind": task["kind"], "period_label": period["period_label"],
        "period_key": period["period_key"], "location_summary": "\n".join(lines),
        "inclusion": "all active employees" if task["inclusion"] == "all"
        else "only new or changed headshots since the last completed submission",
        "omission_summary": ("Not included:\n" + "\n".join(omissions) + "\n") if omissions else "",
        "dispatch_ref": ref, "attachment_list": ", ".join(p["zip_name"] for p in attachments),
        "total_photos": total,
    }
    email = task["email"]
    subject = (email.get("subject_template") or "").format(**values)
    body = (email.get("body_template") or "").format(**values)
    size = sum(p["zip_size"] or 0 for p in attachments)
    return {"subject": subject, "body": body, "to": email.get("to") or [], "cc": email.get("cc") or [],
            "sender": email.get("sender") or settings_store.get(conn, "outlook_default_account") or "",
            "attachments": [{"name": p["zip_name"], "path": p["zip_path"], "bytes": p["zip_size"],
                             "package_id": p["id"], "location": p["location"]} for p in attachments],
            "total_bytes": size, "total_photos": total, "omitted": sum(p["omitted_count"] for p in pkgs)}


def preview(conn, principal: Principal, period_id: int) -> dict:
    if not principal.can("tasks.view"):
        raise Forbidden("You do not have permission to view tasks")
    period = tasks.get_period(conn, period_id)
    task = tasks.load_task(conn, period["task_id"])
    pkgs = tasks.current_packages(conn, period_id)
    if not pkgs:
        raise ValidationFailed("Prepare the packages first.")
    msg = compose(conn, task, period, pkgs, "(assigned when sent)")
    msg["problems"] = check_sendable(conn, task, period, pkgs, allow_incomplete=True)
    msg["incomplete"] = any(p["omitted_count"] for p in pkgs)
    msg["limit_mb"] = task["max_attachment_mb"]
    for a in msg["attachments"]:
        a.pop("path", None)
    return msg


def check_sendable(conn, task, period, pkgs, allow_incomplete: bool) -> list[str]:
    problems = []
    if not task["email"].get("to"):
        problems.append("No To recipients are configured for this task (Tasks > Configure > Email).")
    if len(pkgs) < len(task["locations"]):
        problems.append("Not every location has a prepared package.")
    for p in pkgs:
        issue = packages.verify(p)
        if issue:
            problems.append(issue + ". Regenerate the packages.")
    if not any(p["zip_path"] for p in pkgs):
        problems.append("There are no photos to attach.")
    if any(p["omitted_count"] for p in pkgs) and not allow_incomplete:
        problems.append("The packages are incomplete (some employees were excluded). Confirm sending an incomplete "
                        "package, or fix the exclusions and regenerate.")
    size = sum(p["zip_size"] or 0 for p in pkgs)
    if size > task["max_attachment_mb"] * 1024 * 1024:
        problems.append(f"Attachments total {size / 1048576:.1f} MB, above the {task['max_attachment_mb']:g} MB "
                        "limit. Send the ZIP files manually or raise the limit if your mail system allows it.")
    if period["status"] in tasks.CLOSED:
        problems.append("This period is already complete.")
    return problems


def send(conn, paths, adapter: MailAdapter, period_id: int, mode: str, actor: str, principal: Principal | None,
         automatic: bool = False, allow_incomplete: bool = False, timeout: float | None = None) -> dict:
    """mode: 'draft' (create a draft with attachments) or 'send' (Send Now / scheduled)."""
    if mode not in ("draft", "send"):
        raise ValidationFailed("Unknown email mode")
    if principal is not None:
        if not principal.can("tasks.operate"):
            raise Forbidden("You do not have permission to send task emails")
    if not adapter.supports_send:
        raise ValidationFailed(adapter.status().get("message") or "Outlook automation is not available.")
    timeout = timeout or float(settings_store.get(conn, "outlook_send_timeout_seconds") or 120)
    try:
        with db.transaction(conn):
            period = tasks.get_period(conn, period_id)
            task = tasks.load_task(conn, period["task_id"])
            if principal is not None:
                tasks.require_scope(principal, task)
            existing = tasks.active_dispatch(conn, period_id)
            if existing:
                raise Conflict(_active_message(existing), {"dispatch": existing})
            if automatic:
                blockers = tasks.auto_send_blockers(conn, task)
                if blockers:
                    raise ValidationFailed(" ".join(blockers))
            pkgs = tasks.current_packages(conn, period_id)
            problems = check_sendable(conn, task, period, pkgs, allow_incomplete)
            if problems:
                raise ValidationFailed(" ".join(problems), {"problems": problems})
            ref = new_ref(task, period)
            msg = compose(conn, task, period, pkgs, ref)
            did = conn.execute(
                "INSERT INTO dispatches(period_id, task_id, dispatch_ref, mode, adapter, status, automatic, package_ids, "
                "sender, to_addrs, cc_addrs, subject, body, attachments, total_bytes, created_at, created_by) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (period_id, task["id"], ref, mode, adapter.name, "sending", 1 if automatic else 0,
                 db.jdump([p["id"] for p in pkgs]), msg["sender"], db.jdump(msg["to"]), db.jdump(msg["cc"]),
                 msg["subject"], msg["body"], db.jdump([{k: v for k, v in a.items()} for a in msg["attachments"]]),
                 msg["total_bytes"], timeutil.iso_utc(), actor)).lastrowid
            audit.log(conn, actor, f"email.{mode}_start", "dispatch", did,
                      f"{'Automatic ' if automatic else ''}{'draft' if mode == 'draft' else 'send'} started for "
                      f"{task['name']} {period['period_label']} ({ref})", {"to": msg["to"], "cc": msg["cc"]})
    except sqlite3.IntegrityError:
        raise Conflict("Another email for this period is already in progress or complete.") from None

    mail = MailMessage(dispatch_ref=ref, to=msg["to"], cc=msg["cc"], subject=msg["subject"], body=msg["body"],
                       attachments=[Path(a["path"]) for a in msg["attachments"]], sender=msg["sender"] or None)
    try:
        result = adapter.create(mail, send=(mode == "send"), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - never leave a dispatch in 'sending'
        from .mail.base import SendResult
        result = SendResult("uncertain" if mode == "send" else "failed", error=f"Unexpected error: {exc}")
    now = timeutil.iso_utc()
    with db.transaction(conn):
        if result.status == "draft_created":
            conn.execute("UPDATE dispatches SET status='draft_created', entry_id=? WHERE id=?", (result.entry_id, did))
            note = f"Draft created in Outlook ({ref}). Review and send it from Outlook; the app will look for it in " \
                   "Sent Items."
            tasks.set_status(conn, period_id, period["status"] if period["status"] in (tasks.READY,
                                                                                      tasks.NEEDS_ATTENTION)
                             else tasks.READY, note)
        elif result.status == "submitted":
            conn.execute("UPDATE dispatches SET status='submitted', submitted_at=?, entry_id=? WHERE id=?",
                         (now, result.entry_id, did))
            tasks.set_status(conn, period_id, tasks.SUBMITTED,
                             f"Handed to {adapter.label} at {timeutil.now_local():%d %b %Y %H:%M} ({ref}). Waiting for it to appear in Sent Items. "
                             "Recipient delivery cannot be confirmed by this application.")
        elif result.status == "failed":
            conn.execute("UPDATE dispatches SET status='failed', error=? WHERE id=?", (result.error, did))
            tasks.set_status(conn, period_id, tasks.FAILED, f"Not sent: {result.error}")
        else:
            conn.execute("UPDATE dispatches SET status='uncertain', error=? WHERE id=?", (result.error, did))
            tasks.set_status(conn, period_id, tasks.UNCERTAIN, f"Result uncertain: {result.error}")
        audit.log(conn, actor, f"email.{result.status}", "dispatch", did,
                  f"{task['name']} {period['period_label']}: {result.status}", {"ref": ref, "error": result.error})
    return db.one(conn, "SELECT * FROM dispatches WHERE id=?", (did,))


def _active_message(d: dict) -> str:
    return {
        "sending": "An email for this period is being handed to Outlook right now.",
        "draft_created": "A draft for this period already exists in Outlook. Send or discard it first.",
        "submitted": "This period was already handed to Outlook.",
        "confirmed": "This period was already confirmed in Sent Items.",
        "uncertain": "A previous send has an uncertain result. Check Outlook's Sent Items and resolve it before "
                     "sending again.",
        "manual": "This period was completed manually.",
    }.get(d["status"], "An email for this period already exists.")


def advance_baseline(conn, dispatch_row: dict) -> int:
    count = 0
    for pid in db.jload(dispatch_row["package_ids"], []):
        for it in db.rows(conn, "SELECT * FROM package_items WHERE package_id=?", (pid,)):
            conn.execute("INSERT INTO export_baseline(task_id, employee_id, rhc_id, photo_id, location, period_id, "
                         "dispatch_id, recorded_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_id, employee_id) DO UPDATE "
                         "SET rhc_id=excluded.rhc_id, photo_id=excluded.photo_id, location=excluded.location, "
                         "period_id=excluded.period_id, dispatch_id=excluded.dispatch_id, "
                         "recorded_at=excluded.recorded_at",
                         (dispatch_row["task_id"], it["employee_id"], it["rhc_id"], it["photo_id"], it["location"],
                          dispatch_row["period_id"], dispatch_row["id"], timeutil.iso_utc()))
            count += 1
    return count


def _confirm(conn, d: dict, actor: str, note: str) -> None:
    conn.execute("UPDATE dispatches SET status='confirmed', confirmed_at=?, resolved_by=?, notes=? WHERE id=?",
                 (timeutil.iso_utc(), actor, note, d["id"]))
    n = advance_baseline(conn, d)
    tasks.set_status(conn, d["period_id"], tasks.CONFIRMED,
                     f"Found in Sent Items ({d['dispatch_ref']}). {note} Recipient delivery is not tracked.", actor)
    audit.log(conn, actor, "email.confirmed", "dispatch", d["id"], f"Confirmed in Sent Items: {d['dispatch_ref']}",
              {"baseline_entries": n, "note": note})


def record_manual(conn, task: dict, period: dict, pkgs: list[dict], actor: str, note: str) -> int:
    did = conn.execute(
        "INSERT INTO dispatches(period_id, task_id, dispatch_ref, mode, adapter, status, package_ids, subject, "
        "created_at, created_by, resolved_at, resolved_by, notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (period["id"], task["id"], new_ref(task, period), "manual", "manual", "manual",
         db.jdump([p["id"] for p in pkgs]), None, timeutil.iso_utc(), actor, timeutil.iso_utc(), actor, note)).lastrowid
    if pkgs:
        advance_baseline(conn, db.one(conn, "SELECT * FROM dispatches WHERE id=?", (did,)))
    return did


def reconcile(conn, adapter: MailAdapter, dispatch_id: int | None = None, actor: str = "system") -> list[dict]:
    """Look for open dispatches in Outlook and update their status. Never resends."""
    sql = "SELECT * FROM dispatches WHERE status IN ('draft_created','submitted','uncertain')"
    params: tuple = ()
    if dispatch_id is not None:
        sql += " AND id=?"
        params = (dispatch_id,)
    out = []
    timeout_min = int(settings_store.get(conn, "sent_items_timeout_minutes") or 120)
    for d in db.rows(conn, sql, params):
        if d["adapter"] != adapter.name:
            out.append({"id": d["id"], "result": "skipped", "reason": f"sent with {d['adapter']}"})
            continue
        late = getattr(adapter, "late_results", {}).pop(d["dispatch_ref"], None)
        if late is not None and d["status"] == "uncertain":
            with db.transaction(conn):
                conn.execute("UPDATE dispatches SET notes=COALESCE(notes,'') || ? WHERE id=?",
                             (f" Outlook finished later with result: {late.status} {late.error or ''}.", d["id"]))
        found = adapter.reconcile(d)
        with db.transaction(conn):
            cur = db.one(conn, "SELECT * FROM dispatches WHERE id=?", (d["id"],))
            if cur["status"] != d["status"]:
                continue
            if found == "sent":
                _confirm(conn, cur, actor, "Matched by dispatch ID.")
            elif found == "not_found" and cur["status"] == "submitted":
                age = (timeutil.now_utc() - timeutil.parse_iso_utc(cur["submitted_at"])).total_seconds() / 60
                if age > timeout_min:
                    conn.execute("UPDATE dispatches SET status='uncertain', error=? WHERE id=?",
                                 (f"Not found in Sent Items {int(age)} minutes after submission.", cur["id"]))
                    tasks.set_status(conn, cur["period_id"], tasks.UNCERTAIN,
                                     f"Not found in Sent Items after {int(age)} minutes. Check Outlook's Outbox and "
                                     "Sent Items, then resolve. It will not be resent automatically.")
                    audit.log(conn, actor, "email.uncertain", "dispatch", cur["id"], "Not found in Sent Items")
            elif found == "not_found" and cur["status"] == "draft_created":
                conn.execute("UPDATE dispatches SET status='uncertain', error=? WHERE id=?",
                             ("The draft is no longer in Drafts and was not found in Sent Items or the Outbox.",
                              cur["id"]))
                tasks.set_status(conn, cur["period_id"], tasks.UNCERTAIN,
                                 "The Outlook draft disappeared without appearing in Sent Items. Resolve it.")
            elif found == "outbox" and cur["status"] == "uncertain":
                conn.execute("UPDATE dispatches SET notes=? WHERE id=?", ("Found in the Outbox (not yet sent).",
                                                                        cur["id"]))
        out.append({"id": d["id"], "result": found})
    return out


def resolve(conn, adapter: MailAdapter, principal: Principal, dispatch_id: int, action: str, note: str) -> dict:
    """Human resolution of drafts and uncertain sends.

    confirm_sent: the operator checked Outlook and the message is in Sent Items.
    not_sent:     the operator checked and it was not sent; a new send is allowed.
    discard_draft: remove the draft (best effort in Outlook) and allow a new send.
    """
    if not principal.can("tasks.operate"):
        raise Forbidden("You do not have permission to resolve task emails")
    note = (note or "").strip()
    with db.transaction(conn):
        d = db.one(conn, "SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
        if not d:
            raise NotFound("Email record not found")
        task = tasks.load_task(conn, d["task_id"])
        tasks.require_scope(principal, task)
        if action == "confirm_sent":
            if d["status"] not in ("submitted", "uncertain", "draft_created"):
                raise Conflict(f"This email is '{d['status']}' and cannot be confirmed.")
            if not note:
                raise ValidationFailed("Describe how you checked (for example 'Seen in Sent Items at 10:04').")
            _confirm(conn, d, principal.label, f"Confirmed by {principal.label}: {note}")
        elif action in ("not_sent", "discard_draft"):
            if d["status"] not in ("uncertain", "draft_created", "failed"):
                raise Conflict(f"This email is '{d['status']}'; it cannot be marked as not sent.")
            if action == "not_sent" and not note:
                raise ValidationFailed("Describe how you checked that it was not sent.")
            conn.execute("UPDATE dispatches SET status='abandoned', resolved_at=?, resolved_by=?, notes=? WHERE id=?",
                         (timeutil.iso_utc(), principal.label, note or "Draft discarded", dispatch_id))
            period = tasks.get_period(conn, d["period_id"])
            status, st_note = tasks.evaluate(conn, task, period["id"])
            tasks.set_status(conn, period["id"], status if status != tasks.MANUAL else tasks.NEEDS_ATTENTION, st_note)
            audit.log(conn, principal.label, f"email.{action}", "dispatch", dispatch_id,
                      f"Marked {d['dispatch_ref']} as {'not sent' if action == 'not_sent' else 'draft discarded'}",
                      {"note": note})
        else:
            raise ValidationFailed("Unknown resolution")
    if action == "discard_draft" and d["status"] == "draft_created":
        adapter.discard_draft(d)
    return db.one(conn, "SELECT * FROM dispatches WHERE id=?", (dispatch_id,))


def ready_folder(conn, paths, principal: Principal, period_id: int) -> dict:
    """Copy the current ZIPs and the email text to a ready folder for manual sending."""
    if not principal.can("tasks.operate"):
        raise Forbidden("You do not have permission to operate tasks")
    period = tasks.get_period(conn, period_id)
    task = tasks.load_task(conn, period["task_id"])
    tasks.require_scope(principal, task)
    pkgs = tasks.current_packages(conn, period_id)
    if not pkgs:
        raise ValidationFailed("Prepare the packages first.")
    for p in pkgs:
        issue = packages.verify(p)
        if issue:
            raise ValidationFailed(issue)
    ref = f"MANUAL-{task['kind']}-{period['period_key']}"
    msg = compose(conn, task, period, pkgs, ref)
    folder = paths.exports / "ready" / f"{task['kind']}_{period['period_key']}"
    if folder.exists():
        packages._force_rmtree(folder)
    folder.mkdir(parents=True)
    for a in msg["attachments"]:
        shutil.copyfile(a["path"], folder / a["name"])
    (folder / "email_subject.txt").write_text(msg["subject"], encoding="utf-8")
    (folder / "email_body.txt").write_text(msg["body"], encoding="utf-8")
    (folder / "recipients.txt").write_text("To: " + "; ".join(msg["to"]) + "\nCC: " + "; ".join(msg["cc"]) + "\n",
                                           encoding="utf-8")
    with db.transaction(conn):
        audit.log(conn, principal.label, "task.ready_folder", "period", period_id, f"Ready folder created: {folder}")
    return {"folder": str(folder), "subject": msg["subject"], "body": msg["body"], "to": msg["to"], "cc": msg["cc"],
            "files": [a["name"] for a in msg["attachments"]]}


def open_folder(path: str) -> bool:
    """Open a folder in Explorer (only meaningful on the computer running the application)."""
    if sys.platform == "win32" and os.path.isdir(path):
        os.startfile(path)  # noqa: S606 - local folder path created by the application
        return True
    return False
