"""Dover boards: the parser, pasted board URLs, the three-step fetch, the name lookup and the
watchlist pin. Payloads are trimmed from real responses (2026-11-19, Augment Markets' board).
No live network: httpx is routed to a MockTransport."""

from __future__ import annotations

import asyncio

import httpx

import test_hunt
from test_hunt import _run, hunt_tools, net  # noqa: F401 — fixtures

mods = test_hunt.mods
BOARD_ID = "4da07eda-19a8-4c85-a402-4119e8dfa619"
FE, LEGAL, SOCIAL = "4a8aa7ba-b3cf-42d2-8ec9-03bf9483a960", "1c5f42aa-8ea2-49e3-b170-08b9f546d0fb", "52398a49"
SLUG = {"id": BOARD_ID, "slug": "augment-markets", "name": "Augment Markets"}


def _loc(kind, name, city="", state=""):
    option = {"display_name": name, "city": city, "state": state, "country": "US"}
    return {"location_type": kind, "location_option": option, "name": name, "is_primary": True}


ROWS = [
    {"id": LEGAL, "title": "Director, Legal (Capital Markets)", "workplace_type": "REMOTE", "is_published": True,
     "is_sample": False, "locations": [_loc("REMOTE", "United States")]},
    {"id": SOCIAL, "title": "Social Media Producer", "workplace_type": "HYBRID", "is_published": True,
     "is_sample": False, "locations": [_loc("HYBRID", "Austin, TX", "Austin", "Texas")]},
    {"id": FE, "title": "Software Engineer, Frontend", "workplace_type": "REMOTE", "is_published": True,
     "is_sample": False, "locations": [_loc("REMOTE", "International")]},
    {"id": "sample-1", "title": "Sample Role", "workplace_type": "REMOTE", "is_published": True, "is_sample": True,
     "locations": []},
    {"id": "draft-1", "title": "Unpublished Role", "workplace_type": "REMOTE", "is_published": False,
     "is_sample": False, "locations": []},
]  # fmt: skip
DETAILS = {
    FE: {"id": FE, "client_name": "Augment Markets", "title": "Software Engineer, Frontend",
         "created": "2026-01-09T22:30:48.527900Z", "compensation": None,
         "user_provided_description": "<h2>About Augment</h2><p>A FINRA-registered marketplace.</p>"
         + "<p>Build with React and TypeScript.</p>" * 30},
    LEGAL: {"id": LEGAL, "client_name": "Augment Markets", "created": "2026-11-02T10:00:00Z",
            "user_provided_description": "<p>Lead capital markets legal work.</p>",
            "compensation": {"lower_bound": 185000, "upper_bound": 245000, "currency_code": "USD",
                             "open_to_sharing_comp": True, "salary_range_type": "YEARLY"}},
}  # fmt: skip


def _handler(calls: list, rows=None, slugs=None):
    rows = ROWS if rows is None else rows
    slugs = slugs or {"augment-markets": SLUG}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "app.dover.com":
            return test_hunt._route(request)
        calls.append(request.url.path)
        parts = request.url.path.split("/")  # /api/v1/<kind>/<id>[/jobs]
        if parts[3] == "careers-page-slug":
            board = slugs.get(parts[4].lower())  # Dover answers any letter case
            return httpx.Response(200, json=board) if board else httpx.Response(404, json={"detail": "Not found."})
        if parts[3] == "careers-page":
            offset = int(request.url.params.get("offset", 0))
            limit = int(request.url.params.get("limit", 100))
            mine = [{**ROWS[1], "id": "other-1"}] if parts[4] == "other" else rows  # another company's board
            return httpx.Response(200, json={"count": len(mine), "results": mine[offset : offset + limit]})
        detail = DETAILS.get(parts[5])
        return httpx.Response(200, json=detail) if detail else httpx.Response(404, json={})

    return handler


def _mock(monkeypatch, handler):
    real = test_hunt._ASYNC_CLIENT
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_a_pasted_dover_url_names_its_board(mods):
    bfu = mods.boards.board_from_url
    assert bfu(f"https://jobs.dover.com/augment-markets/{FE}?src=app") == ("dover", "augment-markets")
    assert bfu("https://app.dover.com/jobs/augment-markets?embed=1") == ("dover", "augment-markets")
    assert bfu(f"app.dover.com/apply/augment-markets/{FE}") == ("dover", "augment-markets")
    assert bfu("https://dover.com/pricing") == ("", "")


