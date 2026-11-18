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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..record import profile as _store
from ..record import state
from .boards import age_days
from ..record.companies import company_key, role_key

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


# ── the JSearch request budget ───────────────────────────────────────────────
# RapidAPI's free JSearch plan is 200 requests a month. The watch asks for five roles every pass,
# about 40 a day, so unbudgeted it spends the month in five days and every later call is a 429.
# Each JSearch answer says how many requests are left and when they refill; the watch spreads
# what's left over the days until then. A bigger plan gets a bigger daily share by the same sum.
JSEARCH_QUOTA = "jsearch-quota.json"


def _quota_now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def note_jsearch_call(quota: dict, now: datetime | None = None) -> None:
    """Count one JSearch request against today, and keep the allowance its answer reported
    (``jobsource.parse_jsearch_quota``; empty when the vendor sent none). Never raises: the
    count is bookkeeping, and losing it must not cost a search its results."""
    now = _quota_now(now)
    today = now.date().isoformat()
    try:
        rec = _load(JSEARCH_QUOTA, {})
    except StoreUnreadable:
        rec = {}
    used = int(rec.get("used_today") or 0) if rec.get("day") == today else 0
    out = {"day": today, "used_today": used + 1}
    if quota:
        out["limit"] = int(quota.get("limit") or 0)
        out["remaining"] = int(quota.get("remaining") or 0)
        out["reset_at"] = (now + timedelta(seconds=int(quota.get("reset_s") or 0))).isoformat(timespec="seconds")
    elif "remaining" in rec:
        # no allowance in this answer (a timeout, or a vendor that reports none): keep what the
        # last answer said, one request poorer. Forgetting it would lift the cap.
        out["limit"] = int(rec.get("limit") or 0)
        out["remaining"] = max(0, int(rec.get("remaining") or 0) - 1)
        out["reset_at"] = rec.get("reset_at") or ""
    try:
        _save(JSEARCH_QUOTA, out)
    except OSError:
        pass


