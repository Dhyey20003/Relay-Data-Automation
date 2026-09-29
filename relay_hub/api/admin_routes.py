"""Settings, users, integration tokens, custom fields, audit log, backups and server control."""
from __future__ import annotations

import re
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import audit, backup, db, fields, security, settings_store, timeutil
from ..config import resource_dir
from ..errors import Conflict, Forbidden, NotFound, ValidationFailed
from ..security import Principal
from .deps import cleanup, client_ip, get_conn, get_ctx, require, require_any, save_upload

router = APIRouter()


# ------------------------------------------------------------------ settings

EDITABLE_SETTINGS = {"locations", "timezone", "date_day_first", "historical_enrich_fields", "mail_adapter",
                     "outlook_default_account", "sent_items_timeout_minutes", "outlook_send_timeout_seconds",
                     "photo_limits", "import_limits", "backup", "role_permissions", "role_dob_access"}


@router.get("/api/settings")
def get_settings(ctx=Depends(get_ctx), principal: Principal = Depends(require_any("settings.manage", "users.manage")),
                 conn=Depends(get_conn)):
    s = settings_store.all_settings(conn)
    s["role_permissions"] = security.role_permissions(conn)
    return {"settings": s, "permissions": security.PERMISSIONS, "roles": security.ROLES,
            "role_defaults": security.ROLE_DEFAULTS, "server": _server_config(ctx),
            "data_dir": str(ctx.paths.root), "fields": [f.as_dict() for f in fields.all_fields(conn)]}


def _server_config(ctx) -> dict:
    c = ctx.config
    return {"lan_enabled": c.lan_enabled, "host": c.host, "port": c.port, "allowed_hosts": c.allowed_hosts,
            "tls_certfile": c.tls_certfile, "tls_keyfile": c.tls_keyfile,
            "session_idle_minutes": c.session_idle_minutes, "session_max_hours": c.session_max_hours}


