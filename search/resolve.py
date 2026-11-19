"""Find the company's own application page for a role a job board or aggregator listed.

Every role from a board (Himalayas, Jobicy, We Work Remotely, Remote OK) or from JSearch is looked
up: JSearch's links go to whichever aggregator syndicated the posting, and applying on the
company's own page beats applying through a middleman. The first case below is why this exists.

Himalayas' "applicationLink" is a page on himalayas.app, and Cloudflare challenges every automated
fetch of it — so a Himalayas role the coach couldn't open was a role it couldn't evaluate or apply
to. Most of those companies post on Ashby, Greenhouse, Lever or SmartRecruiters, whose public APIs
we already read (``boards.py``). This looks the company up there once (cached for a week, hits and
misses alike), finds the same title on its board, and hands back the real posting URL and full
description. A company on the watchlist uses its pinned board instead of a name guess.

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
MAX_COMPANIES = 15  # per pass (the watch batch is 15 roles): probing tries every (ATS, slug guess)


def _foreign(comp: str) -> bool:
    from .fitscore import foreign_currency

    return bool(foreign_currency(comp))


def blocked(url: str) -> bool:
    return any(h in (url or "") for h in BLOCKED_HOSTS)


#: Sources whose link is a job board's or an aggregator's page. JSearch (Google for Jobs) links
#: to whoever syndicated the posting (JobLeads, Jobgether, Indeed, …), never to the company.
INDIRECT_SOURCES = ("himalayas", "jsearch", "openwebninja", "jobicy", "weworkremotely", "remoteok")
#: A link already on one of these is the company's own posting: nothing to look up.
DIRECT_HOSTS = ("greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com", "workable.com", "smartrecruiters.com")


def indirect(job: dict) -> bool:
    """Is this job's link a board's or aggregator's page, so the company's own posting is worth
    looking up? True for a blocked page, and for any role a job board or aggregator listed unless
    its link already points at an applicant-tracking system."""
    url = job.get("url") or ""
    if blocked(url):
        return True
    return job.get("source") in INDIRECT_SOURCES and not any(h in url for h in DIRECT_HOSTS)


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


_STOP = frozenset(
    "about above after again against being below between both could doing during each every from further "
    "having here into itself more most other ours over same should some such than that their theirs them "
    "then there these they this those through under until very were what when where which while will with "
    "would your yours team teams work working role roles experience years company remote including "
    "seeking looking join environment benefits competitive engineer engineering".split()
)


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#-]{4,}", (text or "").lower()) if w not in _STOP}


def match_text(job: dict, board_jobs: list[dict]) -> dict | None:
    """The board posting a REWRITTEN listing was made from. A reposting site changes the title
    ("Frontend Engineer - KYC" becomes "Frontend Engineer - AI-Driven Web Apps (Remote)") and
    paraphrases the text, so the title match fails; the distinctive words survive. Takes the
    board posting that contains the largest share of the listing's content words, when that share
    is at least 60% and clearly ahead of the runner-up (15 points). A listing with fewer than 12
    content words is too thin to match on."""
    want = _content_words(" ".join(str(job.get(k) or "") for k in ("snippet", "description")))
    if len(want) < 12:
        return None
    scored = []
    for b in board_jobs:
        have = _content_words(f"{b.get('title', '')} {b.get('description', '')}")
        if have:
            scored.append((len(want & have) / len(want), b))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    best, hit = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    return hit if best >= 0.6 and best - second >= 0.15 else None


def _transient(problem: str) -> bool:
    """A probe problem that may clear up (a 429, timeout, 5xx), as opposed to its deterministic
    rejections — a board that names another company, or one matched only by a short-name guess —
    which are a real "no board here" and safe to cache."""
    return "'s board, not " not in problem and "matched by the short name" not in problem


def _fresh(entry: dict, now: datetime) -> bool:
    try:
        at = datetime.fromisoformat(entry.get("checked", ""))
    except ValueError:
        return False
    return now - at < timedelta(days=CACHE_DAYS)


async def resolve(jobs: list[dict], *, now: datetime | None = None) -> int:
    """Fill ``apply_url`` (and a fuller ``description``) in place for jobs a board or aggregator
    listed (``indirect``), from the company's own ATS, and record the outcome as ``ats_lookup``: "found",
    "no title" (the company's board doesn't list it) or "no board" (no board on a supported ATS).
    A job not looked up this pass (past the cap, or a probe that hit a 429/timeout) is left without
    ``ats_lookup``. Returns how many it resolved. Never raises."""
    import httpx

    from ..record.companies import company_key
    from . import boards
    from . import watchlist as wl

    now = now or datetime.now(timezone.utc)
    todo = [j for j in jobs if indirect(j) and not j.get("apply_url") and j.get("company")]
    if not todo:
        return 0
    try:
        cache = wl._load(CACHE, {})
    except Exception:  # noqa: BLE001 — an unreadable cache just means looking again
        cache = {}
    by_co: dict[str, list[dict]] = {}
    for j in todo:
        by_co.setdefault(company_key(j["company"]), []).append(j)

    try:
        rows = wl.load_watchlist()
    except Exception:  # noqa: BLE001 — an unreadable watchlist just means guessing, as before
        rows = []
    pinned = {company_key(r.get("company", "")): r for r in rows if r.get("ats") and r.get("slug")}
    boards_for: dict[str, list[dict]] = {}
    probed = 0
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        sem = asyncio.Semaphore(10)

        async def _lookup(key: str, name: str, slug: str, titles: list[str]):
            entry = cache.get(key)
            if key in pinned:  # the operator's own pin beats any guess, cached or not
                entry = {"ats": pinned[key]["ats"], "slug": pinned[key]["slug"], "checked": now.isoformat()}
            if entry and _fresh(entry, now) and not entry.get("ats"):
                return key, None  # a recent miss: don't probe again this week
            if entry and _fresh(entry, now) and entry.get("ats"):
                ats, s = entry["ats"], entry["slug"]
            else:
                found: list = []
                trouble: list = []
                # the board's own company slug first (often the ATS slug too), then name guesses
                for cand in ([slug] if slug else []) + [""]:
                    try:
                        found, problems = await boards.probe(name, cand, client=client, sem=sem)
                        trouble += [p for p in problems if _transient(p)]
                    except Exception as e:  # noqa: BLE001
                        log.info("[careercoach] ATS probe failed for %s: %s", name, e)
                        found, trouble = [], trouble + [str(e)]
                    if found:
                        break
                if not found:
                    if trouble:  # a 429 or timeout isn't "no board": look again next pass, cache nothing
                        log.info("[careercoach] ATS lookup inconclusive for %s: %s", name, trouble[:2])
                        return key, "retry"
                    cache[key] = {"company": name, "ats": "", "slug": "", "checked": now.isoformat()}
                    return key, None
                ats, s = found[0][0], found[0][1]
                cache[key] = {"company": name, "ats": ats, "slug": s, "checked": now.isoformat()}
            try:
                # the titles are the search on boards that search (SmartRecruiters)
                return key, await boards.fetch_board(ats, s, name, client=client, search=titles)
            except Exception as e:  # noqa: BLE001 — the board exists; it just didn't load this time
                log.info("[careercoach] ATS board read failed for %s (%s/%s): %s", name, ats, s, e)
                return key, "retry"

        calls = []
        for key, group in by_co.items():
            if probed >= MAX_COMPANIES:
                break
            probed += 1
            titles = [j.get("title") or "" for j in group]
            calls.append(_lookup(key, group[0]["company"], group[0].get("company_slug") or "", titles))
        for key, got in await asyncio.gather(*calls):
            if got == "retry":
                continue
            # a company looked up and found on no board: say so on its cards, so nothing claims a
            # lookup that never ran (companies past the cap, or an inconclusive probe, stay unmarked)
            boards_for[key] = got or []

    resolved = 0
    for key, group in by_co.items():
        if key not in boards_for:
            continue
        board_jobs = boards_for[key]
        for j in group:
            hit = match_title(j.get("title", ""), board_jobs) if board_jobs else None
            if hit is None and board_jobs and j.get("via_blocked"):
                # a reposting site rewrote the title: find the posting by its words, and give the
                # role back the company's own title
                hit = match_text(j, board_jobs)
                if hit and hit.get("url") and hit.get("title"):
                    j["listed_title"] = j.get("title") or ""
                    j["title"] = hit["title"]
            j["ats_lookup"] = "found" if hit and hit.get("url") else ("no title" if board_jobs else "no board")
            if hit and hit.get("url"):
                j["apply_url"] = hit["url"]
                if j.get("via_blocked"):
                    # nothing the reposter added is kept: its pay range is its own invention
                    # (Binance's posting gave none; the listing said $90-150K)
                    j.pop("comp", None)
                if (hit.get("location") or "").strip():
                    # the company's own posting says where the job is; a board's "Remote (US)"
                    # for an on-site San Francisco role is the board's guess
                    if hit["location"].strip() != (j.get("location") or "").strip():
                        j["listed_location"] = j.get("location") or ""
                    j["location"] = hit["location"]
                    if hit.get("remote") is not None:
                        # only when the company SAYS remote or on site: an ATS that doesn't say
                        # (Greenhouse, unless the location text does) leaves the board's word standing
                        j["remote"] = hit["remote"]
                        j.pop("countries", None)  # the board's country list no longer describes it
                if len(hit.get("description") or "") > len(j.get("description") or ""):
                    j["description"] = hit["description"]
                if hit.get("comp") and (not j.get("comp") or _foreign(j["comp"])):
                    j["comp"] = hit["comp"]  # the company's own range beats a board's other-currency one
                resolved += 1
    try:
        wl._save(CACHE, cache)
    except Exception as e:  # noqa: BLE001
        log.info("[careercoach] ATS lookup cache not saved: %s", e)
    return resolved
