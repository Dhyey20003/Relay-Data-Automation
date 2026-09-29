import json
import time
from pathlib import Path

import pytest

from relay_hub import db, settings_store
from relay_hub.config import ServerConfig, looks_like_sync_folder
from relay_hub.context import AppContext, InstanceLock, InstanceLockError


def test_instance_lock_blocks_second_copy(tmp_path):
    lock = tmp_path / "hub.lock"
    lock.write_text(json.dumps({"host": "OTHER-PC", "pid": 1234, "port": 8765, "heartbeat": time.time()}))
    with pytest.raises(InstanceLockError, match="OTHER-PC"):
        InstanceLock(lock, 8765).acquire()
    lock.write_text(json.dumps({"host": "OTHER-PC", "pid": 1234, "port": 8765, "heartbeat": time.time() - 3600}))
    l2 = InstanceLock(lock, 8765)
    l2.acquire()  # stale lock is taken over
    assert json.loads(lock.read_text())["pid"] == l2.pid
    l2.release()
    assert not lock.exists()


def test_transfer_to_new_computer_disables_automatic_sending(tmp_path):
    ctx = AppContext(tmp_path / "d", background=False, lock=False)
    ctx.startup()
    with ctx.connect() as conn:
        conn.execute("UPDATE tasks SET auto_send_enabled=1, auto_send_validated_at='2026-01-01T00:00:00Z'")
        db.set_meta(conn, "last_host", "OLD-LAPTOP")
    ctx.shutdown()
    ctx2 = AppContext(tmp_path / "d", background=False, lock=False)
    ctx2.startup()
    with ctx2.connect() as conn:
        rv = settings_store.get(conn, "revalidation_required")
        assert "OLD-LAPTOP" in rv["reason"]
        assert db.scalar(conn, "SELECT COUNT(*) FROM tasks WHERE auto_send_validated_at IS NOT NULL") == 0


def test_sync_folders_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive - Relay"))
    assert looks_like_sync_folder(tmp_path / "OneDrive - Relay" / "HubData")
    assert looks_like_sync_folder(Path("/Users/x/Dropbox/hub"))
    assert looks_like_sync_folder(tmp_path / "LocalData") is None


def test_lan_access_is_off_unless_configured(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"host": "0.0.0.0", "lan_enabled": False}))
    cfg = ServerConfig.load(tmp_path)
    assert cfg.bind_host() == "127.0.0.1"
    (tmp_path / "config.json").write_text(json.dumps({"host": "0.0.0.0", "lan_enabled": True}))
    assert ServerConfig.load(tmp_path).bind_host() == "0.0.0.0"


def test_backup_before_schema_migration(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path / "d", background=False, lock=False)
    ctx.startup()
    ctx.shutdown()
    ran = []
    monkeypatch.setattr(db, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(db.MIGRATIONS, 2, lambda conn: ran.append(True))
    ctx2 = AppContext(tmp_path / "d", background=False, lock=False)
    ctx2.startup()
    assert ran == [True]
    backups = list((tmp_path / "d" / "backups").glob("RelayEmployeeHub_backup_pre-migration_*.zip"))
    assert len(backups) == 1
    with ctx2.connect() as conn:
        assert db.schema_version(conn) == 2
