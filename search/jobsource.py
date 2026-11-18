"""Live job search — a small provider abstraction over real job APIs.

Providers:
- ``jsearch``    — [JSearch on RapidAPI](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch),
  which aggregates Google-for-Jobs (LinkedIn, Indeed, ZipRecruiter, …). Needs a key
  (`X-RapidAPI-Key`, the plugin's `jobs_api_key` secret). Calls ``/search-v2``: RapidAPI removed
  ``/search`` (it answers 404), and v2 nests the jobs under ``data.jobs``.
- ``openwebninja`` — the same JSearch API bought directly from
  [OpenWeb Ninja](https://www.openwebninja.com/api/jsearch): another host and an `x-api-key`
  header. Explicit only: both vendors issue 50-character keys, so ``auto`` can't tell whose key
  it holds and keeps meaning RapidAPI.
- ``himalayas``  — the [Himalayas](https://himalayas.app/docs/remote-jobs-api) remote-jobs board.
  Keyless, searches by query, newest first, with a posting date and salary. The zero-setup default.
- ``hn``         — this month's Hacker News "Ask HN: Who is hiring?" thread, searched through the
  Algolia HN API. Keyless; startups and remote roles, posted on the first weekday of each month.
- ``jobicy``, ``weworkremotely``, ``remoteok`` — keyless feeds of recent remote postings
  (``feeds.py``). A feed can't be searched: its list is downloaded once an hour and ranked against
  the query here.
- ``remotive``   — the Remotive board. Kept only for an explicit ``jobs_provider: remotive``: since
  2026-10 its API returns one fixed 14-posting feed whatever the query, so ``auto`` never picks it.

Selection: ``provider="auto"`` uses jsearch when a key is present, else himalayas. When a
keyed source fails (a rejected key, a spent quota, an outage), the search is answered
by Himalayas instead and the result says why: a bad key must not cost a search its results.

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

from . import feeds

JSEARCH_HOST = "jsearch.p.rapidapi.com"
#: JSearch runs a live Google-for-Jobs query per request: one in five answers took longer than the
#: 20 s the other boards get (measured 2026-11-18), and each lost answer still spends a request.
KEYED_TIMEOUT = 45.0
JSEARCH_PATH = "/search-v2"  # RapidAPI removed /search (404 "Endpoint '/search' does not exist")
OPENWEBNINJA_URL = "https://api.openwebninja.com/jsearch/search"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
HIMALAYAS_URL = "https://himalayas.app/jobs/api/search"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"


DESCRIPTION_CAP = 8000  # the full posting text, for scoring and reading without a page fetch


def _job(
    *,
    title: str,
    company: str,
    location: str,
    url: str,
    posted: str = "",
    snippet: str = "",
    source: str = "",
    description: str = "",
) -> dict:
    return {
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "url": url.strip(),
        "posted": posted,
        "snippet": snippet.strip(),
        "description": (description or "").strip()[:DESCRIPTION_CAP],
        "source": source,
    }


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# ── pure response parsers (unit-tested against fixture payloads) ──────────────
def parse_jsearch(payload: dict) -> list[dict]:
    """JSearch jobs from either response shape: v1 ``{"data": [job, …]}`` (RapidAPI and OpenWeb
    Ninja ``/search``) or v2 ``{"data": {"jobs": [job, …], "cursor": …}}`` (``/search-v2``)."""
    data = (payload or {}).get("data")
    if isinstance(data, dict):
        data = data.get("jobs")
    out: list[dict] = []
    for j in data or []:
        loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if x)
        remote = bool(j.get("job_is_remote"))
        if remote:
            # "Remote (US)", not a bare "US": the location filter reads "remote" from this text
            loc = f"Remote ({loc})" if loc else "Remote"
        job = _job(
            # an aggregator's page title: "AI-Augmented Software Engineer job at CGI in Knoxville"
            title=re.sub(r"\s+job\s+at\s+.+$", "", j.get("job_title") or "", flags=re.I)
            if j.get("employer_name")
            else (j.get("job_title") or ""),
            company=j.get("employer_name") or "",
            location=loc,
            url=j.get("job_apply_link") or j.get("job_google_link") or "",
            # v2 can send a null datetime with the epoch-seconds timestamp still set
            posted=j.get("job_posted_at_datetime_utc") or _iso_epoch(j.get("job_posted_at_timestamp")),
            snippet=(j.get("job_description") or "")[:400],
            source="jsearch",
            description=j.get("job_description") or "",
        )
        if remote:
            job["remote"] = True
            if j.get("job_country"):
                # structured, so the filter never reads the text: "Remote (Berlin, DE)" would
                # otherwise pass as Delaware
                job["countries"] = [str(j["job_country"])]
        lo, hi = j.get("job_min_salary"), j.get("job_max_salary")
        # JSearch sends pay as bare numbers. They are dollars when it says USD, or says nothing
        # for a US posting. Any other pay is left out, so it is never compared to the (USD) floor
        # as if it were dollars.
        currency = str(j.get("job_salary_currency") or "").strip().upper()
        us = str(j.get("job_country") or "US").strip().upper() in ("US", "USA", "UNITED STATES")
        if lo and hi and (currency == "USD" or (not currency and us)):
            job["comp"] = f"{currency} {int(lo):,}–{int(hi):,}".strip()
            period = (j.get("job_salary_period") or "").strip().lower()
            if period and period != "year":
                job["comp"] += f" per {period}"
        out.append(job)
    return out


def parse_jsearch_quota(headers) -> dict:
    """RapidAPI's request allowance from a response's headers: ``{"limit", "remaining",
    "reset_s"}`` (requests in the plan's period, requests left, seconds until it refills). ``{}``
    when the headers aren't there (another vendor, or a proxy that strips them)."""

    def _int(name: str) -> int | None:
        try:
            return int(str(headers.get(name) or "").strip())
        except (TypeError, ValueError):
            return None

    remaining = _int("x-ratelimit-requests-remaining")
    if remaining is None:
        return {}
    return {
        "limit": _int("x-ratelimit-requests-limit") or 0,
        "remaining": max(0, remaining),
        "reset_s": max(0, _int("x-ratelimit-requests-reset") or 0),
    }


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
                description=_strip_html(j.get("description") or ""),
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
            # the whole posting: himalayas.app job pages refuse automated fetches (Cloudflare),
            # so this API text is the only copy of the posting the coach can read
            description=_strip_html(j.get("description") or j.get("excerpt") or ""),
        )
        if j.get("companySlug"):
            job["company_slug"] = j["companySlug"]
        lo, hi = j.get("minSalary"), j.get("maxSalary")
        if lo and hi:
            job["comp"] = f"{j.get('currency') or ''} {int(lo):,}–{int(hi):,}".strip()
            period = (j.get("salaryPeriod") or "").strip().lower()
            if period and period not in ("year", "yearly", "annual", "annually"):
                per = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month"}.get(period, period)
                job["comp"] += f" per {per}"
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
                description=_strip_html(h.get("comment_text") or ""),
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
    "openwebninja": "JSearch via OpenWeb Ninja (Google-for-Jobs)",
    "himalayas": "Himalayas (remote-jobs board)",
    "hn": "Hacker News 'Who is hiring?' (this month's thread)",
    "remotive": "Remotive (remote-jobs board; returns a fixed feed, query ignored)",
}

