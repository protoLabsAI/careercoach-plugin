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

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from . import profile as _store
from .companies import company_key, role_key

# Application lifecycle — the status values the coach + dashboard understand. `preparing` sits
# between considering and applied (the role is being readied — packet/resume in flight). Statuses
# are ADDITIVE: the digest and the goal verifier read `considering`/`applied` by name, so an added
# value never changes their meaning.
STATUSES = ("considering", "preparing", "applied", "interviewing", "offer", "rejected", "passed")
ACTIVE = ("applied", "interviewing", "offer")

#: The dashboard's pipeline board — a column label mapped to the status(es) that fall under it.
#: A mapping (not a rename) so existing rows keep their status and the digest/verifier keep theirs.
COLUMNS = {
    "Saved": ["considering"],
    "Preparing": ["preparing"],
    "Applied": ["applied"],
    "Interviewing": ["interviewing"],
    "Offer": ["offer"],
    "Closed": ["rejected", "passed"],
}
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


def _row_id(row: dict) -> str:
    """A stable, deterministic id for a row that predates ids: a short sha1 of its
    company/role/date, so the SAME row backfills to the SAME id on every load with nothing
    written. Derived from the normalized keys so cosmetic company/role re-typing keeps the id."""
    basis = "\x00".join(
        (company_key(row.get("company", "")), role_key(row.get("role", "")), str(row.get("date") or ""))
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _backfill(rows: list[dict]) -> list[dict]:
    """Give legacy rows the fields newer rows carry, in place, deterministically (so a read alone
    never needs a write): a stable ``id``, and a one-entry ``history`` derived from the row's
    status + its ``updated`` (else ``date``)."""
    for r in rows:
        if not r.get("id"):
            r["id"] = _row_id(r)
        if not r.get("history"):
            r["history"] = [{"status": r.get("status", ""), "date": r.get("updated") or r.get("date") or ""}]
    return rows


def _read_rows() -> tuple[list[dict], str]:
    """``(rows, error)`` — ``error`` is why an existing file can't be used, else ""."""
    raw, err = _store._read_json(_path())
    if not err and raw is not None and not isinstance(raw, list):
        err = "not a JSON list"
    if err:
        return [], err
    return _backfill(list(raw or [])), ""


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
    posting: dict | None = None,
    insert_only: bool = False,
) -> dict:
    """Insert or update one application. The same posting URL, or the same company + role after
    normalizing ("Arize AI" = "Arize", case and punctuation ignored), is the same application.
    Returns the stored row. An update preserves prior notes/source when new ones are blank, and
    appends a ``{status, date}`` history entry whenever the status actually changes. An optional
    ``posting`` ({title, location, snippet, url, posted}) is stored verbatim on the row.
    ``insert_only`` returns an existing match unchanged, for inbox promotions that must not
    overwrite operator updates. Matching and insertion share the applications lock.
    Raises ``StoreUnreadable`` rather than save one row over a pipeline it couldn't read."""
    today = _today()
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
        r = _find(rows, company, role, source)
        if r is not None:
            if insert_only:
                return r
            if status != r.get("status"):
                r.setdefault("history", []).append({"status": status, "date": today})
            r.update(
                # a status-only update ("mark it applied") passes the default 0 — keep the score
                fit_score=int(fit_score) or int(r.get("fit_score") or 0),
                status=status,
                notes=notes or r.get("notes", ""),
                source=source or r.get("source", ""),
                updated=today,
            )
            if posting:
                r["posting"] = posting
            _save(rows)
            return r
        row = {
            "date": today,
            "company": company,
            "role": role,
            "fit_score": int(fit_score),
            "status": status,
            "notes": notes,
            "source": source,
            "updated": today,
            "history": [{"status": status, "date": today}],
        }
        row["id"] = _row_id(row)
        if posting:
            row["posting"] = posting
        rows.insert(0, row)
        _save(rows)
        return row


