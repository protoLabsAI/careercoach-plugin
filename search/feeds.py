"""Keyless remote-job FEEDS — boards that publish their recent postings whole, with no search.

- ``jobicy``         — [Jobicy](https://jobicy.com/jobs-rss-feed): remote roles open to the US, with
  a structured country list and pay. Engineering, design and data-science listings.
- ``weworkremotely`` — [We Work Remotely](https://weworkremotely.com/remote-job-rss-feed): the
  programming and design category feeds (RSS). Each posting names its region ("USA Only",
  "Anywhere in the World", "Europe Only", …).
- ``remoteok``       — [Remote OK](https://remoteok.com/api): its newest postings. Location is
  free text and often blank.

A feed can't be asked a question: every call returns the same list, so a search over a feed is
that list ranked against the query locally (``jobsource.rank_by_query``), like the other keyless
boards. Because the list doesn't depend on the query, it is fetched once and kept for
``CACHE_SECONDS``: the watch asks once per target role, and thirty identical downloads a pass would
be rude to a free service (Jobicy asks for about one request an hour).

All three ask to be credited and linked: every posting keeps the board's own URL as its link and
its ``source`` names the board.

Parsers are pure and unit-tested; only ``fetch()`` touches the network.
"""

from __future__ import annotations

import html
import re
import time

CACHE_SECONDS = 3600

JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"
#: Jobicy "industry" slugs worth reading for a software search ("dev" is its engineering list).
JOBICY_INDUSTRIES = ("dev", "design-multimedia", "data-science")
WWR_URL = "https://weworkremotely.com/categories/{category}.rss"
WWR_CATEGORIES = (
    "remote-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-front-end-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-design-jobs",
)
REMOTEOK_URL = "https://remoteok.com/api"

LABELS = {
    "jobicy": "Jobicy (remote-jobs feed, US)",
    "weworkremotely": "We Work Remotely (remote-jobs feed)",
    "remoteok": "Remote OK (remote-jobs feed)",
}
FEEDS = tuple(LABELS)
HOSTS = ("jobicy.com", "weworkremotely.com", "remoteok.com")


