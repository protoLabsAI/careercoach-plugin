"""First-run setup uses confirmed profile fields without Settings or live network."""

import asyncio
import importlib
from pathlib import Path

import pytest

from _plugin_testkit import FakeRegistry


def test_empty_profile_hint_updates_after_write(plugin, profile):
    reg = FakeRegistry()
    plugin.register(reg)
    mw = next(m for f in reg.middlewares if type(m := f(None)).__name__ == "_ProfileMiddleware")
    assert "First run:" in mw._current()
    assert "skills/job-hunt/SKILL.md" in mw._current()
    profile.update_field("target_roles", "Software Engineer")
    assert "First run:" not in mw._current()


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_profile_filter_fallback_and_settings_precedence(plugin, profile, blank):
    hunt = importlib.import_module(plugin.__name__ + ".hunt")
    profile.update_field("target_roles", "Software Engineer")
    profile.update_field("target_locations", "Remote")
    f = hunt.filters({"target_roles": blank, "locations": blank})
    assert f["roles"] == "Software Engineer"
    assert f["locations"] == "Remote"
    f = hunt.filters({"target_roles": "Designer", "locations": "London"})
    assert f["roles"] == "Designer"
    assert f["locations"] == "London"


@pytest.mark.parametrize("configured", [False, True])
def test_default_market_is_keyless_and_feeds_view(plugin, profile, monkeypatch, configured):
    reg = FakeRegistry({"jobs_provider": "jsearch", "jobs_api_key": "test-key"} if configured else {})
    plugin.register(reg)
    tools = {t.name: t for t in reg.tools}
    for field, value in [("target_roles", "Software Engineer"), ("target_locations", "Remote")]:
        tools["careercoach_profile"].invoke({"action": "update", "field": field, "content": value})
    source = importlib.import_module(plugin.__name__ + ".jobsource")
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    calls = []

    async def fetch(query, country):
        calls.append((query, country))
        jobs = [
            {
                "title": "Software Engineer",
                "company": "Example",
                "location": "Remote",
                "remote": True,
                "url": "https://example.com/jobs/1",
                "source": "himalayas",
                "snippet": "Software engineering",
                "posted": "",
            }
        ]
        return jobs + [{**jobs[0], "url": "https://example.com/jobs/2", "location": "Berlin", "remote": False}]

    monkeypatch.setattr(source, "_get_himalayas", fetch)
    assert source.choose_provider("auto", "") == "himalayas"
    args = {"source": "market"}
    if configured:
        args["provider"] = "himalayas"
    result = asyncio.run(tools["careercoach_find_roles"].ainvoke(args))
    assert calls == [("Software Engineer", "")]
    assert "Himalayas" in result
    if not configured:
        assert "keyless" in result
    assert len(wl.inbox_for_view()) == 1
    assert wl.peek_inbox() == []
    digest = asyncio.run(tools["careercoach_digest"].ainvoke({}))
    assert "From the background watch" not in digest
    assert len(wl.inbox_for_view()) == 1
    tools["careercoach_answers"].invoke({"action": "get"})
    answers = importlib.import_module(plugin.__name__ + ".answers")
    stored, error = answers.load_checked()
    assert not error
    assert stored
    assert all(entry["status"] == "draft" for entry in stored.values())


def test_skill_first_run_sequence():
    text = (Path(__file__).resolve().parents[1] / "skills/job-hunt/SKILL.md").read_text()
    flow = text.split("## First run / set me up", 1)[1].split("### Optional enrichment", 1)[0]
    steps = ["Import profile", "Confirm targets once", "Seed answers", "Find roles", "Open the view"]
    assert [flow.index(step) for step in steps] == sorted(flow.index(step) for step in steps)
    for required in [
        "at most one confirmation question",
        "here's my resume",
        "empty profile",
        "knowledge_ingest",
        "answers.seed_from_profile",
        "**drafts**",
        'field="target_roles"',
        'field="location"',
        'field="target_locations"',
        'provider="himalayas"',
        "**inbox**",
    ]:
        assert required in flow


def test_unreadable_profile_is_not_onboarding(plugin, profile):
    profile.update_field("target_roles", "Software Engineer")
    profile._path().write_text("{broken", encoding="utf-8")
    reg = FakeRegistry()
    plugin.register(reg)
    mw = next(m for f in reg.middlewares if type(m := f(None)).__name__ == "_ProfileMiddleware")
    assert "unreadable" in mw._current()
    assert "First run:" not in mw._current()
    hunt = importlib.import_module(plugin.__name__ + ".hunt")
    with pytest.raises(profile.StoreUnreadable):
        hunt.filters({})
    assert hunt.filters({"target_roles": "Designer", "locations": "Remote"})["roles"] == "Designer"


def test_home_location_does_not_restrict_existing_searches(plugin, profile):
    hunt = importlib.import_module(plugin.__name__ + ".hunt")
    boards = importlib.import_module(plugin.__name__ + ".boards")
    profile.update_field("location", "Portland, OR")
    profile.update_field("target_roles", "Software Engineer")
    for blank in ("", "   ", None):
        filters = hunt.filters({"locations": blank})
        assert filters["locations"] == ""
        assert boards.passes({"title": "Software Engineer", "location": "Remote"}, locations=filters["locations"])
    profile.update_field("target_locations", "Remote")
    assert hunt.filters({})["locations"] == "Remote"