@router.patch("/api/settings")
def patch_settings(body: dict, ctx=Depends(get_ctx), principal: Principal = Depends(require("settings.manage")),
                   conn=Depends(get_conn)):
    unknown = set(body) - EDITABLE_SETTINGS
    if unknown:
        raise ValidationFailed(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    if ("role_permissions" in body or "role_dob_access" in body) and not principal.can("users.manage"):
        raise Forbidden("Changing role permissions requires user-management permission.")
    if "backup" in body and not principal.can("backups.manage"):
        raise Forbidden("Changing backup settings requires backup permission.")
    clean = {}
    for key, value in body.items():
        clean[key] = _validate_setting(conn, ctx, key, value)
    with db.transaction(conn):
        for key, value in clean.items():
            settings_store.set(conn, key, value, principal.label)
        audit.log(conn, principal.label, "settings.update", "settings", None, f"Updated settings: {', '.join(clean)}",
                  clean)
    if "timezone" in clean:
        timeutil.set_timezone(clean["timezone"])
    if "mail_adapter" in clean:
        ctx.reset_adapter()
    return get_settings(ctx, principal, conn)


def _validate_setting(conn, ctx, key, value):
    if key == "locations":
        locs = [str(v).strip() for v in value if str(v).strip()]
        if not locs or len(set(l.lower() for l in locs)) != len(locs):
            raise ValidationFailed("Enter at least one location, without duplicates.")
        current = settings_store.locations(conn)
        removed = [l for l in current if l not in locs]
        for l in removed:
            n = db.scalar(conn, "SELECT COUNT(*) FROM employees WHERE location=?", (l,))
            if n:
                raise ValidationFailed(f"{l} cannot be removed while {n} employee record(s) use it.")
        return locs
    if key == "timezone":
        try:
            timeutil.set_timezone(value)
            timeutil.set_timezone(settings_store.get(conn, "timezone"))
        except Exception:  # noqa: BLE001
            raise ValidationFailed(f"Unknown timezone '{value}'.") from None
        return value
    if key == "date_day_first":
        return bool(value)
    if key == "historical_enrich_fields":
        allowed = {f.key for f in fields.all_fields(conn) if f.importable}
        return [v for v in value if v in allowed]
    if key == "mail_adapter":
        if value not in ("auto", "outlook", "manual", "dry_run"):
            raise ValidationFailed("Unknown email mode.")
        return value
    if key == "outlook_default_account":
        return str(value or "").strip()
    if key in ("sent_items_timeout_minutes", "outlook_send_timeout_seconds"):
        v = int(value)
        if not 10 <= v <= 10080:
            raise ValidationFailed("Enter a value between 10 and 10080.")
        return v
    if key in ("photo_limits", "import_limits"):
        cur = settings_store.get(conn, key)
        for k, v in value.items():
            if k not in cur:
                raise ValidationFailed(f"Unknown limit {k}")
            try:
                n = float(v)
            except (TypeError, ValueError):
                raise ValidationFailed(f"{k} must be a number") from None
            if n <= 0:
                raise ValidationFailed(f"{k} must be positive")
            cur[k] = int(n) if n.is_integer() else n
        return cur
    if key == "backup":
        cur = settings_store.get(conn, "backup")
        cur.update({k: v for k, v in value.items() if k in cur})
        if cur.get("destination"):
            try:
                cur["destination"] = str(backup.validate_destination(ctx.paths, Path(cur["destination"])))
            except backup.BackupError as exc:
                raise ValidationFailed(str(exc)) from None
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(cur.get("daily_time", ""))):
            raise ValidationFailed("Daily backup time must be HH:MM.")
        cur["retention_daily"] = max(1, int(cur.get("retention_daily") or 30))
        cur["retention_manual"] = max(0, int(cur.get("retention_manual") or 0))
        return cur
    if key == "role_permissions":
        out = {}
        for role, perms in value.items():
            if role not in security.ROLES or role == "admin":
                continue
            out[role] = [p for p in perms if p in security.PERMISSIONS]
        return out
    if key == "role_dob_access":
        return {r: v for r, v in value.items() if r in security.ROLES and v in security.DOB_ACCESS}
    raise ValidationFailed("Unknown setting")


class RevalidateBody(BaseModel):
    task_ids: list[int] = []
    confirm: bool = False


@router.post("/api/settings/revalidate")
def revalidate(body: RevalidateBody, principal: Principal = Depends(require("tasks.configure")),
               conn=Depends(get_conn)):
    if not principal.is_admin:
        raise Forbidden("Only an administrator can revalidate after a restore or transfer.")
    if not body.confirm:
        raise ValidationFailed("Confirm that you checked the sender account, recipients and task ownership.")
    with db.transaction(conn):
        for tid in body.task_ids:
            conn.execute("UPDATE tasks SET auto_send_validated_at=?, auto_send_validated_by=?, owner_user_id=? "
                         "WHERE id=? AND auto_send_enabled=1", (timeutil.iso_utc(), principal.label, principal.id, tid))
        settings_store.set(conn, "revalidation_required", None, principal.label)
        audit.log(conn, principal.label, "settings.revalidate", "settings", None,
                  "Revalidated sender, recipients and task ownership", {"task_ids": body.task_ids})
    return {"ok": True}


class ServerBody(BaseModel):
    lan_enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8765
    allowed_hosts: list[str] = []
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    session_idle_minutes: int = 60
    session_max_hours: int = 12
    confirm_lan: bool = False


