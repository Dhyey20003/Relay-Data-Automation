"""Application settings kept in the database (included in backups)."""
from __future__ import annotations

import copy

from . import db, timeutil

DEFAULT_LOCATIONS = ["Ahmedabad", "Vadodara"]

DEFAULTS: dict = {
    "timezone": timeutil.DEFAULT_TZ,
    "locations": DEFAULT_LOCATIONS,
    # Numeric dates such as 03/04/2026 are read day-first (3 April 2026).
    "date_day_first": True,
    # Historical imports may fill these fields on matched directory employees
    # (only where the directory value is blank unless the import says otherwise).
    "historical_enrich_fields": ["date_of_joining", "date_of_birth", "department", "official_email"],
    # auto | outlook | manual | dry_run
    "mail_adapter": "auto",
    "outlook_default_account": "",
    # A submitted email not seen in Sent Items after this many minutes becomes
    # "Delivery Uncertain" and needs review. It is never resent automatically.
    "sent_items_timeout_minutes": 120,
    "outlook_send_timeout_seconds": 120,
    "photo_limits": {
        "max_file_mb": 25,
        "max_files_per_batch": 2000,
        "max_zip_mb": 1024,
        "max_zip_entries": 5000,
        "max_zip_uncompressed_mb": 4096,
        "max_pdf_pages": 300,
        "max_image_megapixels": 80,
    },
    "import_limits": {"max_file_mb": 50, "max_rows": 50000},
    "backup": {
        "destination": "",
        "daily_enabled": True,
        "daily_time": "13:00",
        "retention_daily": 30,
        "retention_manual": 0,
        "include_package_zips": False,
    },
    # Set after a restore or when the data folder is moved to another computer.
    "revalidation_required": None,
    "role_permissions": None,
    "role_dob_access": {"admin": "full", "editor": "full", "viewer": "day_month", "export_operator": "hidden"},
}


def get(conn, key: str):
    raw = db.scalar(conn, "SELECT value FROM settings WHERE key=?", (key,))
    if raw is None:
        return copy.deepcopy(DEFAULTS.get(key))
    value = db.jload(raw)
    default = DEFAULTS.get(key)
    if isinstance(default, dict) and isinstance(value, dict):
        merged = copy.deepcopy(default)
        merged.update(value)
        return merged
    return value


def set(conn, key: str, value, actor: str | None = None) -> None:  # noqa: A001 - mirrors get()
    conn.execute(
        "INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, db.jdump(value), timeutil.iso_utc(), actor))


def all_settings(conn) -> dict:
    return {k: get(conn, k) for k in DEFAULTS}


def locations(conn) -> list[str]:
    return list(get(conn, "locations") or DEFAULT_LOCATIONS)