def _text(s) -> str:
    """Plain text from a feed's HTML (possibly entity-escaped once, as in RSS)."""
    s = html.unescape(str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _job(*, title, company, location, url, posted, description, source, countries=None) -> dict:
    from .boards import _iso
    from .jobsource import DESCRIPTION_CAP

    text = _text(description)
    job = {
        "title": _text(title),
        "company": _text(company),
        "location": location.strip(),
        "url": str(url or "").strip(),
        "posted": _iso(posted),
        "snippet": text[:400],
        "description": text[:DESCRIPTION_CAP],
        "source": source,
        "remote": True,  # every board here lists remote roles only
    }
    if countries:
        job["countries"] = countries
    return job


def _num(value) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


# ── pure parsers ──────────────────────────────────────────────────────────────
def parse_jobicy(payload: dict) -> list[dict]:
    out: list[dict] = []
    for j in (payload or {}).get("jobs") or []:
        geo = [g.strip() for g in str(j.get("jobGeo") or "").split(",") if g.strip()]
        # "Anywhere" is not a country: leave the list off so the text ("Remote (Anywhere)") decides
        countries = [] if any(g.lower() == "anywhere" for g in geo) else geo
        job = _job(
            title=j.get("jobTitle"),
            company=j.get("companyName"),
            location=f"Remote ({', '.join(geo)})" if geo else "Remote",
            url=j.get("url"),
            posted=j.get("pubDate"),
            description=j.get("jobDescription") or j.get("jobExcerpt"),
            source="jobicy",
            countries=countries,
        )
        lo, hi = _num(j.get("salaryMin")), _num(j.get("salaryMax"))
        currency = str(j.get("salaryCurrency") or "").strip().upper()
        if lo and hi and currency not in ("", "NONE"):
            job["comp"] = f"{currency} {lo:,}–{hi:,}"
            period = str(j.get("salaryPeriod") or "").strip().lower()
            if period and period not in ("none", "year", "yearly", "annual", "annually"):
                per = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month"}.get(period, period)
                job["comp"] += f" per {per}"
        out.append(job)
    return out


def _rss_field(item: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", item, re.S)
    if not m:
        return ""
    raw = m.group(1).strip()
    cdata = re.fullmatch(r"<!\[CDATA\[(.*?)\]\]>", raw, re.S)
    return cdata.group(1) if cdata else raw


def parse_weworkremotely(xml: str) -> list[dict]:
    """Each ``<item>``: the title reads "Company: Role", ``<region>`` says who may apply."""
    from email.utils import parsedate_to_datetime

    out: list[dict] = []
    for item in re.findall(r"<item>(.*?)</item>", xml or "", re.S):
        head = _text(_rss_field(item, "title"))
        company, _, title = head.partition(": ")
        if not title:  # no "Company: " prefix
            company, title = "", head
        region = _text(_rss_field(item, "region"))
        try:
            posted = parsedate_to_datetime(_text(_rss_field(item, "pubDate"))).isoformat()
        except (TypeError, ValueError):
            posted = ""
        out.append(
            _job(
                title=title,
                company=company,
                location=f"Remote ({region})" if region else "Remote",
                url=_text(_rss_field(item, "link")) or _text(_rss_field(item, "guid")),
                posted=posted,
                description=_rss_field(item, "description"),
                source="weworkremotely",
                # structured where the region is a country, so "USA Only" never depends on text
                countries=["usa"] if region.lower().startswith("usa") else None,
            )
        )
    return out


def _unmangle(s) -> str:
    """Remote OK sends some text UTF-8-decoded as Latin-1 ("â\x80\x94" for an em dash). Undo it
    when the round trip works; text that was never mangled fails the round trip and is kept."""
    s = str(s or "")
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def parse_remoteok(payload: list) -> list[dict]:
    """Remote OK's list opens with a legal-notice row (no ``position``); the rest are postings."""
    out: list[dict] = []
    for j in payload or []:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        where = _text(j.get("location"))
        job = _job(
            title=_unmangle(j.get("position")),
            company=_unmangle(j.get("company")),
            location=where if "remote" in where.lower() else (f"Remote ({where})" if where else "Remote"),
            url=j.get("url") or j.get("apply_url"),
            posted=j.get("date") or j.get("epoch"),
            description=_unmangle(j.get("description")),
            source="remoteok",
        )
        lo, hi = _num(j.get("salary_min")), _num(j.get("salary_max"))
        if lo and hi:
            job["comp"] = f"USD {lo:,}–{hi:,}"  # Remote OK publishes pay in US dollars
        out.append(job)
    return out


# ── network (httpx — a host dependency; kept lazy) ────────────────────────────
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_LOCKS: dict[str, object] = {}  # one download at a time per feed: concurrent searches share it
#: A generic client name: Remote OK refuses requests with no User-Agent.
_HEADERS = {"User-Agent": "protoagent-careercoach", "Accept": "application/json, application/rss+xml, */*"}


def _dedupe(jobs: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for j in jobs:
        seen.setdefault(j["url"] or f"{j['company']}|{j['title']}", j)
    return list(seen.values())


async def _fetch_jobicy(client) -> list[dict]:
    jobs: list[dict] = []
    for industry in JOBICY_INDUSTRIES:
        resp = await client.get(JOBICY_URL, params={"count": "100", "geo": "usa", "industry": industry})
        resp.raise_for_status()
        jobs += parse_jobicy(resp.json())
    return jobs


async def _fetch_weworkremotely(client) -> list[dict]:
    jobs: list[dict] = []
    for category in WWR_CATEGORIES:
        resp = await client.get(WWR_URL.format(category=category))
        resp.raise_for_status()
        jobs += parse_weworkremotely(resp.text)
    return jobs


async def _fetch_remoteok(client) -> list[dict]:
    resp = await client.get(REMOTEOK_URL)
    resp.raise_for_status()
    return parse_remoteok(resp.json())


_FETCHERS = {"jobicy": _fetch_jobicy, "weworkremotely": _fetch_weworkremotely, "remoteok": _fetch_remoteok}


async def fetch(name: str, now: float | None = None) -> list[dict]:
    """A feed's current postings, newest first, deduplicated. Served from memory for
    ``CACHE_SECONDS`` after a successful download; a failed download raises and is not cached, so
    the next call tries again. Returns copies: a caller that stamps an id or a score on a posting
    must not change what the next caller reads."""
    import httpx

    if name not in _FETCHERS:
        raise ValueError(f"unknown job feed {name!r}")
    import asyncio

    now = time.monotonic() if now is None else now
    lock = _LOCKS.get(name)
    # a lock belongs to the event loop that made it: a new loop (each asyncio.run) gets its own
    if lock is None or getattr(lock, "_cc_loop", None) is not asyncio.get_running_loop():
        lock = _LOCKS[name] = asyncio.Lock()
        lock._cc_loop = asyncio.get_running_loop()
    async with lock:
        hit = _CACHE.get(name)
        if hit is None or now - hit[0] >= CACHE_SECONDS:
            async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS, follow_redirects=True) as client:
                jobs = _dedupe(await _FETCHERS[name](client))
            jobs.sort(key=lambda j: j.get("posted") or "", reverse=True)
            _CACHE[name] = hit = (now, jobs)
    return [dict(j) for j in hit[1]]