KEYLESS = ("himalayas", "hn", "remotive")
# The keyless feeds (search/feeds.py): boards that publish their recent postings whole.
PROVIDER_LABELS.update(feeds.LABELS)
KEYLESS = (*KEYLESS, *feeds.FEEDS)

# Both keyed vendors sell the same JSearch API, so both spend the one request budget
# (``watchlist.note_jsearch_call`` / ``jsearch_allowance``). Test membership here, never the
# literal name "jsearch": that is how OpenWeb Ninja requests went uncounted.
JSEARCH_VENDORS = ("jsearch", "openwebninja")


def choose_providers(provider: str, api_key: str) -> list[str]:
    """Expand and validate a selection before network calls. Keys cannot identify their vendor."""
    names = [p.strip() for p in (provider or "auto").strip().lower().split(",")]
    for name in names:
        if name not in (*PROVIDER_LABELS, "auto", "all"):
            raise ValueError(f"unknown job source {name!r} — use auto, all, or any of: {', '.join(PROVIDER_LABELS)}")
    vendors = [p for p in names if p in JSEARCH_VENDORS]
    expanded = []
    for name in names:
        if name == "all":
            expanded.extend(p for p in KEYLESS if p != "remotive")
            if (api_key or "").strip():
                expanded.extend(vendors or ["jsearch"])
        else:
            expanded.append(choose_provider(name, api_key))
    return list(dict.fromkeys(expanded))


