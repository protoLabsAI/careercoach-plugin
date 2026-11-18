"""Live job search relevance — a keyless board must answer the QUERY, and say it answered.

Remotive's ``?search=`` came back with the same unrelated postings for every query (by 2026-10 its API
returned one fixed 14-posting feed), and the code took ``jobs[:limit]`` as-is, so "data engineer" and
"pastry chef" returned identical lists. The keyless default is now Himalayas, whose search is loose, so
``search_jobs`` still ranks/filters keyless results client-side with ``prescore`` and reports which
provider answered. Remotive stays reachable only when configured explicitly.

No live network: ``httpx.AsyncClient`` is swapped for one on an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest

#: What Remotive sent back regardless of ?search= — the bug's exact shape.
_SAME_FOR_EVERY_QUERY = {
    "jobs": [
        {
            "title": "Customer Success Manager",
            "company_name": "Initech",
            "candidate_required_location": "Worldwide",
            "url": "https://remotive.com/1",
            "description": "<p>Delight customers.</p>",
        },
        {
            "title": "Senior Data Engineer",
            "company_name": "Globex",
            "candidate_required_location": "USA",
            "url": "https://remotive.com/2",
            "description": "<p>Build data pipelines in Python.</p>",
        },
        {
            "title": "Marketing Lead",
            "company_name": "Hooli",
            "candidate_required_location": "Europe",
            "url": "https://remotive.com/3",
            "description": "<p>Work with the data team on campaigns.</p>",
        },
        {
            "title": "Pastry Chef (remote recipe developer)",
            "company_name": "Bakery Co",
            "candidate_required_location": "Worldwide",
            "url": "https://remotive.com/4",
            "description": "<p>Develop pastry recipes.</p>",
        },
    ]
}


@pytest.fixture
def js(plugin):
    return importlib.import_module(plugin.__name__ + ".search.jobsource")


def _as_himalayas(payload: dict) -> dict:
    """The same four postings in Himalayas' response shape."""
    return {
        "jobs": [
            {
                "title": j["title"],
                "companyName": j["company_name"],
                "locationRestrictions": [j["candidate_required_location"]],
                "applicationLink": j["url"],
                "excerpt": j["description"],
                "pubDate": 1791000000,
                "minSalary": 150000,
                "maxSalary": 200000,
                "currency": "USD",
            }
            for j in payload["jobs"]
        ]
    }