@router.put("/api/server-config")
def put_server(body: ServerBody, ctx=Depends(get_ctx), principal: Principal = Depends(require("settings.manage")),
               conn=Depends(get_conn)):
    if not principal.is_admin:
        raise Forbidden("Only an administrator can change network settings.")
    if body.lan_enabled and not body.confirm_lan:
        raise ValidationFailed("Confirm that you understand the office-network access warning.")
    if not 1024 <= body.port <= 65535:
        raise ValidationFailed("Port must be between 1024 and 65535.")
    if not 5 <= body.session_idle_minutes <= 720 or not 1 <= body.session_max_hours <= 72:
        raise ValidationFailed("Session limits are out of range.")
    for h in body.allowed_hosts:
        if not re.fullmatch(r"[A-Za-z0-9.\-]+", h):
            raise ValidationFailed(f"'{h}' is not a valid host name or IP address.")
    if (body.tls_certfile or body.tls_keyfile) and not (body.tls_certfile and body.tls_keyfile):
        raise ValidationFailed("Provide both the TLS certificate and key file, or neither.")
    for f in (body.tls_certfile, body.tls_keyfile):
        if f and not Path(f).exists():
            raise ValidationFailed(f"File not found: {f}")
    cfg = ctx.config
    cfg.lan_enabled = body.lan_enabled
    cfg.host = body.host if body.lan_enabled else "127.0.0.1"
    cfg.port = body.port
    cfg.allowed_hosts = body.allowed_hosts
    cfg.tls_certfile, cfg.tls_keyfile = body.tls_certfile, body.tls_keyfile
    cfg.session_idle_minutes, cfg.session_max_hours = body.session_idle_minutes, body.session_max_hours
    cfg.save(ctx.paths.root)
    with db.transaction(conn):
        audit.log(conn, principal.label, "settings.server", "settings", None,
                  f"Network settings saved (LAN access {'ON' if body.lan_enabled else 'off'}); restart required",
                  body.model_dump())
    return {"ok": True, "restart_required": True, "server": _server_config(ctx)}


@router.post("/api/server/shutdown")
def shutdown(request: Request, ctx=Depends(get_ctx), principal: Principal = Depends(require("settings.manage")),
             conn=Depends(get_conn)):
    if not principal.is_admin:
        raise Forbidden("Only an administrator can stop the application.")
    if client_ip(request) not in ("127.0.0.1", "::1"):
        raise Forbidden("The application can only be stopped from the computer running it.")
    with db.transaction(conn):
        audit.log(conn, principal.label, "system.shutdown", None, None, "Application stopped from Settings")
    if ctx.shutdown_callback:
        threading.Timer(1.0, ctx.shutdown_callback).start()
    return {"ok": True}


# --------------------------------------------------------------------- users

class UserBody(BaseModel):
    email: str
    display_name: str
    role: str
    password: str | None = None
    locations: list[str] = []
    perm_grants: list[str] = []
    perm_revokes: list[str] = []
    hidden_fields: list[str] = []
    dob_access: str | None = None
    is_active: bool = True


def _user_out(u: dict) -> dict:
    return {k: (db.jload(v, []) if k in ("locations", "perm_grants", "perm_revokes", "hidden_fields") else v)
            for k, v in u.items() if k != "password_hash"}


@router.get("/api/users")
def list_users(principal: Principal = Depends(require("users.manage")), conn=Depends(get_conn)):
    return [_user_out(u) for u in db.rows(conn, "SELECT * FROM users ORDER BY email")]


def _validate_user(conn, body: UserBody) -> None:
    if body.role not in security.ROLES:
        raise ValidationFailed("Choose a valid role.")
    locs = settings_store.locations(conn)
    if any(l not in locs for l in body.locations):
        raise ValidationFailed("Unknown location in scope.")
    if any(p not in security.PERMISSIONS for p in body.perm_grants + body.perm_revokes):
        raise ValidationFailed("Unknown permission.")
    if body.dob_access and body.dob_access not in security.DOB_ACCESS:
        raise ValidationFailed("Unknown date-of-birth access level.")


