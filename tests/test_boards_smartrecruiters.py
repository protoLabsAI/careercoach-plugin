"""SmartRecruiters boards: the parser, pasted board URLs, paged + searched fetches, the watchlist
pin, the scan and the resolve lookup. Payloads are trimmed from real responses (2026-11-18,
ServiceNow's board). No live network: httpx is routed to a MockTransport."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import httpx
import pytest

import test_hunt
from test_hunt import _run, hunt_tools, net  # noqa: F401 — fixtures

mods = test_hunt.mods
LONG = "Build the agent platform our customers run on. Python, TypeScript, React, LLM evals. " * 10


def _mock(monkeypatch, handler):
    real = test_hunt._ASYNC_CLIENT
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_a_pasted_board_url_names_its_board(mods):
    bfu = mods.boards.board_from_url
    assert bfu("https://jobs.smartrecruiters.com/ServiceNow/744000162408293-sr-manager") == (
        "smartrecruiters",
        "ServiceNow",
    )
    assert bfu("careers.smartrecruiters.com/BoschGroup") == ("smartrecruiters", "BoschGroup")
    assert bfu("https://jobs.ashbyhq.com/acme") == ("ashby", "acme")
    assert bfu("https://job-boards.greenhouse.io/globex/jobs/1") == ("greenhouse", "globex")
    assert bfu("https://jobs.lever.co/initech/l1") == ("lever", "initech")
    for not_a_board in ("ServiceNow", "https://example.com/careers", "https://nike.wd1.myworkdayjobs.com/nke"):
        assert bfu(not_a_board) == ("", "")


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


def test_probe_finds_smartrecruiters_and_checks_whose_board_it_is(mods, monkeypatch):
    calls: list = []
    pages = {("servicenow", "", 0): SR_LIST}
    _mock(monkeypatch, _sr_handler(calls, pages))
    found, problems = asyncio.run(mods.boards.probe("ServiceNow"))
    assert found == [("smartrecruiters", "servicenow", 3)] and problems == []
    # a shortened guess must name the company asked for — SmartRecruiters says whose board it is
    found, problems = asyncio.run(mods.boards.probe("ServiceNow Labs"))
    assert found == [] and problems == ["smartrecruiters/servicenow is 'ServiceNow''s board, not 'ServiceNow Labs'"]


# ── the tools and the lookups ─────────────────────────────────────────────────
AI_ROW = {**SR_LIST["content"][0], "id": "744000162321959", "name": "Principal AI Platform Forward Deployment Engineer"}


@pytest.fixture
def board_net(monkeypatch):
    """SmartRecruiters beside the other three boards (the watchlist tool, the scan and resolve)."""
    calls: list = []
    rows = SR_LIST["content"]
    sr = _sr_handler(
        calls,
        {
            ("servicenow", "", 0): SR_LIST,
            ("servicenow", "AI Engineer", 0): {"totalFound": 1, "content": [AI_ROW]},
            ("servicenow", rows[0]["name"], 0): {"totalFound": 1, "content": [rows[0]]},
        },
    )

    def handler(request):
        return sr(request) if request.url.host == "api.smartrecruiters.com" else test_hunt._route(request)

    _mock(monkeypatch, handler)
    return calls


def test_watchlist_pins_a_board_from_its_url_and_finds_smartrecruiters(hunt_tools, board_net):  # noqa: F811
    wl = hunt_tools["careercoach_watchlist"]
    url = "https://jobs.smartrecruiters.com/ServiceNow/744000162408293-sr-customer-success-manager"
    out = _run(wl, {"action": "add", "company": "ServiceNow", "slug": url})
    assert out == "Watching ServiceNow — smartrecruiters/ServiceNow, 3 open role(s) right now."
    out = _run(wl, {"action": "add", "company": "ServiceNow, Nowhere"})
    assert "ServiceNow — smartrecruiters/servicenow, 3 open role(s)" in out
    assert "Not found on Ashby, Greenhouse, Lever or SmartRecruiters: Nowhere" in out
    # SmartRecruiters answers an unknown company with an empty board: don't pin a typo
    empty = _run(wl, {"action": "add", "company": "Visa", "ats": "smartrecruiters", "slug": "Visa"})
    assert "has no postings" in empty and "Visa" not in _run(wl, {})


def test_scan_searches_smartrecruiters_by_the_target_roles(hunt_tools, board_net):  # noqa: F811
    _run(hunt_tools["careercoach_watchlist"], {"action": "add", "company": "ServiceNow", "ats": "smartrecruiters", "slug": "ServiceNow"})  # fmt: skip
    board_net.clear()
    out = _run(hunt_tools["careercoach_scan_boards"], {"locations": "mountain view", "max_age_days": 0})
    assert "Principal AI Platform Forward Deployment Engineer — ServiceNow" in out
    searched = {httpx.URL(c).params.get("q") for c in board_net if c.endswith("offset=0") or "q=" in c}
    assert searched == {"Forward Deployed Engineer", "Design Engineer", "AI Engineer"}  # never the whole board


def test_resolve_uses_the_watchlist_pin_and_searches_the_title(plugin, mods, board_net):
    resolve = importlib.import_module(plugin.__name__ + ".search.resolve")
    mods.watchlist.add_company("ServiceNow", "smartrecruiters", "ServiceNow")
    title = SR_LIST["content"][0]["name"]
    job = {"title": title, "company": "ServiceNow", "source": "jsearch", "url": "https://www.jobleads.com/x"}
    assert asyncio.run(resolve.resolve([job])) == 1
    assert job["apply_url"] == SR_DETAIL["postingUrl"] and job["ats_lookup"] == "found"
    assert job["description"].startswith("Build the agent platform")
    assert {httpx.URL(c).params.get("q") for c in board_net if "q=" in c} == {title}


def test_the_manifest_lists_every_board_host(plugin, mods):
    import yaml

    manifest = yaml.safe_load((Path(plugin.__file__).parent / "protoagent.plugin.yaml").read_text())
    assert set(mods.boards.HOSTS) <= set(manifest["capabilities"]["network"])


def test_two_openings_with_no_text_read_do_not_share_a_hash(plugin):
    """A SmartRecruiters list carries no descriptions: same title, same place, different roles."""
    import importlib

    boards = importlib.import_module(plugin.__name__ + ".search.boards")
    common = dict(company="ServiceNow", ats="smartrecruiters", slug="servicenow", title="Senior Software Engineer",
                  location="Santa Clara, CA, US", remote=False, url="https://x", posted="", description="")  # fmt: skip
    a, b = boards._job(job_id="1", **common), boards._job(job_id="2", **common)
    assert a["hash"] != b["hash"]
    same = dict(common, description="The same posting text. " * 20)
    assert boards._job(job_id="1", **same)["hash"] == boards._job(job_id="2", **same)["hash"]  # a repost still matches
