"""The market reply lists what reached the inbox (S5c re-run: 10 reported, 3 inboxed, UK roles listed)."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from _plugin_testkit import FakeRegistry


def posting(n, location, remote=False):
    return {
        "title": "Software Engineer",
        "company": f"Co {n}",
        "location": location,
        "remote": remote,
        "url": f"https://example.com/jobs/{n}",
        "source": "himalayas",
        "snippet": "Software engineering",
        "posted": "",
    }


@pytest.fixture
def market(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    source = importlib.import_module(plugin.__name__ + ".jobsource")
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    reg = FakeRegistry({"jobs_provider": "himalayas"}, plugin_id="careercoach")
    plugin.register(reg)
    tool = next(t for t in reg.tools if t.name == "careercoach_find_roles")

    def run(jobs, locations):
        async def fetch(*_args, **_kwargs):
            return jobs

        monkeypatch.setattr(source, "_get_himalayas", fetch)
        return asyncio.run(tool.ainvoke({"source": "market", "query": "Software Engineer", "locations": locations}))

    return run, wl


def test_only_roles_that_reach_the_inbox_are_listed(market):
    run, wl = market
    out = run([posting(1, "Remote, US", True), posting(2, "London, UK"), posting(3, "Remote, US", True)], "remote")
    assert "Found 2 role(s)" in out
    assert "Co 2" not in out
    assert "1 other(s) outside your locations" in out
    assert len(wl.inbox_for_view()) == 2


def test_no_location_filter_lists_everything(market):
    run, _wl = market
    out = run([posting(1, "Remote, US", True), posting(2, "London, UK")], "")
    assert "Found 2 role(s)" in out and "outside your locations" not in out


def test_every_result_outside_the_locations_says_so(market):
    run, wl = market
    out = run([posting(1, "London, UK")], "remote")
    assert "outside your locations" in out and "Found" not in out
    assert wl.inbox_for_view() == []
