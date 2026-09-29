"""SQLite access, schema and migrations.

One central database file (employees.db) is used by every user of the
installation. Connections use WAL mode and a busy timeout; write operations
use BEGIN IMMEDIATE transactions so concurrent writers queue instead of
failing half-way.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import timeutil

SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    locations TEXT NOT NULL DEFAULT '[]',
    perm_grants TEXT NOT NULL DEFAULT '[]',
    perm_revokes TEXT NOT NULL DEFAULT '[]',
    hidden_fields TEXT NOT NULL DEFAULT '[]',
    dob_access TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    password_changed_at TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    updated_at TEXT,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    locations TEXT NOT NULL DEFAULT '[]',
    fields TEXT NOT NULL DEFAULT '[]',
    include_photos INTEGER NOT NULL DEFAULT 0,
    include_left INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT,
    expires_at TEXT,
    revoked_at TEXT,
    revoked_by TEXT,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rhc_id TEXT UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL,
    designation TEXT,
    department TEXT,
    official_email TEXT COLLATE NOCASE,
    location TEXT,
    date_of_joining TEXT,
    date_of_birth TEXT,
    employment_status TEXT NOT NULL DEFAULT 'Active',
    last_working_date TEXT,
    assignment_status TEXT NOT NULL DEFAULT 'Unknown',
    client_name TEXT,
    assignment_start TEXT,
    assignment_end TEXT,
    bench_start TEXT,
    bench_end TEXT,
    notes TEXT,
    record_type TEXT NOT NULL DEFAULT 'directory',
    current_photo_id INTEGER,
    custom TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_emp_status ON employees(record_type, employment_status);
CREATE INDEX IF NOT EXISTS ix_emp_email ON employees(official_email);
CREATE INDEX IF NOT EXISTS ix_emp_name ON employees(full_name);

CREATE TABLE IF NOT EXISTS employee_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    kind TEXT NOT NULL,
    status TEXT,
    client_name TEXT,
    start_date TEXT,
    end_date TEXT,
    details TEXT,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_hist_emp ON employee_history(employee_id);

CREATE TABLE IF NOT EXISTS field_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TEXT NOT NULL,
    changed_by TEXT,
    source TEXT,
    import_batch_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fc_emp ON field_changes(employee_id, field, id);
CREATE INDEX IF NOT EXISTS ix_fc_batch ON field_changes(import_batch_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    summary TEXT,
    details TEXT,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS custom_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    field_type TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    is_active INTEGER NOT NULL DEFAULT 1,
    is_restricted INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    stored_path TEXT NOT NULL,
    thumb_path TEXT,
    original_filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    format TEXT,
    file_size INTEGER,
    source TEXT,
    source_detail TEXT,
    is_current INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT,
    replaced_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_photos_emp ON photos(employee_id);

CREATE TABLE IF NOT EXISTS photo_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    staging_dir TEXT NOT NULL,
    summary TEXT,
    committed_at TEXT
);

CREATE TABLE IF NOT EXISTS photo_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES photo_batches(id),
    source_kind TEXT NOT NULL,
    source_name TEXT,
    original_filename TEXT,
    staged_path TEXT,
    thumb_path TEXT,
    sha256 TEXT,
    width INTEGER,
    height INTEGER,
    format TEXT,
    page_no INTEGER,
    bbox TEXT,
    context_text TEXT,
    suggested_employee_id INTEGER,
    match_method TEXT,
    match_note TEXT,
    flags TEXT NOT NULL DEFAULT '[]',
    assigned_employee_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    sort_key TEXT,
    committed_photo_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pbi_batch ON photo_batch_items(batch_id);

CREATE TABLE IF NOT EXISTS pdf_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES photo_batches(id),
    source_name TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    render_path TEXT,
    render_scale REAL,
    width_px INTEGER,
    height_px INTEGER,
    text TEXT,
    image_count INTEGER,
    flags TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS import_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    purpose TEXT NOT NULL,
    sheet_name TEXT,
    header_row INTEGER NOT NULL DEFAULT 1,
    mapping TEXT NOT NULL DEFAULT '{}',
    options TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_by TEXT,
    updated_at TEXT,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    file_path TEXT NOT NULL,
    purpose TEXT,
    template_id INTEGER,
    template_name TEXT,
    scope TEXT NOT NULL DEFAULT '[]',
    sheet_name TEXT,
    header_row INTEGER,
    mapping TEXT,
    options TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded',
    operator TEXT,
    created_at TEXT NOT NULL,
    previewed_at TEXT,
    committed_at TEXT,
    committed_by TEXT,
    undone_at TEXT,
    undone_by TEXT,
    summary TEXT,
    preview TEXT,
    decisions TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_ib_hash ON import_batches(file_hash);

CREATE TABLE IF NOT EXISTS import_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id),
    employee_id INTEGER NOT NULL,
    row_no INTEGER,
    action TEXT NOT NULL,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    field_change_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ic_batch ON import_changes(batch_id);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    schedule TEXT NOT NULL,
    inclusion TEXT,
    naming_template TEXT,
    export_format TEXT NOT NULL DEFAULT 'original',
    locations TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '{}',
    auto_send_enabled INTEGER NOT NULL DEFAULT 0,
    auto_send_validated_at TEXT,
    auto_send_validated_by TEXT,
    auto_send_with_omissions INTEGER NOT NULL DEFAULT 0,
    max_attachment_mb REAL NOT NULL DEFAULT 20,
    skip_when_empty INTEGER NOT NULL DEFAULT 1,
    catchup_grace_hours INTEGER NOT NULL DEFAULT 24,
    tracking_start TEXT,
    scheduled_through TEXT,
    owner_user_id INTEGER,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS task_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    period_key TEXT NOT NULL,
    period_label TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL,
    status_note TEXT,
    origin TEXT NOT NULL DEFAULT 'schedule',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    completed_by TEXT,
    UNIQUE(task_id, period_key)
);

CREATE TABLE IF NOT EXISTS period_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES task_periods(id),
    location TEXT NOT NULL,
    current_package_id INTEGER,
    status TEXT,
    UNIQUE(period_id, location)
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES task_periods(id),
    task_id INTEGER NOT NULL,
    location TEXT NOT NULL,
    revision INTEGER NOT NULL,
    zip_name TEXT NOT NULL,
    zip_path TEXT,
    zip_sha256 TEXT,
    zip_size INTEGER,
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT,
    inclusion TEXT,
    included_count INTEGER NOT NULL DEFAULT 0,
    omitted_count INTEGER NOT NULL DEFAULT 0,
    is_complete INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT,
    superseded_at TEXT,
    UNIQUE(period_id, location, revision)
);

CREATE TABLE IF NOT EXISTS package_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES packages(id),
    employee_id INTEGER NOT NULL,
    rhc_id TEXT,
    photo_id INTEGER NOT NULL,
    location TEXT NOT NULL,
    exported_filename TEXT NOT NULL,
    original_filename TEXT
);
CREATE INDEX IF NOT EXISTS ix_pi_pkg ON package_items(package_id);

CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES task_periods(id),
    task_id INTEGER NOT NULL,
    dispatch_ref TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    adapter TEXT NOT NULL,
    status TEXT NOT NULL,
    automatic INTEGER NOT NULL DEFAULT 0,
    package_ids TEXT NOT NULL DEFAULT '[]',
    sender TEXT,
    to_addrs TEXT,
    cc_addrs TEXT,
    subject TEXT,
    body TEXT,
    attachments TEXT NOT NULL DEFAULT '[]',
    total_bytes INTEGER,
    created_at TEXT NOT NULL,
    created_by TEXT,
    submitted_at TEXT,
    confirmed_at TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    entry_id TEXT,
    error TEXT,
    notes TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_active ON dispatches(period_id)
    WHERE status IN ('sending', 'draft_created', 'submitted', 'confirmed', 'uncertain', 'manual');

CREATE TABLE IF NOT EXISTS export_baseline (
    task_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    rhc_id TEXT,
    photo_id INTEGER NOT NULL,
    location TEXT NOT NULL,
    period_id INTEGER,
    dispatch_id INTEGER,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (task_id, employee_id)
);

CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    size INTEGER,
    sha256 TEXT,
    file_count INTEGER,
    verified_at TEXT,
    error TEXT
);
"""

