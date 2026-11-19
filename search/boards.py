"""Company job boards — read open roles straight from the ATS a company posts through.

Most companies hire through Ashby, Greenhouse or Lever, and each publishes its board as a keyless,
documented JSON API that carries the date a posting first went up. That is the freshest source
there is (an aggregator re-lists it hours or days later) and it is the employer's own data, so
there is no scraping and no terms-of-service gray area:

- Ashby       ``api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true``
               → ``publishedAt``, ``isRemote``, ``workplaceType``, structured compensation
- Greenhouse  ``boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true``
               → ``first_published`` (NOT ``updated_at``: an edit bumps that and fakes freshness)
- Lever       ``api.lever.co/v0/postings/{slug}?mode=json`` → ``createdAt`` (epoch ms)

None of them searches, so the whole board comes back and ``passes`` filters it here by title
(the target roles), location (remote-US / named places) and age.

Big employers mostly hire through Workday or SmartRecruiters instead, and both serve their public
careers sites from keyless JSON endpoints too — but they page, they search, and their list pages
carry no description, so they are read differently:

- SmartRecruiters ``api.smartrecruiters.com/v1/companies/{company}/postings`` (100 a page,
               ``q=`` searches) → ``releasedDate``, structured city/region/country + a ``remote``
               flag; the text is on ``.../postings/{id}``. An unknown company answers 200 with no
               postings, never a 404.
- Workday     ``POST {tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`` (20 a page,
               ``searchText`` searches) → a posted AGE as text ("Posted 3 Days Ago"), a location
               summary ("14 Locations") and ``remoteType``; the text, real locations, country and
               start date are on ``.../wday/cxs/{tenant}/{site}{externalPath}``. A board is three
               things, so its slug is ``tenant/wdN/site`` (``nike/wd1/nke``), and ``board_from_url``
               reads it off a pasted careers URL. Its site name can't be guessed from a company
               name, so ``probe`` never tries Workday: a Workday board is pinned by URL or slug.

A 5,000-posting board must not be downloaded to find five engineers, so ``fetch_board`` takes the
target roles as ``search``: those two ATSs run one search per role (capped), and only postings whose
title matches a role get the per-posting read for their description (capped too). The other three
ignore it. Parsers + filters are pure and unit-tested; only ``fetch_board`` / ``probe`` touch the
network (httpx, a host dependency).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta

ATS = ("ashby", "greenhouse", "lever", "smartrecruiters", "workday")
#: The boards ``probe`` guesses a company name against. Workday needs a site name no guess finds.
PROBED = ("ashby", "greenhouse", "lever", "smartrecruiters")

ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
SMARTRECRUITERS_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
SMARTRECRUITERS_JOB_URL = "https://jobs.smartrecruiters.com/{company}/{id}"
WORKDAY_URL = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
# Workday tenants are subdomains; the manifest lists the suffix form (it is disclosure, not a jail)
HOSTS = (
    "api.ashbyhq.com",
    "boards-api.greenhouse.io",
    "api.lever.co",
    "api.smartrecruiters.com",
    "*.myworkdayjobs.com",
)

MAX_POSTINGS = 200  # per search (or per board, unsearched) on the paging ATSs
MAX_DETAILS = 30  # per-posting reads per board fetch, title matches only
SR_PAGE = 100
WD_PAGE = 20  # Workday refuses a larger page


def _strip_html(s: str) -> str:
    import html

    # Greenhouse entity-escapes its HTML (&lt;p&gt;), so unescape BEFORE stripping tags too
    text = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _iso(value) -> str:
    """Normalize a timestamp (ISO string, epoch s or ms) to an ISO-8601 UTC string, "" if absent."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            n = float(value)
            if n > 1e12:  # epoch milliseconds
                n /= 1000
            return datetime.fromtimestamp(n, UTC).isoformat(timespec="seconds")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:  # a board's naive timestamp means UTC, not this host's local time
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""