@router.post("/api/users")
def create_user(body: UserBody, request: Request, principal: Principal = Depends(require("users.manage")),
                conn=Depends(get_conn)):
    email = body.email.strip().lower()
    if not security.EMAIL_RE.match(email):
        raise ValidationFailed("The username must be an email address.")
    _validate_user(conn, body)
    problem = security.password_problem(body.password or "", email)
    if problem:
        raise ValidationFailed("Temporary password: " + problem)
    if body.role == "admin" and not principal.is_admin:
        raise Forbidden("Only an administrator can create administrators.")
    with db.transaction(conn):
        if db.one(conn, "SELECT id FROM users WHERE email=?", (email,)):
            raise Conflict("A user with this email already exists.")
        uid = conn.execute(
            "INSERT INTO users(email, display_name, password_hash, role, is_active, locations, perm_grants, "
            "perm_revokes, hidden_fields, dob_access, must_change_password, created_at, created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (email, body.display_name.strip() or email, security.hash_password(body.password), body.role,
             1 if body.is_active else 0, db.jdump(body.locations), db.jdump(body.perm_grants),
             db.jdump(body.perm_revokes), db.jdump(body.hidden_fields), body.dob_access, timeutil.iso_utc(),
             principal.label)).lastrowid
        audit.log(conn, principal.label, "user.create", "user", uid, f"Created user {email} ({body.role})",
                  ip=client_ip(request))
    return _user_out(db.one(conn, "SELECT * FROM users WHERE id=?", (uid,)))


@router.put("/api/users/{user_id}")
def update_user(user_id: int, body: UserBody, request: Request,
                principal: Principal = Depends(require("users.manage")), conn=Depends(get_conn)):
    _validate_user(conn, body)
    with db.transaction(conn):
        u = db.one(conn, "SELECT * FROM users WHERE id=?", (user_id,))
        if not u:
            raise NotFound("User not found")
        if (u["role"] == "admin" or body.role == "admin") and not principal.is_admin:
            raise Forbidden("Only an administrator can change administrator accounts.")
        if u["role"] == "admin" and (body.role != "admin" or not body.is_active):
            admins = db.scalar(conn, "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1")
            if admins <= 1:
                raise ValidationFailed("At least one active administrator is required.")
        conn.execute("UPDATE users SET display_name=?, role=?, is_active=?, locations=?, perm_grants=?, perm_revokes=?, "
                     "hidden_fields=?, dob_access=?, updated_at=?, updated_by=? WHERE id=?",
                     (body.display_name.strip() or u["email"], body.role, 1 if body.is_active else 0,
                      db.jdump(body.locations), db.jdump(body.perm_grants), db.jdump(body.perm_revokes),
                      db.jdump(body.hidden_fields), body.dob_access, timeutil.iso_utc(), principal.label, user_id))
        if not body.is_active or body.role != u["role"]:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        audit.log(conn, principal.label, "user.update", "user", user_id, f"Updated user {u['email']}",
                  {"role": body.role, "active": body.is_active, "locations": body.locations,
                   "grants": body.perm_grants, "revokes": body.perm_revokes, "hidden_fields": body.hidden_fields,
                   "dob_access": body.dob_access}, ip=client_ip(request))
    return _user_out(db.one(conn, "SELECT * FROM users WHERE id=?", (user_id,)))


class ResetBody(BaseModel):
    temporary_password: str


