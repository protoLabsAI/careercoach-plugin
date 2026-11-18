"""The background watch's market sweep: every target role, across every configured source.

The watch used to search the market for the first five target roles only, one request per role
per source. With thirty-odd target roles, most were never searched, and the five that were cost a
keyed source five requests a pass. This module covers all of them:

- **Role groups.** Target roles that say the same thing ("Full Stack Engineer", "Fullstack
  Engineer", "Software Engineer Full-Stack") collapse into one query (``role_groups``). The watch
  filters results by every target role afterwards, so a shared query loses nothing.
- **Feeds** (Jobicy, We Work Remotely, Remote OK) can't be searched: the whole feed is read once a
  pass and filtered locally, so every role is covered for one (cached) download.
- **Keyless search boards** (Himalayas, Hacker News) are asked every group, every pass.
- **Keyed sources** (JSearch) have a request allowance: each pass asks the next
  ``watch_market_queries`` groups and remembers where it stopped (``watch-cursor.json``), so the
  whole list is covered over a few passes instead of the same five roles forever.

A keyed source that REFUSES (a rejected key, a spent quota, a cooldown) is done for the pass. One
that merely times out costs that one query: JSearch is slow, not broken, and one slow answer must
not cost the pass the rest.

``role_groups`` is pure; ``market_sweep`` touches the network through ``jobsource`` and ``feeds``.
"""

from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger("protoagent.plugins.careercoach")

CURSOR = "watch-cursor.json"
KEYED_QUERIES_DEFAULT = 3  # keyed requests per source per pass (setting: watch_market_queries)
KEYED_SPACING_S = 1.1  # between keyed requests: five inside one second drew 429s
KEYLESS_SPACING_S = 0.2  # a courtesy between requests to a free board
TRANSIENT_LIMIT = 2  # this many timeouts in a row and the source is left alone until next pass

#: Spellings folded together before roles are compared. Applied to the lowercased role.
_FOLD = (
    (r"\bfull[\s-]*stack\b", "fullstack"),
    (r"\bfront[\s-]*end\b", "frontend"),
    (r"\bback[\s-]*end\b", "backend"),
    (r"\bdev[\s-]*ex\b", "developer experience"),
    (r"\bdeveloper\b(?!\s+(experience|productivity))", "engineer"),  # "Frontend Developer" = "Frontend Engineer"
)
#: Words that carry no meaning once another word names the specialty ("Software Engineer Frontend").
_FILLER = {"software", "engineer", "engineering", "senior", "sr", "staff", "lead", "principal"}


def _group_key(role: str) -> tuple[str, ...]:
    text = role.lower()
    for pattern, to in _FOLD:
        text = re.sub(pattern, to, text)
    words = re.findall(r"[a-z0-9+#]+", text)
    core = sorted({w for w in words if w not in _FILLER})
    return tuple(core) if core else tuple(sorted(set(words)))  # "Software Engineer" keeps its own words


def role_groups(roles: str) -> list[str]:
    """One market query per distinct role among the comma-separated ``roles``, in first-seen order.
    Two roles are the same when they share their meaningful words after folding spellings
    (full-stack/fullstack, front-end/frontend, developer/engineer, devex/developer experience) and
    dropping filler ("software", "engineer", seniority). The query is the first spelling given:
    "Full Stack Engineer, Fullstack Engineer, Software Engineer Full-Stack" → ["Full Stack Engineer"]."""
    seen: dict[tuple[str, ...], str] = {}
    for role in (r.strip() for r in (roles or "").split(",")):
        if role:
            seen.setdefault(_group_key(role), role)
    return list(seen.values())


def keyed_queries(cfg: dict) -> int:
    try:
        return max(1, min(10, int(str((cfg or {}).get("watch_market_queries") or KEYED_QUERIES_DEFAULT).strip())))
    except (TypeError, ValueError):
        return KEYED_QUERIES_DEFAULT


def _cursor(source: str) -> int:
    from . import watchlist as wl

    try:
        return max(0, int(wl._load(CURSOR, {}).get(source) or 0))
    except (wl.StoreUnreadable, TypeError, ValueError):
        return 0


def _save_cursor(source: str, position: int) -> None:
    from . import watchlist as wl

    try:
        record = wl._load(CURSOR, {})
    except wl.StoreUnreadable:
        record = {}
    record[source] = position
    try:
        wl._save(CURSOR, record)
    except OSError as e:  # the cursor is bookkeeping: losing it repeats a query, nothing worse
        log.info("[careercoach] watch cursor not saved: %s", e)


