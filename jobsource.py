"""Live job search — a small provider abstraction over real job APIs.

Providers:
- ``jsearch``    — [JSearch on RapidAPI](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch),
  which aggregates Google-for-Jobs (LinkedIn, Indeed, ZipRecruiter, …). Needs a key
  (`X-RapidAPI-Key`, the plugin's `jobs_api_key` secret).
- ``himalayas``  — the [Himalayas](https://himalayas.app/docs/remote-jobs-api) remote-jobs board.
  Keyless, searches by query, newest first, with a posting date and salary. The zero-setup default.
- ``hn``         — this month's Hacker News "Ask HN: Who is hiring?" thread, searched through the
  Algolia HN API. Keyless; startups and remote roles, posted on the first weekday of each month.
- ``remotive``   — the Remotive board. Kept only for an explicit ``jobs_provider: remotive``: since
  2026-10 its API returns one fixed 14-posting feed whatever the query, so ``auto`` never picks it.

Selection: ``provider="auto"`` uses jsearch when a key is present, else himalayas.

Neither Himalayas' nor Remotive's search is a relevance filter we can rely on (Himalayas matches
query words loosely — "design engineer" returns plumbing roles), so keyless results are ranked and
filtered client-side against the query with ``prescore``, and the result says which provider
answered, so a keyless fallback is never mistaken for a search of the whole market.

Company job boards (Ashby / Greenhouse / Lever) are read by ``boards.py``. Only ``search_jobs()``
here touches the network (via httpx, a host dependency); the response parsers and ``prescore`` are
pure and unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

JSEARCH_HOST = "jsearch.p.rapidapi.com"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
HIMALAYAS_URL = "https://himalayas.app/jobs/api/search"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"


def _job(
    *, title: str, company: str, location: str, url: str, posted: str = "", snippet: str = "", source: str = ""
) -> dict:
    return {
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "url": url.strip(),
        "posted": posted,
        "snippet": snippet.strip(),
        "source": source,
    }


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# ── pure response parsers (unit-tested against fixture payloads) ──────────────
def parse_jsearch(payload: dict) -> list[dict]:
    out: list[dict] = []
    for j in (payload or {}).get("data") or []:
        loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if x)
        if not loc and j.get("job_is_remote"):
            loc = "Remote"
        out.append(
            _job(
                title=j.get("job_title") or "",
                company=j.get("employer_name") or "",
                location=loc,
                url=j.get("job_apply_link") or j.get("job_google_link") or "",
                posted=j.get("job_posted_at_datetime_utc") or "",
                snippet=(j.get("job_description") or "")[:400],
                source="jsearch",
            )
        )
    return out


def parse_remotive(payload: dict) -> list[dict]:
    out: list[dict] = []
    for j in (payload or {}).get("jobs") or []:
        out.append(
            _job(
                title=j.get("title") or "",
                company=j.get("company_name") or "",
                location=j.get("candidate_required_location") or "Remote",
                url=j.get("url") or "",
                posted=j.get("publication_date") or "",
                snippet=_strip_html(j.get("description") or "")[:400],
                source="remotive",
            )
        )
    return out


def _iso_epoch(value) -> str:
    from .boards import _iso

    return _iso(value)


def parse_himalayas(payload: dict) -> list[dict]:
    out: list[dict] = []
    for j in (payload or {}).get("jobs") or []:
        where = ", ".join(j.get("locationRestrictions") or []) or "Anywhere"
        job = _job(
            title=j.get("title") or "",
            company=j.get("companyName") or "",
            location=f"Remote ({where})",
            url=j.get("applicationLink") or j.get("guid") or "",
            posted=_iso_epoch(j.get("pubDate")),
            snippet=_strip_html(j.get("excerpt") or j.get("description") or "")[:400],
            source="himalayas",
        )
        lo, hi = j.get("minSalary"), j.get("maxSalary")
        if lo and hi:
            job["comp"] = f"{j.get('currency') or ''} {int(lo):,}–{int(hi):,}".strip()
        out.append(job)
    return out


def _hn_fields(text: str) -> tuple[str, str, str]:
    """``(company, role, location)`` from a Who-is-hiring comment's first line, which by thread
    convention reads ``Company | Role | Location | …`` (order varies; best effort)."""
    import html as _html

    first = _html.unescape(re.split(r"<p>|\n", text or "", maxsplit=1)[0])
    first = re.sub(r"<[^>]+>", "", first)
    parts = [p.strip() for p in first.split("|") if p.strip()]
    if not parts:
        return "", "", ""
    company = re.sub(r"\s*\(.*?\)\s*", " ", parts[0]).strip()
    role = next(
        (
            p
            for p in parts[1:]
            if re.search(r"engineer|developer|designer|scientist|lead|architect|manager|head", p, re.I)
        ),
        "",
    )
    location = next(
        (p for p in parts[1:] if re.search(r"remote|onsite|on-site|hybrid|, [A-Z]{2}\b|\bUS\b|USA|NYC|SF\b", p)), ""
    )
    return company, role or (parts[1] if len(parts) > 1 else ""), location


def parse_hn(payload: dict) -> list[dict]:
    out: list[dict] = []
    for h in (payload or {}).get("hits") or []:
        company, role, location = _hn_fields(h.get("comment_text") or "")
        if not company:
            continue
        out.append(
            _job(
                title=role,
                company=company,
                location=location,
                url=f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                posted=h.get("created_at") or "",
                snippet=_strip_html(h.get("comment_text") or "")[:400],
                source="hn",
            )
        )
    return out


def choose_provider(provider: str, api_key: str) -> str:
    """Resolve ``auto`` → ``jsearch`` when a key is set, else ``himalayas``. Explicit wins."""
    p = (provider or "auto").strip().lower()
    if p == "auto":
        return "jsearch" if (api_key or "").strip() else "himalayas"
    return p


def _terms(text: str) -> set[str]:
    """The scoreable words of a query / target-roles string (3+ characters)."""
    return {t for t in re.split(r"[\s,/]+", (text or "").lower()) if len(t) > 2}


def prescore(job: dict, target_roles: str) -> int:
    """A lightweight 0-100 keyword-overlap proxy — **not** the LLM fit rubric. The background
    watch uses it to decide which fresh postings are worth surfacing; the coach then runs the
    real evaluation on demand. Full coverage of the target terms with a title hit → 100."""
    terms = _terms(target_roles)
    if not terms:
        return 0
    title = (job.get("title") or "").lower()
    blob = title + " " + (job.get("snippet") or "").lower()
    anywhere = {t for t in terms if t in blob}
    if not anywhere:
        return 0
    coverage = len(anywhere) / len(terms)
    title_hit = 1.0 if any(t in title for t in terms) else 0.0
    return min(100, round(70 * coverage + 30 * title_hit))


PROVIDER_LABELS = {
    "jsearch": "JSearch (Google-for-Jobs)",
    "himalayas": "Himalayas (remote-jobs board)",
    "hn": "Hacker News 'Who is hiring?' (this month's thread)",
    "remotive": "Remotive (remote-jobs board; returns a fixed feed, query ignored)",
}

KEYLESS = ("himalayas", "hn", "remotive")


def rank_by_query(jobs: list[dict], query: str) -> list[dict]:
    """Keep only the jobs that mention the query (``prescore`` > 0), best first; ties keep the
    provider's order. A query with no scoreable word (all terms under 3 characters, e.g. "ML")
    cannot be judged, so the jobs are returned unchanged rather than all dropped."""
    if not _terms(query):
        return list(jobs)
    scored = [(prescore(j, query), i, j) for i, j in enumerate(jobs)]
    return [j for score, _, j in sorted(scored, key=lambda x: (-x[0], x[1])) if score > 0]


@dataclass
class JobSearch:
    """What a search returned and WHO answered it — ``provider`` is the source that was actually
    queried, ``keyless_fallback`` is True when ``auto`` fell back to a keyless board for want of a key,
    ``fetched`` is how many postings the provider sent, ``matched`` how many survived relevance
    filtering (before ``limit``)."""

    jobs: list[dict] = field(default_factory=list)
    provider: str = ""
    keyless_fallback: bool = False
    fetched: int = 0
    matched: int = 0

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider or "unknown")


# ── network (httpx — a host dependency; kept lazy) ────────────────────────────
async def search_jobs(
    query: str,
    *,
    location: str = "",
    remote: bool = False,
    limit: int = 10,
    api_key: str = "",
    provider: str = "auto",
    rank: bool = True,
    country: str = "",
) -> JobSearch:
    """Search live postings. Picks the provider per ``choose_provider``; returns a ``JobSearch``
    naming the provider that answered. Keyless results are filtered to the query and ranked
    by ``prescore`` before ``limit`` is applied, unless ``rank=False`` (a caller that scores
    the raw feed itself against more than this one query, like the watch scan). ``country``
    narrows Himalayas to roles open to that country. Raises ``ValueError`` if jsearch is
    selected without a key, or for an unknown provider."""
    limit = max(1, min(int(limit), 25))
    prov = choose_provider(provider, api_key)
    keyless = (provider or "auto").strip().lower() == "auto" and prov in KEYLESS
    if prov not in PROVIDER_LABELS:
        raise ValueError(f"unknown job source {prov!r} — use auto, jsearch, himalayas, hn or remotive")
    if prov == "jsearch":
        if not (api_key or "").strip():
            raise ValueError(
                "JSearch needs a Job-source API key (Settings → Career Coach), or set jobs_provider to 'himalayas' (keyless)."
            )
        jobs = await _get_jsearch(query, location, remote, api_key)
        fetched = len(jobs)
    else:
        if prov == "himalayas":
            raw = await _get_himalayas(query, country)
        elif prov == "hn":
            raw = await _get_hn(query)
        else:
            raw = await _get_remotive(query)
        fetched = len(raw)
        jobs = rank_by_query(raw, query) if rank else raw
    return JobSearch(jobs=jobs[:limit], provider=prov, keyless_fallback=keyless, fetched=fetched, matched=len(jobs))


async def _get_jsearch(query: str, location: str, remote: bool, api_key: str) -> list[dict]:
    import httpx

    q = (query or "software engineer").strip()
    if location:
        q = f"{q} in {location}"
    params = {"query": q, "page": "1", "num_pages": "1"}
    if remote:
        params["remote_jobs_only"] = "true"
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": JSEARCH_HOST}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"https://{JSEARCH_HOST}/search", params=params, headers=headers)
        resp.raise_for_status()
        return parse_jsearch(resp.json())


async def _get_remotive(query: str) -> list[dict]:
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(REMOTIVE_URL, params={"search": (query or "").strip()})
        resp.raise_for_status()
        return parse_remotive(resp.json())


async def _get_himalayas(query: str, country: str = "") -> list[dict]:
    import httpx

    params = {"q": (query or "").strip(), "sort": "recent"}
    if country:
        params["country"] = country
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(HIMALAYAS_URL, params=params)
        resp.raise_for_status()
        return parse_himalayas(resp.json())


async def _get_hn(query: str) -> list[dict]:
    """Find the newest "Ask HN: Who is hiring?" story, then search its comments for ``query``."""
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(HN_BY_DATE_URL, params={"tags": "story,author_whoishiring", "hitsPerPage": "10"})
        resp.raise_for_status()
        story = next(
            (
                h
                for h in resp.json().get("hits") or []
                if (h.get("title") or "").lower().startswith("ask hn: who is hiring")
            ),
            None,
        )
        if story is None:
            return []
        resp = await client.get(
            HN_SEARCH_URL,
            params={"tags": f"comment,story_{story['objectID']}", "query": (query or "").strip(), "hitsPerPage": "50"},
        )
        resp.raise_for_status()
        # top-level comments only — replies are questions to the poster, not postings
        hits = [h for h in resp.json().get("hits") or [] if str(h.get("parent_id")) == str(story["objectID"])]
        return parse_hn({"hits": hits})
