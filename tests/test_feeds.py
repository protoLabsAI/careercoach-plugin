"""The keyless feeds (Jobicy, We Work Remotely, Remote OK): parsing, the US-remote filter, the cache.

Fixtures are trimmed copies of what each board sent on 2026-11-18. No live network:
``httpx.AsyncClient`` is swapped for one on an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest

_ASYNC_CLIENT = httpx.AsyncClient  # the real class, before any fixture swaps it

JOBICY = {
    "jobs": [
        {
            "jobTitle": "Staff Software Engineer, AI-Native Systems",
            "companyName": "Wheel",
            "jobGeo": "USA",
            "pubDate": "2026-11-18T13:57:28+00:00",
            "url": "https://jobicy.com/jobs/163138-staff-software-engineer-ai-native-systems-2",
            "jobDescription": "<p>Build <strong>agent</strong> systems &amp; tooling.</p>",
            "salaryMin": "185725",
            "salaryMax": "264500",
            "salaryCurrency": "USD",
            "salaryPeriod": "yearly",
        },
        {
            "jobTitle": "Full-Stack Engineer - ElevenMusic",
            "companyName": "Elevenlabs",
            "jobGeo": "Denmark,  France,  Netherlands,  Poland,  UK,  USA",
            "pubDate": "2026-11-18T12:43:16+00:00",
            "url": "https://jobicy.com/jobs/163113-full-stack-engineer-elevenmusic",
            "jobExcerpt": "Music product.",
            "salaryMin": None,
            "salaryMax": None,
            "salaryCurrency": None,
        },
        {
            "jobTitle": "Frontend Engineer",
            "companyName": "Berlinco",
            "jobGeo": "Germany",
            "pubDate": "2026-11-17T10:00:00+00:00",
            "url": "https://jobicy.com/jobs/1-frontend-engineer",
            "salaryMin": "50",
            "salaryMax": "70",
            "salaryCurrency": "EUR",
            "salaryPeriod": "hourly",
        },
        {
            "jobTitle": "Design Engineer",
            "companyName": "Everywhere Inc",
            "jobGeo": "Anywhere",
            "pubDate": "2026-11-16T10:00:00+00:00",
            "url": "https://jobicy.com/jobs/2-design-engineer",
        },
    ]
}

WWR = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>We Work Remotely</title>
<item>
  <title>Volga Partners: AI/ML Engineer - Production AI &amp; LLM Systems (India)</title>
  <region>Asia Only</region>
  <pubDate>Wed, 18 Nov 2026 11:31:31 +0000</pubDate>
  <description>&lt;p&gt;&lt;strong&gt;Headquarters:&lt;/strong&gt; Pune&lt;/p&gt;&lt;p&gt;Ship LLM systems.&lt;/p&gt;</description>
  <guid>https://weworkremotely.com/remote-jobs/volga-partners-ai-ml-engineer</guid>
  <link>https://weworkremotely.com/remote-jobs/volga-partners-ai-ml-engineer</link>
</item>
<item>
  <title>Acme: Senior Frontend Engineer</title>
  <region>USA Only</region>
  <pubDate>Tue, 17 Nov 2026 09:00:00 +0000</pubDate>
  <description><![CDATA[<p>React &amp; design systems.</p>]]></description>
  <link>https://weworkremotely.com/remote-jobs/acme-senior-frontend-engineer</link>
</item>
<item>
  <title>Globex: Design Engineer</title>
  <region>Anywhere in the World</region>
  <pubDate>not a date</pubDate>
  <link>https://weworkremotely.com/remote-jobs/globex-design-engineer</link>
</item>
<item>
  <title>Platform Engineer with no company prefix</title>
  <region>Europe Only</region>
  <link>https://weworkremotely.com/remote-jobs/platform-engineer</link>
</item>
</channel></rss>"""

REMOTEOK = [
    {"last_updated": "1795017649", "legal": "API Terms of Service: Please link back …"},
    {
        "position": "AI Engineer",
        "company": "Clera",
        "location": "",
        "date": "2026-11-17T00:00:29+00:00",
        "salary_min": "0",
        "salary_max": "0",
        "description": "<p>Agents.</p>",
        "url": "https://remoteOK.com/remote-jobs/remote-ai-engineer-clera-1",
    },
    {
        "position": "Scaled Account Manager EMEA",
        "company": "DoiT",
        "location": "Estonia",
        "epoch": "1794556815",
        "salary_min": "90000",
        "salary_max": "120000",
        "url": "https://remoteOK.com/remote-jobs/remote-scaled-account-manager-emea-doit-2",
    },
    {
        "position": "Design Systems Engineer",
        "company": "PatientPoint",
        "location": "Remote",
        "date": "2026-11-15T00:00:00+00:00",
        "url": "https://remoteOK.com/remote-jobs/remote-design-systems-engineer-3",
    },
]

