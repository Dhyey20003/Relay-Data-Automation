"""Application context: data paths, startup checks, instance lock and background worker."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

from . import __version__, backup, db, dispatch, imports, security, settings_store, tasks, timeutil
from .config import Paths, ServerConfig
from .mail.base import MailAdapter, select_adapter

log = logging.getLogger("relay_hub")
HEARTBEAT_S = 30
STALE_S = 150


class InstanceLockError(RuntimeError):
    def __init__(self, message: str, same_host: bool, port: int | None = None):
        super().__init__(message)
        self.same_host = same_host
        self.port = port


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InstanceLock:
    """Guards against two running copies using one data folder (hub.lock with heartbeat)."""

    def __init__(self, path: Path, port: int):
        self.path = path
        self.port = port
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.held = False

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def acquire(self, force: bool = False) -> None:
        cur = self.read()
        if cur and not force:
            age = time.time() - float(cur.get("heartbeat", 0))
            if age < STALE_S and cur.get("pid") != self.pid:
                if cur.get("host") == self.host and _pid_alive(int(cur.get("pid", 0))):
                    raise InstanceLockError("Relay Employee Hub is already running on this computer.", True,
                                            cur.get("port"))
                if cur.get("host") != self.host:
                    raise InstanceLockError(
                        f"This data folder is in use by Relay Employee Hub on computer '{cur.get('host')}'. "
                        "Only one copy may use a data folder at a time. Stop it there first.", False)
        self.write()
        self.held = True

    def write(self) -> None:
        data = {"host": self.host, "pid": self.pid, "port": self.port, "heartbeat": time.time(),
                "version": __version__}
        tmp = self.path.with_suffix(".lock.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.path)

    def release(self) -> None:
        cur = self.read()
        if self.held and cur and cur.get("pid") == self.pid and cur.get("host") == self.host:
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False


def setup_logging(paths: Paths) -> None:
    logs = paths.root / "logs"
    logs.mkdir(exist_ok=True)
    root = logging.getLogger("relay_hub")
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    h = logging.handlers.RotatingFileHandler(logs / "relay_hub.log", maxBytes=2_000_000, backupCount=5,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    root.setLevel(logging.INFO)


class AppContext:
    def __init__(self, data_dir: Path, config: ServerConfig | None = None, mail_override: MailAdapter | None = None,
                 background: bool = True, lock: bool = True):
        self.paths = Paths(data_dir)
        self.paths.ensure()
        self.config = config or ServerConfig.load(self.paths.root)
        self.mail_override = mail_override
        self.background = background
        self.use_lock = lock
        self.maintenance: str | None = None
        self.ip_throttle = security.IpThrottle()
        self._adapter: MailAdapter | None = None
        self._adapter_mode: str | None = None
        self._adapter_lock = threading.Lock()
        self._active = 0
        self._active_lock = threading.Condition()
        self.scheduler: Scheduler | None = None
        self.instance_lock = InstanceLock(self.paths.lock, self.config.port)
        self.shutdown_callback = None
        self.started = False

    # -- database -------------------------------------------------------
    def connect(self):
        return db.connect(self.paths.db)

    def enter_request(self) -> None:
        with self._active_lock:
            self._active += 1

    def exit_request(self) -> None:
        with self._active_lock:
            self._active -= 1
            self._active_lock.notify_all()

    def wait_idle(self, timeout: float = 30, allowance: int = 1) -> bool:
        end = time.time() + timeout
        with self._active_lock:
            while self._active > allowance:
                left = end - time.time()
                if left <= 0:
                    return False
                self._active_lock.wait(left)
        return True

    # -- startup --------------------------------------------------------
    def startup(self) -> None:
        setup_logging(self.paths)
        if self.use_lock:
            self.instance_lock.acquire()
        if db.needs_migration(self.paths.db):
            log.info("Schema migration required; creating a pre-migration backup")
            conn = self.connect()
            try:
                backup.create_backup(conn, self.paths, "pre-migration", "system")
            finally:
                conn.close()
        conn = self.connect()
        try:
            db.init_schema(conn)
            with db.transaction(conn):
                if not db.get_meta(conn, "instance_id"):
                    db.set_meta(conn, "instance_id", secrets.token_hex(8))
                tasks.ensure_default_tasks(conn)
                host = socket.gethostname()
                last_host = db.get_meta(conn, "last_host")
                if last_host and last_host != host:
                    settings_store.set(conn, "revalidation_required",
                                       {"reason": f"The data folder was moved from computer '{last_host}' to '{host}'.",
                                        "since": timeutil.iso_utc(), "by": "system"}, "system")
                    conn.execute("UPDATE tasks SET auto_send_validated_at=NULL, auto_send_validated_by=NULL")
                    from . import audit
                    audit.log(conn, "system", "system.transfer_detected", None, None,
                              f"Data folder moved from {last_host} to {host}; automatic sending disabled until "
                              "revalidated")
                db.set_meta(conn, "last_host", host)
                db.set_meta(conn, "last_started_at", timeutil.iso_utc())
                db.set_meta(conn, "app_version", __version__)
            timeutil.set_timezone(settings_store.get(conn, "timezone") or timeutil.DEFAULT_TZ)
            tasks.recover_after_restart(conn)
            security.purge_sessions(conn)
        finally:
            conn.close()
        imports.cleanup_staging(self.paths)
        if self.background:
            self.scheduler = Scheduler(self)
            self.scheduler.start()
        self.started = True
        log.info("Relay Employee Hub %s started with data folder %s", __version__, self.paths.root)

    def shutdown(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
        self.instance_lock.release()

    # -- mail -----------------------------------------------------------
    def adapter(self, conn=None) -> MailAdapter:
        if self.mail_override is not None:
            return self.mail_override
        own = conn is None
        conn = conn or self.connect()
        try:
            mode = settings_store.get(conn, "mail_adapter") or "auto"
        finally:
            if own:
                conn.close()
        with self._adapter_lock:
            if self._adapter is None or self._adapter_mode != mode:
                self._adapter = select_adapter(mode, self.paths)
                self._adapter_mode = mode
            return self._adapter

    def reset_adapter(self) -> None:
        with self._adapter_lock:
            self._adapter = None


class Scheduler(threading.Thread):
    """Background worker: schedules, automatic sends, reconciliation, daily backups, heartbeat."""

    def __init__(self, ctx: AppContext, interval: float = 30):
        super().__init__(name="relay-scheduler", daemon=True)
        self.ctx = ctx
        self.interval = interval
        self._stop = threading.Event()
        self._last_reconcile = 0.0
        self._last_purge = 0.0
        self.paused = False
        self.busy = threading.Lock()  # held while a tick runs (restore waits on it)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        time.sleep(3)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("Scheduler tick failed")
            self._stop.wait(self.interval)

    def tick(self) -> None:
        with self.busy:
            self._tick()

    def _tick(self) -> None:
        if self.ctx.use_lock and self.ctx.instance_lock.held:
            self.ctx.instance_lock.write()
        if self.ctx.maintenance or self.paused:
            return
        conn = self.ctx.connect()
        try:
            for action in tasks.schedule_tick(conn, self.ctx.paths):
                self.auto_process(conn, action)
            if time.time() - self._last_reconcile > 300:
                self._last_reconcile = time.time()
                open_count = db.scalar(conn, "SELECT COUNT(*) FROM dispatches WHERE status IN "
                                             "('draft_created','submitted','uncertain')")
                if open_count:
                    dispatch.reconcile(conn, self.ctx.adapter(conn))
            if backup.daily_due(conn):
                try:
                    backup.create_backup(conn, self.ctx.paths, "daily", "system")
                except backup.BackupError:
                    log.exception("Daily backup failed")
            if time.time() - self._last_purge > 3600:
                self._last_purge = time.time()
                security.purge_sessions(conn)
        finally:
            conn.close()

    def auto_process(self, conn, action: dict) -> None:
        task, period = action["task"], action["period"]
        actor = "system (scheduler)"
        try:
            result = tasks.prepare(conn, self.ctx.paths, actor, period["id"], automatic=True)
        except Exception as exc:  # noqa: BLE001 - status already recorded
            log.warning("Automatic preparation failed for period %s: %s", period["id"], exc)
            return
        task = tasks.load_task(conn, task["id"])
        blockers = tasks.auto_send_blockers(conn, task)
        if result["status"] not in (tasks.READY, tasks.NEEDS_ATTENTION):
            return
        if blockers:
            if task["auto_send_enabled"]:
                with db.transaction(conn):
                    cur = tasks.get_period(conn, period["id"])
                    tasks.set_status(conn, period["id"], tasks.NEEDS_ATTENTION,
                                     (cur["status_note"] or "") + " Automatic sending skipped: " + " ".join(blockers))
            return
        if result["status"] == tasks.NEEDS_ATTENTION and not task["auto_send_with_omissions"]:
            return
        adapter = self.ctx.adapter(conn)
        if not adapter.supports_auto:
            with db.transaction(conn):
                tasks.set_status(conn, period["id"], tasks.NEEDS_ATTENTION,
                                 "Packages are ready but automatic sending is unavailable: "
                                 + (adapter.status().get("message") or ""))
            return
        try:
            dispatch.send(conn, self.ctx.paths, adapter, period["id"], "send", actor, None, automatic=True,
                          allow_incomplete=bool(task["auto_send_with_omissions"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("Automatic send not performed for period %s: %s", period["id"], exc)
            with db.transaction(conn):
                cur = tasks.get_period(conn, period["id"])
                if cur["status"] in (tasks.READY, tasks.NEEDS_ATTENTION):
                    tasks.set_status(conn, period["id"], tasks.NEEDS_ATTENTION,
                                     f"Automatic sending was not performed: {getattr(exc, 'message', exc)}")
