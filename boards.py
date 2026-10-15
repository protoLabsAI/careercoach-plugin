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
(the target roles), location (remote-US / named places) and age. Parsers + filters are pure and
unit-tested; only ``fetch_board`` / ``probe`` touch the network (httpx, a host dependency).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime

ATS = ("ashby", "greenhouse", "lever")

ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
HOSTS = ("api.ashbyhq.com", "boards-api.greenhouse.io", "api.lever.co")


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
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC).isoformat(timespec="seconds")
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


PARSERS = {"ashby": parse_ashby, "greenhouse": parse_greenhouse, "lever": parse_lever}
URLS = {"ashby": ASHBY_URL, "greenhouse": GREENHOUSE_URL, "lever": LEVER_URL}


# ── filters ───────────────────────────────────────────────────────────────────
_US = re.compile(
    r"\b(us|usa|u\.s\.|united states|north america|americas|anywhere|worldwide|global|"
    r"san francisco|sf|bay area|new york|nyc|seattle|austin|boston|chicago|los angeles|denver|portland|"
    r"atlanta|washington|miami|san diego|san jose|palo alto|mountain view|"
    r"west coast|east coast|pacific|eastern|central|mountain time|pst|pdt|pt|est|edt|et|cst|cdt|ct|mst|mt|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
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
            elif _US.search(loc) or _US_STATE.search(raw):
                return True
            elif not re.search(r"[a-z]", _REMOTE_NOISE.sub(" ", loc)):
                return True  # plain "Remote" — no place named
            # anything else names a non-US place ("Remote, Sweden", "Remote - Latin America")
        elif re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", loc):
            return True
    return False


def location_terms(locations: str) -> list[str]:
    """Split the ``locations`` setting into terms. Commas separate terms, but a 2-letter piece
    belongs to the place before it — "remote, portland, or" is ["remote", "portland, or"], not a
    bare "or" that would match New Y-or-k and Singap-or-e."""
    out: list[str] = []
    for piece in (p.strip().lower() for p in (locations or "").split(",")):
        if not piece:
            continue
        if len(piece) == 2 and piece.isalpha() and out and out[-1] not in ("remote", "remote-us", "remote us"):
            out[-1] = f"{out[-1]}, {piece}"
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


async def fetch_board(ats: str, slug: str, company: str = "", *, client=None) -> list[dict]:
    """Every open role on one company's board, parsed. Raises ``BoardNotFound`` on a 404 (the
    company isn't on that ATS, or the slug is wrong — Ashby slugs are case-sensitive)."""
    import httpx

    if ats not in URLS:
        raise ValueError(f"unknown ATS {ats!r} (use one of {', '.join(ATS)})")
    own = client is None
    client = client or httpx.AsyncClient(timeout=25.0, follow_redirects=True)
    try:
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
    from .companies import company_hint_key

    # the loose key ("Scale AI" → scale) is a guess; probe() checks a board found that way names
    # the right company before trusting it
    base = re.sub(r"\.(com|ai|io|co|dev|app|org|so)$", "", company.strip(), flags=re.I)  # "Owner.com" → owner
    braw = re.findall(r"[A-Za-z0-9]+", base.replace(".", "")) or raw
    joined = "".join(braw).lower()
    out = ["".join(raw).lower(), joined, company_hint_key(base), "-".join(braw).lower(), "".join(braw)]
    # boards often carry a suffix the company name doesn't ("Arize" posts as arizeai, Glean as
    # gleanwork) — guesses, so probe() name-checks what they find
    out += [f"{joined}{suffix}" for suffix in ("ai", "hq", "labs", "work", "inc")]
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _board_name_ok(company: str, jobs: list[dict]) -> bool:
    """Greenhouse names the company on every posting; a board found under a shortened slug must name
    the company we asked for ("Mercury Systems" must not land on the Mercury fintech board)."""
    from .companies import company_key

    names = {j.get("company", "") for j in jobs if j.get("ats") == "greenhouse"}
    if not names:
        return True  # Ashby / Lever don't say; trust the slug
    want = company_key(company)
    # exact, or the asked-for name inside the board's ("Grafana" → "Grafana Labs") — never the
    # reverse, which is how "Mercury Systems" would land on "Mercury"
    return any(company_key(n) == want or company.lower() in n.lower() for n in names)


async def probe(company: str, slug: str = "", *, client=None, sem=None) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Find which ATS boards a company posts on: tries every (ATS, slug candidate). Returns
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
        results = await asyncio.gather(*(_try(a, s) for a in ATS for s in slugs))
    finally:
        if own:
            await client.aclose()
    for ats, s, jobs, err in results:
        if err:
            problems.append(err)
        if not jobs or any(h[0] == ats for h in hits):
            continue
        if not slug and s in loose:
            # found under a SHORTENED name: Greenhouse says whose board it is — check; Ashby and
            # Lever don't, so say it's unverified rather than trust it silently
            if not _board_name_ok(company, jobs):
                problems.append(f"{ats}/{s} is {jobs[0].get('company')!r}'s board, not {company!r}")
                continue
            if ats != "greenhouse":
                problems.append(f"{ats}/{s} matched by the short name {s!r} — confirm it's {company}'s board")
        hits.append((ats, s, len(jobs)))
    return sorted(hits, key=lambda h: -h[2]), problems
