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
import re
from datetime import UTC, datetime
from pathlib import Path

from . import profile as _store
from . import state
from .boards import age_days
from .companies import company_key, role_key

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


# ── when the watch last ran ───────────────────────────────────────────────────
WATCH_RUN = "watch-run.json"


def record_watch_run() -> None:
    """Note that a watch pass finished with at least one source answering."""
    _save(WATCH_RUN, {"last_run": _now()})


def watch_last_run() -> str:
    """The ISO time of the last watch pass, "" if it has never run. A read never raises: an
    unreadable note reads as "not run yet", and the board still renders."""
    try:
        return str(_load(WATCH_RUN, {}).get("last_run") or "")
    except StoreUnreadable:
        return ""


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
DISMISSED = "inbox-dismissed.json"
INBOX_CAP = 200  # trim to this: handled (digested/kept) rows evict first, oldest within each group


def match_reason(job: dict, roles: str) -> str:
    """Explain title overlap, location and posting date without I/O or a clock."""
    title = set(re.findall(r"[a-z0-9]+", (job.get("title") or "").lower()))
    best = None
    for role in re.split(r"[,;\n]", roles or ""):
        words = re.findall(r"[a-z0-9]+", role.lower())
        if words:
            hits = sum(w in title for w in words)
            if best is None or hits / len(words) > best[0]:
                best = (hits / len(words), role.strip(), hits, len(words))
    bits = [f'title matches "{best[1]}" ({best[2]}/{best[3]} words)'] if best and best[2] else []
    if job.get("remote") or "remote" in (job.get("location") or "").lower():
        bits.append("remote")
    if job.get("posted"):
        bits.append(f"posted {job['posted'][:10]}")
    return " ".join(" · ".join(bits or ["No target title words matched"]).split())


def _dismissed(job: dict, dismissed: dict) -> bool:
    return job.get("id") in dismissed or bool(
        job.get("hash") and job["hash"] in {h for hashes in dismissed.values() for h in hashes}
    )


def _url_key(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _pipeline_keys() -> set[tuple[str, str]]:
    """What the pipeline already holds: (company, role) and (company, posting URL) pairs. An unreadable
    pipeline reads as empty, so the inbox shows a card rather than hide one it can't check."""
    keys = set()
    for row in state.load_applications():
        ck = company_key(row.get("company", ""))
        keys.add((ck, role_key(row.get("role", ""))))
        if _url_key(row.get("source")):
            keys.add((ck, _url_key(row.get("source"))))
    return keys


def _tracked(job: dict, keys: set[tuple[str, str]]) -> bool:
    """A card is settled once its role is in the pipeline, whatever status that row has. The URL
    catches a role tracked under a different title; it never crosses companies."""
    ck = company_key(job.get("company", ""))
    url = _url_key(job.get("url"))
    return (ck, role_key(job.get("title", ""))) in keys or bool(url and (ck, url) in keys)


def _newest(jobs) -> list[dict]:
    return sorted(jobs, key=lambda j: j.get("posted") or j.get("first_seen") or "", reverse=True)


def add_to_inbox(jobs: list[dict]) -> int:
    """Queue matches once, retaining their evidence and prior operator decisions."""
    keep = (
        "id",
        "company",
        "title",
        "location",
        "url",
        "posted",
        "comp",
        "source",
        "first_seen",
        "ghost",
        "reason",
        "snippet",
        "hash",
    )
    with _store._locked("inbox"):
        box = _load(INBOX, [])
        dismissed = _load(DISMISSED, {})
        have = {j.get("id") for j in box}
        for j in jobs:
            if j.get("id") not in have and not _dismissed(j, dismissed):
                box.append({**{k: j.get(k) for k in keep}, "score": int(j.get("score") or 0)})
                have.add(j.get("id"))
        # Evict handled rows first, oldest within each group; preserve insertion order.
        excess = max(0, len(box) - INBOX_CAP)
        priority = sorted(range(len(box)), key=lambda i: not (box[i].get("digested") or box[i].get("kept")))
        evict = set(priority[:excess])
        box = [j for i, j in enumerate(box) if i not in evict]
        _save(INBOX, box)
        return len(box)


def inbox_for_view() -> list[dict]:
    """Actionable items, including those already digested, newest posting first. Read before the
    inbox lock: the pipeline has its own lock, and tracking a role never needs this one."""
    tracked = _pipeline_keys()
    with _store._locked("inbox"):
        dismissed = _load(DISMISSED, {})
        return _newest(
            j
            for j in _load(INBOX, [])
            if not j.get("kept") and not _dismissed(j, dismissed) and not _tracked(j, tracked)
        )


def inbox_item(id: str) -> dict | None:
    """One undecided card by id, tracked or not, so Keep from a view rendered before the role was
    tracked still lands on the existing row instead of failing."""
    with _store._locked("inbox"):
        dismissed = _load(DISMISSED, {})
        return next(
            (j for j in _load(INBOX, []) if j.get("id") == id and not j.get("kept") and not _dismissed(j, dismissed)),
            None,
        )


def peek_inbox() -> list[dict]:
    """Pending digest items, newest first, without consuming them."""
    return [j for j in inbox_for_view() if not j.get("digested")]


def drop_from_inbox(ids) -> None:
    """Mark just the items shown by a successful digest, retaining them for the view."""
    ids = set(ids)
    if not ids:
        return
    with _store._locked("inbox"):
        box = _load(INBOX, [])
        for j in box:
            if j.get("id") in ids and not j.get("digested"):
                j["digested"] = _now()
        _save(INBOX, box)


def take_inbox() -> list[dict]:
    """Consume all pending digest items, retaining their rows."""
    tracked = _pipeline_keys()
    with _store._locked("inbox"):
        rows = _load(INBOX, [])
        dismissed = _load(DISMISSED, {})
        box = _newest(
            j
            for j in rows
            if not j.get("digested") and not j.get("kept") and not _dismissed(j, dismissed) and not _tracked(j, tracked)
        )
        result = [dict(j) for j in box]
        for j in box:
            j["digested"] = _now()
        _save(INBOX, rows)
        return result


def dismiss(ids) -> None:
    """Remember ids and known content fingerprints, even after inbox eviction."""
    ids = set(ids)
    with _store._locked("inbox"):
        dismissed = _load(DISMISSED, {})
        for jid in ids:
            dismissed.setdefault(jid, [])
        for j in _load(INBOX, []):
            if j.get("id") in ids and j.get("hash") and j["hash"] not in dismissed[j["id"]]:
                dismissed[j["id"]].append(j["hash"])
        _save(DISMISSED, dismissed)


def mark_kept(id: str) -> None:
    """Hide an item the operator chose to keep from the actionable inbox."""
    with _store._locked("inbox"):
        box = _load(INBOX, [])
        for j in box:
            if j.get("id") == id:
                j["kept"] = j.get("kept") or _now()
        _save(INBOX, box)


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
