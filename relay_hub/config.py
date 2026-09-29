"""Data-folder resolution and server configuration.

Writable data lives in one folder, separate from the application binaries:

    <data>/employees.db      SQLite database (single central database)
    <data>/images/           managed original photos (one folder per employee)
    <data>/thumbnails/       generated thumbnails
    <data>/exports/          packages, ready folders, spreadsheet exports
    <data>/import_staging/   uploaded files awaiting review
    <data>/backups/          default backup destination (never inside a backup)
    <data>/config.json       server settings (host, port, LAN access)

The data folder is chosen, in order, from: the --data-dir argument, the
RELAY_HUB_DATA_DIR environment variable, the pointer file written by
"--set-data-dir", and finally the per-user default
(%LOCALAPPDATA%\\RelayEmployeeHub\\Data on Windows).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATA_SUBDIRS = ("images", "thumbnails", "exports", "import_staging", "backups")
SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "sharepoint", "box sync")
DEFAULT_PORT = 8765


def app_home() -> Path:
    """Per-user folder for the data-folder pointer (never synchronised)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "RelayEmployeeHub"
    return Path.home() / ".relay_employee_hub"


def pointer_file() -> Path:
    return app_home() / "data_location.json"


def default_data_dir() -> Path:
    return app_home() / "Data"


def resolve_data_dir(cli_value: str | None = None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    env = os.environ.get("RELAY_HUB_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    pf = pointer_file()
    if pf.exists():
        try:
            value = json.loads(pf.read_text(encoding="utf-8")).get("data_dir")
            if value:
                return Path(value).expanduser().resolve()
        except (OSError, ValueError):
            pass
    return default_data_dir().resolve()


def write_pointer(data_dir: Path) -> None:
    pf = pointer_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"data_dir": str(Path(data_dir).resolve())}, indent=2), encoding="utf-8")


def looks_like_sync_folder(path: Path) -> str | None:
    """Return a reason if the path appears to be inside a cloud-sync folder."""
    p = str(Path(path).resolve()).lower()
    for env in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(env)
        if root and p.startswith(str(Path(root).resolve()).lower()):
            return f"inside the {env} folder ({root})"
    for marker in SYNC_MARKERS:
        if marker in p.replace("\\", "/").split("/") or any(part.startswith(marker) for part in p.replace("\\", "/").split("/")):
            return f"path contains '{marker}'"
    return None


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    lan_enabled: bool = False
    # Extra host names accepted in the Host header when LAN access is on
    # (for example the computer name or its fixed office IP address).
    allowed_hosts: list[str] = field(default_factory=list)
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    session_idle_minutes: int = 60
    session_max_hours: int = 12

    @classmethod
    def load(cls, data_dir: Path) -> "ServerConfig":
        path = Path(data_dir) / "config.json"
        cfg = cls()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (OSError, ValueError):
                pass
        if not cfg.lan_enabled:
            # LAN access must be switched on explicitly; otherwise always loopback.
            cfg.host = "127.0.0.1"
        return cfg

    def save(self, data_dir: Path) -> None:
        path = Path(data_dir) / "config.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def bind_host(self) -> str:
        return (self.host or "0.0.0.0") if self.lan_enabled else "127.0.0.1"


class Paths:
    """Resolved locations inside the data folder."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve()
        self.db = self.root / "employees.db"
        self.images = self.root / "images"
        self.thumbnails = self.root / "thumbnails"
        self.exports = self.root / "exports"
        self.staging = self.root / "import_staging"
        self.backups = self.root / "backups"
        self.config = self.root / "config.json"
        self.lock = self.root / "hub.lock"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in DATA_SUBDIRS:
            (self.root / sub).mkdir(exist_ok=True)
        for sub in ("packages", "ready", "spreadsheets", "reports", "dry_run_mail"):
            (self.exports / sub).mkdir(exist_ok=True)

    def inside(self, candidate: Path, base: Path | None = None) -> bool:
        base = (base or self.root).resolve()
        try:
            Path(candidate).resolve().relative_to(base)
            return True
        except ValueError:
            return False


def resource_dir() -> Path:
    """Location of bundled read-only resources (static files) in source and frozen builds."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "relay_hub"
    return Path(__file__).resolve().parent