def _clip(text: str, n: int = 160) -> str:
    """Board text is untrusted and lands in tool output and the tracker: one line, bounded length."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text or "").strip()[:n]


def age_days(posted: str, now: datetime | None = None) -> float | None:
    """Days since ``posted`` (an ISO string), or None when unknown."""
    if not posted:
        return None
    try:
        then = datetime.fromisoformat(posted)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - then).total_seconds() / 86400)


def content_hash(title: str, description: str, location: str = "") -> str:
    """A fingerprint of a posting's substance — the same text re-listed under a new id/URL (a
    repost) keeps its hash, so the first-seen log can tell a repost from a new opening. Location is
    part of it: one role listed per city with identical text is several openings, not a repost."""
    text = re.sub(r"\s+", " ", f"{title} {location} {description}".lower()).strip()
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _job(*, company: str, ats: str, slug: str, job_id: str, title: str, location: str, remote, url: str,
         posted: str, description: str, comp: str = "", department: str = "",
         countries: list[str] | None = None) -> dict:  # fmt: skip
    return {
        "company": company.strip(),
        "ats": ats,
        "slug": slug,
        "id": f"{ats}:{slug}:{job_id}",
        "title": _clip(title),
        "location": _clip(location, 200),
        "remote": remote,  # True / False / None (unknown)
        "countries": sorted({c for c in countries or [] if c}),  # structured, when the ATS gives it
        "url": (url or "").strip(),
        "posted": posted,
        "comp": comp,
        "department": department,
        "snippet": description[:400],
        "description": description[:8000],
        "desc_len": len(description),
        "hash": content_hash(title or "", description, location or ""),
        "source": ats,
    }


# ── pure parsers ──────────────────────────────────────────────────────────────
def parse_ashby(payload: dict, *, company: str, slug: str) -> list[dict]:
    out = []
    for j in (payload or {}).get("jobs") or []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]
        comp = ((j.get("compensation") or {}).get("scrapeableCompensationSalarySummary") or "").strip()
        # workplaceType is the truth when present: Ashby sets isRemote on Hybrid roles too
        wt = (j.get("workplaceType") or "").lower()
        remote = wt == "remote" if wt else bool(j.get("isRemote"))
        addrs = [j.get("address") or {}] + [s.get("address") or {} for s in j.get("secondaryLocations") or []]
        countries = [((a.get("postalAddress") or {}).get("addressCountry") or "") for a in addrs]
        out.append(
            _job(
                company=company,
                ats="ashby",
                slug=slug,
                job_id=j.get("id") or "",
                title=j.get("title") or "",
                location=" / ".join(x for x in locs if x),
                remote=remote,
                url=j.get("jobUrl") or "",
                posted=_iso(j.get("publishedAt")),
                description=_strip_html(j.get("descriptionHtml") or j.get("descriptionPlain") or ""),
                comp=comp,
                department=j.get("department") or "",
                countries=countries,
            )  # fmt: skip
        )
    return out


def parse_greenhouse(payload: dict, *, company: str, slug: str) -> list[dict]:
    out = []
    for j in (payload or {}).get("jobs") or []:
        loc = (j.get("location") or {}).get("name") or ""
        desc = _strip_html(j.get("content") or "")
        remote = True if re.search(r"\bremote\b", loc, re.I) else None
        out.append(
            _job(
                company=j.get("company_name") or company,
                ats="greenhouse",
                slug=slug,
                job_id=str(j.get("id") or ""),
                title=j.get("title") or "",
                location=loc,
                remote=remote,
                url=j.get("absolute_url") or "",
                # first_published, never updated_at — an edit bumps updated_at and fakes freshness
                posted=_iso(j.get("first_published")),
                description=desc,
                department=", ".join(d.get("name", "") for d in j.get("departments") or []),
            )  # fmt: skip
        )
    return out


def parse_lever(payload: list, *, company: str, slug: str) -> list[dict]:
    out = []
    for j in payload or []:
        if not isinstance(j, dict):
            continue
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        wt = (j.get("workplaceType") or "").lower()
        desc = " ".join(x for x in (j.get("descriptionPlain"), j.get("additionalPlain")) if x)
        rng = j.get("salaryRange") or {}
        comp = ""
        if rng.get("min") and rng.get("max"):
            comp = f"{rng.get('currency', '')} {rng['min']:,}–{rng['max']:,}".strip()
        out.append(
            _job(
                company=company,
                ats="lever",
                slug=slug,
                job_id=j.get("id") or "",
                title=j.get("text") or "",
                location=" / ".join(x for x in locs if x),
                remote=(wt == "remote") or None,
                url=j.get("hostedUrl") or "",
                posted=_iso(j.get("createdAt")),
                description=desc,
                comp=comp,
                department=cats.get("team") or "",
                countries=["United States" if (j.get("country") or "").upper() == "US" else (j.get("country") or "")],
            )  # fmt: skip
        )
    return out


_SR_COUNTRY = {"us": "United States"}


def _sr_description(detail: dict) -> str:
    sections = ((detail or {}).get("jobAd") or {}).get("sections") or {}
    # the role first; the company blurb every posting repeats goes last
    order = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")
    return " ".join(_strip_html((sections.get(k) or {}).get("text") or "") for k in order).strip()


def parse_smartrecruiters(payload: dict, *, company: str, slug: str, details: dict | None = None) -> list[dict]:
    """One page (or several, concatenated into ``content``) of SmartRecruiters postings. The list
    carries no description: ``details`` maps a posting id to its ``.../postings/{id}`` response,
    and a posting without one keeps ``desc_len`` None (unknown, so no "short description" flag)."""
    out = []
    for j in (payload or {}).get("content") or []:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        detail = (details or {}).get(str(j["id"])) or {}
        loc = j.get("location") or {}
        code = (loc.get("country") or "").lower()
        where = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"), code.upper()) if x)
        where = re.sub(r"(?:,\s*){2,}", ", ", where).strip(", ")  # "Dublin, , Ireland" (no region)
        if loc.get("remote") and "remote" not in where.lower():
            where = f"Remote - {where}" if where else "Remote"
        ident = (j.get("company") or {}).get("identifier") or slug
        desc = _sr_description(detail)
        job = _job(
            company=(j.get("company") or {}).get("name") or company,
            ats="smartrecruiters",
            slug=slug,
            job_id=str(j["id"]),
            title=j.get("name") or "",
            location=where,
            remote=bool(loc.get("remote")),  # hybrid and on-site both read False, as on Ashby
            url=detail.get("postingUrl") or SMARTRECRUITERS_JOB_URL.format(company=ident, id=j["id"]),
            posted=_iso(j.get("releasedDate")),
            description=desc,
            department=(j.get("department") or {}).get("label") or (j.get("function") or {}).get("label") or "",
            countries=[_SR_COUNTRY.get(code, code.upper())],
        )  # fmt: skip
        if not detail:
            job["desc_len"] = None
        out.append(job)
    return out


_WD_URL = re.compile(
    r"^(?:https?://)?(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:wday/cxs/[^/]+/)?(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_-]+)",
    re.I,
)
_WD_SLUG = re.compile(r"^([a-z0-9-]+)/(wd\d+)/([A-Za-z0-9_-]+)$", re.I)


def workday_slug(text: str) -> str:
    """A Workday board as the stored slug ``tenant/wdN/site``, from that slug itself or from any
    careers URL on it — ``https://nike.wd1.myworkdayjobs.com/nke``, the localized
    ``https://workday.wd5.myworkdayjobs.com/en-US/Workday``, a job link, or the API path. "" when
    it isn't one (a ``myworkdaysite.com`` link names its tenant differently and isn't read)."""
    text = (text or "").strip()
    m = _WD_SLUG.match(text)
    if m:
        return f"{m.group(1).lower()}/{m.group(2).lower()}/{m.group(3)}"
    m = _WD_URL.match(text)
    if not m or m.group("site").lower() in ("wday", "job"):
        return ""
    return f"{m.group('tenant').lower()}/{m.group('wd').lower()}/{m.group('site')}"


def _wd_parts(slug: str) -> tuple[str, str, str]:
    norm = workday_slug(slug)
    if not norm:
        raise ValueError(f"a Workday slug is tenant/wdN/site (like nike/wd1/nke) or a careers URL, not {slug!r}")
    tenant, wd, site = norm.split("/")
    return tenant, wd, site


def board_from_url(text: str) -> tuple[str, str]:
    """``(ats, slug)`` for a pasted careers-board URL on a supported ATS, else ``("", "")``."""
    text = (text or "").strip()
    if "myworkdayjobs.com" in text.lower():
        wd = workday_slug(text)
        return ("workday", wd) if wd else ("", "")
    patterns = (
        ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
        ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
        ("greenhouse", r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
        ("lever", r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    )
    for ats, pat in patterns:
        if m := re.search(r"^(?:https?://)?" + pat, text, re.I):
            return ats, m.group(1)
    return "", ""


_WD_AGO = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


def workday_posted(text: str, now: datetime | None = None) -> str:
    """Workday's posted age ("Posted Today", "Posted Yesterday", "Posted 3 Days Ago", "Posted 30+
    Days Ago") as an approximate ISO date-time. "30+" is a floor, so it dates the posting 30 days
    back: old enough to fall out of a 30-day filter, which it should. "" when unreadable."""
    m = _WD_AGO.search(text or "")
    if not m:
        return ""
    word = m.group(1).lower()
    days = 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2))
    return ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat(timespec="seconds")


def _wd_remote(remote_type: str, location: str):
    rt = (remote_type or "").strip().lower()
    if rt:
        return rt == "remote"  # "Flex" / "Hybrid" / "Onsite" are not remote
    return True if re.search(r"\bremote\b", location or "", re.I) else None


def parse_workday(payload: dict, *, company: str, slug: str, details: dict | None = None,
                  now: datetime | None = None) -> list[dict]:  # fmt: skip
    """Workday ``jobPostings`` (pages concatenated). ``details`` maps an ``externalPath`` to that
    posting's detail response, which carries what the list lacks: the description, every location,
    the country and a real start date. Without one, a posting keeps the list's summary — a "14
    Locations" count reads as the primary location named in the job path instead."""
    tenant, wd, site = _wd_parts(slug)
    out = []
    for j in (payload or {}).get("jobPostings") or []:
        path = (j or {}).get("externalPath") or ""
        if not path:
            continue
        info = ((details or {}).get(path) or {}).get("jobPostingInfo") or {}
        if info:
            locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
            location = " / ".join(x for x in locs if x)
            country = ((info.get("jobRequisitionLocation") or {}).get("country") or info.get("country") or {}).get(
                "descriptor"
            )
            # one country stands for the posting only when it has one location; a role listed in
            # several places may be open in countries the primary one doesn't name
            countries = [country or ""] if not info.get("additionalLocations") else []
            posted = _iso(info.get("startDate")) or workday_posted(info.get("postedOn") or j.get("postedOn"), now)
        else:
            location = j.get("locationsText") or ""
            if re.fullmatch(r"\d+\s+locations?", location.strip(), re.I):
                seg = path.split("/")[2] if path.count("/") >= 3 else ""
                location = f"{seg.replace('-', ' ')} (+{int(location.split()[0]) - 1} more)" if seg else location
            countries = []
            posted = workday_posted(j.get("postedOn"), now)
        desc = _strip_html(info.get("jobDescription") or "")
        job = _job(
            company=company,
            ats="workday",
            slug=slug,
            job_id=path.rsplit("/", 1)[-1],
            title=info.get("title") or j.get("title") or "",
            location=location,
            remote=_wd_remote(info.get("remoteType") or j.get("remoteType") or "", location),
            url=info.get("externalUrl") or f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
            posted=posted,
            description=desc,
            countries=countries,
        )  # fmt: skip
        if not info:
            job["desc_len"] = None
        out.append(job)
    return out


