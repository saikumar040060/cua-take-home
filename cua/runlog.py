"""Structured run logging — JSONL events, redacted at the boundary.

Every run (discovery or replay) gets a directory::

    runs/<run_id>/
        events.jsonl        one JSON object per observe/decide/act/verify event
        screenshots/        numbered PNGs (checkpoint failures, interventions)
        dom/                HTML snapshots captured on failure

Nothing is written to any of these files except through ``RunLogger``, which
scrubs every event through the injected ``Redactor`` first. Evidence runs
are copied verbatim from ``runs/`` into ``/evidence/`` — same files, no
separate (and therefore divergent) logging path.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cua.safety.redact import Redactor


def new_run_id(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{ts}-{uuid.uuid4().hex[:6]}"


class RunLogger:
    def __init__(self, runs_root: str | Path, run_id: str, redactor: Redactor):
        self.run_id = run_id
        self.dir = Path(runs_root) / run_id
        self.screenshots_dir = self.dir / "screenshots"
        self.dom_dir = self.dir / "dom"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(exist_ok=True)
        self.dom_dir.mkdir(exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.redactor = redactor
        self._seq = 0

    def event(self, kind: str, **fields) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": kind,
            **fields,
        }
        record = self.redactor.scrub_obj(record)
        with self.events_path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_screenshot(self, page, label: str) -> str:
        path = self.screenshots_dir / f"{self._seq:03d}_{label}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            return ""
        return str(path)

    def save_dom(self, page, label: str) -> str:
        path = self.dom_dir / f"{self._seq:03d}_{label}.html"
        try:
            path.write_text(self.redactor.scrub(page.content()))
        except Exception:
            return ""
        return str(path)

    def save_json(self, name: str, obj) -> str:
        path = self.dir / name
        path.write_text(json.dumps(self.redactor.scrub_obj(obj), indent=2, default=str))
        return str(path)
