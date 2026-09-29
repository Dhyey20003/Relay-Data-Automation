"""Development adapter: records messages to disk and never sends email."""
from __future__ import annotations

import json
from pathlib import Path

from .. import timeutil
from .base import MailAdapter, MailMessage, SendResult


class DryRunAdapter(MailAdapter):
    name = "dry_run"
    label = "Dry run (no email is sent)"
    supports_send = True
    supports_auto = True

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict:
        return {"adapter": self.name, "label": self.label, "available": True, "can_send": True,
                "accounts": [{"name": "Dry-run sender", "smtp": "dry-run@example.invalid"}],
                "message": f"DRY RUN: messages are written to {self.folder} and nothing is emailed."}

    def create(self, msg: MailMessage, send: bool, timeout: float) -> SendResult:
        target = self.folder / msg.dispatch_ref
        target.mkdir(parents=True, exist_ok=True)
        record = {"dispatch_ref": msg.dispatch_ref, "to": msg.to, "cc": msg.cc, "subject": msg.subject,
                  "body": msg.body, "sender": msg.sender, "created_at": timeutil.iso_utc(),
                  "folder": "Sent Items" if send else "Drafts",
                  "attachments": [{"name": p.name, "bytes": p.stat().st_size} for p in msg.attachments]}
        (target / "message.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return SendResult("submitted" if send else "draft_created", entry_id=f"dryrun:{msg.dispatch_ref}")

    def reconcile(self, dispatch: dict) -> str:
        path = self.folder / dispatch["dispatch_ref"] / "message.json"
        if not path.exists():
            return "not_found"
        folder = json.loads(path.read_text(encoding="utf-8")).get("folder")
        return "sent" if folder == "Sent Items" else "draft"

    def discard_draft(self, dispatch: dict) -> bool:
        path = self.folder / dispatch["dispatch_ref"] / "message.json"
        if path.exists():
            path.unlink()
            return True
        return False