@router.post("/api/users/{user_id}/reset-password")
def reset_password(user_id: int, body: ResetBody, request: Request,
                   principal: Principal = Depends(require("users.manage")), conn=Depends(get_conn)):
    with db.transaction(conn):
        u = db.one(conn, "SELECT * FROM users WHERE id=?", (user_id,))
        if not u:
            raise NotFound("User not found")
        if u["role"] == "admin" and not principal.is_admin:
            raise Forbidden("Only an administrator can reset an administrator's password.")
        problem = security.password_problem(body.temporary_password, u["email"])
        if problem:
            raise ValidationFailed(problem)
        conn.execute("UPDATE users SET password_hash=?, must_change_password=1, failed_logins=0, locked_until=NULL, "
                     "password_changed_at=? WHERE id=?",
                     (security.hash_password(body.temporary_password), timeutil.iso_utc(), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        audit.log(conn, principal.label, "user.password_reset", "user", user_id,
                  f"Reset password for {u['email']} (must change at next sign-in)", ip=client_ip(request))
    return {"ok": True}


@router.post("/api/users/{user_id}/unlock")
def unlock(user_id: int, principal: Principal = Depends(require("users.manage")), conn=Depends(get_conn)):
    with db.transaction(conn):
        conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (user_id,))
        audit.log(conn, principal.label, "user.unlock", "user", user_id, "Unlocked account")
    return {"ok": True}


# -------------------------------------------------------------------- tokens

class TokenBody(BaseModel):
    name: str
    locations: list[str] = []
    fields: list[str] = []
    include_photos: bool = False
    include_left: bool = False
    expires_days: int | None = None


@router.get("/api/tokens")
def list_tokens(principal: Principal = Depends(require("users.manage")), conn=Depends(get_conn)):
    items = db.rows(conn, "SELECT id, name, token_prefix, locations, fields, include_photos, include_left, created_at, "
                          "created_by, expires_at, revoked_at, revoked_by, last_used_at FROM api_tokens ORDER BY id DESC")
    for t in items:
        t["locations"] = db.jload(t["locations"], [])
        t["fields"] = db.jload(t["fields"], [])
    return items


@router.post("/api/tokens")
def create_token(body: TokenBody, principal: Principal = Depends(require("users.manage")), conn=Depends(get_conn)):
    if not body.name.strip():
        raise ValidationFailed("Name the token after the integration that will use it.")
    allowed = sorted(fields.token_allowed_fields(conn, body.fields))
    token = security.new_fetch_token()
    expires = None
    if body.expires_days:
        import datetime as dt
        expires = timeutil.iso_utc(timeutil.now_utc() + dt.timedelta(days=int(body.expires_days)))
    with db.transaction(conn):
        tid = conn.execute("INSERT INTO api_tokens(name, token_prefix, token_hash, locations, fields, include_photos, "
                           "include_left, created_at, created_by, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (body.name.strip(), token[:12], security.sha256_hex(token), db.jdump(body.locations),
                            db.jdump(allowed), 1 if body.include_photos else 0, 1 if body.include_left else 0,
                            timeutil.iso_utc(), principal.label, expires)).lastrowid
        audit.log(conn, principal.label, "token.create", "token", tid, f"Created fetch token '{body.name}'",
                  {"fields": allowed, "locations": body.locations})
    return {"id": tid, "token": token, "fields": allowed,
            "note": "Copy this token now. It is stored only as a hash and cannot be shown again."}


@router.post("/api/tokens/{token_id}/revoke")
def revoke_token(token_id: int, principal: Principal = Depends(require("users.manage")), conn=Depends(get_conn)):
    with db.transaction(conn):
        conn.execute("UPDATE api_tokens SET revoked_at=?, revoked_by=? WHERE id=? AND revoked_at IS NULL",
                     (timeutil.iso_utc(), principal.label, token_id))
        audit.log(conn, principal.label, "token.revoke", "token", token_id, "Revoked fetch token")
    return {"ok": True}


# ------------------------------------------------------------- custom fields

class CustomFieldBody(BaseModel):
    label: str
    field_type: str
    options: list[str] = []
    is_restricted: bool = False
    is_active: bool = True
    sort_order: int = 0
    key: str | None = None


@router.get("/api/custom-fields")
def list_custom(principal: Principal = Depends(require("settings.manage")), conn=Depends(get_conn)):
    items = db.rows(conn, "SELECT * FROM custom_fields ORDER BY sort_order, id")
    for i in items:
        i["options"] = db.jload(i["options"], [])
    return items


def _check_custom(body: CustomFieldBody) -> None:
    if body.field_type not in fields.CUSTOM_TYPES:
        raise ValidationFailed("Field type must be text, number, date, dropdown or yes/no.")
    if not body.label.strip():
        raise ValidationFailed("Enter a label.")
    if body.field_type == "dropdown":
        opts = [o.strip() for o in body.options if o.strip()]
        if not opts:
            raise ValidationFailed("A dropdown needs at least one option.")
        body.options = opts


