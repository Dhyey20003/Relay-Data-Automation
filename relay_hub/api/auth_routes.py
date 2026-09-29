"""First-run setup, sign in/out, current user."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import APP_ID, APP_NAME, __version__, audit, db, fields, security, settings_store, timeutil
from ..errors import ValidationFailed
from ..security import Principal
from .deps import client_ip, get_conn, get_ctx, get_principal

router = APIRouter()


class SetupBody(BaseModel):
    email: str
    display_name: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


def _set_cookie(request: Request, response: Response, token: str) -> None:
    ctx = get_ctx(request)
    response.set_cookie(security.SESSION_COOKIE, token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=ctx.config.session_max_hours * 3600, path="/")


@router.get("/api/health")
def health(conn=Depends(get_conn)):
    users = db.scalar(conn, "SELECT COUNT(*) FROM users")
    return {"app": APP_ID, "name": APP_NAME, "version": __version__, "setup_required": users == 0}


@router.post("/api/setup")
def first_run_setup(body: SetupBody, request: Request, response: Response, conn=Depends(get_conn)):
    email = body.email.strip().lower()
    if not security.EMAIL_RE.match(email):
        raise ValidationFailed("Enter a valid email address as the username.")
    problem = security.password_problem(body.password, email)
    if problem:
        raise ValidationFailed(problem)
    if not body.display_name.strip():
        raise ValidationFailed("Enter your name.")
    ctx = get_ctx(request)
    with db.transaction(conn):
        if db.scalar(conn, "SELECT COUNT(*) FROM users"):
            raise HTTPException(409, "Setup has already been completed. Sign in instead.")
        now = timeutil.iso_utc()
        uid = conn.execute("INSERT INTO users(email, display_name, password_hash, role, created_at, created_by, "
                           "password_changed_at) VALUES(?,?,?,?,?,?,?)",
                           (email, body.display_name.strip(), security.hash_password(body.password), "admin", now,
                            "first-run setup", now)).lastrowid
        audit.log(conn, email, "user.setup_admin", "user", uid, "First administrator created", ip=client_ip(request))
        token, _ = security.create_session(conn, uid, ctx.config.session_idle_minutes, ctx.config.session_max_hours,
                                           client_ip(request), request.headers.get("user-agent"))
    _set_cookie(request, response, token)
    return {"ok": True}


@router.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response, conn=Depends(get_conn)):
    ctx = get_ctx(request)
    ip = client_ip(request)
    if ctx.ip_throttle.blocked(ip):
        raise HTTPException(429, "Too many failed sign-in attempts from this computer. Wait 15 minutes.")
    email = body.email.strip().lower()
    generic = "Email or password is incorrect."
    with db.transaction(conn):
        user = db.one(conn, "SELECT * FROM users WHERE email=?", (email,))
        now = timeutil.now_utc()
        if user and user["locked_until"] and timeutil.parse_iso_utc(user["locked_until"]) > now:
            raise HTTPException(423, "This account is temporarily locked after repeated failed sign-ins. Try again "
                                     "later or ask an administrator to unlock it.")
        if not user or not user["is_active"] or not security.verify_password(body.password, user["password_hash"]):
            ctx.ip_throttle.fail(ip)
            if user:
                fails = user["failed_logins"] + 1
                locked = None
                if fails >= security.MAX_FAILED_LOGINS:
                    locked = timeutil.iso_utc(now + dt.timedelta(minutes=security.LOCKOUT_MINUTES))
                    fails = 0
                conn.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (fails, locked, user["id"]))
                audit.log(conn, email, "auth.login_failed", "user", user["id"], "Failed sign-in", ip=ip)
            if not user:
                security.verify_password(body.password, security.hash_password("timing-equaliser"))
            response.status_code = 401
            return {"error": generic}
        conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL, last_login_at=? WHERE id=?",
                     (timeutil.iso_utc(now), user["id"]))
        token, _ = security.create_session(conn, user["id"], ctx.config.session_idle_minutes,
                                           ctx.config.session_max_hours, ip, request.headers.get("user-agent"))
        audit.log(conn, email, "auth.login", "user", user["id"], "Signed in", ip=ip)
    ctx.ip_throttle.reset(ip)
    _set_cookie(request, response, token)
    return {"ok": True, "must_change_password": bool(user["must_change_password"])}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, principal: Principal = Depends(get_principal),
           conn=Depends(get_conn)):
    with db.transaction(conn):
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (principal.session_hash,))
        audit.log(conn, principal.label, "auth.logout", "user", principal.id, "Signed out", ip=client_ip(request))
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
def me(request: Request, principal: Principal = Depends(get_principal), conn=Depends(get_conn)):
    ctx = get_ctx(request)
    fdefs = []
    for f in fields.all_fields(conn):
        if not principal.field_visible(f.key):
            continue
        d = f.as_dict()
        d["editable"] = f.editable and principal.field_editable(f.key) and principal.can("employees.edit")
        if f.key == "date_of_birth":
            d["dob_access"] = principal.dob_access
        if f.key == "photo" and not principal.can("photos.view"):
            continue
        fdefs.append(d)
    adapter = ctx.adapter(conn)
    return {
        "user": {"id": principal.id, "email": principal.email, "name": principal.name, "role": principal.role,
                 "role_label": security.ROLES.get(principal.role, principal.role),
                 "must_change_password": principal.must_change_password},
        "permissions": sorted(principal.perms),
        "locations": principal.locations or settings_store.locations(conn),
        "all_locations": settings_store.locations(conn),
        "csrf": principal.csrf,
        "fields": fdefs,
        "timezone": timeutil.tz_name(),
        "today": timeutil.today().isoformat(),
        "revalidation_required": settings_store.get(conn, "revalidation_required"),
        "mail_adapter": {"name": adapter.name, "label": adapter.label},
        "data_dir": str(ctx.paths.root) if principal.is_admin else None,
        "lan_enabled": ctx.config.lan_enabled,
        "version": __version__,
    }


@router.post("/api/me/password")
def change_password(body: PasswordBody, request: Request, principal: Principal = Depends(get_principal),
                    conn=Depends(get_conn)):
    with db.transaction(conn):
        user = db.one(conn, "SELECT * FROM users WHERE id=?", (principal.id,))
        if not security.verify_password(body.current_password, user["password_hash"]):
            raise ValidationFailed("The current password is incorrect.")
        problem = security.password_problem(body.new_password, user["email"])
        if problem:
            raise ValidationFailed(problem)
        if body.new_password == body.current_password:
            raise ValidationFailed("Choose a new password that is different from the current one.")
        conn.execute("UPDATE users SET password_hash=?, must_change_password=0, password_changed_at=? WHERE id=?",
                     (security.hash_password(body.new_password), timeutil.iso_utc(), principal.id))
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (principal.id, principal.session_hash))
        audit.log(conn, principal.label, "user.password_change", "user", principal.id, "Changed own password",
                  ip=client_ip(request))
    return {"ok": True}
