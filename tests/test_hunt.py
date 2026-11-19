"""The job-hunt surface: company boards, the watchlist + first-seen log, history import, contacts,
the daily digest. Payload shapes are trimmed from real Ashby / Greenhouse / Lever responses
(2026-10-15). No live network: httpx is routed to a MockTransport."""

from __future__ import annotations

import asyncio
import importlib
import json
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest

NOW = datetime.now(UTC)
_ASYNC_CLIENT = httpx.AsyncClient  # the real class, before any fixture swaps it


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


LONG = "We build agents for enterprises. " * 30  # a real-length description

ASHBY = {
    "jobs": [
        {
            "id": "a1",
            "title": "Forward Deployed Engineer",
            "location": "United States",
            "secondaryLocations": [],
            "isListed": True,
            "isRemote": True,
            "workplaceType": "Remote",
            "address": {"postalAddress": {"addressCountry": "United States"}},
            "publishedAt": _ago(2),
            "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
            "descriptionHtml": f"<p>{LONG}</p>",
            "compensation": {"scrapeableCompensationSalarySummary": "$200K - $260K"},
        },
        {  # isRemote lies on Hybrid roles — workplaceType wins
            "id": "a2",
            "title": "Forward Deployed Engineer - UAE",
            "location": "Abu Dhabi, UAE",
            "isListed": True,
            "isRemote": True,
            "workplaceType": "Hybrid",
            "address": {"postalAddress": {}},
            "publishedAt": _ago(3),
            "jobUrl": "https://jobs.ashbyhq.com/acme/a2",
            "descriptionHtml": f"<p>{LONG}</p>",
        },
        {
            "id": "a3",
            "title": "Account Executive",
            "location": "Remote",
            "isListed": True,
            "workplaceType": "Remote",
            "publishedAt": _ago(1),
            "jobUrl": "https://jobs.ashbyhq.com/acme/a3",
            "descriptionHtml": "<p>Sell.</p>",
        },
        {"id": "a4", "title": "Design Engineer", "isListed": False, "jobUrl": "x", "publishedAt": _ago(1)},
    ]
}
GREENHOUSE = {
    "jobs": [
        {
            "id": 101,
            "title": "Staff Design Engineer",
            "location": {"name": "Remote, US"},
            "absolute_url": "https://boards.greenhouse.io/globex/jobs/101",
            "first_published": _ago(60),
            "updated_at": _ago(0),  # an edit — must NOT read as fresh
            "content": "&lt;p&gt;" + LONG + "&lt;/p&gt;",
            "company_name": "Globex Inc",
            "departments": [{"name": "Design"}],
        },
        {
            "id": 102,
            "title": "Senior AI Engineer",
            "location": {"name": "Remote - EMEA"},
            "absolute_url": "https://boards.greenhouse.io/globex/jobs/102",
            "first_published": _ago(1),
            "content": LONG,
        },
        {
            "id": 103,
            "title": "Senior AI Engineer",
            "location": {"name": "Portland, OR"},
            "absolute_url": "https://boards.greenhouse.io/globex/jobs/103",
            "first_published": _ago(1),
            "content": "short",
        },
    ]
}
LEVER = [
    {
        "id": "l1",
        "text": "AI Engineer Intern",
        "categories": {"location": "Remote", "team": "Eng"},
        "workplaceType": "remote",
        "country": "US",
        "createdAt": int((NOW - timedelta(days=1)).timestamp() * 1000),
        "hostedUrl": "https://jobs.lever.co/initech/l1",
        "descriptionPlain": LONG,
    },
    {
        "id": "l2",
        "text": "Applied AI Engineer",
        "categories": {"location": "San Francisco", "allLocations": ["San Francisco", "Remote - US"]},
        "workplaceType": "remote",
        "country": "US",
        "createdAt": int((NOW - timedelta(days=4)).timestamp() * 1000),
        "hostedUrl": "https://jobs.lever.co/initech/l2",
        "descriptionPlain": LONG,
        "salaryRange": {"min": 180000, "max": 240000, "currency": "USD"},
    },
]

BOARDS = {
    ("api.ashbyhq.com", "acme"): ASHBY,
    ("boards-api.greenhouse.io", "globex"): GREENHOUSE,
    ("api.lever.co", "initech"): LEVER,
}


def _route(request: httpx.Request) -> httpx.Response:
    for (host, slug), payload in BOARDS.items():
        if request.url.host == host and f"/{slug}" in request.url.path:
            return httpx.Response(200, json=payload)
    return httpx.Response(404, json={"ok": False})


@pytest.fixture
def net(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_route), **kw))


# Which subpackage each module lives in.
PACKAGE_OF = {
    "boards": "search",
    "companies": "record",
    "watchlist": "search",
    "contacts": "search",
    "scan": "search",
    "state": "record",
}


@pytest.fixture
def mods(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))

    class M:
        pass

    m = M()
    for name in ("boards", "companies", "watchlist", "contacts", "scan", "state"):
        setattr(m, name, importlib.import_module(f"{plugin.__name__}.{PACKAGE_OF[name]}.{name}"))
    return m


# ── companies ─────────────────────────────────────────────────────────────────
def test_company_key_is_exact_and_the_hint_key_is_loose(mods):
    c = mods.companies
    assert c.company_key("DPR COnstruction") == c.company_key("DPR Construction, Inc.")
    assert c.company_key("Change.org") == "changeorg"
    # different companies must never share the EXACT key — a tracker write keys on it
    for a, b in (("Mercury", "Mercury Systems"), ("Magic", "Magic Labs"), ("Box", "Box Group"), ("Arize", "Arize AI")):
        assert c.company_key(a) != c.company_key(b)
    assert c.company_hint_key("Arize AI") == c.company_hint_key("arize")  # fine for a hint
    assert c.company_hint_key("AI") == "ai"  # never stripped to nothing


# ── board parsers + filters ───────────────────────────────────────────────────
def test_parsers_normalize_each_ats(mods):
    b = mods.boards
    ash = b.parse_ashby(ASHBY, company="Acme", slug="acme")
    assert [j["title"] for j in ash] == [
        "Forward Deployed Engineer",
        "Forward Deployed Engineer - UAE",
        "Account Executive",
    ]
    assert ash[0]["remote"] is True and ash[0]["countries"] == ["United States"] and ash[0]["comp"] == "$200K - $260K"
    assert ash[1]["remote"] is False  # Hybrid, despite isRemote
    gh = b.parse_greenhouse(GREENHOUSE, company="Globex", slug="globex")
    assert gh[0]["company"] == "Globex Inc" and gh[0]["posted"] == b._iso(GREENHOUSE["jobs"][0]["first_published"])
    assert "<" not in gh[0]["snippet"]  # entity-escaped HTML is unescaped then stripped
    lv = b.parse_lever(LEVER, company="Initech", slug="initech")
    assert lv[1]["comp"] == "USD 180,000–240,000" and lv[1]["countries"] == ["United States"]
    assert lv[1]["posted"][:10] == (NOW - timedelta(days=4)).date().isoformat()


def test_title_filter_is_all_words_of_some_role(mods):
    tm = mods.boards.title_matches
    roles = "Forward Deployed Engineer, Design Engineer, AI Engineer"
    assert tm("Senior Design Systems Engineer", roles)
    assert tm("Forward Deployed Engineer (FDE), Applied AI", roles)
    assert not tm("Product Designer", roles)
    assert not tm("AI Engineer Intern", roles, exclude="intern")
    assert tm("Anything", "")


def test_location_filter_remote_means_open_to_the_us(mods):
    b = mods.boards
    lm = b.location_matches
    assert lm({"location": "United States", "remote": True, "countries": ["United States"]}, "remote")
    assert not lm({"location": "Abu Dhabi, UAE", "remote": False}, "remote")
    assert not lm({"location": "Remote - EMEA", "remote": True}, "remote")
    assert lm({"location": "Remote (US or Canada)", "remote": True}, "remote")
    assert not lm({"location": "Remote", "remote": True, "countries": ["Germany"]}, "remote")
    assert lm({"location": "Portland, OR", "remote": None}, "remote, portland")
    assert lm({"location": "Tokyo"}, "")


