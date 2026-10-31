"""A tracked role leaves the inbox, and the agent can triage the inbox itself (jobCoach friction,
2026-10-31: a role tracked as applied kept its card, and no tool could dismiss it)."""

from __future__ import annotations

import importlib

import pytest

from _plugin_testkit import FakeRegistry

HIMALAYAS = "https://himalayas.app/companies/acme/jobs/frontend-engineer?utm_source=api"


def card(jid="one", **overrides):
    return {
        "id": jid,
        "company": "Acme",
        "title": "Frontend Engineer",
        "location": "Remote",
        "url": f"https://example.com/jobs/{jid}",
        "posted": "2026-10-26T00:00:00+00:00",
        "hash": f"hash-{jid}",
        "score": 80,
        "reason": 'title matches "Frontend Engineer" (2/2 words)',
        **overrides,
    }


@pytest.fixture
def stores(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    state = importlib.import_module(plugin.__name__ + ".state")
    return wl, state


@pytest.fixture
def track(plugin, iso, monkeypatch):
    """The `careercoach_track` tool, plus the registry so tests can read `emitted`."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    reg = FakeRegistry({}, plugin_id="careercoach")
    plugin.register(reg)
    return next(t for t in reg.tools if t.name == "careercoach_track"), reg


def ids(jobs):
    return [j["id"] for j in jobs]


def test_tracking_the_same_company_and_role_clears_the_card(stores):
    wl, state = stores
    wl.add_to_inbox([card()])
    state.track_application(company="Acme Inc", role="frontend engineer", status="applied")
    assert wl.inbox_for_view() == []
    assert wl.peek_inbox() == []


def test_tracking_with_the_watch_url_clears_a_retitled_card(stores):
    wl, state = stores
    wl.add_to_inbox([card(url=HIMALAYAS, title="Sr/Staff Frontend Engineer | Web Products")])
    state.track_application(company="Acme", role="Senior Frontend Engineer", status="applied", source=HIMALAYAS)
    assert wl.inbox_for_view() == []


def test_a_row_tracked_before_the_fix_clears_with_no_backfill(stores):
    wl, state = stores
    state.track_application(company="Acme", role="Frontend Engineer", status="rejected")
    wl.add_to_inbox([card()])  # already in the store when the view reads it
    assert wl.inbox_for_view() == []


def test_another_role_or_company_keeps_its_card(stores):
    wl, state = stores
    wl.add_to_inbox([card("a"), card("b", title="Backend Engineer"), card("c", company="Globex")])
    state.track_application(company="Acme", role="Frontend Engineer", status="applied")
    assert sorted(ids(wl.inbox_for_view())) == ["b", "c"]


def test_a_shared_url_at_another_company_does_not_clear_a_card(stores):
    wl, state = stores
    wl.add_to_inbox([card(company="Globex", url=HIMALAYAS)])
    state.track_application(company="Acme", role="Other Role", status="applied", source=HIMALAYAS)
    assert ids(wl.inbox_for_view()) == ["one"]


def test_the_digest_does_not_consume_or_repeat_a_tracked_card(stores):
    wl, state = stores
    wl.add_to_inbox([card("a"), card("b", title="Backend Engineer")])
    state.track_application(company="Acme", role="Frontend Engineer", status="applied")
    assert ids(wl.take_inbox()) == ["b"]


def test_an_unreadable_pipeline_shows_the_cards_rather_than_hiding_them(stores):
    wl, state = stores
    wl.add_to_inbox([card()])
    state._path().parent.mkdir(parents=True, exist_ok=True)
    state._path().write_text("{not json")
    assert ids(wl.inbox_for_view()) == ["one"]


def test_inbox_action_lists_cards_with_their_ids(stores, track):
    wl, _ = stores
    tool, _reg = track
    wl.add_to_inbox([card()])
    out = tool.invoke({"action": "inbox"})
    assert "Frontend Engineer @ Acme" in out and "one" in out


def test_inbox_action_reports_an_empty_inbox(stores, track):
    tool, _reg = track
    assert "empty" in tool.invoke({"action": "inbox"}).lower()


def test_inbox_list_is_capped_and_says_how_many_it_left_out(stores, track):
    wl, _ = stores
    tool, _reg = track
    wl.add_to_inbox([card(f"c{i}", title=f"Engineer {i}") for i in range(35)])
    out = tool.invoke({"action": "inbox"})
    assert out.count("\n- ") == 30
    assert "5 more" in out


def test_dismiss_by_id_sticks_and_tells_the_view(stores, track):
    wl, _ = stores
    tool, reg = track
    wl.add_to_inbox([card()])
    out = tool.invoke({"action": "dismiss", "inbox_id": "one"})
    assert "Dismissed Frontend Engineer @ Acme" in out
    assert wl.inbox_for_view() == []
    wl.add_to_inbox([card()])  # the next scan finds it again
    assert wl.inbox_for_view() == []
    assert "careercoach.inbox_changed" in [topic for topic, _ in reg.emitted]


def test_dismiss_by_company_and_role(stores, track):
    wl, _ = stores
    tool, _reg = track
    wl.add_to_inbox([card()])
    assert "Dismissed" in tool.invoke({"action": "dismiss", "company": "Acme Inc", "role": "Frontend Engineer"})
    assert wl.inbox_for_view() == []


def test_keep_promotes_the_card_with_its_evidence(stores, track):
    wl, state = stores
    tool, reg = track
    wl.add_to_inbox([card()])
    out = tool.invoke({"action": "keep", "inbox_id": "one"})
    assert "Kept Frontend Engineer @ Acme" in out
    assert wl.inbox_for_view() == []
    row = state.load_applications()[0]
    assert (row["status"], row["fit_score"], row["source"]) == ("considering", 0, "https://example.com/jobs/one")
    assert "careercoach.application_tracked" in [topic for topic, _ in reg.emitted]


@pytest.mark.parametrize("action", ["keep", "dismiss"])
def test_a_card_that_is_not_there_is_refused_by_name(stores, track, action):
    wl, _ = stores
    tool, _reg = track
    wl.add_to_inbox([card()])
    assert "No inbox card" in tool.invoke({"action": action, "inbox_id": "nope"})
    assert "No inbox card" in tool.invoke({"action": action, "company": "Globex", "role": "Frontend Engineer"})
    assert ids(wl.inbox_for_view()) == ["one"]


def test_the_keep_route_still_works(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    reg = FakeRegistry({}, plugin_id="careercoach")
    plugin.register(reg)
    prefix, router = next((p, r) for p, r in reg.routers if p == "/api/plugins/careercoach")
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    wl.add_to_inbox([card()])
    client = TestClient(app)
    res = client.post(f"{prefix}/inbox/keep", json={"id": "one"})
    assert res.status_code == 200 and res.json()["application"]["status"] == "considering"
    assert client.post(f"{prefix}/inbox/keep", json={"id": "one"}).status_code == 404
