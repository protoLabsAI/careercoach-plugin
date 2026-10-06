"""Live job search relevance — the keyless Remotive fallback must answer the QUERY, and say it answered.

Remotive's ``?search=`` came back with the same unrelated postings for every query, and the code took
``jobs[:limit]`` as-is, so "data engineer" and "pastry chef" returned identical lists. ``search_jobs``
now ranks/filters Remotive results client-side with ``prescore`` and reports which provider answered.

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
    return importlib.import_module(plugin.__name__ + ".jobsource")


@pytest.fixture
def remotive(monkeypatch):
    """Route every httpx call to a mock Remotive that ignores the query. Returns the request log."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
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
    assert remotive[0].url.params["search"] == "data engineer"  # still asks Remotive to search


def test_irrelevant_postings_are_dropped_not_padded(js, remotive):
    out = _search(js, "kubernetes administrator")
    assert out.jobs == [] and out.fetched == 4 and out.matched == 0


def test_limit_applies_after_relevance_ranking(js, remotive):
    out = _search(js, "data", limit=1)
    assert [j["company"] for j in out.jobs] == ["Globex"]  # the title hit, not the first posting
    assert out.matched == 2


def test_the_result_names_the_provider_and_a_keyless_fallback(js, remotive):
    auto = _search(js, "data engineer")
    assert auto.provider == "remotive" and auto.keyless_fallback is True
    chosen = _search(js, "data engineer", provider="remotive")
    assert chosen.provider == "remotive" and chosen.keyless_fallback is False  # explicitly configured


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


def test_the_tool_reports_which_provider_answered(tools, remotive):
    tool = tools["careercoach_search_jobs"]
    out = asyncio.run(tool.ainvoke({"query": "data engineer"}))
    assert "Senior Data Engineer" in out and "Customer Success" not in out
    assert "Remotive" in out and "keyless fallback" in out and "2 of the 4" in out

    empty = asyncio.run(tool.ainvoke({"query": "kubernetes administrator"}))
    assert empty.startswith("No postings found") and "Remotive" in empty and "0 of the 4" in empty