def job_specific_url(url: str) -> bool:
    """True when a URL names ONE posting — a path segment that IS a job id (digits or a UUID), or a
    ``gh_jid``-style query id — not a careers page, a search page or a tracking parameter."""
    from urllib.parse import parse_qs, urlparse

    u = urlparse(url or "")
    for seg in u.path.split("/"):
        if re.fullmatch(r"\d{5,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", seg, re.I):
            return True
    q = parse_qs(u.query)
    return any(
        re.fullmatch(r"\d{5,}", v[0] or "") for k, v in q.items() if k.lower() in ("gh_jid", "jobid", "job_id", "jid")
    )


def _find(rows: list[dict], company: str, role: str, source: str = "") -> dict | None:
    """The tracked row this (company, role, posting URL) refers to. The same job-specific URL at the
    same company is the same application even if the title was retyped; a shared URL (a careers
    page) never merges two roles, and a URL never merges two companies."""
    ck = company_key(company)
    url = (source or "").strip().rstrip("/")
    if url and job_specific_url(url):
        for r in rows:
            if (r.get("source") or "").strip().rstrip("/") == url and company_key(r.get("company", "")) == ck:
                return r
    key = (ck, role_key(role))
    for r in rows:
        if (company_key(r.get("company", "")), role_key(r.get("role", ""))) == key:
            return r
    return None


def active_at_company(company: str, rows: list[dict] | None = None) -> list[dict]:
    """The tracked roles at ``company`` that are still in play (applied / interviewing / offer)."""
    key = company_key(company)
    return [r for r in (load_applications() if rows is None else rows)
            if company_key(r.get("company", "")) == key and r.get("status") in ACTIVE]  # fmt: skip