US_REMOTE = "remote, portland, or"


@pytest.fixture
def mods(plugin):
    class M:
        pass

    m = M()
    for name in ("feeds", "boards", "jobsource"):
        setattr(m, name, importlib.import_module(f"{plugin.__name__}.search.{name}"))
    m.feeds._CACHE.clear()
    yield m
    m.feeds._CACHE.clear()


def _serve(monkeypatch, fail: set[str] | None = None):
    """Answer the three boards from the fixtures; hosts in ``fail`` answer 503. Returns the request log."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host = request.url.host
        if host in (fail or set()):
            return httpx.Response(503)
        if host == "jobicy.com":
            return httpx.Response(200, json=JOBICY)
        if host == "weworkremotely.com":
            return httpx.Response(200, text=WWR)
        if host == "remoteok.com":
            return httpx.Response(200, json=REMOTEOK)
        raise AssertionError(f"unexpected host {host}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return seen


# ── parsers ───────────────────────────────────────────────────────────────────
def test_jobicy_carries_countries_pay_and_plain_text(mods):
    wheel, eleven, berlin, anywhere = mods.feeds.parse_jobicy(JOBICY)
    assert (wheel["company"], wheel["source"], wheel["remote"]) == ("Wheel", "jobicy", True)
    assert wheel["location"] == "Remote (USA)" and wheel["countries"] == ["USA"]
    assert wheel["comp"] == "USD 185,725–264,500" and wheel["posted"].startswith("2026-11-18T13:57")
    assert wheel["description"] == "Build agent systems & tooling."
    assert wheel["url"].startswith("https://jobicy.com/jobs/")  # the board's own link: its terms ask for it
    assert "comp" not in eleven and eleven["countries"][-1] == "USA" and eleven["description"] == "Music product."
    assert berlin["comp"] == "EUR 50–70 per hour"
    assert anywhere["location"] == "Remote (Anywhere)" and "countries" not in anywhere


def test_weworkremotely_splits_company_from_role_and_reads_the_region(mods):
    volga, acme, globex, bare = mods.feeds.parse_weworkremotely(WWR)
    assert (volga["company"], volga["title"]) == (
        "Volga Partners",
        "AI/ML Engineer - Production AI & LLM Systems (India)",
    )
    assert volga["location"] == "Remote (Asia Only)" and "countries" not in volga
    assert volga["description"] == "Headquarters: Pune Ship LLM systems." and volga["posted"].startswith(
        "2026-11-18T11:31"
    )
    assert acme["countries"] == ["usa"] and acme["description"] == "React & design systems."
    assert globex["posted"] == "" and globex["location"] == "Remote (Anywhere in the World)"
    assert (bare["company"], bare["title"]) == ("", "Platform Engineer with no company prefix")


def test_remoteok_skips_its_legal_row_and_reads_pay_as_dollars(mods):
    clera, doit, patient = mods.feeds.parse_remoteok(REMOTEOK)
    assert (clera["title"], clera["location"]) == ("AI Engineer", "Remote") and "comp" not in clera  # 0–0 is no pay
    assert doit["location"] == "Remote (Estonia)" and doit["comp"] == "USD 90,000–120,000"
    assert doit["posted"].startswith("2026-11-1")  # from the epoch when there is no date
    assert patient["location"] == "Remote"
    mangled = {"position": "ML Engineer \u00e2\u0080\u0094 Task Design", "company": "Café Zürich", "url": "https://x/1"}
    job = mods.feeds.parse_remoteok([mangled])[0]
    assert job["title"] == "ML Engineer — Task Design"  # Remote OK's Latin-1 mojibake, undone
    assert job["company"] == "Café Zürich"  # text that was never mangled is left alone


def test_only_roles_open_to_the_us_pass_the_remote_filter(mods):
    """The watch keeps a role only if `locations` allows it: a feed's region must decide that."""
    ok = lambda job: mods.boards.location_matches(job, US_REMOTE)  # noqa: E731
    wheel, eleven, berlin, anywhere = mods.feeds.parse_jobicy(JOBICY)
    assert [ok(wheel), ok(eleven), ok(berlin), ok(anywhere)] == [True, True, False, True]
    volga, acme, globex, bare = mods.feeds.parse_weworkremotely(WWR)
    assert [ok(volga), ok(acme), ok(globex), ok(bare)] == [False, True, True, False]
    clera, doit, patient = mods.feeds.parse_remoteok(REMOTEOK)
    assert [ok(clera), ok(doit), ok(patient)] == [True, False, True]