def test_parse_dover_reads_place_remote_pay_and_skips_samples(mods):
    b = mods.boards
    legal, social, fe = b.parse_dover({"results": ROWS}, company="x", slug="augment-markets", details=DETAILS)
    assert [j["title"] for j in (legal, social, fe)] == [r["title"] for r in ROWS[:3]]  # no sample, no draft
    assert (legal["company"], legal["location"], legal["remote"]) == ("Augment Markets", "Remote (United States)", True)
    assert legal["countries"] == ["United States"] and legal["comp"] == "USD 185,000–245,000"
    assert (
        legal["posted"].startswith("2026-11-02") and legal["url"] == f"https://jobs.dover.com/augment-markets/{LEGAL}"
    )
    assert (social["location"], social["remote"], social["desc_len"], social["posted"]) == (
        "Austin, TX",
        False,
        None,
        "",
    )
    # "International" names no place: the role isn't tied to one, and no pay is shared
    assert (fe["location"], fe["remote"], fe["countries"], fe["comp"]) == ("Remote", True, [], "")
    assert fe["description"].startswith("About Augment A FINRA-registered marketplace.") and fe["desc_len"] > 600
    assert b.location_matches(fe, "remote, portland, or") and b.location_matches(legal, "remote")
    assert not b.location_matches(social, "remote, portland, or")
    assert (
        b.parse_dover({}, company="x", slug="y") == []
        and b.parse_dover({"results": [None, {}]}, company="x", slug="y") == []
    )


def test_dover_pay_is_shown_only_as_a_shared_range(mods):
    comp = mods.boards._dover_comp
    base = {"lower_bound": 60, "upper_bound": 80, "currency_code": "usd", "salary_range_type": "HOURLY"}
    assert comp({"compensation": base}) == "USD 60–80 per hour"
    assert comp({"compensation": {**base, "open_to_sharing_comp": False}}) == ""
    assert comp({"compensation": {**base, "upper_bound": None}}) == "" and comp({}) == "" and comp(None) == ""


def test_dover_fetch_reads_the_board_then_only_the_matching_roles_text(mods, monkeypatch):
    calls: list = []
    _mock(monkeypatch, _handler(calls))
    jobs = asyncio.run(mods.boards.fetch_board("dover", "Augment-Markets", search="Frontend Engineer"))
    assert [j["title"] for j in jobs] == [r["title"] for r in ROWS[:3]]
    assert calls[:2] == ["/api/v1/careers-page-slug/Augment-Markets", f"/api/v1/careers-page/{BOARD_ID}/jobs"]
    assert [c for c in calls if "application-portal-job" in c] == [f"/api/v1/inbound/application-portal-job/{FE}"]
    fe = next(j for j in jobs if "Frontend" in j["title"])
    assert fe["slug"] == "augment-markets" and fe["url"].startswith("https://jobs.dover.com/augment-markets/")
    calls.clear()
    asyncio.run(mods.boards.fetch_board("dover", "augment-markets"))  # nothing searched: a small board, read whole
    assert sum("application-portal-job" in c for c in calls) == 3


def test_an_unknown_dover_slug_is_no_board_and_a_long_board_is_paged(mods, monkeypatch):
    calls: list = []
    many = [{**ROWS[2], "id": f"r{i}", "title": f"Role {i}"} for i in range(150)]
    _mock(monkeypatch, _handler(calls, rows=many))
    try:
        asyncio.run(mods.boards.fetch_board("dover", "nobody-here"))
        raise AssertionError("expected BoardNotFound")
    except mods.boards.BoardNotFound as e:
        assert "no dover board named 'nobody-here'" in str(e)
    calls.clear()
    jobs = asyncio.run(mods.boards.fetch_board("dover", "augment-markets", search="Nothing Matches"))
    assert len(jobs) == 150 and sum(c.endswith("/jobs") for c in calls) == 2


def test_probe_finds_a_dover_board_by_the_companys_name(mods, monkeypatch):
    calls: list = []
    _mock(monkeypatch, _handler(calls))
    found, problems = asyncio.run(mods.boards.probe("Augment Markets"))
    assert found == [("dover", "augment-markets", 3)] and problems == []
    assert asyncio.run(mods.boards.probe("Nobody Here Inc")) == ([], [])  # a 404 is "no board", not a problem


def test_watchlist_pins_a_dover_board_from_its_url(hunt_tools, monkeypatch):  # noqa: F811
    calls: list = []
    _mock(monkeypatch, _handler(calls))
    wl = hunt_tools["careercoach_watchlist"]
    out = _run(
        wl, {"action": "add", "company": "Augment Markets", "slug": f"https://jobs.dover.com/augment-markets/{FE}"}
    )
    assert out == "Watching Augment Markets — dover/augment-markets, 3 open role(s) right now."


def test_dover_is_wired_everywhere_a_board_is_named(plugin, mods):
    from pathlib import Path

    import importlib

    import yaml

    b = mods.boards
    assert "dover" in b.ATS and "dover" in b.PARSERS and "dover" in b.NAMED and "app.dover.com" in b.HOSTS
    manifest = yaml.safe_load((Path(plugin.__file__).parent / "protoagent.plugin.yaml").read_text())
    assert set(b.HOSTS) <= set(manifest["capabilities"]["network"])
    resolve = importlib.import_module(plugin.__name__ + ".search.resolve")
    triage = importlib.import_module(plugin.__name__ + ".search.triage")
    # a role already linking to the company's Dover posting needs no lookup; its keeps count as a board's
    assert not resolve.indirect({"source": "jsearch", "url": f"https://jobs.dover.com/augment-markets/{FE}"})
    assert "dover" in triage.BOARD_SOURCES