def dedupe_jobs(jobs: list[dict]) -> list[dict]:
    """Keep first-seen postings with combined provenance, without modifying inputs."""
    from urllib.parse import urlsplit

    merged = {}
    for job in jobs:
        company = " ".join((job.get("company") or "").casefold().split())
        company = re.sub(r"(?:[\s,]+(?:incorporated|inc|llc|ltd|limited|corp|corporation)\.?)+$", "", company).strip()
        title = re.sub(r"[\W_]+", " ", (job.get("title") or "").casefold()).strip()
        url = (job.get("url") or "").strip()
        parts = urlsplit(url if "://" in url else "//" + url)
        key = (company, title, parts.netloc.casefold() + parts.path.rstrip("/"))
        sources = job.get("sources") or [job.get("source", "")]
        if key not in merged:
            merged[key] = {**job, "sources": list(sources)}
        else:
            merged[key]["sources"] = list(dict.fromkeys([*merged[key]["sources"], *sources]))
    return list(merged.values())


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
    queried, ``keyless_fallback`` is True when a search fell back to a keyless board (for want of a
    key, or because a keyed source failed: then ``fallback_reason`` says how), ``fetched`` is how many
    postings the provider sent, ``matched`` how many survived relevance filtering (before
    ``limit``). ``quota`` is JSearch's request allowance as of this call (``parse_jsearch_quota``),
    empty when JSearch didn't answer or didn't say. ``keyed_quotas`` holds one entry per request a
    JSearch vendor (``JSEARCH_VENDORS``) answered — that request's quota, ``{}`` when the vendor
    reported none — so a caller can count every one against the shared request budget. Multi-source
    searches populate ``providers`` with every answering source and ``failures`` with per-source
    reasons."""

    jobs: list[dict] = field(default_factory=list)
    provider: str = ""
    keyless_fallback: bool = False
    fetched: int = 0
    matched: int = 0
    fallback_reason: str = ""
    quota: dict = field(default_factory=dict)

    providers: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    #: The keyed source failed without refusing: a timeout or a dropped connection, worth another try.
    transient: bool = False
    keyed_quotas: list[dict] = field(default_factory=list)

    @property
    def provider_label(self) -> str:
        if self.providers:
            return ", ".join(PROVIDER_LABELS[p] for p in self.providers)
        return PROVIDER_LABELS.get(self.provider, self.provider or "none")


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
    max_age_days: float = 0,
    monthly_limit: int = 200,
    _fallback: bool = True,
) -> JobSearch:
    """Search live postings. Picks the provider per ``choose_provider``; returns a ``JobSearch``
    naming the provider that answered. Keyless results are filtered to the query and ranked
    by ``prescore`` before ``limit`` is applied, unless ``rank=False`` (a caller that scores
    the raw feed itself against more than this one query, like the watch scan). ``country``
    narrows Himalayas to roles open to that country; ``max_age_days`` asks JSearch for postings
    no older than that. Raises ``ValueError`` if jsearch or openwebninja is selected without a
    key or for an unknown provider. A keyed failure falls back to keyless sources and
    ``fallback_reason`` carries the vendor message, including any active cooldown. Every request
    sent to a keyed source is counted against its month (``watchlist.note_month_call``); once the
    count reaches ``monthly_limit`` (the ``jobs_api_monthly_limit`` setting, 0 = no limit; a
    vendor-reported limit wins) the source is skipped without a request, like any other keyed
    failure. Lists and ``all`` merge deduped results in source order and retain individual
    failures."""
    limit = max(1, min(int(limit), 25))
    providers = choose_providers(provider, api_key)
    if "," in (provider or "") or (provider or "").strip().lower() == "all":
        combined = JobSearch()
        jobs = []
        for selected in providers:
            try:
                result = await search_jobs(
                    query,
                    location=location,
                    remote=remote,
                    limit=25,
                    api_key=api_key,
                    provider=selected,
                    rank=rank,
                    country=country,
                    max_age_days=max_age_days,
                    monthly_limit=monthly_limit,
                    _fallback=False,
                )
            except Exception as exc:  # noqa: BLE001 — partial results survive a source outage
                combined.failures[selected] = _failure(exc)
                continue
            combined.failures.update(result.failures)
            if result.fallback_reason:
                combined.keyless_fallback = True
                combined.fallback_reason = "; ".join(filter(None, [combined.fallback_reason, result.fallback_reason]))
                if not any(p in KEYLESS for p in providers):
                    providers.append("himalayas")
            if result.provider:
                combined.providers.append(result.provider)
            combined.fetched += result.fetched
            # Every answering JSearch vendor counts against the budget, whatever its name; the
            # allowance shown is the first one a vendor actually reported.
            combined.keyed_quotas.extend(result.keyed_quotas)
            if result.quota and not combined.quota:
                combined.quota = result.quota
            jobs.extend({**job, "source": selected} for job in result.jobs)
        merged = dedupe_jobs(jobs)
        combined.jobs = merged[:limit]
        combined.matched = len(merged)
        combined.provider = ",".join(combined.providers)
        return combined
    prov = choose_provider(provider, api_key)
    auto = (provider or "auto").strip().lower() == "auto"
    keyless = auto and prov in KEYLESS
    reason = ""
    quota: dict = {}
    if prov not in PROVIDER_LABELS:
        raise ValueError(f"unknown job source {prov!r} — use auto, all, or any of: {', '.join(PROVIDER_LABELS)}")
    failures = {}
    transient = False
    if prov in JSEARCH_VENDORS:
        from . import watchlist

        if not (api_key or "").strip():
            raise ValueError(
                f"{_JOB_API_LABELS[prov]} needs a Job-source API key (Settings → Career Coach), "
                "or set jobs_provider to 'himalayas' (keyless)."
            )
        until = watchlist.source_cooldown(prov)
        month = watchlist.month_usage(prov, monthly_limit)
        try:
            if month["spent"]:
                raise ValueError(
                    f"{_JOB_API_LABELS[prov]}: this month's {month['limit']} requests are used up; "
                    f"they refill on {month['refill']} (UTC)"
                )
            if until:
                raise ValueError(f"{_JOB_API_LABELS[prov]} is cooling down until {until}")
            if prov == "jsearch":
                jobs, quota = await _get_jsearch(query, location, remote, api_key, max_age_days)
            else:
                jobs = await _get_openwebninja(query, location, remote, api_key)
            fetched = len(jobs)
            watchlist.note_month_call(prov, quota)
            watchlist.clear_source_cooldown(prov)
        except Exception as e:  # noqa: BLE001 — a keyed outage must not cost the search
            import httpx

            transient = isinstance(e, httpx.TransportError)  # a timeout or a dropped connection
            if transient:
                watchlist.note_month_call(prov)  # the request was sent: the vendor may have counted it
            reason = _failure(e)
            if not any(label in reason for label in ("RapidAPI", "OpenWeb Ninja")):
                reason = f"{_JOB_API_LABELS[prov]}: {reason}"
            failures[prov] = reason
            if not _fallback:
                return JobSearch(failures=failures, fallback_reason=reason, keyless_fallback=True, transient=transient)
            prov, keyless = "himalayas", True
    if prov in KEYLESS:
        if prov == "himalayas":
            raw = await _get_himalayas(query, country)
        elif prov == "hn":
            raw = await _get_hn(query)
        elif prov in feeds.FEEDS:
            raw = await feeds.fetch(prov)  # the whole feed (cached); ranked against the query below
        else:
            raw = await _get_remotive(query)
        fetched = len(raw)
        jobs = rank_by_query(raw, query) if rank else raw
    return JobSearch(
        jobs=jobs[:limit],
        provider=prov,
        keyless_fallback=keyless,
        fetched=fetched,
        matched=len(jobs),
        fallback_reason=reason,
        failures=failures,
        transient=transient,
        quota=quota,
        keyed_quotas=[quota] if prov in JSEARCH_VENDORS else [],
    )


#: Who refused the request, as the user knows the vendor (not the feed's label in PROVIDER_LABELS).
_JOB_API_LABELS = {"jsearch": "RapidAPI (JSearch)", "openwebninja": "OpenWeb Ninja (JSearch)"}


def _job_api_message(resp) -> str:
    """The short human reason in an error body: the JSON ``message`` (RapidAPI) or ``error``
    (a string, or an object carrying ``message``); else the first of a plain-text body."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — not JSON (an HTML gateway page, an empty body)
        body = None
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        for cand in (body.get("message"), err.get("message") if isinstance(err, dict) else err):
            if isinstance(cand, str) and cand.strip():
                msg = cand
                break
    elif body is None:
        text = (getattr(resp, "text", "") or "").strip()
        msg = "" if text.startswith("<") else text
    return re.sub(r"\s+", " ", msg).strip()[:200]


