"""The watch's market sweep: every target role, a keyed source in turn, a timeout that costs one query.

Before this, the watch searched the market for the first five target roles only, and one JSearch
timeout ended JSearch for the pass. No live network: ``httpx.AsyncClient`` runs on a mock transport.
"""

from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest

_ASYNC_CLIENT = httpx.AsyncClient  # the real class, before any fixture swaps it

#: The operator's real list on 2026-11-18: 34 roles, most of them respellings.
REAL_ROLES = (
    "AI Engineer, Applied AI Engineer, AI Software Engineer, Software Engineer AI, LLM Engineer, Agent Engineer, "
    "Full Stack Engineer, Fullstack Engineer, Full-Stack Engineer, Software Engineer Full Stack, "
    "Software Engineer Fullstack, Software Engineer Full-Stack, Frontend Engineer, Front End Engineer, "
    "Front-End Engineer, Software Engineer Frontend, Software Engineer Front-End, Frontend Developer, "
    "Front-End Developer, Product Engineer, Design Engineer, Design Systems Engineer, "
    "Software Engineer Design Systems, UI Engineer, UX Engineer, Design Technologist, "
    "Developer Productivity Engineer, Developer Experience Engineer, DevEx Engineer, AI Platform Engineer, "
    "Agent Platform Engineer, Automation Engineer, Workflow Engineer, AI Automation Engineer"
)
ROLES = "AI Engineer, Frontend Engineer, Design Engineer, Product Engineer, Agent Engineer"  # five groups


@pytest.fixture
def mods(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))

    class M:
        pass

    m = M()
    for name in ("sweep", "jobsource", "watchlist", "feeds"):
        setattr(m, name, importlib.import_module(f"{plugin.__name__}.search.{name}"))
    monkeypatch.setattr(m.sweep, "KEYED_SPACING_S", 0)
    monkeypatch.setattr(m.sweep, "KEYLESS_SPACING_S", 0)
    m.feeds._CACHE.clear()
    yield m
    m.feeds._CACHE.clear()