@pytest.fixture
def remotive(monkeypatch):
    """Route every httpx call to a mock keyless board (Himalayas, or Remotive when asked for) that
    ignores the query. Returns the request log."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "himalayas.app":
            return httpx.Response(200, json=_as_himalayas(_SAME_FOR_EVERY_QUERY))
        assert request.url.host == "remotive.com"
        return httpx.Response(200, json=_SAME_FOR_EVERY_QUERY)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def _search(js, query, **kw):
    return asyncio.run(js.search_jobs(query, **kw))


def test_different_queries_get_different_relevant_results(js, remotive):
    data = _search(js, "data engineer")
    chef = _search(js, "pastry chef")
    assert [j["company"] for j in data.jobs] == ["Globex", "Hooli"]  # title hit ranks above a snippet mention
    assert [j["company"] for j in chef.jobs] == ["Bakery Co"]
    assert remotive[0].url.params["q"] == "data engineer"  # still asks the board to search
    assert remotive[0].url.params["sort"] == "recent"


def test_irrelevant_postings_are_dropped_not_padded(js, remotive):
    out = _search(js, "kubernetes administrator")
    assert out.jobs == [] and out.fetched == 4 and out.matched == 0


def test_limit_applies_after_relevance_ranking(js, remotive):
    out = _search(js, "data", limit=1)
    assert [j["company"] for j in out.jobs] == ["Globex"]  # the title hit, not the first posting
    assert out.matched == 2


def test_rank_false_returns_the_raw_feed_for_a_caller_that_scores_it_itself(js, remotive):
    # The watch scan queries with the first target role but scores against ALL of them
    # (best_prescore), so a posting matching only a later role must survive the search.
    out = _search(js, "data engineer", rank=False, limit=25)
    assert len(out.jobs) == out.fetched == 4
    assert "Bakery Co" in [j["company"] for j in out.jobs]


def test_the_result_names_the_provider_and_a_keyless_fallback(js, remotive):
    auto = _search(js, "data engineer")
    assert auto.provider == "himalayas" and auto.keyless_fallback is True
    chosen = _search(js, "data engineer", provider="himalayas")
    assert chosen.provider == "himalayas" and chosen.keyless_fallback is False  # explicitly configured


def test_auto_never_picks_remotive_but_an_explicit_choice_still_works(js, remotive):
    out = _search(js, "data engineer", provider="remotive")
    assert out.provider == "remotive" and [j["company"] for j in out.jobs] == ["Globex", "Hooli"]
    assert remotive[-1].url.host == "remotive.com"
    assert all(r.url.host != "remotive.com" for r in remotive[:-1])


def test_himalayas_carries_date_salary_and_country(js, remotive):
    out = _search(js, "data engineer", country="United States")
    assert remotive[0].url.params["country"] == "United States"
    top = out.jobs[0]
    assert top["posted"].startswith("2026-") and top["comp"] == "USD 150,000–200,000"
    assert top["location"] == "Remote (USA)"


def test_an_unknown_provider_is_refused(js):
    with pytest.raises(ValueError, match="unknown job source"):
        _search(js, "x", provider="monster")


def test_hn_searches_this_months_thread_top_level_only(js, monkeypatch):
    stories = {"hits": [
        {"objectID": "1", "title": "Ask HN: Who wants to be hired? (October 2026)"},
        {"objectID": "2", "title": "Ask HN: Who is hiring? (October 2026)"},
    ]}  # fmt: skip
    comments = {"hits": [
        {"objectID": "10", "parent_id": 2, "created_at": "2026-10-01T15:05:00Z",
         "comment_text": "Acme AI (<a href=\"https://acme.ai\">acme.ai</a>) | Forward Deployed Engineer | REMOTE (US) | $200k<p>We build agents."},
        {"objectID": "11", "parent_id": 10, "created_at": "2026-10-01T16:00:00Z",
         "comment_text": "Is this role open to Canada?"},
    ]}  # fmt: skip
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "hn.algolia.com"
        return httpx.Response(200, json=stories if "search_by_date" in request.url.path else comments)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = _search(js, "forward deployed", provider="hn")
    assert seen[1].url.params["tags"] == "comment,story_2"  # the hiring thread, not "wants to be hired"
    assert [(j["company"], j["title"], j["location"]) for j in out.jobs] == [
        ("Acme AI", "Forward Deployed Engineer", "REMOTE (US)")
    ]
    assert out.jobs[0]["url"] == "https://news.ycombinator.com/item?id=10"


def test_an_unscoreable_query_is_not_emptied(js, remotive):
    """Every term under 3 characters ("ML", "QA") can't be judged by prescore — keep the provider's list."""
    assert len(_search(js, "QA").jobs) == 4


def test_jsearch_results_are_not_filtered(js, monkeypatch):
    payload = {"data": [{"job_title": "Staff SWE", "employer_name": "Acme", "job_apply_link": "https://a/1"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "jsearch.p.rapidapi.com"
        return httpx.Response(200, json=payload)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = _search(js, "software engineer", api_key="KEY")
    assert out.provider == "jsearch" and out.keyless_fallback is False
    assert [j["company"] for j in out.jobs] == ["Acme"]  # JSearch does real search; no client-side drop


def test_openwebninja_calls_its_own_host_with_x_api_key(js, monkeypatch):
    """A JSearch key bought from OpenWeb Ninja goes to api.openwebninja.com in ``x-api-key`` —
    RapidAPI answers that key "not subscribed", so no X-RapidAPI-* header may ride along."""
    payload = {
        "status": "OK",
        "request_id": "r1",
        "parameters": {"query": "staff engineer in Austin"},
        "data": [
            {
                "job_title": "Staff SWE",
                "employer_name": "Acme",
                "job_apply_link": "https://a/1",
                "job_city": "Austin",
                "job_state": "TX",
                "job_country": "US",
                "job_posted_at_datetime_utc": "2026-11-17T00:00:00.000Z",
                "job_description": "Build the platform.",
            },
            {"job_title": "Pastry Chef", "employer_name": "Bakery Co", "job_apply_link": "https://a/2"},
        ],
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = _search(js, "staff engineer", location="Austin", remote=True, api_key="OWN-KEY", provider="openwebninja")

    (request,) = seen
    assert request.method == "GET"
    assert request.url.host == "api.openwebninja.com" and request.url.path == "/jsearch/search"
    assert request.headers["x-api-key"] == "OWN-KEY"
    assert not [h for h in request.headers if h.lower().startswith("x-rapidapi")]
    assert dict(request.url.params) == {
        "query": "staff engineer in Austin",
        "page": "1",
        "num_pages": "1",
        "remote_jobs_only": "true",
    }
    assert out.provider == "openwebninja" and out.keyless_fallback is False
    assert out.provider_label == "JSearch via OpenWeb Ninja (Google-for-Jobs)"
    assert [j["company"] for j in out.jobs] == ["Acme", "Bakery Co"]  # real search; no client-side drop
    assert (out.fetched, out.matched) == (2, 2)
    assert out.jobs[0]["location"] == "Austin, TX, US" and out.jobs[0]["url"] == "https://a/1"
    assert out.jobs[0]["posted"] == "2026-11-17T00:00:00.000Z"


def test_openwebninja_without_remote_omits_the_remote_param(js, monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = _search(js, "data engineer", api_key="OWN-KEY", provider="OpenWebNinja")
    assert dict(seen[0].url.params) == {"query": "data engineer", "page": "1", "num_pages": "1"}
    assert out.provider == "openwebninja" and out.jobs == []


def test_openwebninja_needs_a_key(js, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError("no request without a key")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="Settings → Career Coach"):
            _search(js, "x", api_key=blank, provider="openwebninja")


def test_the_unknown_provider_error_lists_openwebninja(js):
    with pytest.raises(ValueError, match="openwebninja"):
        _search(js, "x", provider="monster")


def test_auto_never_picks_openwebninja(js):
    """Both vendors issue 50-character keys, so the key can't say whose it is: auto stays RapidAPI."""
    assert js.choose_provider("auto", "k" * 50) == "jsearch"
    assert js.choose_provider("auto", "") == "himalayas"
    assert js.choose_provider("openwebninja", "") == "openwebninja"
    assert "openwebninja" not in js.KEYLESS


