"""Workday and SmartRecruiters boards: parsers, slugs from pasted URLs, paged + searched fetches,
the watchlist pin, the scan and the resolve lookup. Payloads are trimmed from real responses
(2026-11-18: Workday's own board ``workday/wd5/Workday`` and ServiceNow's SmartRecruiters board).
No live network: httpx is routed to a MockTransport."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import test_hunt
from test_hunt import _run, hunt_tools, net  # noqa: F401 — fixtures

mods = test_hunt.mods
NOW = datetime(2026, 11, 18, 18, 0, tzinfo=UTC)
LONG = "Build the agent platform our customers run on. Python, TypeScript, React, LLM evals. " * 10

# ── Workday ───────────────────────────────────────────────────────────────────
WD_SLUG = "workday/wd5/Workday"
WD_LIST = {
    "total": 3,
    "jobPostings": [
        {
            "title": "Software Engineer/Senior Software Engineer - AI Platform (Agent Cell)",
            "externalPath": "/job/USA-CO-Boulder/Software-Engineer-Senior-Software-Engineer---AI-Platform--Agent-Runtime-_JR-0109507",
            "locationsText": "3 Locations",
            "postedOn": "Posted 2 Days Ago",
            "remoteType": "Flex",
            "bulletFields": ["JR-0109507"],
        },
        {
            "title": "Senior Frontend Engineer",
            "externalPath": "/job/USA-NY-New-York-City/Senior-Frontend-Engineer_JR-0111619",
            "locationsText": "USA, NY, Remote",
            "postedOn": "Posted Today",
            "remoteType": "Remote",
            "bulletFields": ["JR-0111619"],
        },
        {
            "title": "Medium Enterprise | Customer Base | Account Executive - Nonprofit",
            "externalPath": "/job/USA-IL-Chicago/Medium-Enterprise---Customer-Base---Account-Executive---Nonprofit_JR-0111553",
            "locationsText": "2 Locations",
            "postedOn": "Posted 30+ Days Ago",
            "remoteType": "Onsite",
            "bulletFields": ["JR-0111553"],
        },
    ],
    "facets": [],
    "userAuthenticated": False,
}
WD_DETAIL = {
    "jobPostingInfo": {
        "id": "a745735e75c510008ce7cbf172480000",
        "title": "Senior Frontend Engineer",
        "jobDescription": f"<p><b>Your work days are brighter here.</b></p><p>{LONG}</p>",
        "location": "USA, NY, New York City",
        "additionalLocations": ["USA, NJ, Remote", "USA, MA, Boston"],
        "postedOn": "Posted Today",
        "startDate": "2026-11-18",
        "timeType": "Full Time",
        "jobReqId": "JR-0111619",
        "country": {"descriptor": "United States of America", "id": "bc33aa3152ec42d4995f4791a106ed09"},
        "jobRequisitionLocation": {
            "descriptor": "USA, NY, New York City",
            "country": {"descriptor": "United States of America", "alpha2Code": "US"},
        },
        "remoteType": "Remote",
        "externalUrl": "https://workday.wd5.myworkdayjobs.com/Workday/job/USA-NY-New-York-City/Senior-Frontend-Engineer_JR-0111619",
    },
    "hiringOrganization": {"name": "Workday, Inc.", "url": ""},
}
WD_DETAIL_ONE_PLACE = {
    "jobPostingInfo": {
        "title": "Software Engineer/Senior Software Engineer - AI Platform (Agent Cell)",
        "jobDescription": f"<p>{LONG}</p>",
        "location": "USA.VA.Reston",
        "additionalLocations": None,
        "postedOn": "Posted 30+ Days Ago",
        "startDate": "2026-06-09",
        "country": {"descriptor": "United States of America"},
        "remoteType": "Onsite",
    }
}


def test_workday_slug_from_a_slug_or_any_careers_url(mods):
    ws = mods.boards.workday_slug
    assert ws("https://nike.wd1.myworkdayjobs.com/nke") == "nike/wd1/nke"
    assert ws("https://workday.wd5.myworkdayjobs.com/en-US/Workday") == WD_SLUG
    assert ws("workday.wd5.myworkdayjobs.com/en-US/Workday/job/USA-CA/Engineer_JR-1?source=x") == WD_SLUG
    assert ws("https://nike.wd1.myworkdayjobs.com/wday/cxs/nike/nke/jobs") == "nike/wd1/nke"
    assert ws("Nike/WD1/nke") == "nike/wd1/nke"  # tenant and data center fold; the site keeps its case
    for bad in ("nike", "nike/nke", "https://nike.wd1.myworkdayjobs.com/", "https://example.com/careers"):
        assert ws(bad) == ""
    bfu = mods.boards.board_from_url
    assert bfu("https://nike.wd1.myworkdayjobs.com/nke") == ("workday", "nike/wd1/nke")
    assert bfu("https://jobs.smartrecruiters.com/ServiceNow/744000162408293-sr-manager") == (
        "smartrecruiters",
        "ServiceNow",
    )
    assert bfu("https://jobs.ashbyhq.com/acme") == ("ashby", "acme")
    assert bfu("https://job-boards.greenhouse.io/globex/jobs/1") == ("greenhouse", "globex")
    assert bfu("nike/wd1/nke") == ("", "")  # a bare slug is not a URL


def test_workday_posted_age_text_becomes_an_approximate_date(mods):
    wp = mods.boards.workday_posted
    assert wp("Posted Today", NOW) == NOW.isoformat(timespec="seconds")
    assert wp("Posted Yesterday", NOW)[:10] == "2026-11-17"
    assert wp("Posted 3 Days Ago", NOW)[:10] == "2026-11-15"
    assert wp("Posted 30+ Days Ago", NOW)[:10] == "2026-10-19"  # a floor: drops out of a 30-day filter
    assert wp("", NOW) == "" and wp("Hiring now", NOW) == ""


def test_parse_workday_list_rows_and_detail_rows(mods):
    b = mods.boards
    path = WD_LIST["jobPostings"][1]["externalPath"]
    jobs = b.parse_workday(WD_LIST, company="Workday", slug=WD_SLUG, details={path: WD_DETAIL}, now=NOW)
    agent, fe, ae = jobs
    # list only: no description read, so no "short description" warning; "3 Locations" reads as the
    # primary location in the job path
    assert agent["desc_len"] is None and agent["description"] == ""
    assert agent["location"] == "USA CO Boulder (+2 more)" and agent["remote"] is False  # Flex
    assert agent["posted"][:10] == "2026-11-16"
    assert agent["url"] == "https://workday.wd5.myworkdayjobs.com/Workday" + WD_LIST["jobPostings"][0]["externalPath"]
    assert agent["id"].startswith("workday:workday/wd5/Workday:Software-Engineer")
    # detail: every location, the description, the start date, the external URL
    assert fe["remote"] is True and fe["posted"] == "2026-11-18T00:00:00+00:00"
    assert fe["location"] == "USA, NY, New York City / USA, NJ, Remote / USA, MA, Boston"
    assert fe["countries"] == []  # several places: one country can't stand for them all
    assert fe["desc_len"] > 400 and "<" not in fe["snippet"]
    assert fe["url"] == WD_DETAIL["jobPostingInfo"]["externalUrl"]
    assert ae["remote"] is False and ae["posted"][:10] == "2026-10-19"
    one = b.parse_workday(
        {"jobPostings": [WD_LIST["jobPostings"][0]]}, company="Workday", slug=WD_SLUG,
        details={WD_LIST["jobPostings"][0]["externalPath"]: WD_DETAIL_ONE_PLACE}, now=NOW,
    )[0]  # fmt: skip
    assert one["countries"] == ["United States of America"] and one["posted"][:10] == "2026-06-09"
    assert b.location_matches(fe, "remote") and not b.location_matches(one, "remote")


def test_workday_location_filter_reads_the_list_summary(mods):
    b = mods.boards
    jobs = b.parse_workday(WD_LIST, company="Workday", slug=WD_SLUG, now=NOW)
    assert b.location_matches(jobs[1], "remote")  # "USA, NY, Remote" + remoteType Remote
    assert b.location_matches(jobs[0], "boulder")


def _wd_handler(calls: list, *, pages: dict | None = None, status: int = 200):
    """Serve the Workday list (one page per offset, per searchText) and the one detail."""
    pages = pages or {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content.decode() if request.content else ""))
        if request.url.host != "workday.wd5.myworkdayjobs.com":
            return httpx.Response(404, json={})
        if status != 200:
            return httpx.Response(status, json={"errorCode": f"HTTP_{status}"})
        if request.method == "POST" and request.url.path == "/wday/cxs/workday/Workday/jobs":
            body = json.loads(request.content)
            assert body["limit"] == 20  # Workday answers a larger page with HTTP 400
            key = (body["searchText"], body["offset"])
            return httpx.Response(200, json=pages.get(key, {"total": 0, "jobPostings": []}))
        if request.method == "GET" and request.url.path.endswith("Senior-Frontend-Engineer_JR-0111619"):
            return httpx.Response(200, json=WD_DETAIL)
        return httpx.Response(404, json={})

    return handler


def _mock(monkeypatch, handler):
    real = test_hunt._ASYNC_CLIENT
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_workday_fetch_searches_each_role_and_reads_only_title_matches(mods, monkeypatch):
    calls: list = []
    rows = WD_LIST["jobPostings"]
    pages = {
        ("Frontend Engineer", 0): {"total": 2, "jobPostings": [rows[1], rows[2]]},
        ("AI Engineer", 0): {"total": 1, "jobPostings": [rows[1]]},  # the same posting twice: one row
    }
    _mock(monkeypatch, _wd_handler(calls, pages=pages))
    jobs = asyncio.run(mods.boards.fetch_board("workday", WD_SLUG, "Workday", search="Frontend Engineer, AI Engineer"))
    assert [j["title"] for j in jobs] == ["Senior Frontend Engineer", rows[2]["title"]]
    searched = [json.loads(c[2])["searchText"] for c in calls if c[0] == "POST"]
    assert searched == ["Frontend Engineer", "AI Engineer"]
    gets = [c[1] for c in calls if c[0] == "GET"]
    assert len(gets) == 1 and gets[0].endswith("Senior-Frontend-Engineer_JR-0111619")  # not the AE
    assert jobs[0]["desc_len"] > 400 and jobs[1]["desc_len"] is None


def test_workday_pages_until_a_short_page_and_stops_at_the_cap(mods, monkeypatch):
    calls: list = []
    row = WD_LIST["jobPostings"][2]

    def page(offset, n):
        return {"total": 45 if offset == 0 else 0, "jobPostings": [{**row, "externalPath": f"/job/x/AE_{offset + i}"} for i in range(n)]}  # fmt: skip

    _mock(monkeypatch, _wd_handler(calls, pages={("", 0): page(0, 20), ("", 20): page(20, 20), ("", 40): page(40, 5)}))
    assert len(asyncio.run(mods.boards.fetch_board("workday", WD_SLUG))) == 45
    assert [json.loads(c[2])["offset"] for c in calls] == [0, 20, 40]
    monkeypatch.setattr(mods.boards, "MAX_POSTINGS", 20)
    calls.clear()
    assert len(asyncio.run(mods.boards.fetch_board("workday", WD_SLUG))) == 20 and len(calls) == 1


def test_a_missing_workday_board_is_not_found_and_a_rate_limit_is_not(mods, monkeypatch):
    for status in (404, 422):  # unknown site / unknown tenant or data center
        _mock(monkeypatch, _wd_handler([], status=status))
        with pytest.raises(mods.boards.BoardNotFound):
            asyncio.run(mods.boards.fetch_board("workday", WD_SLUG))
    _mock(monkeypatch, _wd_handler([], status=429))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(mods.boards.fetch_board("workday", WD_SLUG))
    with pytest.raises(ValueError, match="tenant/wdN/site"):
        asyncio.run(mods.boards.fetch_board("workday", "nike"))


# ── SmartRecruiters ───────────────────────────────────────────────────────────
SR_LIST = {
    "offset": 0,
    "limit": 100,
    "totalFound": 3,
    "content": [
        {
            "id": "744000162190099",
            "name": "Software Engineer, Agentic Systems - Moveworks",
            "company": {"identifier": "ServiceNow", "name": "ServiceNow"},
            "releasedDate": "2026-11-18T00:35:19.111Z",
            "location": {
                "city": "Mountain View",
                "region": "CALIFORNIA",
                "country": "us",
                "remote": False,
                "hybrid": False,
                "fullLocation": "Mountain View, CALIFORNIA, United States",
            },
            "department": {},
            "function": {"id": "engineering", "label": "Engineering"},
            "ref": "https://api.smartrecruiters.com/v1/companies/ServiceNow/postings/744000162190099",
        },
        {
            "id": "744000162298199",
            "name": "Software Engineer",
            "company": {"identifier": "ServiceNow", "name": "ServiceNow"},
            "releasedDate": "2026-11-18T13:35:38.802Z",
            "location": {
                "city": "Dublin",
                "country": "ie",
                "remote": False,
                "hybrid": True,
                "fullLocation": "Dublin, , Ireland",
            },
            "department": {},
            "function": {"label": "Engineering"},
        },
        {
            "id": "744000162408293",
            "name": "Sr. Customer Success Manager - Moveworks",
            "company": {"identifier": "ServiceNow", "name": "ServiceNow"},
            "releasedDate": "2026-11-18T23:48:46.403Z",
            "location": {"city": "San Diego", "region": "California", "country": "us", "remote": True},
            "department": {"id": "2294004", "label": "Customer Outcomes"},
        },
    ],
}
SR_DETAIL = {
    "id": "744000162190099",
    "name": "Software Engineer, Agentic Systems - Moveworks",
    "postingUrl": "https://jobs.smartrecruiters.com/ServiceNow/744000162190099-software-engineer-agentic-systems-moveworks",
    "applyUrl": "https://jobs.smartrecruiters.com/ServiceNow/744000162190099-software-engineer-agentic-systems-moveworks?oga=true",
    "jobAd": {
        "sections": {
            "companyDescription": {
                "title": "Company Description",
                "text": "<p>At ServiceNow, our technology makes the world work.</p>",
            },
            "jobDescription": {"title": "Job Description", "text": f"<p>{LONG}</p>"},
            "qualifications": {"title": "Qualifications", "text": "<ul><li>Python</li></ul>"},
            "additionalInformation": {"title": "Additional Information", "text": "<p>Base pay $150,000 - $260,000</p>"},
        }
    },
}


def test_parse_smartrecruiters_rows_and_details(mods):
    b = mods.boards
    jobs = b.parse_smartrecruiters(
        SR_LIST, company="ServiceNow", slug="ServiceNow", details={"744000162190099": SR_DETAIL}
    )
    agentic, dublin, csm = jobs
    assert agentic["url"] == SR_DETAIL["postingUrl"] and agentic["posted"] == "2026-11-18T00:35:19+00:00"
    assert agentic["countries"] == ["United States"] and agentic["remote"] is False
    assert agentic["description"].startswith("Build the agent platform")  # the role before the company blurb
    assert agentic["description"].endswith("our technology makes the world work.")
    assert agentic["department"] == "Engineering" and agentic["company"] == "ServiceNow"
    assert dublin["location"] == "Dublin, Ireland" and dublin["countries"] == ["IE"] and dublin["desc_len"] is None
    assert dublin["url"] == "https://jobs.smartrecruiters.com/ServiceNow/744000162298199"
    assert csm["remote"] is True and csm["location"] == "Remote - San Diego, California, US"
    assert csm["department"] == "Customer Outcomes"
    assert b.location_matches(csm, "remote") and not b.location_matches(dublin, "remote")


def _sr_handler(calls: list, pages: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host != "api.smartrecruiters.com":
            return httpx.Response(404, json={})
        parts = request.url.path.split("/")  # /v1/companies/{company}/postings[/{id}]
        if len(parts) == 6:
            return (
                httpx.Response(200, json=SR_DETAIL) if parts[5] == "744000162190099" else httpx.Response(404, json={})
            )
        key = (parts[3].lower(), request.url.params.get("q", ""), int(request.url.params["offset"]))
        # an unknown company is an empty board, never a 404 — as the live API answers
        return httpx.Response(200, json=pages.get(key, {"offset": 0, "limit": 100, "totalFound": 0, "content": []}))

    return handler


def test_smartrecruiters_fetch_searches_and_reads_matching_details(mods, monkeypatch):
    calls: list = []
    rows = SR_LIST["content"]
    pages = {("servicenow", "AI Engineer", 0): {"totalFound": 2, "content": rows[:2]}}
    _mock(monkeypatch, _sr_handler(calls, pages))
    jobs = asyncio.run(mods.boards.fetch_board("smartrecruiters", "ServiceNow", "ServiceNow", search=["AI Engineer"]))
    assert [j["title"] for j in jobs] == [rows[0]["name"], "Software Engineer"]
    # "Software Engineer, Agentic Systems" doesn't hold "AI": neither title matches, so no detail reads
    assert not any("/postings/7440" in c for c in calls)
    calls.clear()
    pages[("servicenow", "Software Engineer", 0)] = {"totalFound": 2, "content": rows[:2]}
    jobs = asyncio.run(mods.boards.fetch_board("smartrecruiters", "ServiceNow", search="Software Engineer"))
    assert sum("/postings/7440" in c for c in calls) == 2 and jobs[0]["desc_len"] > 400


def test_smartrecruiters_pages_by_its_total(mods, monkeypatch):
    calls: list = []
    row = SR_LIST["content"][2]
    pages = {
        ("servicenow", "", off): {"totalFound": 150, "content": [{**row, "id": str(off + i)} for i in range(min(100, 150 - off))]}
        for off in (0, 100)
    }  # fmt: skip
    _mock(monkeypatch, _sr_handler(calls, pages))
    assert len(asyncio.run(mods.boards.fetch_board("smartrecruiters", "ServiceNow"))) == 150 and len(calls) == 2


def test_probe_finds_smartrecruiters_but_never_guesses_workday(mods, monkeypatch):
    calls: list = []
    pages = {("servicenow", "", 0): SR_LIST}
    _mock(monkeypatch, _sr_handler(calls, pages))
    found, problems = asyncio.run(mods.boards.probe("ServiceNow"))
    assert found == [("smartrecruiters", "servicenow", 3)] and problems == []
    assert not any("myworkdayjobs" in c for c in calls)
    # a shortened guess must name the company asked for — SmartRecruiters says whose board it is
    found, problems = asyncio.run(mods.boards.probe("ServiceNow Labs"))
    assert found == [] and problems == ["smartrecruiters/servicenow is 'ServiceNow''s board, not 'ServiceNow Labs'"]


# ── the tools and the lookups ─────────────────────────────────────────────────
@pytest.fixture
def board_net(monkeypatch):
    """Both new boards on one transport (the watchlist tool, the scan and resolve share it)."""
    calls: list = []
    rows = WD_LIST["jobPostings"]
    wd = _wd_handler(
        calls,
        pages={
            ("", 0): WD_LIST,
            ("Design Engineer", 0): {"total": 0, "jobPostings": []},
            ("AI Engineer", 0): {"total": 1, "jobPostings": [rows[0]]},
            ("Forward Deployed Engineer", 0): {"total": 0, "jobPostings": []},
            ("Senior Frontend Engineer", 0): {"total": 1, "jobPostings": [rows[1]]},
        },
    )
    sr = _sr_handler(calls, {("servicenow", "", 0): SR_LIST})

    def handler(request):
        if request.url.host.endswith("myworkdayjobs.com"):
            return wd(request)
        if request.url.host == "api.smartrecruiters.com":
            return sr(request)
        return test_hunt._route(request)

    _mock(monkeypatch, handler)
    return calls


def test_watchlist_pins_workday_from_a_pasted_url(hunt_tools, board_net):  # noqa: F811
    wl = hunt_tools["careercoach_watchlist"]
    out = _run(
        wl, {"action": "add", "company": "Workday", "slug": "https://workday.wd5.myworkdayjobs.com/en-US/Workday"}
    )
    assert out == "Watching Workday — workday/workday/wd5/Workday, 3 open role(s) right now."
    assert "Workday — workday/workday/wd5/Workday" in _run(wl, {})
    bad = _run(wl, {"action": "add", "company": "Nike", "ats": "workday", "slug": "nike"})
    assert "Couldn't read the workday/nike board" in bad and "tenant/wdN/site" in bad
    out = _run(wl, {"action": "add", "company": "ServiceNow, Nowhere"})
    assert "ServiceNow — smartrecruiters/servicenow, 3 open role(s)" in out
    assert "Not found on Ashby, Greenhouse, Lever or SmartRecruiters: Nowhere" in out and "Workday" in out
    empty = _run(wl, {"action": "add", "company": "Visa", "ats": "smartrecruiters", "slug": "Visa"})
    assert "has no postings" in empty and "Visa" not in _run(wl, {})


def test_scan_searches_workday_by_the_target_roles(hunt_tools, board_net):  # noqa: F811
    _run(
        hunt_tools["careercoach_watchlist"], {"action": "add", "company": "Workday", "ats": "workday", "slug": WD_SLUG}
    )
    board_net.clear()
    out = _run(hunt_tools["careercoach_scan_boards"], {"locations": "boulder", "max_age_days": 0})
    assert "Software Engineer/Senior Software Engineer - AI Platform (Agent Cell) — Workday" in out
    searched = {json.loads(c[2])["searchText"] for c in board_net if c[0] == "POST"}
    assert searched == {"Forward Deployed Engineer", "Design Engineer", "AI Engineer"}  # never the whole board


def test_resolve_uses_the_watchlist_pin_for_a_workday_company(plugin, mods, board_net):
    resolve = importlib.import_module(plugin.__name__ + ".search.resolve")
    mods.watchlist.add_company("Workday", "workday", WD_SLUG)
    job = {
        "title": "Senior Frontend Engineer",
        "company": "Workday",
        "source": "jsearch",
        "url": "https://www.jobleads.com/x",
    }
    assert asyncio.run(resolve.resolve([job])) == 1
    assert job["apply_url"] == WD_DETAIL["jobPostingInfo"]["externalUrl"] and job["ats_lookup"] == "found"
    assert json.loads(next(c[2] for c in board_net if c[0] == "POST"))["searchText"] == "Senior Frontend Engineer"


def test_the_manifest_lists_every_board_host(plugin, mods):
    import yaml

    manifest = yaml.safe_load((Path(plugin.__file__).parent / "protoagent.plugin.yaml").read_text())
    assert set(mods.boards.HOSTS) <= set(manifest["capabilities"]["network"])
