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


@pytest.fixture
def mods(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))

    class M:
        pass

    m = M()
    for name in ("boards", "companies", "watchlist", "contacts", "scan", "state"):
        setattr(m, name, importlib.import_module(f"{plugin.__name__}.{name}"))
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
    assert "Not found on Ashby, Greenhouse or Lever: Nowhere" in out
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


# ── review regressions (PR #29) ───────────────────────────────────────────────
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
    first = asyncio.run(plugin._watch_tick(reg, cfg, 75))
    second = asyncio.run(plugin._watch_tick(reg, cfg, 75))
    assert len(first) == 15 and len(second) >= 5
    assert not {m["url"] for m in first} & {m["url"] for m in second}


# ── review round 2 ────────────────────────────────────────────────────────────
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


def test_watch_records_fit_zero_and_gates_on_the_whole_role(mods):
    bp = mods.boards and importlib.import_module(mods.state.__name__.rsplit(".", 1)[0] + ".jobsource").best_prescore
    roles = "AI Engineer, Forward Deployed Engineer"
    assert bp({"title": "Solutions Engineer"}, roles) < 75  # "AI" counts — not just "engineer"
    assert bp({"title": "Senior AI Engineer"}, roles) == 100
