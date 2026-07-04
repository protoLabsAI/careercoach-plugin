"""Live job search — a small provider abstraction over real job APIs.

Providers:
- ``jsearch``  — [JSearch on RapidAPI](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch),
  which aggregates Google-for-Jobs (LinkedIn, Indeed, ZipRecruiter, …). Needs a key
  (`X-RapidAPI-Key`, the plugin's `jobs_api_key` secret).
- ``remotive``  — the [Remotive](https://remotive.com/api-documentation) remote-jobs board.
  Keyless — the zero-setup default for remote roles.

Selection: ``provider="auto"`` uses jsearch when a key is present, else remotive.

**This module is the only outbound-calling code in the plugin** — `capabilities.network` in the
manifest lists exactly these two hosts. Only ``search_jobs()`` touches the network (via httpx, a
host dependency); the response parsers and ``prescore`` are pure and unit-tested.
"""

from __future__ import annotations

import re

JSEARCH_HOST = "jsearch.p.rapidapi.com"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"


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


def choose_provider(provider: str, api_key: str) -> str:
    """Resolve ``auto`` → ``jsearch`` when a key is set, else ``remotive``. Explicit wins."""
    p = (provider or "auto").strip().lower()
    if p == "auto":
        return "jsearch" if (api_key or "").strip() else "remotive"
    return p


def prescore(job: dict, target_roles: str) -> int:
    """A lightweight 0-100 keyword-overlap proxy — **not** the LLM fit rubric. The background
    watch uses it to decide which fresh postings are worth surfacing; the coach then runs the
    real evaluation on demand. Full coverage of the target terms with a title hit → 100."""
    terms = {t for t in re.split(r"[\s,/]+", (target_roles or "").lower()) if len(t) > 2}
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


# ── network (httpx — a host dependency; kept lazy) ────────────────────────────
async def search_jobs(
    query: str,
    *,
    location: str = "",
    remote: bool = False,
    limit: int = 10,
    api_key: str = "",
    provider: str = "auto",
) -> list[dict]:
    """Search live postings and return a normalized list of job dicts. Picks the provider per
    ``choose_provider``. Raises ``ValueError`` if jsearch is selected without a key."""
    limit = max(1, min(int(limit), 25))
    prov = choose_provider(provider, api_key)
    if prov == "jsearch":
        if not (api_key or "").strip():
            raise ValueError(
                "JSearch needs a Job-source API key (Settings → Career Coach), or set jobs_provider to 'remotive'."
            )
        jobs = await _get_jsearch(query, location, remote, api_key)
    else:
        jobs = await _get_remotive(query)
    return jobs[:limit]


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