PARSERS = {
    "ashby": parse_ashby,
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "smartrecruiters": parse_smartrecruiters,
    "workday": parse_workday,
}
URLS = {
    "ashby": ASHBY_URL,
    "greenhouse": GREENHOUSE_URL,
    "lever": LEVER_URL,
    "smartrecruiters": SMARTRECRUITERS_URL,
    "workday": WORKDAY_URL,
}


# ── filters ───────────────────────────────────────────────────────────────────
_US = re.compile(
    r"\b(us|usa|u\.s\.|united states|north america|americas|anywhere|worldwide|global|"
    r"san francisco|sf|bay area|new york|nyc|seattle|austin|boston|chicago|los angeles|denver|portland|"
    r"atlanta|washington|miami|san diego|san jose|palo alto|mountain view|"
    r"west coast|east coast|(?<!asia )(?<!asia-)(?:pacific|eastern|central)"
    r"(?!\s*(?:(?:&|and)\s*\w+\s*)?(?:europe|asia|apac|africa|america|/|-\s*apac))|"
    r"mountain time|pst|pdt|pt|est|edt|et|cst|cdt|ct|mst|mt|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    r"georgia(?!\s*\(country\))|hawaii|idaho|"
    r"illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|north carolina|"
    r"north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|"
    r"texas|utah|vermont|virginia|west virginia|wisconsin|wyoming)\b",
    re.I,
)
# A US state as written after a city ("Portland, OR") — upper-case, after a word and a comma only, so
# "in"/"or"/"me" never read as states and a bare "Remote - DE" (Germany) isn't Delaware.
_US_STATE = re.compile(
    r"[A-Za-z.]{3,},\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|"
    r"NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
)
# What's left of a location once the remote/workplace words are gone — if it names anything, it
# names a place, and that place must be in the US for a role to count as US-remote.
_REMOTE_NOISE = re.compile(
    r"\b(remote|fully|hybrid|on-?site|in-office|first|friendly|optional|only|or|and|any|location|locations)\b", re.I
)
_US_COUNTRIES = {"united states", "us", "usa", "united states of america"}


