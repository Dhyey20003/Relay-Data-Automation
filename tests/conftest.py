"""Shared fixtures. Every test uses a fresh temporary data folder, synthetic
employees and generated placeholder images. No real email is ever sent: the
mail adapter is a mock (or the dry-run adapter)."""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from relay_hub import timeutil
from relay_hub.app import create_app
from relay_hub.context import AppContext
from relay_hub.mail.base import MailAdapter, SendResult

ADMIN = ("admin@relay.test", "CorrectHorse9!")
PASSWORD = "UserPassw0rd!"


class MockAdapter(MailAdapter):
    """Records calls; behaviour is set per test. Never sends anything."""
    name = "mock"
    label = "Mock Outlook"
    supports_send = True
    supports_auto = True

    def __init__(self):
        self.calls = []
        self.result = "normal"  # normal | failed | uncertain
        self.found = "sent"     # value returned by reconcile()
        self.delay = 0.0
        self.lock = threading.Lock()

    def status(self):
        return {"adapter": self.name, "label": self.label, "available": True, "can_send": True, "accounts": [],
                "message": "mock"}

    def create(self, msg, send, timeout):
        time.sleep(self.delay)
        with self.lock:
            self.calls.append({"ref": msg.dispatch_ref, "send": send, "to": msg.to, "cc": msg.cc,
                               "subject": msg.subject, "body": msg.body,
                               "attachments": [Path(a).name for a in msg.attachments]})
        if self.result == "failed":
            return SendResult("failed", error="Mock failure")
        if self.result == "uncertain":
            return SendResult("uncertain", error="Mock timeout")
        return SendResult("submitted" if send else "draft_created", entry_id="mock-entry")

    def reconcile(self, dispatch):
        return self.found

    def discard_draft(self, dispatch):
        return True


class Api:
    """Thin wrapper adding the application header and CSRF token."""

    def __init__(self, client: TestClient):
        self.c = client
        self.csrf = None

    def _h(self, extra=None):
        h = {"X-Requested-With": "RelayHub"}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        h.update(extra or {})
        return h

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, json=None, **kw):
        return self.c.post(url, json=json, headers=self._h(kw.pop("headers", None)), **kw)

    def patch(self, url, json=None, **kw):
        return self.c.patch(url, json=json, headers=self._h(kw.pop("headers", None)), **kw)

    def put(self, url, json=None, **kw):
        return self.c.put(url, json=json, headers=self._h(kw.pop("headers", None)), **kw)

    def delete(self, url, **kw):
        return self.c.delete(url, headers=self._h(kw.pop("headers", None)), **kw)

    def login(self, email, password):
        r = self.post("/api/auth/login", {"email": email, "password": password})
        assert r.status_code == 200, r.text
        self.csrf = self.get("/api/me").json()["csrf"]
        return self


@pytest.fixture(autouse=True)
def _reset_clock():
    timeutil.freeze(None)
    timeutil.set_timezone("Asia/Kolkata")
    yield
    timeutil.freeze(None)


@pytest.fixture
def mock_mail():
    return MockAdapter()


@pytest.fixture
def env(tmp_path, mock_mail):
    ctx = AppContext(tmp_path / "data", background=False, lock=False, mail_override=mock_mail)
    app = create_app(ctx)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        api = Api(client)
        r = api.post("/api/setup", {"email": ADMIN[0], "display_name": "Admin", "password": ADMIN[1]})
        assert r.status_code == 200, r.text
        api.csrf = api.get("/api/me").json()["csrf"]
        yield Env(ctx, app, api, mock_mail)


class Env:
    def __init__(self, ctx, app, api, mail):
        self.ctx, self.app, self.api, self.mail = ctx, app, api, mail

    def conn(self):
        return self.ctx.connect()

    def new_client(self) -> Api:
        return Api(TestClient(self.app, base_url="http://127.0.0.1"))

    def add_user(self, email, role, **extra) -> Api:
        body = {"email": email, "display_name": email.split("@")[0], "role": role, "password": PASSWORD, **extra}
        r = self.api.post("/api/users", body)
        assert r.status_code == 200, r.text
        c = self.new_client().login(email, PASSWORD)
        # New users must change the temporary password first.
        r = c.post("/api/me/password", {"current_password": PASSWORD, "new_password": PASSWORD + "x"})
        assert r.status_code == 200, r.text
        return c

    def employee(self, **values) -> dict:
        values.setdefault("full_name", "Test Person")
        values.setdefault("location", "Ahmedabad")
        r = self.api.post("/api/employees", {"values": values})
        assert r.status_code == 200, r.text
        return r.json()


def image_bytes(color=(90, 120, 200), fmt="JPEG", size=(200, 240), text=None) -> bytes:
    im = Image.new("RGB" if fmt != "PNG" else "RGBA", size, color if fmt != "PNG" else color + (255,))
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


def upload_photo(env: Env, emp_id: int, color=(10, 20, 30), fmt="JPEG", name="photo.jpg"):
    r = env.api.post(f"/api/employees/{emp_id}/photo", files={"file": (name, image_bytes(color, fmt), "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()["photo_id"]