def _raise_for_job_api(resp, provider: str) -> None:
    """``raise_for_status()`` for the keyed JSearch vendors, keeping the reason the body gives.
    A bare "403 Forbidden" reads as "Settings lost my key"; the vendor's message says whether
    the key was rejected, belongs to the other vendor, or ran out of quota. Raises
    ``ValueError`` with what happened and the one step that fixes it; 2xx returns."""
    status = resp.status_code
    if 200 <= status < 300:
        return
    label = _JOB_API_LABELS.get(provider, provider)
    msg = _job_api_message(resp)
    if status == 403 and provider == "jsearch" and "not subscribed" in msg.lower():
        raise ValueError(
            "RapidAPI accepted the key, but its account isn't subscribed to JSearch. Subscribe at "
            "https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch, or if this key is from OpenWeb Ninja "
            "(api.openwebninja.com), set Job source to 'openwebninja' in Settings → Career Coach."
        )
    if status == 401:
        raise ValueError(
            f"{label} rejected the key. Check Settings → Career Coach → Job-source API key, and that "
            "Job source matches the vendor the key came from ('jsearch' for RapidAPI, 'openwebninja' "
            "for OpenWeb Ninja)." + (f" It said: {msg}" if msg else "")
        )
    if status == 403:
        raise ValueError(f"{label} denied access: {msg or '403 Forbidden, no reason given'}")
    if status == 429:
        from . import watchlist

        watchlist.note_source_cooldown(provider, resp.headers.get("Retry-After", ""))
        raise ValueError(f"{label} rate limit or monthly quota reached. Wait and retry, or check your plan.")
    raise ValueError(f"{label} returned HTTP {status}" + (f": {msg}" if msg else ""))