def _phrases(roles: str) -> list[list[str]]:
    """Target roles ("Forward Deployed Engineer, AI Engineer") → word lists per role."""
    return [ws for ws in (re.findall(r"[a-z0-9]+", p.lower()) for p in re.split(r"[,;\n]", roles or "")) if ws]


# Abbreviations a title uses for a role spelled out in target roles (and vice versa).
_ALIASES = {
    "fde": ("forward", "deployed", "engineer"),
    "mts": ("member", "technical", "staff"),
    "swe": ("software", "engineer"),
    "ml": ("machine", "learning"),
}


def _title_words(title: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    for short, long in _ALIASES.items():
        if short in words:
            words.update(long)
        if all(w in words for w in long):
            words.add(short)
    return words


def title_matches(title: str, roles: str, exclude: str = "") -> bool:
    """True when every word of at least one target role appears in the title ("Design Engineer"
    matches "Senior Design Systems Engineer", not "Product Designer"), and no exclude word does.
    An empty ``roles`` matches every title. Common abbreviations count both ways: "FDE" = "Forward
    Deployed Engineer", "MTS" = "Member of Technical Staff", "SWE", "ML"."""
    words = _title_words(title)
    for bad in _phrases(exclude):
        if all(w in words for w in bad):
            return False
    phrases = _phrases(roles)
    return not phrases or any(all(w in words for w in p) for p in phrases)


def location_matches(job: dict, locations: str) -> bool:
    """``locations`` is a comma list of places plus the special term ``remote`` (= remote and
    open to the US). Empty matches everything. A remote role counts when its structured country
    list includes the US, or its text says US (or a US state/city), or it names no place at all
    ("Remote"); "Remote - EMEA", "Remote, Sweden", "Remote - Latin America" don't. A place term
    matches as whole words in the location ("portland, or" to rule out Portland, ME)."""
    terms = location_terms(locations)
    if not terms:
        return True
    raw = job.get("location") or ""
    loc = raw.lower()
    for t in terms:
        if t in ("remote", "remote-us", "remote us"):
            if not (job.get("remote") is True or "remote" in loc):
                continue
            countries = [c.lower() for c in job.get("countries") or []]
            if countries:  # structured beats text: a remote role must be open in the US
                if any(c in _US_COUNTRIES for c in countries):
                    return True
            elif (_US.search(loc) or _US_STATE.search(raw)) and not re.search(r"\btbilisi\b", loc):
                return True
            elif not re.search(r"[a-z]", _REMOTE_NOISE.sub(" ", loc)):
                return True  # plain "Remote" — no place named
            # anything else names a non-US place ("Remote, Sweden", "Remote - Latin America")
        elif _place_matches(t, raw):
            return True
    return False


_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
    "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}  # fmt: skip
# City shorthands people put in a location list — a term of their own (never a state glued onto the
# term before it), matched by what they stand for.
_CITY_SHORT = {
    "sf": ("san francisco", "bay area", "sf"),
    "la": ("los angeles",),
    "ny": ("new york", "nyc", "ny"),
    "nyc": ("new york", "nyc"),
    "dc": ("washington, dc", "washington dc", "district of columbia"),
    "atx": ("austin",),
    "pdx": ("portland",),
    "sea": ("seattle",),
    "chi": ("chicago",),
    "bos": ("boston",),
}
# Canadian provinces — glued onto a city like a state, so "toronto, on" never leaves a bare "on".
_PROVINCES = {"on", "bc", "qc", "ab", "ns", "mb", "sk", "nb", "nl", "pe"}


def _word(text: str, needle: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", text))


def _states_named(raw: str) -> set[str]:
    """US state codes a location names — ", OR" style codes and spelled-out names."""
    found = {m.group(1).lower() for m in _US_STATE.finditer(raw)}
    low = raw.lower()
    # two-word names first, cut out, so "virginia" isn't found inside "west virginia"
    for code, name in sorted(_STATES.items(), key=lambda kv: -len(kv[1])):
        if _word(low, name):
            found.add(code)
            low = re.sub(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", " ", low)
    return found


def _place_matches(term: str, raw: str) -> bool:
    """One place term against a posting location. "portland, or" = the city by whole word, and if
    the location names a state it must be Oregon (so "Portland, Oregon" and "Portland OR" match,
    "Portland, ME" doesn't). A bare state code ("or") = that state named in the location. Anything
    else = whole words."""
    low = raw.lower()
    if term in _CITY_SHORT:
        return any(_word(low, alias) for alias in _CITY_SHORT[term])
    if ", " in term:
        city, code = term.rsplit(", ", 1)
        if code in _PROVINCES:
            return _word(low, city) and (_word(low, code) or "canada" in low or len(code) != 2)
        if not _word(low, city):
            return False
        named = _states_named(raw)
        if not named and re.search(rf"\b{code}\b", raw, re.I) and len(code) == 2:
            named = {code}  # "Portland OR" with no comma
        return not named or code in named
    if term in _STATES:
        return term in _states_named(raw)
    return _word(low, term)


def location_terms(locations: str) -> list[str]:
    """Split the ``locations`` setting into terms. Commas separate terms, but a US state code
    belongs to the city before it — "remote, portland, or" is ["remote", "portland, or"], not a bare
    "or" that would match New Y-or-k. City shorthands ("sf, la, nyc") stay separate terms."""
    out: list[str] = []
    for piece in (re.sub(r"\s+", " ", p).strip().lower() for p in (locations or "").split(",")):
        if not piece:
            continue
        m = re.fullmatch(r"(.{3,}) ([a-z]{2})", piece)  # "portland or" typed without the comma
        if m and m.group(2) in _STATES:
            piece = f"{m.group(1)}, {m.group(2)}"
        prev = out[-1] if out else ""
        glue = (
            (piece in _STATES and piece not in _CITY_SHORT)
            or piece in _PROVINCES
            or (piece == "dc" and prev == "washington")
        )
        if (
            glue
            and prev
            and ", " not in prev
            and prev not in ("remote", "remote-us", "remote us")
            and prev not in _CITY_SHORT
            and prev not in _STATES
        ):
            out[-1] = "dc" if piece == "dc" else f"{prev}, {piece}"  # "washington, dc" → the dc shorthand
        else:
            out.append(piece)
    return out


def passes(job: dict, *, roles: str = "", exclude: str = "", locations: str = "", max_age_days: float = 0,
           now: datetime | None = None) -> bool:  # fmt: skip
    """The whole filter: title, location, and age (``max_age_days`` 0 = any age; a posting with
    no date passes the age check rather than vanishing)."""
    if not title_matches(job.get("title", ""), roles, exclude):
        return False
    if not location_matches(job, locations):
        return False
    if max_age_days:
        age = age_days(job.get("posted", ""), now)
        if age is not None and age > max_age_days:
            return False
    return True


# ── network ───────────────────────────────────────────────────────────────────
class BoardNotFound(LookupError):
    pass


def search_terms(search) -> list[str]:
    """``search`` as the searches to run: a comma/semicolon/newline list of roles ("AI Engineer,
    Frontend Engineer") or a list of exact titles (a title may itself hold a comma)."""
    if isinstance(search, str):
        parts = re.split(r"[,;\n]", search)
    else:
        parts = list(search or [])
    seen, out = set(), []
    for p in (re.sub(r"\s+", " ", str(x)).strip() for x in parts):
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def _detail_worthy(title: str, terms: list[str]) -> bool:
    # every word of one search term in the title: the same test ``passes`` applies to target roles
    return bool(terms) and title_matches(title, ", ".join(t.replace(",", " ") for t in terms))


async def _pages(fetch_page, page: int, cap: int) -> list[dict]:
    """Page until a short page, the reported total, or ``cap``. A failure past the first page keeps
    what came back (a rate limit mid-board shouldn't cost the roles already read)."""
    rows: list[dict] = []
    offset = 0
    while offset < cap:
        try:
            batch, total = await fetch_page(offset)
        except Exception:
            if not offset:
                raise
            break
        rows += batch
        offset += page
        # Workday reports the total on the first page only (later pages say 0)
        if len(batch) < page or (total and offset >= total):
            break
    return rows[:cap]


async def _fetch_smartrecruiters(client, slug: str, company: str, terms: list[str]) -> list[dict]:
    base = SMARTRECRUITERS_URL.format(slug=slug)

    def pager(q: str):
        async def page(offset: int):
            params = {"limit": SR_PAGE, "offset": offset}
            if q:
                params["q"] = q
            resp = await client.get(base, params=params)
            if resp.status_code == 404:
                raise BoardNotFound(f"no smartrecruiters board named {slug!r}")
            resp.raise_for_status()
            data = resp.json() or {}
            return data.get("content") or [], data.get("totalFound") or 0

        return page

    rows: dict[str, dict] = {}
    for q in terms or [""]:
        for r in await _pages(pager(q), SR_PAGE, MAX_POSTINGS):
            rows.setdefault(str(r.get("id")), r)
    want = [i for i, r in rows.items() if _detail_worthy(r.get("name") or "", terms)][:MAX_DETAILS]

    async def detail(pid: str):
        try:
            resp = await client.get(f"{base}/{pid}")
            return pid, resp.json() if resp.status_code == 200 else None
        except Exception:  # noqa: BLE001 — a posting without its text is still a posting
            return pid, None

    sem = asyncio.Semaphore(5)

    async def bounded(pid: str):
        async with sem:
            return await detail(pid)

    details = {pid: d for pid, d in await asyncio.gather(*(bounded(p) for p in want)) if d}
    return parse_smartrecruiters({"content": list(rows.values())}, company=company, slug=slug, details=details)


async def _fetch_workday(client, slug: str, company: str, terms: list[str]) -> list[dict]:
    tenant, wd, site = _wd_parts(slug)
    base = WORKDAY_URL.format(tenant=tenant, wd=wd, site=site)
    slug = f"{tenant}/{wd}/{site}"

    def pager(q: str):
        async def page(offset: int):
            body = {"appliedFacets": {}, "limit": WD_PAGE, "offset": offset, "searchText": q}
            resp = await client.post(base, json=body, headers={"Accept": "application/json"})
            if resp.status_code in (404, 422) or (resp.status_code == 400 and not offset):
                # an unknown tenant or site: Workday answers 404/422, a bad site name sometimes 400
                raise BoardNotFound(f"no workday board at {slug!r}")
            resp.raise_for_status()
            data = resp.json() or {}
            return data.get("jobPostings") or [], data.get("total") or 0

        return page

    rows: dict[str, dict] = {}
    for q in terms or [""]:
        for r in await _pages(pager(q), WD_PAGE, MAX_POSTINGS):
            if r.get("externalPath"):
                rows.setdefault(r["externalPath"], r)
    want = [p for p, r in rows.items() if _detail_worthy(r.get("title") or "", terms)][:MAX_DETAILS]
    root = base.rsplit("/jobs", 1)[0]
    details: dict[str, dict] = {}
    # one at a time: Workday's edge rate-limits bursts from one client hard
    for path in want:
        try:
            resp = await client.get(root + path, headers={"Accept": "application/json"})
        except Exception:  # noqa: BLE001
            break
        if resp.status_code == 429:
            break  # the rest keep their list summary
        if resp.status_code == 200:
            details[path] = resp.json()
    return parse_workday({"jobPostings": list(rows.values())}, company=company, slug=slug, details=details)


async def fetch_board(ats: str, slug: str, company: str = "", *, client=None, search="") -> list[dict]:
    """Every open role on one company's board, parsed. Raises ``BoardNotFound`` on a 404 (the
    company isn't on that ATS, or the slug is wrong — Ashby slugs are case-sensitive).

    ``search`` (target roles as a comma list, or a list of exact titles) narrows the boards that
    search — SmartRecruiters and Workday: one search per term, each capped at ``MAX_POSTINGS``, and
    only title matches get their description read. Unsearched, those two return the first
    ``MAX_POSTINGS`` postings without descriptions. Ashby / Greenhouse / Lever
    always return the whole board."""
    import httpx

    if ats not in URLS:
        raise ValueError(f"unknown ATS {ats!r} (use one of {', '.join(ATS)})")
    own = client is None
    client = client or httpx.AsyncClient(timeout=25.0, follow_redirects=True)
    try:
        if ats == "smartrecruiters":
            return await _fetch_smartrecruiters(client, slug, company or slug, search_terms(search))
        if ats == "workday":
            return await _fetch_workday(client, slug, company or slug, search_terms(search))
        resp = await client.get(URLS[ats].format(slug=slug))
        if resp.status_code == 404:
            raise BoardNotFound(f"no {ats} board named {slug!r}")
        resp.raise_for_status()
        data = resp.json()
    finally:
        if own:
            await client.aclose()
    # an empty Ashby board answers 200 with {"jobs": []}; Lever a bare [] — both are real boards
    return PARSERS[ats](data, company=company or slug, slug=slug)


def slug_candidates(company: str) -> list[str]:
    """Likely board slugs for a company name: "Scale AI" → scaleai, scale, scale-ai, ScaleAI."""
    raw = re.findall(r"[A-Za-z0-9]+", company.replace(".", ""))
    if not raw:
        return []
    from ..record.companies import _GENERIC, _LEGAL, _key

    # the loose key ("Scale AI" → scale) is a guess; probe() checks a board found that way names
    # the right company before trusting it
    base = re.sub(r"\.(com|ai|io|co|dev|app|org|so)$", "", company.strip(), flags=re.I)  # "Owner.com" → owner
    braw = re.findall(r"[A-Za-z0-9]+", base.replace(".", "")) or raw
    joined = "".join(braw).lower()
    # the suffix-stripped name, unfolded — a plural-folded guess would probe misspelled slugs
    out = ["".join(raw).lower(), joined, _key(base, _LEGAL | _GENERIC), "-".join(braw).lower(), "".join(braw)]
    # boards often carry a suffix the company name doesn't ("Arize" posts as arizeai, Glean as
    # gleanwork) — guesses, so probe() name-checks what they find
    out += [f"{joined}{suffix}" for suffix in ("ai", "hq", "labs", "work", "inc") if not joined.endswith(suffix)]
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


NAMED = ("greenhouse", "smartrecruiters")  # boards whose postings say whose board it is


def _board_name_ok(company: str, jobs: list[dict]) -> bool:
    """Greenhouse and SmartRecruiters name the company on every posting; a board found under a
    shortened slug must name the company we asked for ("Mercury Systems" must not land on the
    Mercury fintech board)."""
    from ..record.companies import company_key

    names = {j.get("company", "") for j in jobs if j.get("ats") in NAMED}
    if not names:
        return True  # Ashby / Lever don't say; trust the slug
    want = company_key(company)
    # exact, or the asked-for name inside the board's ("Grafana" → "Grafana Labs") — never the
    # reverse, which is how "Mercury Systems" would land on "Mercury"
    return any(company_key(n) == want or company.lower() in n.lower() for n in names)


async def probe(company: str, slug: str = "", *, client=None, sem=None) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Find which ATS boards a company posts on: tries every (ATS, slug candidate) on the boards a
    name can find (``PROBED`` — not Workday, whose site name no guess reaches). Returns
    ``(found, problems)`` — ``found`` is ``(ats, slug, open_roles)`` for each board that answered
    with roles and names the right company (most roles first); ``problems`` notes boards rejected
    for naming another company and any error that wasn't a plain "no such board" (a 429 or timeout
    must not read as "not found")."""
    import httpx

    slugs = [slug] if slug else slug_candidates(company)
    full = {"".join(re.findall(r"[A-Za-z0-9]+", company.replace(".", " "))).lower()}
    loose = {s for s in slugs if s.lower() not in full and "-" not in s}
    hits: list[tuple[str, str, int]] = []
    problems: list[str] = []
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    sem = sem or asyncio.Semaphore(10)

    async def _try(ats: str, s: str):
        async with sem:
            try:
                return ats, s, await fetch_board(ats, s, company, client=client), ""
            except BoardNotFound:
                return ats, s, None, ""
            except Exception as e:  # noqa: BLE001
                return ats, s, None, f"{ats}/{s}: {type(e).__name__}: {e}"[:200]

    try:
        results = await asyncio.gather(*(_try(a, s) for a in PROBED for s in slugs))
    finally:
        if own:
            await client.aclose()
    for ats, s, jobs, err in results:
        if err:
            problems.append(err)
        if not jobs or any(h[0] == ats for h in hits):
            continue
        if not slug and s in loose:
            # found under a SHORTENED name: Greenhouse and SmartRecruiters say whose board it is —
            # check; Ashby and Lever don't, so say it's unverified rather than trust it silently
            if not _board_name_ok(company, jobs):
                problems.append(f"{ats}/{s} is {jobs[0].get('company')!r}'s board, not {company!r}")
                continue
            if ats not in NAMED:
                problems.append(f"{ats}/{s} matched by the short name {s!r} — confirm it's {company}'s board")
        hits.append((ats, s, len(jobs)))
    # an exact-name board beats a guessed one, whatever their size; then the board with most roles
    return sorted(hits, key=lambda h: (h[1].lower() in loose, -h[2])), problems