# Migrations: {target_version: callable(conn)}. Version 1 is the baseline
# schema above. Future versions add entries here; a full backup is taken
# automatically before any migration runs (see app startup).
MIGRATIONS: dict[int, Any] = {}

_local = threading.local()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; rolls back on any exception.

    Nested use joins the outer transaction.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> dict | None:
    return row_to_dict(conn.execute(sql, tuple(params)).fetchone())


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable = ()):
    r = conn.execute(sql, tuple(params)).fetchone()
    return r[0] if r else None


def jload(value, default=None):
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def get_meta(conn, key, default=None):
    v = scalar(conn, "SELECT value FROM meta WHERE key=?", (key,))
    return v if v is not None else default


def set_meta(conn, key, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, None if value is None else str(value)))


def schema_version(conn) -> int:
    try:
        return int(get_meta(conn, "schema_version", 0))
    except sqlite3.OperationalError:
        return 0


def has_existing_data(db_path: Path) -> bool:
    return db_path.exists() and db_path.stat().st_size > 0


def needs_migration(db_path: Path) -> bool:
    if not has_existing_data(db_path):
        return False
    conn = connect(db_path)
    try:
        v = schema_version(conn)
        return 0 < v < SCHEMA_VERSION
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"This data folder was created by a newer version of the application (schema {current}). "
            "Install the newer version instead of downgrading.")
    conn.executescript(SCHEMA_V1)
    with transaction(conn):
        if current == 0:
            set_meta(conn, "schema_version", SCHEMA_VERSION)
            set_meta(conn, "created_at", timeutil.iso_utc())
        else:
            for target in range(current + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[target](conn)
                set_meta(conn, "schema_version", target)
