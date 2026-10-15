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
    """Add (or re-point) a company. Returns ``(row, created)``. One row per company key: adding
    "Scale AI" when "Scale" is listed updates that row rather than scanning the board twice."""
    with _store._locked("watchlist"):
        rows = _load("watchlist.json", [])
        key = company_key(company)
        for r in rows:
            if company_key(r.get("company", "")) == key:
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
def load_seen() -> dict:
    return _load("seen.json", {})


def record_sightings(jobs: list[dict], now: str | None = None) -> tuple[list[dict], dict]:
    """Log one scan's postings. Returns ``(new_jobs, log)`` — ``new_jobs`` are the postings never
    seen before, each annotated with ``first_seen`` and (when its text matches a posting we already
    logged under another id) ``repost_of``. Every job in ``jobs`` gets ``first_seen``/``sightings``
    stamped in place, so a caller can show freshness for the whole scan."""
    now = now or _now()
    with _store._locked("seen"):
        log = _load("seen.json", {})
        by_hash: dict[str, str] = {}
        for jid, rec in log.items():
            by_hash.setdefault(rec.get("hash", ""), jid)
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
                prior = by_hash.get(j.get("hash", ""))
                if prior and prior != j["id"]:
                    rec["repost_of"] = prior
                    rec["first_seen"] = log[prior].get("first_seen", now)
                log[j["id"]] = rec
                new.append(j)
            else:
                rec["last_seen"] = now
                rec["sightings"] = int(rec.get("sightings", 0)) + 1
            j["first_seen"] = rec["first_seen"]
            j["sightings"] = rec["sightings"]
            if rec.get("repost_of"):
                j["repost_of"] = rec["repost_of"]
        _save("seen.json", log)
    return new, log


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
