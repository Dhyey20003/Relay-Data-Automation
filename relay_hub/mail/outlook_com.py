"""Classic Outlook automation through local COM (pywin32) using the existing profile.

Runs only on Windows with classic Outlook (Outlook.exe) installed. The new
Outlook for Windows and Outlook on the web do not provide COM automation.
All COM calls run on one dedicated single-threaded-apartment worker thread.

Security: this module never bypasses Outlook's Object Model Guard. If Outlook
shows a security prompt, the call waits for the operator; if it times out the
dispatch is recorded as Delivery Uncertain and must be reviewed.

NOTE: this module could not be executed in the development environment
(Linux, no Outlook). See docs/WINDOWS_MANUAL_CHECKLIST.md.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

from .base import MailAdapter, MailMessage, SendResult

OL_MAIL_ITEM = 0
OL_TEXT = 1
OL_FOLDER_OUTBOX = 4
OL_FOLDER_SENT = 5
OL_FOLDER_DRAFTS = 16
OL_MAIL_CLASS = 43
PROP_NAME = "RelayDispatchId"
SEND_USING_ACCOUNT_DISPID = 64209

KNOWN_HRESULTS = {
    -2147221005: "Classic Outlook is not installed (COM class 'Outlook.Application' not found).",
    -2147221164: "Classic Outlook is not registered for automation on this computer.",
    -2146959355: "Outlook could not be started for automation. Close Outlook and try again, and make sure Outlook "
                 "and this application run as the same Windows user (neither 'as administrator').",
    -2147467260: "Outlook blocked or cancelled the operation (for example a security prompt was denied).",
    -2147024891: "Access denied: company policy or Outlook security settings block programmatic access.",
    -2147418111: "Outlook is busy (a dialog may be open). Close any Outlook dialogs and try again.",
}
DEFINITELY_NOT_SENT = {-2147467260, -2147024891}


class ComTimeout(Exception):
    pass


def describe_com_error(exc: BaseException) -> tuple[int | None, str]:
    hresult = getattr(exc, "hresult", None)
    if hresult is None and getattr(exc, "args", None) and isinstance(exc.args[0], int):
        hresult = exc.args[0]
    msg = KNOWN_HRESULTS.get(hresult)
    excepinfo = getattr(exc, "excepinfo", None)
    if excepinfo and len(excepinfo) > 2 and excepinfo[2]:
        detail = str(excepinfo[2])
        msg = f"{msg} Outlook said: {detail}" if msg else f"Outlook said: {detail}"
        scode = excepinfo[5] if len(excepinfo) > 5 else None
        if scode in KNOWN_HRESULTS and hresult not in KNOWN_HRESULTS:
            hresult = scode
    return hresult, msg or f"Outlook automation error: {exc}"


def classic_outlook_problem() -> str | None:
    """Return why classic Outlook COM is unavailable, or None if it looks available."""
    try:
        import winreg
    except ImportError:
        return "Classic Outlook automation requires Windows."
    try:
        import win32com.client  # noqa: F401
        import pythoncom  # noqa: F401
    except ImportError:
        return "The pywin32 package is missing, so Outlook automation is unavailable."
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Outlook.Application\CLSID"):
            pass
    except OSError:
        return ("Classic Outlook is not installed on this computer. The new Outlook for Windows and Outlook on the web "
                "do not support automation, so packages must be attached and sent manually.")
    return None


def new_outlook_toggle_on() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Office\16.0\Outlook\Preferences") as k:
            value, _ = winreg.QueryValueEx(k, "UseNewOutlook")
            return int(value) == 1
    except (ImportError, OSError, ValueError):
        return False


class _ComWorker:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="outlook-com", daemon=True)
        self.thread.start()

    def _run(self):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            while True:
                fn, box, done = self.q.get()
                try:
                    box["result"] = fn()
                except BaseException as exc:  # noqa: BLE001 - relayed to caller
                    box["error"] = exc
                finally:
                    done.set()
        finally:
            pythoncom.CoUninitialize()

    def submit(self, fn):
        box: dict = {}
        done = threading.Event()
        self.q.put((fn, box, done))
        return box, done

    def call(self, fn, timeout: float):
        box, done = self.submit(fn)
        if not done.wait(timeout):
            raise ComTimeout()
        if "error" in box:
            raise box["error"]
        return box["result"]


_worker: _ComWorker | None = None
_worker_lock = threading.Lock()


def worker() -> _ComWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = _ComWorker()
        return _worker


def _app():
    from win32com.client import dynamic
    return dynamic.Dispatch("Outlook.Application")


def _accounts(ns) -> list:
    out = []
    for i in range(1, ns.Accounts.Count + 1):
        out.append(ns.Accounts.Item(i))
    return out


def _find_account(ns, sender: str):
    wanted = (sender or "").strip().lower()
    for acct in _accounts(ns):
        smtp = (getattr(acct, "SmtpAddress", "") or "").lower()
        name = (acct.DisplayName or "").lower()
        if wanted in (smtp, name):
            return acct
    return None


class OutlookComAdapter(MailAdapter):
    name = "outlook"
    label = "Classic Outlook (COM)"
    supports_send = True
    supports_auto = True

    def __init__(self, problem: str | None = None):
        self.problem = problem
        # Late results of calls that timed out: dispatch_ref -> SendResult
        self.late_results: dict[str, SendResult] = {}

    def status(self, timeout: float = 45) -> dict:
        base = {"adapter": self.name, "label": self.label, "available": False, "can_send": False, "accounts": []}
        if self.problem:
            return {**base, "message": self.problem}

        def probe():
            app = _app()
            ns = app.GetNamespace("MAPI")
            accounts = [{"name": a.DisplayName, "smtp": getattr(a, "SmtpAddress", "") or ""} for a in _accounts(ns)]
            return {"version": str(app.Version), "profile": str(ns.CurrentProfileName), "accounts": accounts}

        try:
            info = worker().call(probe, timeout)
        except ComTimeout:
            return {**base, "message": "Outlook did not respond. It may be showing a dialog (profile selection, "
                                       "sign-in or security prompt). Check Outlook and try again."}
        except Exception as exc:  # noqa: BLE001
            return {**base, "message": describe_com_error(exc)[1]}
        msg = f"Classic Outlook {info['version']} is available (profile '{info['profile']}')."
        if new_outlook_toggle_on():
            msg += (" Note: the 'new Outlook' toggle is switched on for this user; automation uses classic Outlook "
                    "in the background.")
        if not info["accounts"]:
            return {**base, "message": "Outlook is installed but has no email accounts in the current profile."}
        return {**base, "available": True, "can_send": True, "accounts": info["accounts"], "message": msg,
                "version": info["version"], "profile": info["profile"]}

    def create(self, msg: MailMessage, send: bool, timeout: float) -> SendResult:
        if self.problem:
            return SendResult("failed", error=self.problem)
        for p in msg.attachments:
            if not Path(p).exists():
                return SendResult("failed", error=f"Attachment missing: {p}")
        state = {"send_called": False, "cancel": False}
        lock = threading.Lock()

        def op():
            app = _app()
            ns = app.GetNamespace("MAPI")
            mail = app.CreateItem(OL_MAIL_ITEM)
            mail.To = "; ".join(msg.to)
            mail.CC = "; ".join(msg.cc)
            mail.Subject = msg.subject
            mail.Body = msg.body
            if msg.sender:
                acct = _find_account(ns, msg.sender)
                if acct is None:
                    raise LookupError(f"The sender account '{msg.sender}' is not in the current Outlook profile.")
                try:
                    mail.SendUsingAccount = acct
                except Exception:  # noqa: BLE001 - late-bound property put workaround
                    mail._oleobj_.Invoke(SEND_USING_ACCOUNT_DISPID, 0, 8, 0, acct)
            for p in msg.attachments:
                mail.Attachments.Add(str(Path(p).resolve()))
            prop = mail.UserProperties.Add(PROP_NAME, OL_TEXT, False)
            prop.Value = msg.dispatch_ref
            mail.Save()
            entry_id = str(mail.EntryID)
            if send:
                with lock:
                    if state["cancel"]:
                        return {"entry_id": entry_id, "cancelled": True}
                    state["send_called"] = True
                mail.Send()
            return {"entry_id": entry_id, "cancelled": False}

        box, done = worker().submit(op)
        if not done.wait(timeout):
            with lock:
                state["cancel"] = True
                send_called = state["send_called"]
            if not send:
                return SendResult("uncertain", error="Outlook did not finish creating the draft in time. Check the "
                                                     "Drafts folder before trying again.")
            if not send_called:
                # Send() will never be called now; a draft may exist.
                return SendResult("failed", error="Outlook did not respond before sending (a dialog may be open). "
                                                  "The message was not sent; a draft may remain in Drafts.")
            self._watch_late(msg.dispatch_ref, box, done)
            return SendResult("uncertain", error="Outlook did not confirm the send in time (a security prompt may be "
                                                 "waiting). Check Outlook's Outbox and Sent Items; the application "
                                                 "will not resend automatically.")
        if "error" in box:
            exc = box["error"]
            if isinstance(exc, LookupError):
                return SendResult("failed", error=str(exc))
            hresult, text = describe_com_error(exc)
            if not state["send_called"]:
                return SendResult("failed", error=text)
            if hresult in DEFINITELY_NOT_SENT:
                return SendResult("failed", error=text)
            return SendResult("uncertain", error=text)
        result = box["result"]
        if result.get("cancelled"):
            return SendResult("failed", error="Sending was cancelled.")
        return SendResult("submitted" if send else "draft_created", entry_id=result["entry_id"])

    def _watch_late(self, ref: str, box: dict, done: threading.Event) -> None:
        def wait():
            done.wait()
            if "error" in box:
                self.late_results[ref] = SendResult("uncertain", error=describe_com_error(box["error"])[1])
            else:
                self.late_results[ref] = SendResult("submitted", entry_id=box["result"].get("entry_id"))
        threading.Thread(target=wait, daemon=True, name=f"late-{ref}").start()

    def reconcile(self, dispatch: dict, timeout: float = 90) -> str:
        if self.problem:
            return "unknown"
        ref = dispatch["dispatch_ref"]
        sender = dispatch.get("sender")

        def op():
            app = _app()
            ns = app.GetNamespace("MAPI")
            store = None
            if sender:
                acct = _find_account(ns, sender)
                if acct is not None:
                    store = acct.DeliveryStore

            def folder(num):
                return store.GetDefaultFolder(num) if store is not None else ns.GetDefaultFolder(num)

            def contains(fld, sort_field, limit=400):
                items = fld.Items
                try:
                    items.Sort(sort_field, True)
                except Exception:  # noqa: BLE001
                    pass
                for i in range(1, min(items.Count, limit) + 1):
                    try:
                        it = items.Item(i)
                        if it.Class != OL_MAIL_CLASS:
                            continue
                        up = it.UserProperties.Find(PROP_NAME)
                        if up is not None and str(up.Value) == ref:
                            return True
                        if ref in str(it.Subject or ""):
                            return True
                    except Exception:  # noqa: BLE001 - skip unreadable items
                        continue
                return False

            if contains(folder(OL_FOLDER_SENT), "[SentOn]"):
                return "sent"
            if contains(folder(OL_FOLDER_OUTBOX), "[CreationTime]"):
                return "outbox"
            if contains(folder(OL_FOLDER_DRAFTS), "[CreationTime]"):
                return "draft"
            return "not_found"

        try:
            return worker().call(op, timeout)
        except Exception:  # noqa: BLE001 - reconciliation is best effort
            return "unknown"

    def discard_draft(self, dispatch: dict, timeout: float = 30) -> bool:
        entry = dispatch.get("entry_id")
        if not entry or self.problem:
            return False

        def op():
            ns = _app().GetNamespace("MAPI")
            item = ns.GetItemFromID(entry)
            if bool(item.Sent):
                return False
            item.Delete()
            return True

        try:
            return bool(worker().call(op, timeout))
        except Exception:  # noqa: BLE001
            return False
