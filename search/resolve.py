"""Find the company's own application page for a role a job board listed.

Himalayas' "applicationLink" is a page on himalayas.app, and Cloudflare challenges every automated
fetch of it — so a Himalayas role the coach couldn't open was a role it couldn't evaluate or apply
to. Most of those companies post on Ashby, Greenhouse or Lever, whose public APIs we already read
(``boards.py``). This looks the company up there once (cached for a week, hits and misses alike),
finds the same title on its board, and hands back the real posting URL and full description.

``match_title`` is pure; ``resolve`` touches the network through ``boards``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("protoagent.plugins.careercoach")

BLOCKED_HOSTS = ("himalayas.app",)  # boards whose own job pages refuse automated fetches
CACHE = "ats-lookup.json"
CACHE_DAYS = 7
MAX_COMPANIES = 8  # per pass: probing tries every (ATS, slug guess), so keep a pass bounded


def blocked(url: str) -> bool:
    return any(h in (url or "") for h in BLOCKED_HOSTS)


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if w not in {"remote", "us", "usa"}}


def match_title(title: str, board_jobs: list[dict]) -> dict | None:
    """The board posting that is this role: an exact title first, else the closest title whose
    words overlap at least 80% (Jaccard) — "Sr Software Engineer I/II, Operational AI" vs the
    board's "Sr. Software Engineer I/II - Operational AI"."""
    want = _words(title)
    if not want:
        return None
    best, best_j = 0.0, None
    for j in board_jobs:
        have = _words(j.get("title", ""))
        if not have:
            continue
        if have == want:
            return j
        jac = len(want & have) / len(want | have)
        if jac > best:
            best, best_j = jac, j
    return best_j if best >= 0.8 else None


def _fresh(entry: dict, now: datetime) -> bool:
    try:
        at = datetime.fromisoformat(entry.get("checked", ""))
    except ValueError:
        return False
    return now - at < timedelta(days=CACHE_DAYS)


async def resolve(jobs: list[dict], *, now: datetime | None = None) -> int:
    """Fill ``apply_url`` (and a fuller ``description``) in place for jobs whose ``url`` is on a
    blocked board, from the company's own ATS. Returns how many it resolved. Never raises: a
    lookup that fails leaves the job as it was."""
    import httpx

    from ..record.companies import company_key
    from . import boards
    from . import watchlist as wl

    now = now or datetime.now(timezone.utc)
    todo = [j for j in jobs if blocked(j.get("url", "")) and not j.get("apply_url") and j.get("company")]
    if not todo:
        return 0
    try:
        cache = wl._load(CACHE, {})
    except Exception:  # noqa: BLE001 — an unreadable cache just means looking again
        cache = {}
    by_co: dict[str, list[dict]] = {}
    for j in todo:
        by_co.setdefault(company_key(j["company"]), []).append(j)

    boards_for: dict[str, list[dict]] = {}
    probed = 0
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        sem = asyncio.Semaphore(10)

        async def _lookup(key: str, name: str, slug: str):
            entry = cache.get(key)
            if entry and _fresh(entry, now) and not entry.get("ats"):
                return key, None  # a recent miss: don't probe again this week
            if entry and _fresh(entry, now) and entry.get("ats"):
                ats, s = entry["ats"], entry["slug"]
            else:
                found: list = []
                # the board's own company slug first (often the ATS slug too), then name guesses
                for cand in ([slug] if slug else []) + [""]:
                    try:
                        found, _ = await boards.probe(name, cand, client=client, sem=sem)
                    except Exception as e:  # noqa: BLE001
                        log.info("[careercoach] ATS probe failed for %s: %s", name, e)
                        found = []
                    if found:
                        break
                if not found:
                    cache[key] = {"company": name, "ats": "", "slug": "", "checked": now.isoformat()}
                    return key, None
                ats, s = found[0][0], found[0][1]
                cache[key] = {"company": name, "ats": ats, "slug": s, "checked": now.isoformat()}
            try:
                return key, await boards.fetch_board(ats, s, name, client=client)
            except Exception as e:  # noqa: BLE001
                log.info("[careercoach] ATS board read failed for %s (%s/%s): %s", name, ats, s, e)
                return key, None

        calls = []
        for key, group in by_co.items():
            if probed >= MAX_COMPANIES:
                break
            probed += 1
            calls.append(_lookup(key, group[0]["company"], group[0].get("company_slug") or ""))
        for key, got in await asyncio.gather(*calls):
            if got:
                boards_for[key] = got

    resolved = 0
    for key, group in by_co.items():
        board_jobs = boards_for.get(key)
        if not board_jobs:
            continue
        for j in group:
            hit = match_title(j.get("title", ""), board_jobs)
            if hit and hit.get("url"):
                j["apply_url"] = hit["url"]
                if len(hit.get("description") or "") > len(j.get("description") or ""):
                    j["description"] = hit["description"]
                if hit.get("comp") and not j.get("comp"):
                    j["comp"] = hit["comp"]
                resolved += 1
    try:
        wl._save(CACHE, cache)
    except Exception as e:  # noqa: BLE001
        log.info("[careercoach] ATS lookup cache not saved: %s", e)
    return resolved