def test_parse_jsearch_reads_the_v2_shape(js):
    payload = {
        "status": "OK",
        "data": {
            "jobs": [
                {
                    "job_title": "ML Engineer",
                    "employer_name": "Globex",
                    "job_apply_link": "https://g/1",
                    "job_is_remote": True,
                    "job_posted_at_datetime_utc": None,
                    "job_posted_at_timestamp": 1794960000,
                    "job_description": "Train models.",
                },
                {
                    "job_title": "Data Engineer",
                    "employer_name": "Hooli",
                    "job_apply_link": "https://h/1",
                    "job_posted_at_datetime_utc": "2026-11-10T08:00:00.000Z",
                    "job_posted_at_timestamp": 1794960000,
                },
                {"job_title": "Analyst", "employer_name": "Initech", "job_apply_link": "https://i/1"},
            ],
            "cursor": "abc",
        },
    }
    jobs = js.parse_jsearch(payload)
    assert [(j["title"], j["company"], j["url"]) for j in jobs] == [
        ("ML Engineer", "Globex", "https://g/1"),
        ("Data Engineer", "Hooli", "https://h/1"),
        ("Analyst", "Initech", "https://i/1"),
    ]
    assert jobs[0]["location"] == "Remote" and jobs[0]["description"] == "Train models."
    assert jobs[0]["posted"] == "2026-11-18T00:00:00+00:00"  # null datetime → the epoch timestamp, as UTC
    assert jobs[1]["posted"] == "2026-11-10T08:00:00.000Z"  # a datetime that is set still wins
    assert jobs[2]["posted"] == ""  # neither field


@pytest.mark.parametrize("payload", [None, {}, {"data": None}, {"data": {}}, {"data": {"jobs": None, "cursor": "c"}}])
def test_parse_jsearch_tolerates_an_empty_payload(js, payload):
    assert js.parse_jsearch(payload) == []


def test_the_manifest_allows_the_openwebninja_host():
    import pathlib
    import tomllib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "protoagent.plugin.yaml").read_text())
    assert "api.openwebninja.com" in manifest["capabilities"]["network"]
    assert "jsearch.p.rapidapi.com" in manifest["capabilities"]["network"]
    settings = {s["key"]: s["description"] for s in manifest["settings"]}
    assert "openwebninja" in settings["jobs_provider"]
    assert "RapidAPI" in settings["jobs_api_key"] and "OpenWeb Ninja" in settings["jobs_api_key"]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert manifest["version"] == pyproject == "0.17.0"


def test_the_tool_reports_which_provider_answered(tools, remotive):
    tool = tools["careercoach_search_jobs"]
    out = asyncio.run(tool.ainvoke({"query": "data engineer"}))
    assert "Senior Data Engineer" in out and "Customer Success" not in out
    assert "Himalayas" in out and "keyless" in out and "2 of the 4" in out
    assert "USD 150,000–200,000" in out and "posted " in out

    empty = asyncio.run(tool.ainvoke({"query": "kubernetes administrator"}))
    assert empty.startswith("No postings found") and "Himalayas" in empty and "0 of the 4" in empty

    picked = asyncio.run(tool.ainvoke({"query": "data engineer", "source": "remotive"}))
    assert "Remotive" in picked and "fixed feed" in picked


# ── a refused or rate-limited key says why, and what to do (careercoach-plugin#131) ──
_NOT_SUBSCRIBED = {"message": "You are not subscribed to this API."}
_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured at import, before any test patches it