def jsearch_allowance(now: datetime | None = None) -> int | None:
    """How many more JSearch requests the watch may make today: what's left in the plan, spread
    evenly over the days until it refills, less today's use. ``None`` when nothing is known yet
    (no call has reported an allowance, or the plan has refilled since): then there is no cap."""
    now = _quota_now(now)
    try:
        rec = _load(JSEARCH_QUOTA, {})
    except StoreUnreadable:
        return None
    if "remaining" not in rec:
        return None
    try:
        reset_at = datetime.fromisoformat(str(rec.get("reset_at") or ""))
    except ValueError:
        return None
    if reset_at <= now:
        return None  # refilled; the next answer reports the new allowance
    remaining = max(0, int(rec.get("remaining") or 0))
    used = int(rec.get("used_today") or 0) if rec.get("day") == now.date().isoformat() else 0
    days_left = max(1, -(-(reset_at - now).total_seconds() // 86400))
    per_day = max(1, int((remaining + used) // days_left)) if remaining else 0
    return max(0, min(remaining, per_day - used))


# ── the month's requests, per keyed source ───────────────────────────────────
# A keyed source's free plan is a hard monthly limit (OpenWeb Ninja JSearch: 200 requests), and
# OpenWeb Ninja reports no allowance headers, so the daily spread above never applies to it. This
# is a local count of every request sent to each keyed source, whoever sent it (watch, chat
# search). The background watch stops at 80% of the limit (``month_usage()["reserved"]``), so the
# last fifth is left for the operator's own searches; at 100% every caller skips the source.
# The count restarts with the calendar month (UTC), or at the vendor's own reset when its headers
# name one. A vendor-reported limit wins over the setting. It sits beside the daily allowance
# above and never replaces it.
SOURCE_MONTHS = "job-source-months.json"
MONTHLY_LIMIT_DEFAULT = 200  # setting: jobs_api_monthly_limit (0 = no limit)
WATCH_SHARE = 0.8  # the watch's share of a month's requests; the rest is reserved


def monthly_limit(cfg: dict | None) -> int:
    """The ``jobs_api_monthly_limit`` setting: requests a month per keyed source, 0 = no limit.
    A missing or unreadable value is the default."""
    raw = (cfg or {}).get("jobs_api_monthly_limit")
    if raw is None or str(raw).strip() == "":
        return MONTHLY_LIMIT_DEFAULT
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return MONTHLY_LIMIT_DEFAULT


def _next_month(now: datetime) -> datetime:
    return datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)


def _month_state(rec, now: datetime) -> dict:
    """One source's stored record as of ``now``: ``{"used", "limit", "reset_at", "noted"}``.
    ``limit``/``reset_at`` are the vendor's, 0/"" when it reported none. A record from an earlier
    period (the vendor's reset has passed, or with no vendor reset the calendar month changed)
    reads as an empty one; so does anything that isn't a record."""
    fresh = {"used": 0, "limit": 0, "reset_at": "", "noted": False}
    if not isinstance(rec, dict):
        return fresh
    try:
        used = max(0, int(rec.get("used") or 0))
        limit = max(0, int(rec.get("limit") or 0))
    except (TypeError, ValueError):
        return fresh
    reset_at = ""
    try:
        reset = datetime.fromisoformat(str(rec.get("reset_at") or ""))
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=UTC)
        if reset <= now:
            return {**fresh, "limit": limit}  # the plan refilled; its size is still the best guess
        reset_at = reset.isoformat(timespec="seconds")
    except ValueError:
        if rec.get("month") != now.strftime("%Y-%m"):
            return {**fresh, "limit": limit}
    return {"used": used, "limit": limit, "reset_at": reset_at, "noted": bool(rec.get("watch_noted"))}


def _update_month(source: str, now: datetime, change) -> None:
    """Read-modify-write one source's month record. Never raises: the count is bookkeeping. An
    unreadable file is started over, since leaving it would lift the cap for good."""
    try:
        with _store._locked("job-source-months"):
            try:
                records = _load(SOURCE_MONTHS, {})
            except StoreUnreadable:
                records = {}
            state_now = _month_state(records.get(source), now)
            change(state_now)
            out = {"month": now.strftime("%Y-%m"), "used": state_now["used"]}
            if state_now["limit"]:
                out["limit"] = state_now["limit"]
            if state_now["reset_at"]:
                out["reset_at"] = state_now["reset_at"]
            if state_now["noted"]:
                out["watch_noted"] = True
            records[source] = out
            _save(SOURCE_MONTHS, records)
    except (StoreUnreadable, OSError, ValueError, TypeError):
        pass


def note_month_call(source: str, quota: dict | None = None, now: datetime | None = None) -> None:
    """Count one request sent to a keyed source this month. When the answer carried the vendor's
    allowance (``jobsource.parse_jsearch_quota``), the vendor's numbers replace the local count:
    it also sees requests made with the same key elsewhere."""
    now = _quota_now(now)

    def change(rec: dict) -> None:
        rec["used"] += 1
        try:
            limit = int((quota or {}).get("limit") or 0)
            remaining = int((quota or {}).get("remaining") or 0)
            reset_s = int((quota or {}).get("reset_s") or 0)
        except (TypeError, ValueError):
            return
        if limit > 0:
            rec["limit"] = limit
            rec["used"] = max(0, limit - max(0, remaining))
            if reset_s > 0:
                rec["reset_at"] = (now + timedelta(seconds=reset_s)).isoformat(timespec="seconds")

    _update_month(source, now, change)


def month_usage(source: str, limit: int = MONTHLY_LIMIT_DEFAULT, now: datetime | None = None) -> dict:
    """Where a keyed source stands this month: ``{"used", "limit", "left", "refill", "reserved",
    "spent"}``. ``limit`` is the setting; a vendor-reported limit wins over it, and a setting of 0
    means no cap at all (``limit`` 0, ``left`` None, never reserved or spent). ``refill`` is the
    ISO date (UTC) the count restarts. ``reserved``: the background watch's share is used.
    ``spent``: no caller may send another request. Never raises: an unreadable record reads as
    nothing used, so it never costs a search."""
    now = _quota_now(now)
    try:
        rec = _month_state(_load(SOURCE_MONTHS, {}).get(source), now)
    except (StoreUnreadable, OSError, ValueError, TypeError):
        rec = _month_state(None, now)
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = MONTHLY_LIMIT_DEFAULT
    if limit:
        limit = rec["limit"] or limit
    used = rec["used"]
    return {
        "used": used,
        "limit": limit,
        "left": max(0, limit - used) if limit else None,
        "refill": (rec["reset_at"] or _next_month(now).isoformat())[:10],
        "reserved": bool(limit) and used >= limit * WATCH_SHARE,
        "spent": bool(limit) and used >= limit,
    }


def note_watch_reserved(source: str, now: datetime | None = None) -> bool:
    """Record that the watch has said it is leaving this source alone for the rest of the period.
    True the first time in a period (the caller logs then), False afterwards."""
    first = []

    def change(rec: dict) -> None:
        if not rec["noted"]:
            first.append(True)
            rec["noted"] = True

    _update_month(source, _quota_now(now), change)
    return bool(first)


# Per-vendor backoff is separate from RapidAPI's request allowance.
SOURCE_COOLDOWNS = "job-source-cooldowns.json"


def source_cooldown(source: str, now: datetime | None = None) -> str:
    """Active expiry, or empty; missing, stale and unreadable notes never block a search."""
    try:
        until = _load(SOURCE_COOLDOWNS, {}).get(source)
        expiry = datetime.fromisoformat(until)
        return until if expiry > _quota_now(now) else ""
    except (StoreUnreadable, OSError, ValueError, TypeError):
        return ""


def note_source_cooldown(source: str, retry_after: str = "", now: datetime | None = None) -> None:
    """Remember a 429, honoring delta seconds or an HTTP date, capped at one day."""
    from email.utils import parsedate_to_datetime

    now = _quota_now(now)
    try:
        seconds = int(retry_after)
    except (ValueError, TypeError):
        try:
            seconds = (parsedate_to_datetime(retry_after) - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            seconds = 3600
    until = (now + timedelta(seconds=max(0, min(seconds, 86400)))).isoformat()
    _update_source_cooldown(source, until)


def clear_source_cooldown(source: str) -> None:
    """Only a successful retry clears the persisted expiry."""
    _update_source_cooldown(source, "")


def _update_source_cooldown(source: str, until: str) -> None:
    try:
        with _store._locked("job-source-cooldowns"):
            records = _load(SOURCE_COOLDOWNS, {})
            if until:
                records[source] = until
            elif source in records:
                del records[source]
            else:
                return
            _save(SOURCE_COOLDOWNS, records)
    except (StoreUnreadable, OSError, ValueError, TypeError):
        pass  # bookkeeping must never cost the operator search results


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
        "sources",
        "first_seen",
        "ghost",
        "reason",
        "snippet",
        "description",
        "apply_url",
        "ats_lookup",
        "hash",
    )
    with _store._locked("inbox"):
        box = _load(INBOX, [])
        dismissed = _load(DISMISSED, {})
        have = {j.get("id") for j in box}

        def _role(j: dict) -> tuple[str, str]:
            return (company_key(j.get("company", "")), role_key(j.get("title", "")))

        # One card per role per company ACROSS sources: the same opening arrives from the
        # company's board, Himalayas and a feed, each under its own id and link. Two postings
        # from one source stay two cards: a company does list one title twice (two locations).
        by_role = {_role(j): j for j in reversed(box) if all(_role(j))}
        for j in jobs:
            if j.get("id") in have or _dismissed(j, dismissed):
                continue
            twin = by_role.get(_role(j)) if all(_role(j)) else None
            listed = set(twin.get("sources") or [twin.get("source")]) if twin else set()
            if twin is not None and j.get("source") and listed - {None} and j["source"] not in listed:
                # note where else it is listed; the card the operator already has stays as it is
                seen = [*(twin.get("sources") or [twin.get("source")]), *(j.get("sources") or [j.get("source")])]
                twin["sources"] = list(dict.fromkeys(s for s in seen if s))
                continue
            card = {**{k: j.get(k) for k in keep}, "score": int(j.get("score") or 0)}
            box.append(card)
            have.add(j.get("id"))
            if all(_role(j)):
                by_role[_role(j)] = card
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
