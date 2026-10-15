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
def test_company_key_folds_legal_and_generic_suffixes(mods):
    ck = mods.companies.company_key
    assert ck("Arize AI") == ck("arize") == "arize"
    assert ck("Scale AI, Inc.") == ck("Scale") == "scale"
    assert ck("DPR COnstruction") == ck("DPR Construction")
    assert ck("Change.org") == "changeorg"
    assert ck("AI") == "ai"  # never stripped to nothing
    assert ck("The Browser Company") == "browser"


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
    _, again = wl.add_company("Scale", "greenhouse", "scale")
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
def test_tracker_dedups_on_normalized_company_and_url(mods):
    st = mods.state
    st.track_application(company="Arize AI", role="Design Engineer", fit_score=83, source="https://x/1")
    st.track_application(company="arize", role="design engineer", status="applied")  # status-only update
    rows = st.load_applications()
    assert len(rows) == 1 and rows[0]["status"] == "applied" and rows[0]["fit_score"] == 83  # score kept
    st.track_application(company="Arize", role="Retitled Design Engineer II", source="https://x/1/")
    assert len(st.load_applications()) == 1  # same posting URL → same application


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
    out = t.invoke({"company": "Acme AI", "role": "Role 4", "status": "applied"})
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