def _net(monkeypatch, jsearch=None):
    """Mock every source. ``jsearch(request)`` answers JSearch (default: one job named after the
    query). Returns ``{host: [query, …]}`` as the requests arrive."""
    asked: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        query = request.url.params.get("query") or request.url.params.get("q") or ""
        asked.setdefault(host, []).append(query)
        if host == "jsearch.p.rapidapi.com":
            if jsearch:
                return jsearch(request)
            job = {"job_title": query, "employer_name": f"{query} Co", "job_apply_link": f"https://j/{query}"}
            return httpx.Response(200, json={"data": {"jobs": [job]}})
        if host == "himalayas.app":
            job = {"title": query, "companyName": f"{query} Ltd", "applicationLink": f"https://h/{query}"}
            return httpx.Response(200, json={"jobs": [job]})
        if host == "jobicy.com":
            job = {
                "jobTitle": "Design Engineer",
                "companyName": "Feedco",
                "jobGeo": "USA",
                "url": "https://jobicy.com/1",
            }
            return httpx.Response(200, json={"jobs": [job]})
        raise AssertionError(f"unexpected host {host}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return asked


def _run(mods, sources, cfg=None, roles=ROLES, key="KEY"):
    return asyncio.run(mods.sweep.market_sweep(cfg or {}, roles, sources, key, 30))


# ── role groups ───────────────────────────────────────────────────────────────
def test_respellings_of_one_role_are_one_query(mods):
    g = mods.sweep.role_groups
    assert g("Full Stack Engineer, Fullstack Engineer, Full-Stack Engineer, Software Engineer Full-Stack") == [
        "Full Stack Engineer"
    ]
    assert g("Frontend Engineer, Front End Engineer, Software Engineer Front-End, Front-End Developer") == [
        "Frontend Engineer"
    ]
    assert g("Developer Experience Engineer, DevEx Engineer") == ["Developer Experience Engineer"]
    assert g("AI Engineer, Software Engineer AI, Applied AI Engineer") == ["AI Engineer", "Applied AI Engineer"]
    assert g("Design Engineer, Design Systems Engineer") == ["Design Engineer", "Design Systems Engineer"]
    assert g("Software Engineer, Senior Software Engineer") == ["Software Engineer", "Senior Software Engineer"]
    assert g("") == [] and g(" , ") == []


def test_the_operators_34_roles_are_19_queries_and_none_is_lost(mods):
    groups = mods.sweep.role_groups(REAL_ROLES)
    assert len(REAL_ROLES.split(",")) == 34 and len(groups) == 19
    for wanted in (
        "Frontend Engineer",
        "Design Engineer",
        "Design Systems Engineer",
        "UI Engineer",
        "Workflow Engineer",
    ):
        assert wanted in groups  # the roles past the old five-role cutoff


# ── every role, every source ──────────────────────────────────────────────────
def test_a_keyless_board_is_asked_every_role_every_pass(mods, monkeypatch):
    asked = _net(monkeypatch)
    jobs, answered = _run(mods, ["himalayas"])
    assert asked["himalayas.app"] == mods.sweep.role_groups(ROLES) and answered
    assert {j["title"] for j in jobs} == set(mods.sweep.role_groups(ROLES))
    assert all(j["source"] == "himalayas" for j in jobs)


def test_a_feed_is_read_once_and_covers_every_role(mods, monkeypatch):
    asked = _net(monkeypatch)
    jobs, answered = _run(mods, ["jobicy"])
    assert len(asked["jobicy.com"]) == len(mods.feeds.JOBICY_INDUSTRIES)  # not once per role
    assert answered and jobs[0]["source"] == "jobicy" and jobs[0]["company"] == "Feedco"


def test_a_keyed_source_takes_the_next_roles_each_pass(mods, monkeypatch):
    """Three requests a pass, continuing where the last pass stopped: five roles in two passes."""
    asked = _net(monkeypatch)
    groups = mods.sweep.role_groups(ROLES)
    _run(mods, ["jsearch"])
    assert asked["jsearch.p.rapidapi.com"] == groups[:3]
    _run(mods, ["jsearch"])
    assert asked["jsearch.p.rapidapi.com"] == groups[:3] + groups[3:] + groups[:1]  # wraps round
    assert "himalayas.app" not in asked  # JSearch answered: no stand-in needed


def test_the_per_pass_count_is_a_setting(mods, monkeypatch):
    asked = _net(monkeypatch)
    _run(mods, ["jsearch"], cfg={"watch_market_queries": 1})
    assert len(asked["jsearch.p.rapidapi.com"]) == 1
    assert mods.sweep.keyed_queries({"watch_market_queries": "junk"}) == 3
    assert mods.sweep.keyed_queries({"watch_market_queries": 99}) == 10


def test_the_days_allowance_still_caps_a_pass(mods, monkeypatch):
    asked = _net(monkeypatch)
    monkeypatch.setattr(mods.watchlist, "jsearch_allowance", lambda: 0)
    jobs, answered = _run(mods, ["jsearch", "himalayas"])
    assert "jsearch.p.rapidapi.com" not in asked and answered  # nothing spent; the keyless board still ran
    _run(mods, ["jsearch"])
    assert "jsearch.p.rapidapi.com" not in asked


# ── timeouts and refusals ─────────────────────────────────────────────────────
def test_one_timeout_costs_one_query_not_the_pass(mods, monkeypatch):
    """Both live passes on 2026-11-18 lost JSearch to a single ReadTimeout."""
    groups = mods.sweep.role_groups(ROLES)

    def slow_once(request):
        if request.url.params["query"] == groups[0]:
            raise httpx.ReadTimeout("slow", request=request)
        q = request.url.params["query"]
        return httpx.Response(200, json={"data": {"jobs": [{"job_title": q, "employer_name": "Acme", "job_apply_link": f"https://j/{q}"}]}})  # fmt: skip

    asked = _net(monkeypatch, slow_once)
    jobs, answered = _run(mods, ["jsearch", "himalayas"])
    assert asked["jsearch.p.rapidapi.com"] == groups[:3]  # it carried on past the timeout
    assert {j["title"] for j in jobs if j["source"] == "jsearch"} == set(groups[1:3]) and answered
    _run(mods, ["jsearch"])
    assert asked["jsearch.p.rapidapi.com"][3] == groups[3]  # the timed-out query isn't re-asked first


def test_two_timeouts_in_a_row_leave_the_source_until_next_pass(mods, monkeypatch, caplog):
    def down(request):
        raise httpx.ConnectTimeout("down", request=request)

    asked = _net(monkeypatch, down)
    with caplog.at_level("WARNING"):
        jobs, answered = _run(mods, ["jsearch"])
    assert len(asked["jsearch.p.rapidapi.com"]) == 2 and "timed out 2 times in a row" in caplog.text
    assert asked["himalayas.app"] and answered  # the only source was keyed and never answered: Himalayas stands in


def test_a_timeout_still_counts_against_the_allowance(mods, monkeypatch):
    from datetime import UTC, datetime

    mods.watchlist.note_jsearch_call({"limit": 200, "remaining": 100, "reset_s": 30 * 86400}, datetime.now(UTC))

    def down(request):
        raise httpx.ReadTimeout("slow", request=request)

    _net(monkeypatch, down)
    _run(mods, ["jsearch", "himalayas"])
    record = mods.watchlist._load(mods.watchlist.JSEARCH_QUOTA, {})
    assert record["remaining"] == 98 and record["used_today"] == 3 and record["limit"] == 200


def test_a_refusal_ends_the_source_for_the_pass_and_keeps_its_place(mods, monkeypatch, caplog):
    asked = _net(monkeypatch, lambda r: httpx.Response(403, json={"message": "You are not subscribed to this API."}))
    with caplog.at_level("WARNING"):
        jobs, answered = _run(mods, ["jsearch", "himalayas"])
    assert len(asked["jsearch.p.rapidapi.com"]) == 1 and "JSearch isn't answering" in caplog.text
    assert answered and all(j["source"] == "himalayas" for j in jobs)
    asked2 = _net(monkeypatch)
    _run(mods, ["jsearch"])
    assert asked2["jsearch.p.rapidapi.com"][0] == mods.sweep.role_groups(ROLES)[0]  # the refused query is asked again


def test_keyed_requests_get_the_longer_timeout(mods, monkeypatch):
    seen = []

    def client(**kw):
        seen.append(kw.get("timeout"))
        return _ASYNC_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    asyncio.run(mods.jobsource.search_jobs("AI Engineer", provider="jsearch", api_key="KEY"))
    asyncio.run(mods.jobsource.search_jobs("AI Engineer", provider="openwebninja", api_key="KEY"))
    assert seen == [mods.jobsource.KEYED_TIMEOUT] * 2 and mods.jobsource.KEYED_TIMEOUT >= 45


def test_one_source_failing_never_costs_the_others(mods, monkeypatch):
    asked = _net(monkeypatch)
    monkeypatch.setattr(mods.feeds, "fetch", lambda name: (_ for _ in ()).throw(RuntimeError("feed down")))
    jobs, answered = _run(mods, ["jobicy", "himalayas"])
    assert answered and {j["source"] for j in jobs} == {"himalayas"} and asked["himalayas.app"]
    assert _run(mods, ["himalayas"], roles="") == ([], False)