@router.post("/api/custom-fields")
def create_custom(body: CustomFieldBody, principal: Principal = Depends(require("settings.manage")),
                  conn=Depends(get_conn)):
    _check_custom(body)
    key = re.sub(r"[^a-z0-9]+", "_", (body.key or body.label).lower()).strip("_")[:40]
    if not key or key[0].isdigit():
        key = "f_" + key
    with db.transaction(conn):
        if db.one(conn, "SELECT id FROM custom_fields WHERE key=?", (key,)):
            raise Conflict(f"A custom field with the key '{key}' already exists.")
        cid = conn.execute("INSERT INTO custom_fields(key, label, field_type, options, is_active, is_restricted, "
                           "sort_order, created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?)",
                           (key, body.label.strip(), body.field_type, db.jdump(body.options),
                            1 if body.is_active else 0, 1 if body.is_restricted else 0, body.sort_order,
                            timeutil.iso_utc(), principal.label)).lastrowid
        audit.log(conn, principal.label, "field.create", "custom_field", cid,
                  f"Added custom field '{body.label}' ({body.field_type})")
    return {"id": cid, "key": key}


@router.put("/api/custom-fields/{field_id}")
def update_custom(field_id: int, body: CustomFieldBody, principal: Principal = Depends(require("settings.manage")),
                  conn=Depends(get_conn)):
    _check_custom(body)
    with db.transaction(conn):
        cur = db.one(conn, "SELECT * FROM custom_fields WHERE id=?", (field_id,))
        if not cur:
            raise NotFound("Custom field not found")
        if cur["field_type"] != body.field_type:
            used = db.scalar(conn, "SELECT COUNT(*) FROM employees WHERE json_extract(custom, ?) IS NOT NULL",
                             (f"$.{cur['key']}",))
            if used:
                raise ValidationFailed(f"The type cannot change because {used} record(s) have values. Add a new field "
                                       "instead.")
        conn.execute("UPDATE custom_fields SET label=?, field_type=?, options=?, is_active=?, is_restricted=?, "
                     "sort_order=? WHERE id=?",
                     (body.label.strip(), body.field_type, db.jdump(body.options), 1 if body.is_active else 0,
                      1 if body.is_restricted else 0, body.sort_order, field_id))
        audit.log(conn, principal.label, "field.update", "custom_field", field_id, f"Updated custom field "
                  f"'{body.label}'", body.model_dump())
    return {"ok": True}


# --------------------------------------------------------------------- audit

@router.get("/api/audit")
def audit_log(action: str = "", actor: str = "", entity_type: str = "", entity_id: str = "", search: str = "",
              limit: int = 100, offset: int = 0, principal: Principal = Depends(require("audit.view")),
              conn=Depends(get_conn)):
    return audit.query(conn, action or None, actor or None, entity_type or None, entity_id or None, search or None,
                       limit, offset)


# ------------------------------------------------------------------- backups

@router.get("/api/backups")
def list_backups(ctx=Depends(get_ctx), principal: Principal = Depends(require("backups.manage")),
                 conn=Depends(get_conn)):
    return {"backups": backup.list_backups(conn, ctx.paths), "settings": settings_store.get(conn, "backup"),
            "destination": str(backup.destination(conn, ctx.paths))}


@router.post("/api/backups")
def create_backup(ctx=Depends(get_ctx), principal: Principal = Depends(require("backups.manage")),
                  conn=Depends(get_conn)):
    try:
        return backup.create_backup(conn, ctx.paths, "manual", principal.label)
    except backup.BackupError as exc:
        raise ValidationFailed(str(exc)) from None


@router.post("/api/backups/{backup_id}/verify")
def verify(backup_id: int, ctx=Depends(get_ctx), principal: Principal = Depends(require("backups.manage")),
           conn=Depends(get_conn)):
    b = db.one(conn, "SELECT * FROM backups WHERE id=?", (backup_id,))
    if not b:
        raise NotFound("Backup not found")
    try:
        info = backup.verify_backup(Path(b["path"]), ctx.paths)
    except (backup.BackupError, OSError) as exc:
        with db.transaction(conn):
            audit.log(conn, principal.label, "backup.verify_failed", "backup", backup_id, str(exc))
        raise ValidationFailed(f"Verification failed: {exc}") from None
    with db.transaction(conn):
        conn.execute("UPDATE backups SET verified_at=? WHERE id=?", (timeutil.iso_utc(), backup_id))
        audit.log(conn, principal.label, "backup.verify", "backup", backup_id, f"Verified {b['filename']}")
    return info


class RestoreBody(BaseModel):
    backup_id: int
    confirm_text: str


def _do_restore(ctx, principal: Principal, conn, path: Path) -> dict:
    if not principal.is_admin:
        raise Forbidden("Only an administrator can restore backups.")
    try:
        info = backup.verify_backup(path, ctx.paths)
    except backup.BackupError as exc:
        raise ValidationFailed(f"The backup cannot be restored: {exc}") from None
    if ctx.maintenance:
        raise Conflict("Another maintenance operation is running.")
    ctx.maintenance = "Restoring a backup. The application will be available again shortly."
    try:
        if ctx.scheduler:
            ctx.scheduler.paused = True
        if not ctx.wait_idle(60):
            raise Conflict("Other requests are still running; try again in a minute.")
        try:
            pre = backup.create_backup(conn, ctx.paths, "pre-restore", principal.label)
        except backup.BackupError as exc:
            raise ValidationFailed(f"Restore stopped: the safety backup of the current data failed ({exc}).") from None
        conn.close()
        if ctx.scheduler:
            with ctx.scheduler.busy:
                backup.restore(ctx.paths, path, principal.label, lambda: None)
        else:
            backup.restore(ctx.paths, path, principal.label, lambda: None)
        new = ctx.connect()
        try:
            from .. import db as dbm
            dbm.init_schema(new)
            backup.after_restore(new, principal.label,
                                 f"Restored backup {path.name} (created {info['manifest'].get('created_at')} on "
                                 f"{info['manifest'].get('hostname')}). Automatic sending is disabled until an "
                                 "administrator revalidates sender, recipients and task ownership.", pre)
            timeutil.set_timezone(settings_store.get(new, "timezone") or timeutil.DEFAULT_TZ)
        finally:
            new.close()
        ctx.reset_adapter()
    finally:
        ctx.maintenance = None
        if ctx.scheduler:
            ctx.scheduler.paused = False
    return {"ok": True, "restored": info["manifest"], "pre_restore_backup": pre["filename"],
            "message": "Restore complete. Everyone must sign in again. Automatic sending stays off until an "
                       "administrator revalidates it."}


@router.post("/api/backups/restore")
def restore(body: RestoreBody, ctx=Depends(get_ctx), principal: Principal = Depends(require("backups.manage")),
            conn=Depends(get_conn)):
    if body.confirm_text.strip().upper() != "RESTORE":
        raise ValidationFailed("Type RESTORE to confirm.")
    b = db.one(conn, "SELECT * FROM backups WHERE id=?", (body.backup_id,))
    if not b or not Path(b["path"]).exists():
        raise NotFound("Backup file not found")
    return _do_restore(ctx, principal, conn, Path(b["path"]))


@router.post("/api/backups/restore-upload")
def restore_upload(confirm_text: str, file: UploadFile = File(...), ctx=Depends(get_ctx),
                   principal: Principal = Depends(require("backups.manage")), conn=Depends(get_conn)):
    if confirm_text.strip().upper() != "RESTORE":
        raise ValidationFailed("Type RESTORE to confirm.")
    tmp = save_upload(file, ctx.paths.staging / "uploads", 64 * 1024 * 1024 * 1024)
    try:
        return _do_restore(ctx, principal, conn, tmp)
    finally:
        cleanup([tmp])


@router.get("/api/licenses", response_class=PlainTextResponse)
def licenses():
    path = resource_dir() / "static" / "THIRD_PARTY_NOTICES.txt"
    return path.read_text(encoding="utf-8") if path.exists() else "Third-party notices file not found."
