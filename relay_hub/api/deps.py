"""Request dependencies: database connection, authentication, CSRF and permissions."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import Depends, HTTPException, Request, UploadFile

from .. import security
from ..context import AppContext
from ..errors import Forbidden, ValidationFailed
from ..security import Principal

PASSWORD_CHANGE_ALLOWED = {"/api/me", "/api/me/password", "/api/auth/logout"}


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_conn(request: Request):
    conn = get_ctx(request).connect()
    try:
        yield conn
    finally:
        conn.close()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def get_principal(request: Request, conn=Depends(get_conn)) -> Principal:
    ctx = get_ctx(request)
    token = request.cookies.get(security.SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Please sign in.")
    loaded = security.load_session(conn, token, ctx.config.session_idle_minutes)
    if not loaded:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    sess, user = loaded
    principal = security.build_principal(conn, user, sess["csrf_token"], sess["token_hash"])
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not security.hmac.compare_digest(sent, sess["csrf_token"]):
            raise HTTPException(403, "Security token missing or expired. Reload the page and try again.")
    if principal.must_change_password and request.url.path not in PASSWORD_CHANGE_ALLOWED:
        raise HTTPException(403, "You must change your password before continuing.")
    return principal


def require(perm: str):
    def dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.can(perm):
            raise Forbidden("Your account does not have permission for this action.")
        return principal
    return dep


def require_any(*perms: str):
    def dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not any(principal.can(p) for p in perms):
            raise Forbidden("Your account does not have permission for this action.")
        return principal
    return dep


def get_token_principal(request: Request, conn=Depends(get_conn)) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Integration token required (Authorization: Bearer ...).")
    principal = security.load_token_principal(conn, auth[7:].strip())
    if not principal:
        raise HTTPException(401, "Invalid, expired or revoked integration token.")
    return principal


def save_upload(upload: UploadFile, folder: Path, max_bytes: int) -> Path:
    """Copy an uploaded file to a temporary file in the staging folder, enforcing a size limit."""
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "").suffix.lower()[:10]
    fd, name = tempfile.mkstemp(prefix="upload_", suffix=suffix, dir=folder)
    written = 0
    with open(fd, "wb") as out:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                out.close()
                Path(name).unlink(missing_ok=True)
                raise ValidationFailed(f"'{upload.filename}' is larger than the {max_bytes // 1048576} MB limit.")
            out.write(chunk)
    return Path(name)


def cleanup(paths: list[Path]) -> None:
    for p in paths:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass
