"""Application-pipeline storage — instance-scoped JSON, host-free.

One small ``applications.json`` under the instance data dir, following the same
convention as the notes plugin (ADR 0004): ``CAREERCOACH_DIR`` overrides the base;
``PROTOAGENT_INSTANCE`` adds a per-instance subdir. No host imports, so the tracker
tools are unit-testable with nothing but a temp dir (set ``CAREERCOACH_DIR``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Application lifecycle — the status values the coach + dashboard understand.
STATUSES = ("considering", "applied", "interviewing", "offer", "rejected", "passed")


def _dir() -> Path:
    base = Path(os.environ.get("CAREERCOACH_DIR") or (Path.home() / ".protoagent" / "careercoach"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base


def _path() -> Path:
    return _dir() / "applications.json"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_applications(status: str | None = None) -> list[dict]:
    """Every tracked application (newest first), optionally filtered by status."""
    try:
        rows = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        rows = []
    if not isinstance(rows, list):
        rows = []
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows


def _save(rows: list[dict]) -> None:
    _path().write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


def track_application(
    *,
    company: str,
    role: str,
    fit_score: int = 0,
    status: str = "considering",
    notes: str = "",
    source: str = "",
) -> dict:
    """Insert or update one application, keyed on (company, role) case-insensitively.
    Returns the stored row. An update preserves prior notes/source when new ones are blank."""
    rows = load_applications()
    key = (company.strip().lower(), role.strip().lower())
    for r in rows:
        if (r.get("company", "").strip().lower(), r.get("role", "").strip().lower()) == key:
            r.update(
                company=company,
                role=role,
                fit_score=int(fit_score),
                status=status,
                notes=notes or r.get("notes", ""),
                source=source or r.get("source", ""),
                updated=_today(),
            )
            _save(rows)
            return r
    row = {
        "date": _today(),
        "company": company,
        "role": role,
        "fit_score": int(fit_score),
        "status": status,
        "notes": notes,
        "source": source,
        "updated": _today(),
    }
    rows.insert(0, row)
    _save(rows)
    return row
