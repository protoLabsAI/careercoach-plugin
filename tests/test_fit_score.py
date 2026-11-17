"""The quick fit score and the Himalayas fixes (jobCoach, 2026-11-16).

Two failures on the live agent: every inbox role scored 100, because the score was the title match the
watch had already filtered on; and the coach couldn't read 9 of 10 shortlisted roles, because their only
link was a himalayas.app page that Cloudflare refuses to automated fetches, while the full posting text
the Himalayas API sent was cut to a 400-character snippet.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import httpx
import pytest

import test_hunt
from _plugin_testkit import FakeRegistry

mods = test_hunt.mods

PROFILE = (
    "**Can lead on:** design systems, React, TypeScript, Web Components, Storybook, accessibility. "
    "Agent systems: LangGraph, MCP, evals, RAG, prompt engineering, Python, FastAPI, Postgres."
)
ROLES = "AI Engineer, Product Engineer, Design Engineer"


@pytest.fixture
def fs(plugin):
    return importlib.import_module(plugin.__name__ + ".search.fitscore")


@pytest.fixture
def resolve(plugin):
    return importlib.import_module(plugin.__name__ + ".search.resolve")


def _job(title, description="", comp=""):
    return {"title": title, "description": description, "comp": comp}


# ── the score spreads; it doesn't just echo the title filter ──────────────────
def test_two_titles_that_both_pass_the_filter_score_apart(fs):
    good = _job(
        "Senior AI Engineer",
        "Build agentic workflows with LLMs, RAG and evals. Python, TypeScript, React, Postgres.",
    )
    compiler = _job(
        "AI Compiler Engineer",
        "Own our MLIR/LLVM compiler stack, C++ codegen and CUDA kernels for our ASIC.",
    )
    g, _ = fs.score(good, ROLES, PROFILE)
    c, why = fs.score(compiler, ROLES, PROFILE)
    assert g >= 85
    assert c <= 45 and "off-domain" in why


def test_seniority_and_junior_cap(fs):
    desc = "Python, TypeScript, React, LLM agents and evals."
    senior, _ = fs.score(_job("Senior Product Engineer", desc), ROLES, PROFILE)
    staff, _ = fs.score(_job("Staff Product Engineer", desc), ROLES, PROFILE)
    principal, _ = fs.score(_job("Principal Product Engineer", desc), ROLES, PROFILE)
    junior, why = fs.score(_job("Junior Product Engineer", desc), ROLES, PROFILE)
    assert senior > staff > principal
    assert junior <= 50 and "junior" in why


def test_pay_floor(fs):
    desc = "Python, TypeScript, React, LLM agents and evals."
    above, why = fs.score(_job("Product Engineer", desc, "USD 150,000–190,000"), ROLES, PROFILE, 120_000)
    assert "pay $150K+" in why
    under, why = fs.score(_job("Product Engineer", desc, "$80K - $110K"), ROLES, PROFILE, 120_000)
    assert under <= 40 and "under your $120K floor" in why
    unposted, why = fs.score(_job("Product Engineer", desc), ROLES, PROFILE, 120_000)
    assert "pay not posted" in why and unposted > under
    # no floor configured: pay doesn't take part at all
    _, why = fs.score(_job("Product Engineer", desc, "$80K - $110K"), ROLES, PROFILE, 0)
    assert "pay" not in why


def test_pay_range_parsing(fs):
    assert fs.pay_range("USD 190,000–255,000") == (190_000, 255_000)
    assert fs.pay_range("$150K - $210K") == (150_000, 210_000)
    assert fs.pay_range("Upto $150/hr") is None
    assert fs.pay_range("") is None


def test_a_thin_posting_scores_its_stack_neutral_and_says_so(fs):
    _, why = fs.score(_job("Product Engineer", "Join us!"), ROLES, PROFILE)
    assert "stack unknown (posting too thin)" in why
    _, why = fs.score(_job("Product Engineer", "Python React TypeScript"), ROLES, "")
    assert "no skills in profile" in why


def test_table_stakes_never_count_as_a_gap(fs):
    _, why = fs.score(_job("Product Engineer", "Python, TypeScript, React, Git, SQL, HTML, REST APIs."), ROLES, PROFILE)
    assert "missing" not in why


def test_do_not_claim_is_never_matched(fs, plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    profile = importlib.import_module(plugin.__name__ + ".record.profile")
    prof = profile.empty_profile()
    prof["sections"]["skills"] = "Python"
    prof["sections"]["do_not_claim"] = "Kubernetes, Rust"
    profile.save_profile(prof)
    text = fs.profile_text()
    assert "Python" in text and "Kubernetes" not in text


# ── Himalayas: keep the posting; find the company's own page ──────────────────
def test_himalayas_parser_keeps_the_full_description_and_company_slug(plugin):
    js = importlib.import_module(plugin.__name__ + ".search.jobsource")
    long = "<p>" + ("Build agent workflows in Python. " * 60) + "</p>"
    [job] = js.parse_himalayas(
        {
            "jobs": [
                {
                    "title": "AI Engineer",
                    "companyName": "Acme",
                    "companySlug": "acme",
                    "applicationLink": "https://himalayas.app/companies/acme/jobs/ai",
                    "description": long,
                }
            ]
        }
    )
    assert len(job["snippet"]) == 400
    assert len(job["description"]) > 1500 and "<p>" not in job["description"]
    assert job["company_slug"] == "acme"


def test_match_title_finds_the_same_role_on_the_board(resolve):
    board = [
        {"title": "Sr. Software Engineer I/II - Operational AI", "url": "https://ats/1"},
        {"title": "Sr Software Engineer, Platform", "url": "https://ats/2"},
    ]
    assert resolve.match_title("Sr Software Engineer I/II, Operational AI", board)["url"] == "https://ats/1"
    assert resolve.match_title("Staff Data Engineer", board) is None


ASHBY_ACME = {
    "jobs": [
        {
            "id": "a1",
            "title": "AI Engineer",
            "location": "Remote",
            "isRemote": True,
            "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
            "publishedAt": "2026-11-15T00:00:00Z",
            "descriptionPlain": "The whole posting from Acme's own board. " * 30,
        }
    ]
}


def _ats_net(monkeypatch, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "api.ashbyhq.com" and request.url.path.endswith("/acme"):
            return httpx.Response(200, json=ASHBY_ACME)
        return httpx.Response(404, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_resolve_finds_the_ats_posting_and_caches_the_lookup(resolve, mods, monkeypatch):
    calls: list[str] = []
    _ats_net(monkeypatch, calls)
    job = {
        "title": "AI Engineer",
        "company": "Acme",
        "company_slug": "acme",
        "url": "https://himalayas.app/companies/acme/jobs/ai-engineer?utm_source=api",
        "description": "short",
    }
    assert asyncio.run(resolve.resolve([job])) == 1
    assert job["apply_url"] == "https://jobs.ashbyhq.com/acme/a1"
    assert job["description"].startswith("The whole posting")
    # a second role at the same company within the week reads the cached board, no probing
    probes = len(calls)
    again = {**job, "apply_url": "", "title": "AI Engineer"}
    asyncio.run(resolve.resolve([again]))
    assert again["apply_url"] and len(calls) == probes + 1  # just the one board read


def test_resolve_remembers_a_miss_and_leaves_other_links_alone(resolve, mods, monkeypatch):
    calls: list[str] = []
    _ats_net(monkeypatch, calls)
    nowhere = {"title": "AI Engineer", "company": "Nowhere Inc", "url": "https://himalayas.app/companies/x/jobs/y"}
    own_site = {"title": "AI Engineer", "company": "Acme", "url": "https://acme.com/careers/1"}
    assert asyncio.run(resolve.resolve([nowhere, own_site])) == 0
    assert "apply_url" not in nowhere and "apply_url" not in own_site
    probes = len(calls)
    asyncio.run(resolve.resolve([dict(nowhere)]))
    assert len(calls) == probes  # a recent miss isn't probed again


# ── the coach can read a card without opening the blocked page ────────────────
@pytest.fixture
def track(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    reg = FakeRegistry({}, plugin_id="careercoach")
    plugin.register(reg)
    wl = importlib.import_module(plugin.__name__ + ".search.watchlist")
    return next(t for t in reg.tools if t.name == "careercoach_track"), wl


def _card(jid, **kw):
    return {
        "id": jid,
        "company": "Acme",
        "title": "AI Engineer",
        "location": "Remote",
        "url": f"https://himalayas.app/companies/acme/jobs/{jid}",
        "hash": jid,
        "score": 70,
        "reason": "stack 4/5 · mid/senior",
        **kw,
    }


def test_show_returns_the_full_posting_and_the_apply_link(track):
    tool, wl = track
    wl.add_to_inbox(
        [_card("one", description="Full posting text. " * 50, apply_url="https://jobs.ashbyhq.com/acme/a1")]
    )
    out = tool.invoke({"action": "show", "inbox_id": "one"})
    assert "Apply on the company's own page: https://jobs.ashbyhq.com/acme/a1" in out
    assert "blocks automated reads" in out
    assert out.count("Full posting text.") == 50
    assert "stack 4/5" in out


def test_show_without_an_ats_match_says_how_to_apply(track):
    tool, wl = track
    wl.add_to_inbox([_card("two", description="Posting.")])
    out = tool.invoke({"action": "show", "company": "Acme", "role": "AI Engineer"})
    assert "company's careers page" in out and "Posting." in out
    assert "wasn't looked up" in out  # never claims a lookup that didn't run


def test_inbox_lists_best_fit_first_with_reasons(track):
    tool, wl = track
    wl.add_to_inbox([_card("low", title="Low Role", score=40), _card("high", title="High Role", score=90)])
    out = tool.invoke({"action": "inbox"})
    assert out.index("High Role") < out.index("Low Role")
    assert "fit 90 (stack 4/5 · mid/senior)" in out


def test_the_board_view_payload_leaves_descriptions_out(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    wl = importlib.import_module(plugin.__name__ + ".search.watchlist")
    wl.add_to_inbox([_card("one", description="x" * 5000)])
    reg = FakeRegistry({}, plugin_id="careercoach")
    plugin.register(reg)
    _prefix, router = next((p, r) for p, r in reg.routers if p == "/api/plugins/careercoach")
    route = next(r for r in router.routes if getattr(r, "path", "") == "/jobs")
    data = json.loads(asyncio.run(route.endpoint()).body)
    assert data["inbox"] and "description" not in data["inbox"][0]


def test_a_rate_limited_probe_is_not_cached_as_a_miss(resolve, mods, monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(429, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    job = {"title": "AI Engineer", "company": "Acme", "url": "https://himalayas.app/companies/acme/jobs/ai"}
    asyncio.run(resolve.resolve([job]))
    assert "ats_lookup" not in job  # inconclusive: no claim either way
    first = len(calls)
    asyncio.run(resolve.resolve([dict(job)]))
    assert len(calls) > first  # looked again, nothing was cached


def test_lookup_outcomes_are_recorded_and_shown(resolve, mods, monkeypatch, track):
    calls: list[str] = []
    _ats_net(monkeypatch, calls)
    on_board = {
        "title": "Staff Data Engineer",
        "company": "Acme",
        "id": "a",
        "url": "https://himalayas.app/companies/acme/jobs/data",
        "hash": "a",
    }
    no_board = {"title": "AI Engineer", "company": "Nowhere Inc", "id": "b",
                "url": "https://himalayas.app/companies/nowhere/jobs/ai", "hash": "b"}  # fmt: skip
    asyncio.run(resolve.resolve([on_board, no_board]))
    assert on_board["ats_lookup"] == "no title" and no_board["ats_lookup"] == "no board"
    tool, wl = track
    wl.add_to_inbox([on_board, no_board, _card("c", company="Elsewhere")])
    assert "board doesn't list this title" in tool.invoke({"action": "show", "inbox_id": "a"})
    assert "has no Ashby/Greenhouse/Lever board" in tool.invoke({"action": "show", "inbox_id": "b"})
    assert "wasn't looked up" in tool.invoke({"action": "show", "inbox_id": "c"})


def test_a_board_that_fails_to_load_is_not_called_missing(resolve, mods, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.ashbyhq.com" and request.url.path.endswith("/acme"):
            handler.n += 1
            return httpx.Response(200, json=ASHBY_ACME) if handler.n == 1 else httpx.Response(503, json={})
        return httpx.Response(404, json={})

    handler.n = 0
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    job = {"title": "AI Engineer", "company": "Acme", "url": "https://himalayas.app/companies/acme/jobs/ai"}
    asyncio.run(resolve.resolve([job]))  # the probe finds the board; the board read then 503s
    assert "ats_lookup" not in job and "apply_url" not in job


def test_a_deterministic_probe_rejection_is_cached(resolve):
    assert not resolve._transient("ashby/acme is 'Other Co''s board, not 'Acme'")
    assert not resolve._transient("lever/acme matched by the short name 'acme' — confirm it's Acme's board")
    assert resolve._transient("greenhouse/acme: HTTPStatusError: 429 Too Many Requests")


@pytest.mark.parametrize("title", ["Embedded Software Engineer", "Hardware Engineer", "GPU Simulation Engineer"])
def test_an_off_domain_title_is_not_capped_when_the_profile_names_it(fs, title):
    desc = "C++, Python, Linux, firmware, RTOS, FPGA bring-up, CUDA."
    profile = "Embedded systems and hardware bring-up: C++, Python, firmware, RTOS, FPGA, CUDA, GPU simulation."
    _, why = fs.score(_job(title, desc), "Software Engineer, Hardware Engineer, Engineer", profile)
    assert "off-domain title" not in why
