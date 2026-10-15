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
from .companies import company_key, role_key

# Application lifecycle — the status values the coach + dashboard understand.
STATUSES = ("considering", "applied", "interviewing", "offer", "rejected", "passed")
ACTIVE = ("applied", "interviewing", "offer")
#: Applying to more open roles than this at one company reads as spray-and-pray to its recruiters
#: (they see every application in one ATS) and trips Ashby / Greenhouse application-limit rules.
COMPANY_CAP = 3

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
    """Insert or update one application. The same posting URL, or the same company + role after
    normalizing ("Arize AI" = "Arize", case and punctuation ignored), is the same application.
    Returns the stored row. An update preserves prior notes/source when new ones are blank.
    Raises ``StoreUnreadable`` rather than save one row over a pipeline it couldn't read."""
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
        r = _find(rows, company, role, source)
        if r is not None:
            r.update(
                company=company,
                role=role,
                # a status-only update ("mark it applied") passes the default 0 — keep the score
                fit_score=int(fit_score) or int(r.get("fit_score") or 0),
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


def _find(rows: list[dict], company: str, role: str, source: str = "") -> dict | None:
    url = (source or "").strip().rstrip("/")
    if url:
        for r in rows:
            if (r.get("source") or "").strip().rstrip("/") == url:
                return r
    key = (company_key(company), role_key(role))
    for r in rows:
        if (company_key(r.get("company", "")), role_key(r.get("role", ""))) == key:
            return r
    return None


def active_at_company(company: str, rows: list[dict] | None = None) -> list[dict]:
    """The tracked roles at ``company`` that are still in play (applied / interviewing / offer)."""
    key = company_key(company)
    return [r for r in (load_applications() if rows is None else rows)
            if company_key(r.get("company", "")) == key and r.get("status") in ACTIVE]  # fmt: skip


def import_history(entries: list[dict]) -> dict:
    """Merge past applications the tracker never saw (a notes file, a spreadsheet) in one locked
    write. Each entry needs ``company`` + ``role``; ``status`` defaults to applied, ``date`` to
    today. An entry that matches a tracked row only fills its blanks — it never overwrites a
    status, so a role tracked as rejected stays rejected. Returns counts + what was skipped."""
    added = updated = 0
    skipped: list[str] = []
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
        for e in entries:
            company = str(e.get("company") or "").strip()
            role = str(e.get("role") or "").strip()
            if not company or not role:
                skipped.append(f"{e!r:.80} (needs company and role)")
                continue
            status = str(e.get("status") or "applied").strip().lower()
            if status not in STATUSES:
                skipped.append(f"{company} / {role} (unknown status {status!r})")
                continue
            source = str(e.get("source") or "").strip()
            cur = _find(rows, company, role, source)
            if cur is not None:
                changed = False
                for k, v in (("source", source), ("notes", str(e.get("notes") or "").strip())):
                    if v and not cur.get(k):
                        cur[k] = v
                        changed = True
                updated += changed
                continue
            when = str(e.get("date") or "").strip() or _today()
            rows.append(
                {
                    "date": when,
                    "company": company,
                    "role": role,
                    "fit_score": int(e.get("fit_score") or 0),
                    "status": status,
                    "notes": str(e.get("notes") or "imported from history").strip(),
                    "source": source,
                    "updated": when,
                }
            )
            added += 1
        rows.sort(key=lambda r: r.get("date") or "", reverse=True)
        _save(rows)
    return {"added": added, "updated": updated, "skipped": skipped, "total": len(rows)}


def _save(rows: list[dict]) -> None:
    """Atomic write (callers hold the ``applications`` lock)."""
    _store._write_store(_path(), json.dumps(rows, indent=2, ensure_ascii=False))