def test_age_filter_uses_first_published_and_keeps_undated(mods):
    b = mods.boards
    gh = b.parse_greenhouse(GREENHOUSE, company="Globex", slug="globex")
    assert not b.passes(gh[0], roles="design engineer", max_age_days=30)  # first published 60d ago
    assert b.passes({**gh[0], "posted": ""}, roles="design engineer", max_age_days=30)


# ── watchlist + first-seen log ────────────────────────────────────────────────
def test_watchlist_one_row_per_company(mods):
    wl = mods.watchlist
    _, created = wl.add_company("Scale AI", "greenhouse", "scaleai")
    _, again = wl.add_company("Scale AI, Inc.", "greenhouse", "scale")
    assert created and not again
    assert [(r["company"], r["slug"]) for r in wl.load_watchlist()] == [("Scale AI", "scale")]
    assert wl.remove_company("scale ai") and wl.load_watchlist() == []


def test_sightings_new_once_and_reposts_keep_their_age(mods):
    wl, b = mods.watchlist, mods.boards
    jobs = b.parse_ashby(ASHBY, company="Acme", slug="acme")[:1]
    new, _ = wl.record_sightings(jobs, now=_ago(50))
    assert len(new) == 1 and jobs[0]["sightings"] == 1
    again = b.parse_ashby(ASHBY, company="Acme", slug="acme")[:1]
    new, _ = wl.record_sightings(again)
    assert new == [] and again[0]["sightings"] == 2
    repost = {**again[0], "id": "ashby:acme:a1-repost", "posted": _ago(0)}  # same text, fresh date
    new, _ = wl.record_sightings([repost])
    assert len(new) == 1 and repost["repost_of"] == "ashby:acme:a1"
    flags = wl.ghost_flags(repost)
    assert any("open 50 days" in f for f in flags) and any("reposted" in f for f in flags)


def test_ghost_flags_short_text(mods):
    assert "very short description" in mods.watchlist.ghost_flags({"desc_len": 5, "posted": _ago(1)})
    assert mods.watchlist.ghost_flags({"desc_len": 4000, "posted": _ago(1)}) == []


# ── tracker: normalized dedup, history import, cap ────────────────────────────
def test_tracker_dedups_on_normalized_company_and_job_url(mods):
    st = mods.state
    url = "https://jobs.ashbyhq.com/arize/8152eaf6-012b-4154-95e2-7d6c52faba93"
    st.track_application(company="Arize AI", role="Design Engineer", fit_score=83, source=url)
    st.track_application(company="arize ai, inc.", role="design engineer", status="applied")  # status-only
    rows = st.load_applications()
    assert len(rows) == 1 and rows[0]["status"] == "applied" and rows[0]["fit_score"] == 83  # score kept
    st.track_application(company="Arize AI", role="Retitled Design Engineer II", source=url + "/")
    assert len(st.load_applications()) == 1  # same job URL, same company → same application
    assert st.load_applications()[0]["role"] == "Design Engineer"  # a URL hit never renames the role


def test_tracker_never_merges_on_a_shared_url_or_a_look_alike_company(mods):
    st = mods.state
    careers = "https://acme.com/careers"
    st.track_application(company="Acme", role="Backend Engineer", status="applied", fit_score=80, source=careers)
    st.track_application(company="Acme", role="Frontend Engineer", source=careers)
    st.track_application(company="Mercury", role="SWE", status="interviewing")
    st.track_application(company="Mercury Systems", role="SWE")
    job = "https://boards.greenhouse.io/x/jobs/123456"
    st.track_application(company="Globex", role="SRE", source=job)
    st.track_application(company="Initech", role="SRE II", source=job)  # same URL, other company
    rows = {(r["company"], r["role"]): r for r in st.load_applications()}
    assert len(rows) == 6
    assert rows[("Acme", "Backend Engineer")]["status"] == "applied"
    assert rows[("Mercury", "SWE")]["status"] == "interviewing"


def test_import_history_never_downgrades_a_status(mods):
    st = mods.state
    st.track_application(company="Nava", role="AI Engineer", status="rejected")
    res = st.import_history(
        [
            {"company": "NAVA", "role": "AI engineer", "status": "applied", "source": "https://nava/1"},
            {"company": "Glean", "role": "Founding FDE", "date": "2026-08-20"},
            {"company": "", "role": "Interview"},
            {"company": "X", "role": "Y", "status": "ghosted"},
        ]
    )
    assert res["added"] == 1 and res["updated"] == 1 and len(res["skipped"]) == 2
    nava = next(r for r in st.load_applications() if r["company"] == "Nava")
    assert nava["status"] == "rejected" and nava["source"] == "https://nava/1"
    glean = next(r for r in st.load_applications() if r["company"] == "Glean")
    assert glean["status"] == "applied" and glean["date"] == "2026-08-20"


def test_tracking_a_fourth_live_application_at_one_company_warns(tools):
    t = tools["careercoach_track_application"]
    for i in range(3):
        assert "Heads-up" not in t.invoke({"company": "Acme", "role": f"Role {i}", "status": "applied"})
    out = t.invoke({"company": "Acme, Inc.", "role": "Role 4", "status": "applied"})
    assert "Heads-up" in out and "4 live applications" in out


