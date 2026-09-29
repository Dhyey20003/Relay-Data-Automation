"""Passwords, sessions, integration tokens, roles and permissions."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from . import db, settings_store, timeutil

# ---------------------------------------------------------------- passwords

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
                                          base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                                maxmem=64 * 1024 * 1024, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def password_problem(password: str, email: str = "") -> str | None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if email and password.lower() == email.lower():
        return "Password must not be the same as the email address."
    if len(set(password)) < 4:
        return "Password is too repetitive."
    return None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# -------------------------------------------------------------- permissions

PERMISSIONS: dict[str, str] = {
    "employees.view": "View the employee directory and profiles",
    "employees.edit": "Add and edit employees",
    "employees.archive": "Archive employees (mark as Left)",
    "employees.export": "Export employee data to Excel",
    "imports.run": "Run imports (historical, partial, designation, selected fields)",
    "imports.roster": "Run complete-roster imports and confirm deactivations",
    "imports.undo": "Undo import batches",
    "photos.view": "View photos",
    "photos.manage": "Upload, assign and replace photos",
    "tasks.view": "View headshot tasks, packages and history",
    "tasks.operate": "Prepare packages, create drafts, send and complete tasks",
    "tasks.configure": "Configure tasks, recipients and automatic sending",
    "users.manage": "Manage users and integration tokens",
    "settings.manage": "Manage settings, custom fields and locations",
    "backups.manage": "Create, verify and restore backups",
    "audit.view": "View the audit log",
}

ROLES = {
    "admin": "Administrator",
    "editor": "Editor",
    "viewer": "Viewer",
    "export_operator": "Export Operator",
}

ROLE_DEFAULTS: dict[str, list[str]] = {
    "admin": list(PERMISSIONS),
    "editor": ["employees.view", "employees.edit", "employees.archive", "employees.export", "imports.run",
               "imports.undo", "photos.view", "photos.manage", "tasks.view"],
    "viewer": ["employees.view", "photos.view"],
    "export_operator": ["employees.view", "photos.view", "employees.export", "tasks.view", "tasks.operate"],
}

DOB_ACCESS = ("full", "day_month", "hidden")


def role_permissions(conn) -> dict[str, list[str]]:
    custom = settings_store.get(conn, "role_permissions") or {}
    out = {}
    for role, perms in ROLE_DEFAULTS.items():
        chosen = custom.get(role, perms) if role != "admin" else list(PERMISSIONS)
        out[role] = [p for p in chosen if p in PERMISSIONS]
    return out


@dataclass
class Principal:
    kind: str  # "user" or "token"
    id: int
    email: str
    name: str
    role: str
    perms: frozenset
    locations: list[str] | None  # None means every location
    hidden_fields: set = field(default_factory=set)
    dob_access: str = "full"
    must_change_password: bool = False
    csrf: str | None = None
    session_hash: str | None = None
    include_photos: bool = False
    include_left: bool = False
    # Integration tokens only: the exact set of fields the token may read.
    allowed_fields: frozenset | None = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "user" and self.role == "admin"

    def can(self, perm: str) -> bool:
        return perm in self.perms

    def location_ok(self, location: str | None) -> bool:
        if self.locations is None:
            return True
        return location in self.locations

    def field_visible(self, key: str) -> bool:
        if self.allowed_fields is not None and key not in self.allowed_fields:
            return False
        if key in self.hidden_fields:
            return False
        if key == "date_of_birth" and self.dob_access == "hidden":
            return False
        return True

    def field_editable(self, key: str) -> bool:
        if not self.field_visible(key):
            return False
        if key == "date_of_birth" and self.dob_access != "full":
            return False
        return True

    @property
    def label(self) -> str:
        return self.email if self.kind == "user" else f"token:{self.name}"


def build_principal(conn, user: dict, csrf: str | None = None, session_hash: str | None = None) -> Principal:
    roles = role_permissions(conn)
    perms = set(roles.get(user["role"], []))
    if user["role"] != "admin":
        perms |= set(db.jload(user["perm_grants"], []))
        perms -= set(db.jload(user["perm_revokes"], []))
    locs = db.jload(user["locations"], []) or None
    if user["role"] == "admin":
        locs = None
    dob_defaults = settings_store.get(conn, "role_dob_access") or {}
    dob = user.get("dob_access") or dob_defaults.get(user["role"], "hidden")
    if user["role"] == "admin":
        dob = "full"
    hidden = set(db.jload(user["hidden_fields"], [])) if user["role"] != "admin" else set()
    return Principal(kind="user", id=user["id"], email=user["email"], name=user["display_name"], role=user["role"],
                     perms=frozenset(p for p in perms if p in PERMISSIONS), locations=locs, hidden_fields=hidden,
                     dob_access=dob, must_change_password=bool(user["must_change_password"]), csrf=csrf,
                     session_hash=session_hash)


# ------------------------------------------------------------------ sessions

SESSION_COOKIE = "rhc_session"


def create_session(conn, user_id: int, idle_minutes: int, max_hours: int, ip: str | None, ua: str | None) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = timeutil.now_utc()
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at, ip, user_agent) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (sha256_hex(token), user_id, csrf, timeutil.iso_utc(now), timeutil.iso_utc(now),
         timeutil.iso_utc(now + dt.timedelta(hours=max_hours)), ip, (ua or "")[:200]))
    return token, csrf


def load_session(conn, token: str, idle_minutes: int) -> tuple[dict, dict] | None:
    if not token:
        return None
    h = sha256_hex(token)
    sess = db.one(conn, "SELECT * FROM sessions WHERE token_hash=?", (h,))
    if not sess:
        return None
    now = timeutil.now_utc()
    if timeutil.parse_iso_utc(sess["expires_at"]) < now or \
            timeutil.parse_iso_utc(sess["last_seen_at"]) + dt.timedelta(minutes=idle_minutes) < now:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
        return None
    user = db.one(conn, "SELECT * FROM users WHERE id=? AND is_active=1", (sess["user_id"],))
    if not user:
        return None
    if (now - timeutil.parse_iso_utc(sess["last_seen_at"])).total_seconds() > 30:
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (timeutil.iso_utc(now), h))
    return sess, user


def purge_sessions(conn) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (timeutil.iso_utc(),))


# ------------------------------------------------------- login rate limiting

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


class IpThrottle:
    """In-memory per-IP failure counter (defence against password guessing)."""

    def __init__(self, limit: int = 20, window_s: int = 900):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            now = time.monotonic()
            hits = [t for t in self._hits.get(ip, []) if now - t < self.window]
            self._hits[ip] = hits
            return len(hits) >= self.limit

    def fail(self, ip: str) -> None:
        with self._lock:
            self._hits.setdefault(ip, []).append(time.monotonic())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._hits.pop(ip, None)


# ------------------------------------------------------ integration tokens

TOKEN_PREFIX = "rhcf_"


def new_fetch_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def load_token_principal(conn, token: str) -> Principal | None:
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rec = db.one(conn, "SELECT * FROM api_tokens WHERE token_hash=? AND revoked_at IS NULL", (sha256_hex(token),))
    if not rec:
        return None
    if rec["expires_at"] and timeutil.parse_iso_utc(rec["expires_at"]) < timeutil.now_utc():
        return None
    conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (timeutil.iso_utc(), rec["id"]))
    from .fields import token_allowed_fields
    allowed = token_allowed_fields(conn, db.jload(rec["fields"], []))
    return Principal(kind="token", id=rec["id"], email="", name=rec["name"], role="fetch",
                     perms=frozenset({"fetch.read"}), locations=db.jload(rec["locations"], []) or None,
                     hidden_fields=set(), dob_access="hidden", include_photos=bool(rec["include_photos"]),
                     include_left=bool(rec["include_left"]), csrf=None, allowed_fields=frozenset(allowed))
