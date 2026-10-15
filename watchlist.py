"""The company watchlist + the first-seen log — what to scan, and what we've already seen.

``watchlist.json``  the companies to check, each pinned to the ATS + board slug it posts on.
``seen.json``       every posting a scan has ever returned, keyed by its board id: when we first
                    and last saw it, how many scans saw it, and a content hash. It is the source
                    of truth for "new" (a posting absent from the log) and for ghost signals —
                    boards reset a posting's date on every repost, so only our own first-seen
                    date is trustworthy.

Both live in the plugin's instance store beside the tracker, through the same locked, atomic
helpers (``profile.py``). Host-free; network only through ``boards``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from . import profile as _store
from .boards import age_days
from .companies import company_key

StoreUnreadable = _store.StoreUnreadable

GHOST_AGE_DAYS = 45  # open this long with no hire → likely evergreen / ghost
SHORT_DESC = 400  # a posting this thin rarely names a real team or a real need


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _path(name: str) -> Path:
    return _store._dir() / name


def _load(name: str, empty):
    raw, err = _store._read_json(_path(name))
    if err:
        raise StoreUnreadable(_path(name), err)
    if raw is None:
        return empty
    if not isinstance(raw, type(empty)):
        raise StoreUnreadable(_path(name), f"not a JSON {type(empty).__name__}")
    return raw


def _save(name: str, data) -> None:
    _store._write_store(_path(name), json.dumps(data, indent=2, ensure_ascii=False))


# ── watchlist ─────────────────────────────────────────────────────────────────
def load_watchlist() -> list[dict]:
    return _load("watchlist.json", [])


def add_company(company: str, ats: str, slug: str, notes: str = "") -> tuple[dict, bool]:
    """Add (or re-point) a company. Returns ``(row, created)``. One row per company and one per
    board: re-adding "Scale AI, Inc." or adding "Scale" on the same ``greenhouse/scaleai`` board
    updates the existing row rather than scanning the board twice."""
    with _store._locked("watchlist"):
        rows = _load("watchlist.json", [])
        key = company_key(company)
        for r in rows:
            if company_key(r.get("company", "")) == key or (r.get("ats"), r.get("slug")) == (ats, slug):
                r.update(ats=ats, slug=slug, notes=notes or r.get("notes", ""))
                _save("watchlist.json", rows)
                return r, False
        row = {"company": company.strip(), "ats": ats, "slug": slug, "added": _now()[:10], "notes": notes}
        rows.append(row)
        rows.sort(key=lambda r: r["company"].lower())
        _save("watchlist.json", rows)
        return row, True


def remove_company(company: str) -> bool:
    with _store._locked("watchlist"):
        rows = _load("watchlist.json", [])
        key = company_key(company)
        keep = [r for r in rows if company_key(r.get("company", "")) != key]
        if len(keep) == len(rows):
            return False
        _save("watchlist.json", keep)
        return True


# ── first-seen log ────────────────────────────────────────────────────────────
SEEN = "seen.json"  # what the scan tool + digest have shown the operator
WATCH_SEEN = "watch-seen.json"  # what the background watch has handled — its own log, so a morning
# digest that shows a role first doesn't leave the watch thinking it's old news


def load_seen(log_name: str = SEEN) -> dict:
    return _load(log_name, {})


def unseen(jobs: list[dict], log_name: str) -> list[dict]:
    """The jobs absent from a log, without recording anything."""
    log = _load(log_name, {})
    return [j for j in jobs if j["id"] not in log]


def record_sightings(jobs: list[dict], now: str | None = None, log_name: str = SEEN) -> tuple[list[dict], dict]:
    """Log one scan's postings. Returns ``(new_jobs, log)`` — ``new_jobs`` are the postings never
    seen before, each annotated with ``first_seen`` and (when its text matches a posting we already
    logged under another id) ``repost_of``. Every job in ``jobs`` gets ``first_seen``/``sightings``
    stamped in place, so a caller can show freshness for the whole scan."""
    now = now or _now()
    with _store._locked(log_name):
        log = _load(log_name, {})
        by_hash: dict[str, str] = {}
        for jid, rec in log.items():
            if rec.get("hash"):  # no hash = no fingerprint; never a repost of another hashless job
                by_hash.setdefault(rec["hash"], jid)
        new: list[dict] = []
        for j in jobs:
            rec = log.get(j["id"])
            if rec is None:
                rec = {
                    "company": j.get("company", ""),
                    "title": j.get("title", ""),
                    "url": j.get("url", ""),
                    "hash": j.get("hash", ""),
                    "posted": j.get("posted", ""),
                    "first_seen": now,
                    "last_seen": now,
                    "sightings": 1,
                }
                prior = by_hash.get(j["hash"]) if j.get("hash") else None
                if prior and prior != j["id"]:
                    rec["repost_of"] = prior
                    rec["first_seen"] = log[prior].get("first_seen", now)
                log[j["id"]] = rec
                if rec["hash"]:
                    by_hash.setdefault(rec["hash"], j["id"])
                new.append(j)
            else:
                rec["last_seen"] = now
                rec["sightings"] = int(rec.get("sightings", 0)) + 1
            j["first_seen"] = rec["first_seen"]
            j["sightings"] = rec["sightings"]
            if rec.get("repost_of"):
                j["repost_of"] = rec["repost_of"]
        _save(log_name, log)
    return new, log


INBOX = "watch-inbox.json"
INBOX_CAP = 200  # oldest drop off; the digest shows the newest


def add_to_inbox(jobs: list[dict]) -> int:
    """Queue the background watch's new matches for the next digest. Returns the inbox size."""
    keep = ("id", "company", "title", "location", "url", "posted", "comp", "source", "first_seen", "ghost")
    with _store._locked("inbox"):
        box = _load(INBOX, [])
        have = {j.get("id") for j in box}
        box += [{k: j.get(k) for k in keep} for j in jobs if j.get("id") not in have]
        box = box[-INBOX_CAP:]
        _save(INBOX, box)
        return len(box)


def peek_inbox() -> list[dict]:
    """What the watch has queued, newest posting first — without removing anything."""
    return sorted(_load(INBOX, []), key=lambda j: j.get("posted") or j.get("first_seen") or "", reverse=True)


def drop_from_inbox(ids) -> None:
    """Remove the items the operator has now been shown (only those — the rest wait)."""
    ids = set(ids)
    if not ids:
        return
    with _store._locked("inbox"):
        box = _load(INBOX, [])
        _save(INBOX, [j for j in box if j.get("id") not in ids])


def take_inbox() -> list[dict]:
    """Everything queued, newest first — and empty it."""
    box = peek_inbox()
    drop_from_inbox(j.get("id") for j in box)
    return box


def ghost_flags(job: dict, now: datetime | None = None) -> list[str]:
    """Reasons to doubt a posting is a live, fillable opening — shown as a warning, never used to
    hide a role. Heuristics, not proof (no standard exists): long-open, reposted, thin text."""
    flags = []
    # the older of the board's date and our own first sighting (a repost resets the board's date,
    # never ours)
    ages = [
        a for a in (age_days(job.get("posted", ""), now), age_days(job.get("first_seen", ""), now)) if a is not None
    ]
    oldest = max(ages, default=None)
    if oldest is not None and oldest > GHOST_AGE_DAYS:
        flags.append(f"open {int(oldest)} days")
    if job.get("repost_of"):
        flags.append("reposted (same text as an earlier listing)")
    if job.get("desc_len") is not None and job["desc_len"] < SHORT_DESC:
        flags.append("very short description")
    return flags