# ── contacts ──────────────────────────────────────────────────────────────────
CONNECTIONS = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Ada,Lovelace,https://www.linkedin.com/in/ada,,Acme AI,Staff Engineer,01 Jan 2024
Grace,Hopper,https://www.linkedin.com/in/grace,grace@x.com,Globex,Recruiter,02 Feb 2025
"""


def test_linkedin_connections_parse_past_the_preamble(mods):
    people = mods.contacts.parse_linkedin_connections(CONNECTIONS)
    assert [(p["name"], p["company"], p["title"]) for p in people] == [
        ("Ada Lovelace", "Acme AI", "Staff Engineer"),
        ("Grace Hopper", "Globex", "Recruiter"),
    ]
    with pytest.raises(ValueError):
        mods.contacts.parse_linkedin_connections("a,b\n1,2")


def test_contacts_merge_match_and_followups(mods):
    c = mods.contacts
    assert c.merge_contacts(c.parse_linkedin_connections(CONNECTIONS)) == (2, 0)
    moved = CONNECTIONS.replace("Globex,Recruiter", "Initech,Head of Talent")
    assert c.merge_contacts(c.parse_linkedin_connections(moved)) == (0, 2)
    assert [p["name"] for p in c.at_company("Acme")] == ["Ada Lovelace"]
    assert [p["name"] for p in c.at_company("Initech")] == ["Grace Hopper"]
    c.log_touch("Ada Lovelace", "Acme", note="asked for referral", next_action="nudge", due="2000-01-01")
    due = c.due_followups()
    assert [d["name"] for d in due] == ["Ada Lovelace"] and due[0]["touches"][0]["note"] == "asked for referral"
    with pytest.raises(ValueError):
        c.log_touch("Ada Lovelace", due="next week")


# ── tools end to end ──────────────────────────────────────────────────────────
@pytest.fixture
def hunt_tools(plugin, iso, monkeypatch, net):
    from _plugin_testkit import FakeRegistry

    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    reg = FakeRegistry(
        {
            "packet_root": str(iso / "ws"),
            "target_roles": "Forward Deployed Engineer, Design Engineer, AI Engineer",
            "title_exclude": "intern",
            "locations": "remote, portland",
            "max_age_days": 30,
            # reach careercoach_scan_boards / import_* / find_referrals by their legacy names (S4b)
            "extended_tools": True,
        }
    )
    plugin.register(reg)
    return {t.name: t for t in reg.tools}


def _run(tool, args):
    return asyncio.run(tool.ainvoke(args))


def test_watchlist_add_probes_every_ats(hunt_tools):
    out = _run(hunt_tools["careercoach_watchlist"], {"action": "add", "company": "Acme, Globex, Initech, Nowhere"})
    assert "Watching 3" in out and "acme — " not in out
    assert "Acme — ashby/acme" in out and "Globex — greenhouse/globex" in out and "Initech — lever/initech" in out
    assert "Not found on Ashby, Greenhouse, Lever or SmartRecruiters: Nowhere" in out
    listed = _run(hunt_tools["careercoach_watchlist"], {})
    assert listed.startswith("3 watched companies")


def test_scan_boards_filters_marks_new_tracked_and_ghosts(hunt_tools):
    _run(hunt_tools["careercoach_watchlist"], {"action": "add", "company": "Acme, Globex, Initech"})
    hunt_tools["careercoach_track_application"].invoke(
        {"company": "Initech", "role": "Applied AI Engineer", "status": "applied"}
    )
    out = _run(hunt_tools["careercoach_scan_boards"], {})
    assert "Scanned 3 board(s)" in out and "3 new since the last scan" in out
    assert "Forward Deployed Engineer — Acme" in out and "$200K - $260K" in out
    assert "UAE" not in out and "Account Executive" not in out and "Intern" not in out  # filtered
    assert "Staff Design Engineer" not in out  # first published 60 days ago
    assert "Remote - EMEA" not in out
    assert "Senior AI Engineer — Globex" in out and "very short description" in out  # Portland, thin text
    assert "already tracked: applied" in out
    second = _run(hunt_tools["careercoach_scan_boards"], {"only_new": True})
    assert "0 new since the last scan" in second and "No matching roles that are new." in second
    anyage = _run(hunt_tools["careercoach_scan_boards"], {"max_age_days": 0})
    assert "Staff Design Engineer" in anyage and "open 60 days" in anyage


def test_import_history_tool(hunt_tools):
    t = hunt_tools["careercoach_import_history"]
    assert "isn't valid JSON" in t.invoke({"entries": "Glean - FDE"})
    out = t.invoke({"entries": json.dumps([{"company": "Glean", "role": "FDE"}, {"company": "Cohere", "role": "FDE"}])})
    assert out.startswith("Imported 2 new role(s)")


def test_connections_import_from_the_export_zip_and_referrals(hunt_tools, iso):
    zpath = iso / "ws" / "Basic_LinkedInDataExport.zip"
    zpath.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("Connections.csv", CONNECTIONS)
    out = hunt_tools["careercoach_import_connections"].invoke({"path": "Basic_LinkedInDataExport.zip"})
    assert out.startswith("Imported 2 connection(s) across 2 companies")
    _run(hunt_tools["careercoach_watchlist"], {"action": "add", "company": "Acme"})
    refs = hunt_tools["careercoach_find_referrals"].invoke({})
    assert "Acme:" in refs and "Ada Lovelace — Staff Engineer" in refs
    assert "No contacts at Initech" in hunt_tools["careercoach_find_referrals"].invoke({"company": "Initech"})


def test_digest_new_roles_followups_and_quiet_applications(hunt_tools, mods):
    _run(hunt_tools["careercoach_watchlist"], {"action": "add", "company": "Acme, Globex"})
    hunt_tools["careercoach_log_contact"].invoke(
        {"name": "Ada Lovelace", "company": "Acme", "next_action": "send portfolio", "due": "2000-01-01"}
    )
    mods.state.import_history([{"company": "Old Co", "role": "Staff Engineer", "date": "2000-01-01"}])
    out = _run(hunt_tools["careercoach_digest"], {"days": 3})
    assert "## New roles" in out and "Forward Deployed Engineer — Acme" in out
    assert "## Follow-ups due" in out and "send portfolio" in out
    assert "## Gone quiet" in out and "Staff Engineer @ Old Co" in out
    assert "## Pipeline" in out and "applied 1" in out


# ── filter edge cases: US-remote text, aliases, per-city listings, look-alike boards, watch ──
@pytest.mark.parametrize(
    ("loc", "ok"),
    [
        ("Remote", True),
        ("Remote (US or Canada)", True),
        ("Remote - San Francisco", True),
        ("Remote, Portland, OR", True),
        ("US - Remote", True),
        ("Remote, Sweden", False),
        ("Remote - Latin America", False),
        ("Remote - Estonia", False),
        ("Remote - work in Colombia", False),  # "in" is not Indiana
    ],
)
def test_text_remote_needs_a_us_place_or_no_place(mods, loc, ok):
    assert mods.boards.location_matches({"location": loc, "remote": None}, "remote") is ok


def test_title_aliases_count_both_ways(mods):
    tm = mods.boards.title_matches
    assert tm("FDE, Enterprise", "Forward Deployed Engineer")
    assert tm("Senior Forward Deployed Engineer", "FDE")
    assert tm("Member of Technical Staff, Applied AI", "MTS")


def test_one_role_per_city_is_not_a_repost(mods):
    wl, b = mods.watchlist, mods.boards
    base = b.parse_ashby(ASHBY, company="Acme", slug="acme")[0]
    cities = [{**base, "id": f"ashby:acme:{c}", "location": c, "hash": b.content_hash(base["title"], LONG, c)}
              for c in ("London", "Paris")]  # fmt: skip
    wl.record_sightings(cities[:1])
    new, _ = wl.record_sightings(cities[1:])
    assert len(new) == 1 and "repost_of" not in cities[1]
    twins = [{**base, "id": f"ashby:acme:t{i}", "hash": "same"} for i in range(2)]  # same text, one scan
    wl.record_sightings(twins)
    assert "repost_of" not in twins[0] and twins[1]["repost_of"] == "ashby:acme:t0"


def test_probe_rejects_a_look_alike_board_and_reports_errors(mods, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io" and request.url.path.endswith("/mercury/jobs"):
            return httpx.Response(200, json={"jobs": [{**GREENHOUSE["jobs"][1], "company_name": "Mercury"}]})
        if request.url.host == "api.lever.co":
            return httpx.Response(429, json={})
        return httpx.Response(404, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    found, problems = asyncio.run(mods.boards.probe("Mercury Systems"))
    assert found == []
    assert any("'Mercury''s board, not 'Mercury Systems'" in p for p in problems)
    assert any("lever/" in p and "429" in p for p in problems)  # a rate limit is not "not found"
    found, _ = asyncio.run(mods.boards.probe("Mercury"))
    assert found == [("greenhouse", "mercury", 1)]


def test_a_corrupt_store_answers_instead_of_raising(hunt_tools, iso):
    (iso / "cc").mkdir(parents=True, exist_ok=True)
    (iso / "cc" / "watchlist.json").write_text("{not json")
    out = _run(hunt_tools["careercoach_watchlist"], {})
    assert "unreadable" in out and "don't overwrite" in out
    assert "unreadable" in _run(hunt_tools["careercoach_scan_boards"], {})


HIMALAYAS = {
    "jobs": [
        {"title": "Forward Deployed Engineer", "companyName": "Newco", "locationRestrictions": ["United States"],
         "applicationLink": "https://himalayas.app/companies/newco/jobs/fde", "excerpt": LONG,
         "pubDate": int((NOW - timedelta(days=1)).timestamp())},
        {"title": "AI Engineer (Latam)", "companyName": "Farco", "locationRestrictions": ["Brazil", "Argentina"],
         "applicationLink": "https://himalayas.app/companies/farco/jobs/ai", "excerpt": LONG,
         "pubDate": int((NOW - timedelta(days=1)).timestamp())},
    ]
}  # fmt: skip


@pytest.fixture
def watch_env(plugin, iso, monkeypatch, mods):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "himalayas.app":
            return httpx.Response(200, json=HIMALAYAS)
        return _route(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    from _plugin_testkit import FakeRegistry

    cfg = {
        "target_roles": "Forward Deployed Engineer, Design Engineer, AI Engineer, Applied AI Engineer, Staff Software Engineer",
        "locations": "remote, portland",
        "max_age_days": 30,
    }
    reg = FakeRegistry(cfg)
    reg.emitted = []
    reg.emit = lambda topic, payload: reg.emitted.append((topic, payload))
    for c, ats, slug in (
        ("Acme", "ashby", "acme"),
        ("Globex", "greenhouse", "globex"),
        ("Initech", "lever", "initech"),
    ):
        mods.watchlist.add_company(c, ats, slug)
    return plugin, reg, cfg


def test_watch_tracks_exact_title_matches_despite_many_target_roles(watch_env, mods):
    plugin, reg, cfg = watch_env
    got = asyncio.run(plugin._watch_tick(reg, cfg, 75))
    titles = sorted((m["company"], m["title"]) for m in got)
    assert ("Acme", "Forward Deployed Engineer") in titles and ("Newco", "Forward Deployed Engineer") in titles
    assert all(m["score"] >= 75 for m in got)
    assert not any(m["company"] == "Farco" for m in got)  # the market sweep honours `locations`
    assert reg.emitted and reg.emitted[0][0] == "new_matches"
    assert asyncio.run(plugin._watch_tick(reg, cfg, 75)) == []  # handled once
    assert mods.state.load_applications() == []  # the watch never writes the tracker
    queued = mods.watchlist.take_inbox()
    assert {j["company"] for j in queued} >= {"Acme", "Newco"} and mods.watchlist.take_inbox() == []


def test_a_watch_pass_records_when_it_ran(watch_env, mods):
    plugin, reg, cfg = watch_env
    assert mods.watchlist.watch_last_run() == ""
    asyncio.run(plugin._watch_tick(reg, cfg, 75))
    assert mods.watchlist.watch_last_run()


def test_a_pass_where_every_source_failed_is_not_a_run(watch_env, mods, monkeypatch):
    plugin, reg, cfg = watch_env

    async def down(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(mods.scan, "scan_watchlist", down)
    monkeypatch.setattr(importlib.import_module(plugin.__name__ + ".search.jobsource"), "search_jobs", down)
    asyncio.run(plugin._watch_tick(reg, cfg, 75))
    assert mods.watchlist.watch_last_run() == ""


# ── the watch and a JSearch key: the keyless sweep always runs; JSearch within its budget ─────────
JSEARCH = {
    "data": {
        "jobs": [
            {"job_title": "Design Engineer", "employer_name": "Googlejobs Co", "job_is_remote": True,
             "job_country": "US", "job_apply_link": "https://gj.example/1", "job_description": LONG,
             "job_posted_at_datetime_utc": (NOW - timedelta(days=1)).isoformat()},
        ]
    }
}  # fmt: skip


def _sweep(plugin):
    return importlib.import_module(plugin.__name__ + ".search.sweep")


def _no_spacing(monkeypatch, plugin):
    monkeypatch.setattr(_sweep(plugin), "KEYED_SPACING_S", 0)
    monkeypatch.setattr(_sweep(plugin), "KEYLESS_SPACING_S", 0)


def _with_jsearch(watch_env, monkeypatch, respond):
    """The watch env plus a mock JSearch. ``respond(request)`` answers each JSearch call; returns
    the list those calls are logged to."""
    plugin, reg, cfg = watch_env
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "jsearch.p.rapidapi.com":
            calls.append(request)
            return respond(request)
        if request.url.host == "himalayas.app":
            return httpx.Response(200, json=HIMALAYAS)
        return _route(request)

    # watch_env already swapped httpx.AsyncClient for its own mock: replace that one, don't wrap it
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw))
    _no_spacing(monkeypatch, plugin)
    cfg["jobs_api_key"] = "KEY"
    return plugin, reg, cfg, calls


def _quota(remaining: int, days: int = 30):
    return {
        "X-RateLimit-Requests-Limit": "200",
        "X-RateLimit-Requests-Remaining": str(remaining),
        "X-RateLimit-Requests-Reset": str(days * 86400),
    }


def test_a_rejected_jsearch_key_costs_the_watch_nothing_from_the_keyless_board(watch_env, mods, monkeypatch, caplog):
    """2026-11-18: a key RapidAPI rejected turned the whole market sweep off. Auto picked JSearch
    for every role, role 1 got a 403, roles 2-5 a 429, and the keyless board was never asked."""
    plugin, reg, cfg, calls = _with_jsearch(
        watch_env, monkeypatch, lambda r: httpx.Response(403, json={"message": "You are not subscribed to this API."})
    )
    with caplog.at_level("WARNING"):
        got = asyncio.run(plugin._watch_tick(reg, cfg, 75))
    assert ("Newco", "Forward Deployed Engineer") in {(m["company"], m["title"]) for m in got}  # Himalayas
    assert len(calls) == 1  # one refusal answers for the pass: no burst of four more
    assert "JSearch isn't answering (RapidAPI accepted the key, but its account isn't subscribed" in caplog.text
    assert "this pass uses the other sources only" in caplog.text
    assert mods.watchlist.watch_last_run()


def test_the_watch_sweeps_jsearch_and_the_keyless_board_together(watch_env, mods, monkeypatch):
    plugin, reg, cfg, calls = _with_jsearch(
        watch_env, monkeypatch, lambda r: httpx.Response(200, json=JSEARCH, headers=_quota(1000))
    )
    got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    companies = {m["company"] for m in got}
    assert {"Googlejobs Co", "Newco"} <= companies  # JSearch's role and Himalayas' role, one pass
    assert len(calls) == 3 and all(c.url.path == "/search-v2" for c in calls)  # watch_market_queries a pass


def test_the_watch_spreads_a_small_jsearch_plan_over_the_month(watch_env, mods, monkeypatch):
    """The free plan is 200 requests a month; five a pass, eight passes a day spends it in five days."""
    left = [193]

    def respond(request):
        left[0] -= 1
        return httpx.Response(200, json=JSEARCH, headers=_quota(left[0]))

    plugin, reg, cfg, calls = _with_jsearch(watch_env, monkeypatch, respond)
    for _ in range(4):  # four passes in one day
        asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 6  # 193 left over 30 days is 6 a day, however many passes run
    assert mods.watchlist.jsearch_allowance() == 0


def test_the_jsearch_allowance_is_whats_left_spread_over_the_days_left(mods):
    wl = mods.watchlist
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    assert wl.jsearch_allowance(now) is None  # nothing known yet: no cap
    wl.note_jsearch_call({"limit": 200, "remaining": 193, "reset_s": 30 * 86400}, now)
    assert wl.jsearch_allowance(now) == 5  # (193 + 1 used) // 30 = 6 a day, one spent
    assert wl.jsearch_allowance(now + timedelta(days=1)) == 6  # a new day, a new share
    assert wl.jsearch_allowance(now + timedelta(days=31)) is None  # the plan refilled
    wl.note_jsearch_call({"limit": 200, "remaining": 0, "reset_s": 86400}, now)
    assert wl.jsearch_allowance(now) == 0  # spent
    wl.note_jsearch_call({}, now)  # an answer with no allowance (a timeout) keeps the last one known
    assert wl.jsearch_allowance(now) == 0
    wl.note_jsearch_call({"limit": 100000, "remaining": 99000, "reset_s": 30 * 86400}, now)
    assert wl.jsearch_allowance(now) > 3000  # a paid plan is never the limit on a watch


def test_an_unreadable_run_note_reads_as_not_run(mods):
    mods.watchlist._path(mods.watchlist.WATCH_RUN).parent.mkdir(parents=True, exist_ok=True)
    mods.watchlist._path(mods.watchlist.WATCH_RUN).write_text("{broken")
    assert mods.watchlist.watch_last_run() == ""


def test_a_digest_does_not_steal_the_watchs_new_roles(watch_env, mods, plugin):
    _, reg, cfg = watch_env
    asyncio.run(
        mods.scan.scan_watchlist(mods.watchlist.load_watchlist(), roles=cfg["target_roles"], locations="remote")
    )  # what the digest / scan tool do: record in seen.json
    got = asyncio.run(plugin._watch_tick(reg, cfg, 75))
    assert any(m["company"] == "Acme" for m in got)


def test_the_per_scan_cap_leaves_the_rest_for_next_time(watch_env, mods, plugin, monkeypatch):
    _, reg, cfg = watch_env
    many = {"jobs": [{**ASHBY["jobs"][0], "id": f"a{i}", "jobUrl": f"https://jobs.ashbyhq.com/acme/{i}",
                      "title": f"Forward Deployed Engineer {i}"} for i in range(20)]}  # fmt: skip
    monkeypatch.setitem(BOARDS, ("api.ashbyhq.com", "acme"), many)
    # threshold 0: this pins the per-scan CAP, not the score (short fixture text caps the score at 70)
    first = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    second = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(first) == 15 and len(second) >= 5
    assert not {m["url"] for m in first} & {m["url"] for m in second}


def test_a_blocked_sites_lead_is_shown_only_with_the_companys_own_posting(watch_env, mods, monkeypatch):
    """The operator saved two JobLeads roles hoping the coach would find where to really apply."""
    lead = lambda n: {"job_title": f"Design Engineer {n}", "employer_name": f"Leadco {n}", "job_is_remote": True,  # noqa: E731
                      "job_country": "US", "job_description": LONG, "job_id": f"jl{n}",
                      "job_apply_link": f"https://www.jobleads.com/us/job/{n}",
                      "job_posted_at_datetime_utc": (NOW - timedelta(days=1)).isoformat()}  # fmt: skip
    plugin, reg, cfg, calls = _with_jsearch(
        watch_env, monkeypatch, lambda r: httpx.Response(200, json={"data": {"jobs": [lead(1), lead(2)]}})
    )
    resolve = importlib.import_module(plugin.__name__ + ".search.resolve")

    async def found_one(batch, **kw):
        for j in batch:
            if j["company"] == "Leadco 1":
                j.update(apply_url="https://jobs.ashbyhq.com/leadco/1", ats_lookup="found", title="Design Engineer")
            elif j.get("via_blocked"):
                j["ats_lookup"] = "no board"
        return 1

    monkeypatch.setattr(resolve, "resolve", found_one)
    got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    leads = [j for j in got if j["company"].startswith("Leadco")]
    assert [(j["company"], j["apply_url"]) for j in leads] == [("Leadco 1", "https://jobs.ashbyhq.com/leadco/1")]
    cards = [c for c in mods.watchlist.inbox_for_view() if c["company"].startswith("Leadco")]
    assert len(cards) == 1 and "jobleads" not in json.dumps(cards)  # the reposter's link is nowhere
    again = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert not [j for j in again if j["company"].startswith("Leadco")]  # the untraced lead doesn't come back


def test_the_pass_queues_the_best_new_roles_not_just_the_newest(watch_env, mods, plugin, monkeypatch):
    """Twenty new roles, fifteen lookups a pass: the strong ones must not wait behind newer weak ones."""
    _, reg, cfg = watch_env
    strong = "Python, TypeScript, React, LLM agents, evals and design systems. " * 20
    weak = "COBOL mainframe batch jobs, SAP ABAP and Salesforce Apex administration. " * 20
    jobs = []
    for i in range(20):
        good = i >= 15  # the five strong roles are the OLDEST
        jobs.append({**ASHBY["jobs"][0], "id": f"b{i}", "jobUrl": f"https://jobs.ashbyhq.com/acme/b{i}",
                     "title": f"AI Engineer {i}", "descriptionHtml": strong if good else weak,
                     "publishedAt": _ago(1 + i)})  # fmt: skip
    monkeypatch.setitem(BOARDS, ("api.ashbyhq.com", "acme"), {"jobs": jobs})
    cfg["target_roles"] = "AI Engineer"
    got = asyncio.run(plugin._watch_tick(reg, cfg, 65))
    assert {m["title"] for m in got} >= {f"AI Engineer {i}" for i in range(15, 20)}  # all five, on the first pass
    assert all(m["score"] >= 65 for m in got)
    # the weak ones were settled without a lookup: nothing is left waiting for a later pass
    assert asyncio.run(plugin._watch_tick(reg, cfg, 65)) == []


# ── one board per row, full-name slugs, spelling-variant hints, US time zones, job URLs ──
def test_one_board_one_watchlist_row(mods):
    wl = mods.watchlist
    wl.add_company("Scale", "greenhouse", "scaleai")
    _, created = wl.add_company("Scale AI", "greenhouse", "scaleai")
    assert not created and len(wl.load_watchlist()) == 1


def test_probe_trusts_a_full_name_slug_and_flags_unverifiable_short_ones(mods, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "boards-api.greenhouse.io" and path.endswith("/figureai/jobs"):
            return httpx.Response(200, json={"jobs": [{**GREENHOUSE["jobs"][1], "company_name": "Figure"}]})
        if request.url.host == "api.ashbyhq.com" and path.endswith("/magic"):
            return httpx.Response(200, json=ASHBY)
        return httpx.Response(404, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    found, problems = asyncio.run(mods.boards.probe("Figure AI"))
    assert found == [("greenhouse", "figureai", 1)] and problems == []
    found, problems = asyncio.run(mods.boards.probe("Magic Labs"))
    assert found == [("ashby", "magic", 3)] and any("confirm it's Magic Labs" in p for p in problems)


def test_already_applied_hides_a_spelling_variant_posting(mods):
    idx = mods.scan.tracked_index([{"company": "Scale", "role": "Forward Deployed Engineer", "status": "applied"}])
    job = mods.scan.annotate({"company": "Scale AI", "title": "Forward Deployed Engineer", "url": ""}, idx)
    assert job["tracked"] == "applied"


@pytest.mark.parametrize(
    ("loc", "ok"),
    [("Remote (West Coast)", True), ("Remote, Pacific Time", True), ("Remote (PST)", True),
     ("Remote - DE", False), ("Remote (IL)", False)],
)  # fmt: skip
def test_us_time_zones_count_and_bare_codes_dont(mods, loc, ok):
    assert mods.boards.location_matches({"location": loc}, "remote") is ok


def test_job_specific_url(mods):
    js = mods.state.job_specific_url
    assert js("https://boards.greenhouse.io/x/jobs/8698330002")
    assert js("https://jobs.ashbyhq.com/x/8152eaf6-012b-4154-95e2-7d6c52faba93")
    assert js("https://databricks.com/careers/job?gh_jid=8805655002")
    assert not js("https://www.indeed.com/q-engineer-l-94105-jobs")
    assert not js("https://acme.com/careers?utm_campaign=2026101")


# ── city + state terms, slug guesses, export zip errors ──
def test_a_state_after_a_city_stays_with_the_city(mods):
    b = mods.boards
    assert b.location_terms("remote, portland, or") == ["remote", "portland, or"]
    assert b.location_terms("Portland, OR, Seattle, WA, remote") == ["portland, or", "seattle, wa", "remote"]
    for loc in ("New York", "Singapore", "San Francisco, CA | New York City, NY"):  # "or" in y-OR-k / singap-OR-e
        assert not b.location_matches({"location": loc, "remote": False}, "remote, portland, or"), loc
    assert b.location_matches({"location": "Portland, OR", "remote": False}, "remote, portland, or")
    assert not b.location_matches({"location": "Portland, ME", "remote": False}, "remote, portland, or")
    assert not b.location_matches({"location": "Seattle", "remote": False}, "eat")  # whole words only
    assert b.location_matches({"location": "Remote - California", "remote": True}, "remote")  # a US state, spelled out


def test_slug_guesses_cover_domain_names_and_board_suffixes(mods):
    sc = mods.boards.slug_candidates
    assert "owner" in sc("Owner.com")
    assert {"arizeai", "gleanwork"} <= set(sc("Arize")) | set(sc("Glean"))


def test_connections_import_reports_a_bad_zip(hunt_tools, iso):
    bad = iso / "ws" / "export.zip"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not a zip")
    out = hunt_tools["careercoach_import_connections"].invoke({"path": str(bad)})
    assert out.startswith("Couldn't read export.zip")


def test_the_watch_waits_its_interval_between_scans(plugin, iso, monkeypatch):
    """The supervisor polls locally; the scheduler separately gates network scans by elapsed time."""
    import sys

    from _plugin_testkit import FakeRegistry

    seen: dict = {}

    class _Engine:
        def start(self):
            pass

        def stop(self):
            pass

    def fake_supervise(work, **kw):
        seen.update(kw)
        return _Engine()

    monkeypatch.setattr(sys.modules["graph.sdk"], "supervise", fake_supervise, raising=False)
    reg = FakeRegistry({"watch_enabled": True, "watch_interval_min": 180, "target_roles": "AI Engineer"})
    plugin.register(reg)
    start, _stop, _reload = reg.surface_specs["careercoach-watch"]
    asyncio.run(start())
    assert seen.get("breath") == 30 and "interval" not in seen


def test_the_digest_names_a_source_that_did_not_answer(hunt_tools, mods):
    assert "didn't answer" not in _run(hunt_tools["careercoach_digest"], {})  # nothing recorded: nothing said
    mods.watchlist.record_watch_run(
        {"himalayas": {"ok": True, "jobs": 40, "note": ""}, "jsearch": {"ok": False, "jobs": 0, "note": "timed out"}}
    )
    out = _run(hunt_tools["careercoach_digest"], {})
    assert "## Sources that didn't answer on the last watch pass\n- JSearch: timed out" in out
    assert "The other sources were searched as usual." in out and "Himalayas" not in out.split("didn't answer")[1]
    mods.watchlist.record_watch_run({"himalayas": {"ok": True, "jobs": 40, "note": ""}})
    assert "didn't answer" not in _run(hunt_tools["careercoach_digest"], {})


def test_the_digest_shows_and_drains_the_watch_inbox(hunt_tools, mods):
    mods.watchlist.add_to_inbox(
        [
            {
                "id": "himalayas:x",
                "company": "Newco",
                "title": "Forward Deployed Engineer",
                "url": "https://h/x",
                "posted": _ago(1),
                "source": "himalayas",
            }
        ]  # fmt: skip
    )
    out = _run(hunt_tools["careercoach_digest"], {})
    assert "## From the background watch (1 queued)" in out and "Newco" in out
    assert "From the background watch" not in _run(hunt_tools["careercoach_digest"], {})


# ── location term splitting, digest inbox paging, region words ──
@pytest.mark.parametrize(
    ("setting", "terms"),
    [
        ("sf, la", ["sf", "la"]),
        ("nyc, la, sf", ["nyc", "la", "sf"]),
        ("remote, sf, la", ["remote", "sf", "la"]),
        ("seattle, wa, ca", ["seattle, wa", "ca"]),
        ("remote, or", ["remote", "or"]),
        ("Portland, OR, remote", ["portland, or", "remote"]),
    ],
)
def test_location_terms_only_glue_a_state_onto_a_city(mods, setting, terms):
    assert mods.boards.location_terms(setting) == terms


@pytest.mark.parametrize(
    ("loc", "setting", "ok"),
    [
        ("Portland, Oregon", "portland, or", True),
        ("Portland OR", "portland, or", True),
        ("Portland, OR (Hybrid)", "portland, or", True),
        ("Portland, ME", "portland, or", False),
        ("Los Angeles, CA", "sf, la", True),  # "la" is the Los Angeles shorthand, not Louisiana
        ("Hybrid - SF", "sf, la", True),
        ("Berlin or Munich", "remote, or", False),  # a bare state code means the state, not the word
        ("Salem, OR", "remote, or", True),
        ("Seattle, Washington", "seattle, wa", True),
    ],
)
def test_city_state_terms_match_spellings_of_the_same_place(mods, loc, setting, ok):
    assert mods.boards.location_matches({"location": loc, "remote": False}, setting) is ok


@pytest.mark.parametrize(
    ("loc", "ok"),
    [("Remote - Tbilisi, Georgia", False), ("Remote - Georgia (country)", False), ("Remote, Central Europe", False),
     ("Remote (Eastern Europe)", False), ("Remote - Pacific / APAC", False), ("Remote - Atlanta, Georgia", True),
     ("Remote (US Central time)", True)],
)  # fmt: skip
def test_us_region_words_dont_admit_foreign_regions(mods, loc, ok):
    assert mods.boards.location_matches({"location": loc}, "remote") is ok


def test_the_digest_keeps_what_it_did_not_show(hunt_tools, mods):
    mods.watchlist.add_to_inbox(
        [
            {"id": f"h:{i}", "company": f"Co{i}", "title": "AI Engineer", "url": f"https://h/{i}", "posted": _ago(1)}
            for i in range(30)
        ]  # fmt: skip
    )
    out = _run(hunt_tools["careercoach_digest"], {})
    assert "(30 queued)" in out and "5 more stay queued" in out
    assert len(mods.watchlist.peek_inbox()) == 5


def test_slug_guesses_skip_redundant_suffixes(mods):
    assert "scaleaiai" not in mods.boards.slug_candidates("Scale AI")


@pytest.mark.parametrize(
    ("loc", "setting", "ok"),
    [
        ("Los Angeles, CA", "sf, la", True),
        ("New Orleans, LA", "sf, la", False),
        ("San Francisco, CA", "sf", True),
        ("NY", "ny", True),
        ("On-site - Anywhere", "toronto, on", False),
        ("Toronto, ON", "toronto, on", True),
        ("Seattle, Washington", "washington, dc", False),
        ("Washington, DC", "washington, dc", True),
        ("Charleston, West Virginia", "charleston, va", False),
        ("Portland, OR", "Portland OR", True),
    ],
)
def test_shorthands_provinces_and_dc(mods, loc, setting, ok):
    assert mods.boards.location_matches({"location": loc, "remote": False}, setting) is ok


@pytest.mark.parametrize(
    "loc",
    ["Remote - Asia Pacific", "Remote - Asia-Pacific", "Remote, Central & Eastern Europe", "Remote - Central America"],
)
def test_more_foreign_regions_stay_out(mods, loc):
    assert not mods.boards.location_matches({"location": loc}, "remote")


# ── fingerprints and plural company names ──
def test_hashless_jobs_are_never_reposts_of_each_other(mods):
    jobs = [{"id": f"x:{i}", "company": "Co", "title": f"Role {i}", "hash": ""} for i in range(3)]
    mods.watchlist.record_sightings(jobs)
    assert not any("repost_of" in j for j in jobs)


def test_the_hint_key_folds_plurals(mods):
    hk = mods.companies.company_hint_key
    assert hk("Bright Visions Technologies") == hk("Bright Vision Technologies")
    assert hk("Glass") == "glass"  # -ss isn't a plural
    assert mods.companies.company_key("Bright Visions") != mods.companies.company_key(
        "Bright Vision"
    )  # exact stays exact


def test_the_plural_fold_spares_one_word_brands_and_keeps_possessives(mods):
    hk = mods.companies.company_hint_key
    for a, b in (("Atlas", "Atla"), ("Canvas", "Canva"), ("Brooks", "Brook")):
        assert hk(a) != hk(b), (a, b)
    assert hk("Moody's") == hk("Moodys") and hk("McDonald’s") == hk("McDonalds")
    assert hk("Wells Fargo") == "wellsfargo"  # only the last word of a name folds
    assert "atla" not in mods.boards.slug_candidates("Atlas")


def test_watch_list_dedupes_before_inbox_and_keeps_quota(watch_env, mods, monkeypatch):
    plugin, reg, cfg, calls = _with_jsearch(
        watch_env, monkeypatch, lambda r: httpx.Response(200, json=JSEARCH, headers=_quota(1000))
    )
    cfg["jobs_provider"] = "jsearch,himalayas"
    js = importlib.import_module(plugin.__name__ + ".search.jobsource")

    async def duplicate(query, country=""):
        job = js.parse_jsearch(JSEARCH)[0]
        return [{**job, "company": "Googlejobs Co Inc.", "url": job["url"] + "/?ref=other", "source": "himalayas"}]

    monkeypatch.setattr(js, "_get_himalayas", duplicate)
    got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    matches = [j for j in got if j["company"] == "Googlejobs Co"]
    assert len(matches) == 1
    assert matches[0]["sources"] == ["jsearch", "himalayas"]
    saved = [j for j in mods.watchlist.inbox_for_view() if j["company"] == "Googlejobs Co"]
    assert len(saved) == 1 and saved[0]["sources"] == ["jsearch", "himalayas"]
    assert len(calls) == 3
    assert mods.watchlist.jsearch_allowance() is not None
    again = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert not [j for j in again if j["company"] == "Googlejobs Co"]


@pytest.mark.parametrize("selection", ["typo", "himalayas,typo"])
def test_invalid_watch_market_selection_preserves_board_results(watch_env, mods, caplog, selection):
    plugin, reg, cfg = watch_env
    cfg["jobs_provider"] = selection
    with caplog.at_level("INFO"):
        got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert any(j["company"] == "Acme" for j in got)
    assert mods.watchlist.inbox_for_view()
    assert mods.watchlist.watch_last_run()
    assert "unknown job source 'typo'" in caplog.text


@pytest.mark.parametrize("selection", ["openwebninja,himalayas", "all,openwebninja"])
@pytest.mark.parametrize("allowance", [0, 2, None])
def test_watch_openwebninja_obeys_budget_counts_calls_and_spaces_requests(
    watch_env, mods, monkeypatch, selection, allowance
):
    plugin, reg, cfg = watch_env
    cfg.update(jobs_provider=selection, jobs_api_key="KEY")
    calls, notes, sleeps = [], [], []

    def handler(request):
        if request.url.host == "api.openwebninja.com":
            calls.append(request)
            return httpx.Response(200, json=JSEARCH)
        if request.url.host == "himalayas.app":
            return httpx.Response(200, json=HIMALAYAS)
        return _route(request)

    async def sleep(seconds):
        sleeps.append(seconds)

    js = importlib.import_module(plugin.__name__ + ".search.jobsource")

    async def hn(query):
        return []

    monkeypatch.setattr(js, "_get_hn", hn)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(
        mods.watchlist, "jsearch_allowance", lambda: None if allowance is None else allowance - len(notes)
    )
    monkeypatch.setattr(mods.watchlist, "note_jsearch_call", notes.append)
    sweep = _sweep(plugin)
    monkeypatch.setattr(sweep.asyncio, "sleep", sleep)
    got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    expected = sweep.KEYED_QUERIES_DEFAULT if allowance is None else allowance
    assert len(calls) == expected
    assert notes == [{}] * expected  # this vendor currently returns no quota headers
    assert [s for s in sleeps if s == sweep.KEYED_SPACING_S] == [sweep.KEYED_SPACING_S] * max(0, expected - 1)
    assert any(j["company"] == "Newco" for j in got)
    assert mods.watchlist.watch_last_run()


def test_watch_openwebninja_refusal_stops_keyed_requests_only(watch_env, mods, monkeypatch, caplog):
    plugin, reg, cfg = watch_env
    cfg.update(jobs_provider="openwebninja,himalayas", jobs_api_key="KEY")
    calls = []

    def handler(request):
        if request.url.host == "api.openwebninja.com":
            calls.append(request)
            return httpx.Response(403, json={"message": "Invalid key"})
        if request.url.host == "himalayas.app":
            return httpx.Response(200, json=HIMALAYAS)
        return _route(request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw))
    with caplog.at_level("WARNING"):
        got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 1
    assert any(j["company"] == "Newco" for j in got)
    assert "OpenWeb Ninja" in caplog.text
    assert mods.watchlist.watch_last_run()


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("selection", ["auto", "jsearch", "jsearch,himalayas"])
def test_watch_refusal_stops_pass_and_cooldown_survives_next_pass(watch_env, mods, monkeypatch, status, selection):
    plugin, reg, cfg, calls = _with_jsearch(watch_env, monkeypatch, lambda r: httpx.Response(status))
    cfg["jobs_provider"] = selection
    first = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 1
    assert any(j["company"] == "Newco" and j["source"] == "himalayas" for j in first)
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == (1 if status == 429 else 2)


# ── a keyed source's month: every request counted, the last fifth kept from the watch ─────────────
_KEYED_HOSTS = {"jsearch": "jsearch.p.rapidapi.com", "openwebninja": "api.openwebninja.com"}
_CHAT = {"query": "Forward Deployed Engineer"}


def _keyed_month(watch_env, monkeypatch, source: str, respond=None, **settings):
    """The watch env with ``source`` as the keyed job source (no allowance headers, like OpenWeb
    Ninja) beside Himalayas. Returns ``(plugin, reg, cfg, calls, chat)``: ``calls`` logs the keyed
    requests, ``chat`` is the chat search tool on the same config."""
    plugin, reg, cfg = watch_env
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _KEYED_HOSTS[source]:
            calls.append(request)
            return respond(request) if respond else httpx.Response(200, json=JSEARCH)
        if request.url.host == "himalayas.app":
            return httpx.Response(200, json=HIMALAYAS)
        return _route(request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw))
    _no_spacing(monkeypatch, plugin)
    cfg.update(jobs_api_key="KEY", jobs_provider=f"{source},himalayas", extended_tools=True, **settings)
    plugin.register(reg)
    return plugin, reg, cfg, calls, {t.name: t for t in reg.tools}["careercoach_search_jobs"]


def _counted(mods, source: str, used: int) -> None:
    """Put ``used`` requests on this month's count for ``source``."""
    wl = mods.watchlist
    wl._save(wl.SOURCE_MONTHS, {source: {"month": wl._quota_now(None).strftime("%Y-%m"), "used": used}})


