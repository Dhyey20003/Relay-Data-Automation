"""Mail adapter contract.

The application distinguishes four separate facts and never merges them:
  draft_created  - a draft with the real attachments exists in Outlook Drafts
  submitted      - Outlook accepted MailItem.Send() (the message is in the Outbox)
  confirmed      - the message was found in Sent Items (matched by dispatch ID)
  delivered      - recipient delivery; this application cannot observe it and
                   never claims it
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MailMessage:
    dispatch_ref: str
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    attachments: list[Path]
    sender: str | None = None


@dataclass
class SendResult:
    status: str  # draft_created | submitted | failed | uncertain
    entry_id: str | None = None
    error: str | None = None
    detail: dict = field(default_factory=dict)


class MailAdapter:
    name = "base"
    label = "Base"
    supports_send = False
    supports_auto = False

    def status(self) -> dict:
        raise NotImplementedError

    def create(self, msg: MailMessage, send: bool, timeout: float) -> SendResult:
        raise NotImplementedError

    def reconcile(self, dispatch: dict) -> str:
        """Return 'sent', 'outbox', 'draft', 'not_found' or 'unknown'."""
        return "unknown"

    def discard_draft(self, dispatch: dict) -> bool:
        return False


class ManualAdapter(MailAdapter):
    """Used when classic Outlook COM automation is not available.

    Packages are still prepared; the attachments are placed in a ready folder
    with copyable subject and body, and the operator completes the task
    manually (recorded in the audit log). mailto: links cannot attach files,
    so none are offered.
    """
    name = "manual"
    label = "Manual (no Outlook automation)"

    def __init__(self, reason: str = ""):
        self.reason = reason

    def status(self) -> dict:
        return {"adapter": self.name, "label": self.label, "available": False, "can_send": False,
                "accounts": [], "message": self.reason or
                "Classic Outlook automation is not in use. Prepare packages, then attach the ZIP files from the "
                "ready folder to an email yourself and mark the task as manually completed."}

    def create(self, msg: MailMessage, send: bool, timeout: float) -> SendResult:
        return SendResult("failed", error="Classic Outlook automation is not available on this computer.")


def select_adapter(mode: str, paths, settings: dict | None = None) -> MailAdapter:
    from .dry_run import DryRunAdapter
    if mode == "dry_run":
        return DryRunAdapter(paths.exports / "dry_run_mail")
    if mode == "manual":
        return ManualAdapter("Email sending is set to manual in Settings.")
    if sys.platform != "win32":
        return ManualAdapter("Classic Outlook automation only works on Windows. This installation is running on "
                             f"{sys.platform}; use manual completion or the dry-run adapter for testing.")
    from .outlook_com import OutlookComAdapter, classic_outlook_problem
    problem = classic_outlook_problem()
    if problem and mode == "auto":
        return ManualAdapter(problem)
    return OutlookComAdapter(problem=problem)
