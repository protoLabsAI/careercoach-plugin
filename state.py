"""Application-pipeline storage — instance-scoped JSON, host-free.

One small ``applications.json`` in the plugin's per-instance store, beside ``profile.json``, and
through the same helpers (``profile.py``): the host's instance root (ADR 0004), copied forward
from the pre-0.7 location on first load, ``CAREERCOACH_DIR`` to override. No host imports, so the
tracker tools are unit-testable with nothing but a temp dir (set ``CAREERCOACH_DIR``).

Writes are serialized and atomic, like the profile's: the tracker has two writers that can
overlap — the agent's ``careercoach_track_application`` (tool calls run in parallel) and the
background job-watch — and a load-edit-save without a lock loses one of them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from . import profile as _store

# Application lifecycle — the status values the coach + dashboard understand.
STATUSES = ("considering", "applied", "interviewing", "offer", "rejected", "passed")

StoreUnreadable = _store.StoreUnreadable


def _dir() -> Path:
    return _store._dir()


def _path() -> Path:
    return _dir() / "applications.json"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _read_rows() -> tuple[list[dict], str]:
    """``(rows, error)`` — ``error`` is why an existing file can't be used, else ""."""
    raw, err = _store._read_json(_path())
    if not err and raw is not None and not isinstance(raw, list):
        err = "not a JSON list"
    if err:
        return [], err
    return list(raw or []), ""


def load_applications_checked(status: str | None = None) -> tuple[list[dict], str]:
    """Like ``load_applications`` but also returns why the file is unreadable ("" when fine)."""
    rows, err = _read_rows()
    if err:
        _store.log.warning("[careercoach] %s is unreadable (%s) — reading as empty, refusing writes", _path(), err)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows, err


def load_applications(status: str | None = None) -> list[dict]:
    """Every tracked application (newest first), optionally filtered by status. An unreadable file
    reads as empty (a dashboard or a turn never breaks), but ``track_application`` won't write
    over it."""
    return load_applications_checked(status)[0]


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
    Returns the stored row. An update preserves prior notes/source when new ones are blank.
    Raises ``StoreUnreadable`` rather than save one row over a pipeline it couldn't read."""
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
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


def _save(rows: list[dict]) -> None:
    """Atomic write (callers hold the ``applications`` lock)."""
    _store._write_store(_path(), json.dumps(rows, indent=2, ensure_ascii=False))