def _answer(monkeypatch, status: int, **response):
    """Every job-API request gets this status and body. Safe to call again within one test."""
    real = _REAL_ASYNC_CLIENT
    handler = lambda request: httpx.Response(status, **response)  # noqa: E731
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def _refusal(js, provider: str) -> str:
    with pytest.raises(ValueError) as e:
        _search(js, "data engineer", api_key="KEY", provider=provider)
    return str(e.value)


def test_a_rapidapi_403_not_subscribed_points_at_openwebninja(js, monkeypatch):
    _answer(monkeypatch, 403, json=_NOT_SUBSCRIBED)
    for provider in ("jsearch", "auto"):  # auto with a key means RapidAPI
        msg = _refusal(js, provider)
        assert "subscribed to JSearch" in msg and "'openwebninja'" in msg
        assert "rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch" in msg and "Settings → Career Coach" in msg


def test_openwebninja_is_not_told_to_switch_to_itself(js, monkeypatch):
    _answer(monkeypatch, 403, json=_NOT_SUBSCRIBED)
    msg = _refusal(js, "openwebninja")
    assert msg == "OpenWeb Ninja (JSearch) denied access: You are not subscribed to this API."


@pytest.mark.parametrize("provider,vendor", [("jsearch", "RapidAPI"), ("openwebninja", "OpenWeb Ninja")])
def test_a_401_names_the_job_source_api_key(js, monkeypatch, provider, vendor):
    _answer(monkeypatch, 401, json={"message": "Invalid API key."})
    msg = _refusal(js, provider)
    assert msg.startswith(f"{vendor} (JSearch) rejected the key")
    assert "Settings → Career Coach → Job-source API key" in msg and "Job source matches the vendor" in msg
    assert "Invalid API key." in msg


@pytest.mark.parametrize("provider,vendor", [("jsearch", "RapidAPI"), ("openwebninja", "OpenWeb Ninja")])
def test_a_429_says_rate_limit_or_quota(js, monkeypatch, provider, vendor):
    _answer(monkeypatch, 429, json={"message": "Too many requests"})
    msg = _refusal(js, provider)
    assert msg == f"{vendor} (JSearch) rate limit or monthly quota reached. Wait and retry, or check your plan."


@pytest.mark.parametrize("provider,vendor", [("jsearch", "RapidAPI"), ("openwebninja", "OpenWeb Ninja")])
def test_another_403_carries_the_bodys_message(js, monkeypatch, provider, vendor):
    _answer(monkeypatch, 403, json={"message": "This endpoint is disabled for your plan."})
    assert _refusal(js, provider) == f"{vendor} (JSearch) denied access: This endpoint is disabled for your plan."


def test_other_errors_keep_the_status_and_a_short_message(js, monkeypatch):
    _answer(monkeypatch, 500, json={"error": {"message": "upstream   timeout\n" + "x" * 500}})
    msg = _refusal(js, "openwebninja")
    assert msg.startswith("OpenWeb Ninja (JSearch) returned HTTP 500: upstream timeout x")
    assert len(msg) < 260  # the body's message is capped, not dumped


@pytest.mark.parametrize(
    "response", [{"text": "<html><body>Bad Gateway</body></html>"}, {"content": b""}, {"json": ["nope"]}, {"json": {}}]
)
def test_an_error_body_with_no_message_still_explains(js, monkeypatch, response):
    _answer(monkeypatch, 502, **response)
    assert _refusal(js, "jsearch") == "RapidAPI (JSearch) returned HTTP 502"
    _answer(monkeypatch, 403, **response)
    assert _refusal(js, "jsearch") == "RapidAPI (JSearch) denied access: 403 Forbidden, no reason given"
    _answer(monkeypatch, 401, **response)
    assert "Job-source API key" in _refusal(js, "jsearch")


def test_the_tool_explains_a_not_subscribed_key(plugin, iso, monkeypatch):
    from _plugin_testkit import FakeRegistry

    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    reg = FakeRegistry({"packet_root": str(iso / "ws"), "extended_tools": True, "jobs_api_key": "KEY"})
    plugin.register(reg)
    tool = {t.name: t for t in reg.tools}["careercoach_search_jobs"]
    _answer(monkeypatch, 403, json=_NOT_SUBSCRIBED)
    out = asyncio.run(tool.ainvoke({"query": "data engineer"}))  # Job source auto + a key → RapidAPI
    assert out.startswith("Job search failed: RapidAPI accepted the key, but its account isn't subscribed to JSearch.")
    assert "set Job source to 'openwebninja' in Settings → Career Coach" in out
    assert "openwebninja" in (tool.description or "")