def move(app_id: str, status: str) -> dict:
    """Move one application (by its stable ``id``) to ``status``: set status + ``updated`` and append
    a ``{status, date}`` history entry, in one locked, atomic write. Keeps what the move replaced in
    ``last_move`` so ``undo_move`` can put it back. Raises ``StoreUnreadable`` on an unreadable file,
    ``ValueError`` on an unknown status, and ``KeyError`` on an unknown id — each before any write,
    so a bad move leaves the pipeline untouched."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; valid: {', '.join(STATUSES)}")
    today = _today()
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
        r = next((x for x in rows if x.get("id") == app_id), None)
        if r is None:
            raise KeyError(f"no application with id {app_id!r}")
        r["last_move"] = {"from": r.get("status", ""), "to": status, "updated": r.get("updated")}
        r.setdefault("history", []).append({"status": status, "date": today})
        r["status"] = status
        r["updated"] = today
        _save(rows)
        return r


def undo_move(app_id: str) -> dict:
    """Take back the last ``move()`` on one application, as if it never happened: the prior status
    (an unknown legacy status included), the prior ``updated``, and the history entry goes. A plain
    move back would not do: it rejects legacy statuses, and its fresh history date resets the
    follow-up clock (``needs_followup``). Raises ``StoreUnreadable`` on an unreadable file,
    ``KeyError`` on an unknown id, and ``ValueError`` when there is no move to undo or the row has
    changed status since — each before any write."""
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
        r = next((x for x in rows if x.get("id") == app_id), None)
        if r is None:
            raise KeyError(f"no application with id {app_id!r}")
        last = r.get("last_move") or {}
        if not last or r.get("status") != last.get("to"):
            raise ValueError(f"no move to undo on {app_id!r}")
        history = r.get("history") or []
        if history and history[-1].get("status") == last["to"]:
            history.pop()
        r["status"] = last.get("from", "")
        if last.get("updated") is None:
            r.pop("updated", None)
        else:
            r["updated"] = last["updated"]
        del r["last_move"]
        _save(rows)
        return r


# ── the apply kit: what the coach prepared for one application, on the row (S2a) ─
# The settled default apply path is an APPLY KIT: the coach readies the tailored résumé, a cover
# letter when the posting asks for one, and every form answer; the operator fills the form. The kit
# lives on the application row in applications.json (same lock, same atomic write) so the record of
# what was prepared — and, on mark_applied, what was sent — stays with the application. Additive:
# a row without a kit reads fine, and the digest/verifier never look at it.
def _empty_kit() -> dict:
    """A fresh kit skeleton, so a row's kit always has a predictable shape for the detail view.

    ``cover_letter.needed`` is ``None`` (unset), not ``False`` — a fresh kit's cover letter is
    *required until the operator says otherwise*, so a kit saved with nothing reads as a DRAFT that
    still needs a letter (``_kit_missing``). ``status``/``missing`` default to a draft that needs
    everything; ``set_kit`` recomputes both against the real parts on every write."""
    return {
        "resume": {"path": "", "version": "", "built": ""},
        "cover_letter": {"text": "", "needed": None},
        "answers": [],  # one-off form answers: [{key, label, value, status: draft|confirmed}]
        "no_form_questions": False,  # the operator confirmed the posting asks no form questions
        "status": "draft",  # "complete" | "draft" — recomputed by set_kit against the merged parts
        "missing": [],  # human-readable parts not yet ready (empty ⇒ complete)
        "updated": "",
        "events": [],  # [{what, date}] — an append-only trail of what the coach prepared, when
    }


def _kit_missing(kit: dict) -> list[str]:
    """The apply-kit parts that aren't ready yet, in plain words (empty ⇒ the kit is complete).

    Required parts: a résumé whose ``path`` is non-empty AND points at a real file; a cover letter
    with non-blank ``text`` unless ``cover_letter.needed`` is explicitly ``False``; and at least one
    answer unless ``no_form_questions`` is true. Called on the MERGED kit after every ``set_kit`` so
    a partial update layered on a complete kit stays complete. The order is résumé → cover letter →
    answers, so the list reads the same way every time."""
    missing: list[str] = []
    resume = kit.get("resume") or {}
    path = str(resume.get("path") or "").strip()
    if not path:
        missing.append("résumé (no path)")
    elif not Path(path).expanduser().is_file():
        missing.append(f"résumé (file not found: {path})")
    cover = kit.get("cover_letter") or {}
    if cover.get("needed") is not False and not str(cover.get("text") or "").strip():
        missing.append("cover letter")
    if not kit.get("no_form_questions") and not (kit.get("answers") or []):
        missing.append("answers")
    return missing


def _answer_snapshot(entries) -> list[dict]:
    """Render kit answers (``[{key, label, value, status}]``) down to the ``{key, value, status}``
    snapshot ``applied_with`` keeps — the status (draft/confirmed) is carried through verbatim so a
    draft one-off answer is never recorded as confirmed."""
    out: list[dict] = []
    for a in entries or []:
        if isinstance(a, dict):
            out.append(
                {
                    "key": str(a.get("key", "") or ""),
                    "value": str(a.get("value", "") or ""),
                    "status": str(a.get("status", "") or "draft"),
                }
            )
    return out


def set_kit(app_id: str, *, event: str = "", **parts) -> dict:
    """Merge ``parts`` into one application's ``kit`` (by its stable ``id``) and append a kit event,
    in one locked, atomic write. A dict part (``resume``, ``cover_letter``) merges into the stored
    sub-dict so a partial update keeps the fields it didn't touch; any other part (``answers``) is
    set outright. ``event`` names the entry in ``kit['events']`` — defaulted to the parts changed.
    After the merge the kit is re-validated (``_kit_missing``) and ``status``/``missing`` are written
    alongside it, so a partial update on top of a complete kit stays complete and an empty save reads
    as a draft. Raises ``StoreUnreadable`` on an unreadable file and ``KeyError`` on an unknown id,
    each before any write."""
    today = _today()
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
        r = next((x for x in rows if x.get("id") == app_id), None)
        if r is None:
            raise KeyError(f"no application with id {app_id!r}")
        kit = r.get("kit") or _empty_kit()
        for key, val in parts.items():
            cur = kit.get(key)
            if isinstance(cur, dict) and isinstance(val, dict):
                cur.update(val)
            else:
                kit[key] = val
        if event:
            what = event
        elif parts:
            what = "prepared " + ", ".join(k.replace("_", " ") for k in parts)
        else:
            what = "updated kit"
        kit["updated"] = today
        kit["missing"] = _kit_missing(kit)
        kit["status"] = "complete" if not kit["missing"] else "draft"
        kit.setdefault("events", []).append({"what": what, "date": today})
        r["kit"] = kit
        _save(rows)
        return r


def mark_applied(app_id: str, answers_used: list[dict] | None = None) -> dict:
    """Move one application (by its stable ``id``) to ``applied`` — the same history-appending move
    ``move()`` makes — and record ``applied_with`` {date, resume_version, answers} as a snapshot of
    what was sent. The answers snapshot is ``answers_used`` when given, else the kit's one-off
    answers, else the saved standard answers (``answers.load()``); each entry keeps its draft/
    confirmed status. Raises ``StoreUnreadable`` on an unreadable file and ``KeyError`` on an unknown
    id, each before any write."""
    from . import answers as _answers

    today = _today()
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
        r = next((x for x in rows if x.get("id") == app_id), None)
        if r is None:
            raise KeyError(f"no application with id {app_id!r}")
        kit = r.get("kit") or {}
        if answers_used is not None:
            snapshot = _answer_snapshot(answers_used)
        elif kit.get("answers"):
            snapshot = _answer_snapshot(kit["answers"])
        else:
            # Fall back to the saved standard answers, rendered to the same {key, value, status}
            # shape; a load never raises (an unreadable store reads as empty here).
            snapshot = [
                {"key": k, "value": str(e.get("value", "") or ""), "status": str(e.get("status", "") or "draft")}
                for k, e in _answers.load().items()
            ]
        resume_version = str(((kit.get("resume") or {}).get("version")) or "")
        # move() logic, inlined so it shares this lock (the lock is not re-entrant) and the move +
        # applied_with land in one atomic write.
        r.setdefault("history", []).append({"status": "applied", "date": today})
        r["status"] = "applied"
        r["updated"] = today
        r["applied_with"] = {"date": today, "resume_version": resume_version, "answers": snapshot}
        r.pop("last_move", None)  # marking applied records a submission: it is not undoable
        _save(rows)
        return r


def set_notes(app_id: str, notes: str) -> dict:
    """Persist ``notes`` on one application (by its stable ``id``) and refresh ``updated``, in one
    locked, atomic write. No status change, so no history entry. Raises ``StoreUnreadable`` on an
    unreadable file and ``KeyError`` on an unknown id, each before any write."""
    today = _today()
    with _store._locked("applications"):
        rows, err = _read_rows()
        if err:
            raise StoreUnreadable(_path(), err)
        r = next((x for x in rows if x.get("id") == app_id), None)
        if r is None:
            raise KeyError(f"no application with id {app_id!r}")
        r["notes"] = notes or ""
        r["updated"] = today
        _save(rows)
        return r


def _last_history_date(row: dict) -> str:
    """The date of the row's most recent history entry, falling back to ``updated``/``date`` for a
    row with no history (a reader may pass a hand-built row)."""
    hist = row.get("history") or []
    if hist:
        return str(hist[-1].get("date") or "")
    return str(row.get("updated") or row.get("date") or "")


def needs_followup(row: dict, days: int, today: str) -> bool:
    """Whether an applied role is due a nudge: ``status == applied`` and its last history entry is at
    least ``days`` old as of ``today`` (both ``YYYY-MM-DD``). Pure — no clock, no I/O."""
    if row.get("status") != "applied":
        return False
    try:
        last = datetime.strptime(_last_history_date(row)[:10], "%Y-%m-%d").date()
        now = datetime.strptime((today or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return (now - last).days >= int(days)


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