async def _keyed(
    source: str, queries: list[str], cfg: dict, api_key: str, max_age_days: float
) -> tuple[list, bool, bool]:
    """Ask a keyed source the next few role groups. Returns ``(jobs, answered, refused)``."""
    from . import jobsource
    from . import watchlist as wl

    jobs: list[dict] = []
    answered = refused = False
    start = _cursor(source) % len(queries)
    asked = timeouts = 0
    for step in range(min(keyed_queries(cfg), len(queries))):
        if wl.jsearch_allowance() == 0:
            log.info("[careercoach] watch: today's JSearch requests are spent; back tomorrow")
            break
        if asked:
            await asyncio.sleep(KEYED_SPACING_S)
        query = queries[(start + step) % len(queries)]
        try:
            got = await jobsource.search_jobs(
                query, remote=True, limit=25, api_key=api_key, provider=source,
                country=cfg.get("search_country", "") or "", max_age_days=max_age_days, _fallback=False,
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001 — a setup error: nothing to retry this pass
            log.warning("[careercoach] watch: JSearch can't be asked (%s)", jobsource._failure(e))
            refused = True
            break
        if got.fallback_reason and not got.transient:
            # a refusal (a rejected key, a spent quota, a cooldown) answers for the whole pass:
            # don't spend more requests on it. Nothing was learned about this query, so the
            # cursor stays on it.
            log.warning(
                "[careercoach] watch: JSearch isn't answering (%s); this pass uses the other sources only.",
                got.fallback_reason,
            )
            refused = True
            break
        asked += 1
        if got.fallback_reason:  # a timeout or a dropped connection: this query's loss, not the pass's
            wl.note_jsearch_call({})  # the request was sent: count it, the vendor may have
            timeouts += 1
            log.info("[careercoach] watch: JSearch timed out on %r (%s)", query, got.fallback_reason)
            if timeouts >= TRANSIENT_LIMIT:
                log.warning("[careercoach] watch: JSearch timed out %d times in a row; back next pass", timeouts)
                break
            continue
        timeouts = 0
        answered = True
        wl.note_jsearch_call(got.quota)
        jobs += [{**j, "source": source} for j in got.jobs]
    _save_cursor(source, (start + asked) % len(queries))
    return jobs, answered, refused


async def _keyless(source: str, queries: list[str], cfg: dict, max_age_days: float) -> tuple[list, bool]:
    """Ask a keyless search board every role group. Returns ``(jobs, answered)``."""
    from . import jobsource

    jobs: list[dict] = []
    answered = False
    failures = 0
    for n, query in enumerate(queries):
        if n:
            await asyncio.sleep(KEYLESS_SPACING_S)
        try:
            got = await jobsource.search_jobs(
                query, remote=True, limit=25, provider=source,
                country=cfg.get("search_country", "") or "", max_age_days=max_age_days,
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.info("[careercoach] watch market source %s failed for %r: %s", source, query, jobsource._failure(e))
            if failures >= 3 and not answered:  # down, not flaky: stop asking it this pass
                break
            continue
        answered = True
        jobs += [{**j, "source": source} for j in got.jobs]
    return jobs, answered


async def market_sweep(
    cfg: dict, roles: str, sources: list[str], api_key: str, max_age_days: float
) -> tuple[list, bool]:
    """Every configured market source, for every target role. Returns ``(jobs, answered)``:
    the raw postings (each stamped with the ``source`` that listed it; the caller filters and
    dedupes) and whether any source answered at all. When only keyed sources are configured and
    one doesn't answer, Himalayas is swept in its place. Never raises."""
    from . import feeds, jobsource

    queries = role_groups(roles)
    jobs: list[dict] = []
    answered = False
    if not queries:
        return jobs, answered
    sources = list(sources)
    keyless_configured = any(s in jobsource.KEYLESS for s in sources)
    stand_in = False
    for source in sources:
        try:
            if source in feeds.FEEDS:
                got, ok = [{**j, "source": source} for j in await feeds.fetch(source)], True
            elif source in jobsource.JSEARCH_VENDORS:
                got, ok, refused = await _keyed(source, queries, cfg, api_key, max_age_days)
                if (refused or not ok) and not keyless_configured and not stand_in:
                    stand_in = True
                    sources.append("himalayas")  # a keyed outage must not cost the pass the market
            else:
                got, ok = await _keyless(source, queries, cfg, max_age_days)
        except Exception as e:  # noqa: BLE001 — one source's failure never costs the others
            log.info("[careercoach] watch market source %s failed: %s", source, jobsource._failure(e))
            continue
        jobs += got
        answered = answered or ok
    return jobs, answered