def _clock(mods, monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(mods.watchlist, "_quota_now", lambda now=None: now or when)


@pytest.mark.parametrize("source", ["jsearch", "openwebninja"])
def test_every_keyed_request_counts_against_the_month_from_the_watch_and_from_chat(
    watch_env, mods, monkeypatch, source
):
    plugin, reg, cfg, calls, chat = _keyed_month(watch_env, monkeypatch, source)
    assert mods.watchlist.month_usage(source)["used"] == 0
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 3 and mods.watchlist.month_usage(source)["used"] == 3  # the watch's three
    out = _run(chat, _CHAT)
    assert len(calls) == 4 and mods.watchlist.month_usage(source)["used"] == 4  # and the chat search's one
    assert "196 of 200 requests left this month" in out
    other = next(s for s in _KEYED_HOSTS if s != source)
    assert mods.watchlist.month_usage(other)["used"] == 0  # one count per source


def test_a_keyed_request_that_times_out_still_counts(watch_env, mods, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    _plugin, _reg, _cfg, calls, chat = _keyed_month(watch_env, monkeypatch, "openwebninja", respond=timeout)
    out = _run(chat, _CHAT)
    assert len(calls) == 1 and mods.watchlist.month_usage("openwebninja")["used"] == 1
    assert "Newco" in out  # answered by the keyless board


def test_a_refused_keyed_request_is_not_counted(watch_env, mods, monkeypatch):
    """A rejected key is asked once a pass, forever: counting it would end the month on a typo."""
    _plugin, _reg, _cfg, calls, chat = _keyed_month(
        watch_env, monkeypatch, "openwebninja", respond=lambda r: httpx.Response(401, json={"message": "bad key"})
    )
    _run(chat, _CHAT)
    assert len(calls) == 1 and mods.watchlist.month_usage("openwebninja")["used"] == 0


def test_at_80_percent_the_watch_leaves_the_keyed_source_for_the_daily_report(watch_env, mods, monkeypatch, caplog):
    plugin, reg, cfg, calls, _chat = _keyed_month(watch_env, monkeypatch, "openwebninja")
    _counted(mods, "openwebninja", 160)
    with caplog.at_level("INFO"):
        got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert calls == []  # 160 of 200: the watch's share is used
    assert any(j["company"] == "Newco" and j["source"] == "himalayas" for j in got)  # keyless still swept
    assert "OpenWeb Ninja (JSearch) has used 160 of this month's 200 requests" in caplog.text
    assert "reserved for the daily report" in caplog.text
    assert mods.watchlist.month_usage("openwebninja")["used"] == 160
    assert mods.watchlist.watch_last_run()
    caplog.clear()
    with caplog.at_level("INFO"):
        asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert calls == [] and "reserved for the daily report" not in caplog.text  # said once, not every pass


def test_the_watch_stops_mid_pass_when_it_reaches_80_percent(watch_env, mods, monkeypatch):
    plugin, reg, cfg, calls, _chat = _keyed_month(watch_env, monkeypatch, "openwebninja")
    _counted(mods, "openwebninja", 158)
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 2 and mods.watchlist.month_usage("openwebninja")["used"] == 160


def test_a_keyed_source_alone_at_80_percent_still_gets_a_keyless_sweep(watch_env, mods, monkeypatch):
    plugin, reg, cfg, calls, _chat = _keyed_month(watch_env, monkeypatch, "openwebninja")
    cfg["jobs_provider"] = "openwebninja"
    _counted(mods, "openwebninja", 160)
    got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert calls == [] and any(j["company"] == "Newco" for j in got)  # Himalayas stands in


def test_at_80_percent_a_chat_search_still_uses_the_keyed_source(watch_env, mods, monkeypatch):
    _plugin, _reg, _cfg, calls, chat = _keyed_month(watch_env, monkeypatch, "openwebninja")
    _counted(mods, "openwebninja", 160)
    out = _run(chat, _CHAT)
    assert len(calls) == 1 and "Googlejobs Co" in out
    assert "39 of 200 requests left this month" in out
    assert "used up" not in out


@pytest.mark.parametrize("selection", ["openwebninja", "openwebninja,himalayas"])
def test_at_100_percent_a_chat_search_skips_the_keyed_source_and_says_when_it_refills(
    watch_env, mods, monkeypatch, selection
):
    _plugin, _reg, cfg, calls, chat = _keyed_month(watch_env, monkeypatch, "openwebninja")
    cfg["jobs_provider"] = selection
    _clock(mods, monkeypatch, datetime(2026, 11, 18, 9, tzinfo=UTC))
    _counted(mods, "openwebninja", 200)
    out = _run(chat, _CHAT)
    assert calls == []  # no request is spent finding out
    assert "Newco" in out and "Himalayas" in out  # answered by the keyless board
    assert "this month's 200 requests are used up; they refill on 2026-12-01 (UTC)" in out
    assert mods.watchlist.month_usage("openwebninja")["used"] == 200
    assert mods.watchlist.source_cooldown("openwebninja") == ""  # a spent month is not a 429 cooldown


def test_a_new_month_starts_the_count_at_zero_and_the_watch_returns(watch_env, mods, monkeypatch):
    plugin, reg, cfg, calls, _chat = _keyed_month(watch_env, monkeypatch, "openwebninja")
    _clock(mods, monkeypatch, datetime(2026, 11, 30, 23, 59, tzinfo=UTC))
    _counted(mods, "openwebninja", 200)
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert calls == [] and mods.watchlist.month_usage("openwebninja")["refill"] == "2026-12-01"
    _clock(mods, monkeypatch, datetime(2026, 12, 1, 0, 1, tzinfo=UTC))
    assert mods.watchlist.month_usage("openwebninja")["used"] == 0
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    usage = mods.watchlist.month_usage("openwebninja")
    assert len(calls) == 3 and usage["used"] == 3 and usage["refill"] == "2027-01-01"
    assert not usage["reserved"]
    # and the once-a-month line is due again when December's share is used
    assert mods.watchlist.note_watch_reserved("openwebninja") is True


def test_a_monthly_limit_of_zero_disables_the_cap(watch_env, mods, monkeypatch):
    plugin, reg, cfg, calls, chat = _keyed_month(watch_env, monkeypatch, "openwebninja", jobs_api_monthly_limit=0)
    _counted(mods, "openwebninja", 500)
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 3  # no reserve
    out = _run(chat, _CHAT)
    assert len(calls) == 4 and "used up" not in out  # no stop
    assert "504 request(s) sent this month, no monthly limit set" in out  # still counted
    usage = mods.watchlist.month_usage("openwebninja", 0)
    assert usage["left"] is None and not usage["reserved"] and not usage["spent"]


def test_the_monthly_limit_setting_moves_the_reserve(watch_env, mods, monkeypatch):
    plugin, reg, cfg, calls, chat = _keyed_month(watch_env, monkeypatch, "openwebninja", jobs_api_monthly_limit="10")
    _counted(mods, "openwebninja", 8)
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert calls == []  # 8 of 10 is the watch's share
    assert "1 of 10 requests left this month" in _run(chat, _CHAT)
    _run(chat, _CHAT)
    assert "this month's 10 requests are used up" in _run(chat, _CHAT) and len(calls) == 2
    assert mods.watchlist.monthly_limit({"jobs_api_monthly_limit": "junk"}) == 200
    assert mods.watchlist.monthly_limit({}) == 200 and mods.watchlist.monthly_limit({"jobs_api_monthly_limit": -5}) == 0


@pytest.mark.parametrize("garbage", ["{not json", "[1, 2]", '{"openwebninja": "x", "jsearch": {"used": "many"}}'])
def test_an_unreadable_quota_file_never_fails_a_search_or_a_watch_tick(watch_env, mods, monkeypatch, iso, garbage):
    plugin, reg, cfg, calls, chat = _keyed_month(watch_env, monkeypatch, "openwebninja")
    wl = mods.watchlist
    for name in (wl.SOURCE_MONTHS, wl.JSEARCH_QUOTA):
        wl._path(name).write_text(garbage, encoding="utf-8")
    assert wl.month_usage("openwebninja")["used"] == 0  # reads as nothing used
    out = _run(chat, _CHAT)
    assert "Googlejobs Co" in out and "Job search failed" not in out
    got = asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert any(j["company"] == "Newco" for j in got)
    assert len(calls) == 4 and wl.month_usage("openwebninja")["used"] == 4  # the count starts over


def test_an_unwritable_month_count_never_fails_a_search(watch_env, mods, monkeypatch):
    _plugin, _reg, _cfg, calls, chat = _keyed_month(watch_env, monkeypatch, "openwebninja")
    real = mods.watchlist._save

    def save(name, data):
        if name == mods.watchlist.SOURCE_MONTHS:
            raise OSError("disk full")
        real(name, data)

    monkeypatch.setattr(mods.watchlist, "_save", save)
    assert "Googlejobs Co" in _run(chat, _CHAT) and len(calls) == 1
    assert mods.watchlist.note_watch_reserved("openwebninja") is True


def test_a_vendor_reported_limit_and_reset_win_over_the_setting_and_the_calendar(mods):
    wl = mods.watchlist
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    wl.note_month_call("jsearch", {}, now)
    assert wl.month_usage("jsearch", 200, now)["used"] == 1
    # RapidAPI says 1000 a period, 150 left, refilling in ten days (5 December)
    wl.note_month_call("jsearch", {"limit": 1000, "remaining": 150, "reset_s": 10 * 86400}, now)
    usage = wl.month_usage("jsearch", 200, now)
    assert (usage["used"], usage["limit"], usage["left"], usage["refill"]) == (850, 1000, 150, "2026-12-05")
    assert usage["reserved"] and not usage["spent"]
    wl.note_month_call("jsearch", {}, now)  # an answer with no headers keeps the vendor's numbers
    assert wl.month_usage("jsearch", 200, now)["used"] == 851
    december = datetime(2026, 12, 2, tzinfo=UTC)  # a new calendar month, but the vendor's period runs on
    assert wl.month_usage("jsearch", 200, december)["used"] == 851
    refilled = wl.month_usage("jsearch", 200, datetime(2026, 12, 5, 12, 1, tzinfo=UTC))
    assert (refilled["used"], refilled["limit"], refilled["refill"]) == (0, 1000, "2027-01-01")
    assert wl.month_usage("jsearch", 0, now)["limit"] == 0  # 0 turns the cap off whatever the vendor says
    assert wl.month_usage("openwebninja", 200, now)["used"] == 0


def test_the_reserve_sits_on_top_of_rapidapis_daily_spread(watch_env, mods, monkeypatch):
    """#133's daily allowance still spreads the plan; the 80% reserve is a second cap, not a swap."""
    left = [60]

    def respond(request):
        left[0] -= 1
        return httpx.Response(200, json=JSEARCH, headers=_quota(left[0]))

    plugin, reg, cfg, calls = _with_jsearch(watch_env, monkeypatch, respond)
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 2  # 59 left over 30 days: the daily spread allows two (#133)
    assert mods.watchlist.jsearch_allowance() == 0
    assert mods.watchlist.month_usage("jsearch")["used"] == 142
    left[0] = 41  # other clients of the key spent more: the next answer reports 40 left
    monkeypatch.setattr(mods.watchlist, "jsearch_allowance", lambda: 5)  # a new day's share
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 3 and mods.watchlist.month_usage("jsearch")["used"] == 160
    asyncio.run(plugin._watch_tick(reg, cfg, 0))
    assert len(calls) == 3  # 160 of 200: reserved, though the daily share allows more