# ── fetch: one download an hour, however many searches ask ────────────────────
def test_a_feed_is_downloaded_once_and_served_from_memory_for_an_hour(mods, monkeypatch):
    seen = _serve(monkeypatch)
    first = asyncio.run(mods.feeds.fetch("remoteok", now=1000.0))
    first[0]["id"] = "stamped by a caller"
    again = asyncio.run(mods.feeds.fetch("remoteok", now=1000.0 + 3599))
    assert len(seen) == 1 and "id" not in again[0]  # cached, and a caller's edits don't leak
    asyncio.run(mods.feeds.fetch("remoteok", now=1000.0 + 3600))
    assert len(seen) == 2  # an hour on, it is read again


def test_a_feed_reads_every_category_and_drops_repeats(mods, monkeypatch):
    seen = _serve(monkeypatch)
    jobs = asyncio.run(mods.feeds.fetch("weworkremotely"))
    assert len(seen) == len(mods.feeds.WWR_CATEGORIES)
    assert len(jobs) == 4  # the same four postings came back from every category
    assert jobs[0]["company"] == "Volga Partners"  # newest first
    asyncio.run(mods.feeds.fetch("jobicy"))
    assert {r.url.params["industry"] for r in seen if r.url.host == "jobicy.com"} == set(mods.feeds.JOBICY_INDUSTRIES)
    assert all(r.headers["user-agent"] == "protoagent-careercoach" for r in seen)  # Remote OK refuses a blank one


def test_concurrent_searches_share_one_download(mods, monkeypatch):
    seen = _serve(monkeypatch)

    async def both():
        return await asyncio.gather(mods.feeds.fetch("remoteok"), mods.feeds.fetch("remoteok"))

    a, b = asyncio.run(both())
    assert len(seen) == 1 and len(a) == len(b) == 3


def test_all_sweeps_every_feed_and_an_unknown_source_names_them(mods, monkeypatch):
    names = mods.jobsource.choose_providers("all", "")
    assert set(mods.feeds.FEEDS) <= set(names) and "remotive" not in names
    with pytest.raises(ValueError, match="jobicy, weworkremotely, remoteok"):
        mods.jobsource.choose_providers("monster", "")


def test_a_failed_download_raises_and_is_not_cached(mods, monkeypatch):
    _serve(monkeypatch, fail={"jobicy.com"})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(mods.feeds.fetch("jobicy"))
    _serve(monkeypatch)
    assert len(asyncio.run(mods.feeds.fetch("jobicy"))) == 4  # the next call tries again
    with pytest.raises(ValueError, match="unknown job feed"):
        asyncio.run(mods.feeds.fetch("monster"))


# ── through search_jobs: a feed is a keyless source like any other ────────────
@pytest.mark.parametrize(
    ("source", "query", "company"),
    [("jobicy", "design engineer", "Everywhere Inc"), ("weworkremotely", "frontend engineer", "Acme"), ("remoteok", "AI engineer", "Clera")],
)  # fmt: skip
def test_a_feed_answers_a_search_ranked_against_the_query(mods, monkeypatch, source, query, company):
    _serve(monkeypatch)
    out = asyncio.run(mods.jobsource.search_jobs(query, provider=source))
    assert out.provider == source and out.keyless_fallback is False
    assert out.jobs[0]["company"] == company and out.fetched >= out.matched
    assert source in mods.jobsource.KEYLESS and mods.jobsource.PROVIDER_LABELS[source] in out.provider_label


def test_the_manifest_allows_every_feed_host(plugin, mods):
    from pathlib import Path

    import yaml

    manifest = yaml.safe_load((Path(plugin.__file__).parent / "protoagent.plugin.yaml").read_text())
    assert set(mods.feeds.HOSTS) <= set(manifest["capabilities"]["network"])