def _failure(e: Exception) -> str:
    """A one-line reason for a failed provider call: ``_raise_for_job_api``'s message for a
    vendor's refusal, else the exception's text (a timeout, a connection error)."""
    return str(e) or type(e).__name__


def _date_posted(max_age_days: float) -> str:
    """JSearch's ``date_posted`` bucket that covers ``max_age_days`` ("" = any age)."""
    if not max_age_days or max_age_days <= 0:
        return ""
    for days, bucket in ((1, "today"), (3, "3days"), (7, "week"), (31, "month")):
        if max_age_days <= days:
            return bucket
    return ""


async def _get_jsearch(
    query: str, location: str, remote: bool, api_key: str, max_age_days: float = 0
) -> tuple[list[dict], dict]:
    """RapidAPI JSearch ``/search-v2`` → ``(jobs, quota)``. v2 dropped ``remote_jobs_only`` (it is
    ignored without an error, and on-site roles come back); the remote filter is ``work_from_home``."""
    import httpx

    q = (query or "software engineer").strip()
    if location:
        q = f"{q} in {location}"
    params = {"query": q, "page": "1", "num_pages": "1"}
    if remote:
        params["work_from_home"] = "true"
    if _date_posted(max_age_days):
        params["date_posted"] = _date_posted(max_age_days)
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": JSEARCH_HOST}
    async with httpx.AsyncClient(timeout=KEYED_TIMEOUT) as client:
        resp = await client.get(f"https://{JSEARCH_HOST}{JSEARCH_PATH}", params=params, headers=headers)
        _raise_for_job_api(resp, "jsearch")
        return parse_jsearch(resp.json()), parse_jsearch_quota(resp.headers)


async def _get_openwebninja(query: str, location: str, remote: bool, api_key: str) -> list[dict]:
    """JSearch bought directly from OpenWeb Ninja: the same search params as RapidAPI's, another
    host, and the key in ``x-api-key`` (a RapidAPI key is refused here, and the reverse)."""
    import httpx

    q = (query or "software engineer").strip()
    if location:
        q = f"{q} in {location}"
    params = {"query": q, "page": "1", "num_pages": "1"}
    if remote:
        params["remote_jobs_only"] = "true"
    async with httpx.AsyncClient(timeout=KEYED_TIMEOUT) as client:
        resp = await client.get(OPENWEBNINJA_URL, params=params, headers={"x-api-key": api_key})
        _raise_for_job_api(resp, "openwebninja")
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
