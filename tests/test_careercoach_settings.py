"""careercoach_settings — get/set the Career Coach search + watch config keys.

The tool reads and writes the allowlisted keys the board scan and the background watch depend on
(target_roles, title_exclude, locations, search_country, max_age_days, watch_enabled,
watch_interval_min, watch_min_score, followup_days). The jobs_api_key secret and every key off the
allowlist are refused and never echoed. A set applies to the in-process config at once — so a
subsequent get and the next ``hunt.filters`` scan read see it with no restart — and persists through
the host's plugin-config store. That durable write is a seam (``_write_plugin_config``); the suite runs
host-free, including stand-ins matching the async host config writer contract.
"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock

import pytest

from _plugin_testkit import FakeRegistry

ALLOWED = (
    "target_roles",
    "title_exclude",
    "locations",
    "search_country",
    "max_age_days",
    "watch_enabled",
    "watch_interval_min",
    "watch_min_score",
    "followup_days",
)


def invoke(tool, args):
    return asyncio.run(tool.ainvoke(args))


def _settings_tool(plugin, cfg):
    """Register the plugin host-free and return (registry, the careercoach_settings tool)."""
    reg = FakeRegistry(cfg, plugin_id="careercoach")
    plugin.register(reg)
    tool = {t.name: t for t in reg.tools}["careercoach_settings"]
    return reg, tool


# ── r1: get lists every allowed key and never the secret ─────────────────────────────────────────
def test_get_lists_every_allowed_key_and_never_the_secret(plugin, iso):
    reg, tool = _settings_tool(
        plugin,
        {"target_roles": "AI Engineer", "max_age_days": 14, "watch_enabled": True, "jobs_api_key": "SEEKRIT"},
    )
    out = invoke(tool, {"action": "get"})
    for key in ALLOWED:
        assert f"{key}:" in out, key
    # the secret is neither listed nor echoed
    assert "jobs_api_key" not in out
    assert "SEEKRIT" not in out
    # values reflect the config, normalized to their type
    assert "max_age_days: 14" in out
    assert "watch_enabled: true" in out
    assert "target_roles: AI Engineer" in out


def test_get_shows_manifest_defaults_for_unset_keys(plugin, iso):
    _reg, tool = _settings_tool(plugin, {})
    out = invoke(tool, {"action": "get"})
    assert "max_age_days: 30" in out
    assert "watch_interval_min: 180" in out
    assert "watch_min_score: 75" in out
    assert "followup_days: 10" in out
    assert "watch_enabled: false" in out
    assert "title_exclude: intern, internship, new grad" in out
    assert "locations: (blank)" in out


def test_get_one_key_shows_just_that_value(plugin, iso):
    _reg, tool = _settings_tool(plugin, {"max_age_days": 21})
    assert invoke(tool, {"action": "get", "key": "max_age_days"}) == "max_age_days: 21"


def test_get_single_secret_or_unknown_key_is_refused(plugin, iso):
    _reg, tool = _settings_tool(plugin, {"jobs_api_key": "SEEKRIT"})
    out = invoke(tool, {"action": "get", "key": "jobs_api_key"})
    assert "secret" in out.lower()
    assert "SEEKRIT" not in out
    assert "search/watch setting" in invoke(tool, {"action": "get", "key": "bogus"})


# ── r2: set applies in-process (next scan sees it) and persists ──────────────────────────────────
def test_set_applies_in_process_so_the_next_scan_sees_it(plugin, iso):
    hunt = importlib.import_module(plugin.__name__ + ".search.hunt")
    reg, tool = _settings_tool(plugin, {"target_roles": "AI Engineer", "locations": "remote", "max_age_days": 30})
    out = invoke(tool, {"action": "set", "key": "max_age_days", "value": "14"})
    assert "Set max_age_days: 30 → 14" in out
    # a subsequent get sees it
    assert "max_age_days: 14" in invoke(tool, {"action": "get"})
    assert invoke(tool, {"action": "get", "key": "max_age_days"}) == "max_age_days: 14"
    # the next board scan reads the SAME registry.config dict through hunt.filters
    assert hunt.filters(reg.config)["max_age_days"] == 14
    # a string key, too — stored verbatim (trimmed) and visible to the scan
    invoke(tool, {"action": "set", "key": "target_roles", "value": "ML Engineer, Data Scientist"})
    assert reg.config["target_roles"] == "ML Engineer, Data Scientist"
    assert hunt.filters(reg.config)["roles"] == "ML Engineer, Data Scientist"


def test_set_reaches_the_host_persist_path(plugin, iso, monkeypatch):
    reg, tool = _settings_tool(plugin, {"watch_min_score": 75})
    calls = []
    monkeypatch.setattr(
        plugin,
        "_write_plugin_config",
        AsyncMock(side_effect=lambda registry, key, value: calls.append((key, value)) or True),
    )
    out = invoke(tool, {"action": "set", "key": "watch_min_score", "value": "80"})
    assert calls == [("watch_min_score", 80)]  # the durable write got the typed value
    assert reg.config["watch_min_score"] == 80
    assert "Set watch_min_score: 75 → 80" in out
    assert "didn't confirm" not in out  # the host confirmed the save, so no fallback note


def test_bool_key_parses_true_false_and_rejects_junk(plugin, iso):
    reg, tool = _settings_tool(plugin, {"watch_enabled": False})
    invoke(tool, {"action": "set", "key": "watch_enabled", "value": "true"})
    assert reg.config["watch_enabled"] is True
    invoke(tool, {"action": "set", "key": "watch_enabled", "value": "off"})
    assert reg.config["watch_enabled"] is False
    out = invoke(tool, {"action": "set", "key": "watch_enabled", "value": "maybe"})
    assert "true/false" in out
    assert reg.config["watch_enabled"] is False  # a bad value changes nothing


# ── r3: the secret and unknown keys are refused and change nothing ───────────────────────────────
def test_secret_is_refused_echoes_nothing_and_does_not_persist(plugin, iso, monkeypatch):
    reg, tool = _settings_tool(plugin, {"jobs_api_key": "SEEKRIT"})
    calls = []
    monkeypatch.setattr(plugin, "_write_plugin_config", lambda *a: calls.append(a) or True)
    out = invoke(tool, {"action": "set", "key": "jobs_api_key", "value": "newkey"})
    assert "secret" in out.lower()
    assert "Nothing changed" in out
    assert "newkey" not in out and "SEEKRIT" not in out  # neither the new nor the old secret is echoed
    assert reg.config["jobs_api_key"] == "SEEKRIT"  # untouched
    assert calls == []  # the persist path is never reached for a refused key


def test_unknown_key_is_refused_and_does_not_persist(plugin, iso, monkeypatch):
    reg, tool = _settings_tool(plugin, {"full_name": "Jo"})
    calls = []
    monkeypatch.setattr(plugin, "_write_plugin_config", lambda *a: calls.append(a) or True)
    # full_name / render_format are real config keys but NOT search/watch keys this tool owns
    for key in ("full_name", "render_format", "bogus"):
        out = invoke(tool, {"action": "set", "key": key, "value": "x"})
        assert "search/watch setting" in out
        assert "Nothing changed" in out
    assert reg.config.get("full_name") == "Jo"  # not overwritten
    assert "render_format" not in reg.config  # not created
    assert "bogus" not in reg.config
    assert calls == []


# ── r4: number keys refuse non-numeric (and out-of-range) values ─────────────────────────────────
def test_number_key_rejects_non_numeric_value(plugin, iso, monkeypatch):
    reg, tool = _settings_tool(plugin, {"max_age_days": 30})
    calls = []
    monkeypatch.setattr(plugin, "_write_plugin_config", lambda *a: calls.append(a) or True)
    out = invoke(tool, {"action": "set", "key": "max_age_days", "value": "soon"})
    assert "Not changed" in out and "number" in out
    assert reg.config["max_age_days"] == 30  # unchanged
    assert calls == []
    assert "Not changed" in invoke(tool, {"action": "set", "key": "watch_interval_min", "value": "abc"})


def test_number_key_rejects_out_of_range_value(plugin, iso):
    reg, tool = _settings_tool(plugin, {})
    assert "between 0 and 100" in invoke(tool, {"action": "set", "key": "watch_min_score", "value": "150"})
    assert "at least 5" in invoke(tool, {"action": "set", "key": "watch_interval_min", "value": "2"})
    assert "0 or more" in invoke(tool, {"action": "set", "key": "max_age_days", "value": "-3"})
    assert "watch_min_score" not in reg.config  # nothing was stored


# ── misc: unknown action and a set with no key both report cleanly ───────────────────────────────
def test_unknown_action_and_missing_key_are_handled(plugin, iso):
    _reg, tool = _settings_tool(plugin, {})
    assert "Unknown action" in invoke(tool, {"action": "frobnicate"})
    assert "pass its key and value" in invoke(tool, {"action": "set"})


@pytest.mark.parametrize("key", ALLOWED)
def test_every_allowed_key_round_trips_through_set_then_get(plugin, iso, key):
    """Each allowlisted key accepts a valid value and reads it straight back."""
    reg, tool = _settings_tool(plugin, {})
    value = {
        "watch_enabled": "true",
        "max_age_days": "7",
        "watch_interval_min": "30",
        "watch_min_score": "60",
        "followup_days": "5",
    }.get(key, "something")
    out = invoke(tool, {"action": "set", "key": key, "value": value})
    assert out.startswith(f"Set {key}:")
    assert key in reg.config
    assert invoke(tool, {"action": "get", "key": key}).startswith(f"{key}:")


def install_host_writer(monkeypatch, writer):
    import sys
    from types import ModuleType

    ops = ModuleType("ops")
    config = ModuleType("ops.config")
    config.set_config = writer
    ops.config = config
    monkeypatch.setitem(sys.modules, "ops", ops)
    monkeypatch.setitem(sys.modules, "ops.config", config)


def test_async_host_write_updates_live_and_captured_scan_config(plugin, iso, monkeypatch):
    reg, tool = _settings_tool(plugin, {"max_age_days": 30, "locations": "remote"})
    reg.config_section = "custom_section"
    captured = reg.config
    live = {"max_age_days": 21, "locations": "remote"}
    reg.live_config = lambda: live
    calls = []

    async def set_config(updates, *, ctx=None, apply_settings=None):
        nonlocal live
        calls.append(updates)
        live = {**live, **updates["plugin_config"]["custom_section"]}
        return {"ok": True}

    install_host_writer(monkeypatch, set_config)
    out = invoke(tool, {"action": "set", "key": "max_age_days", "value": "7"})
    assert "21 → 7" in out
    assert "didn't confirm" not in out
    assert calls == [{"plugin_config": {"custom_section": {"max_age_days": 7}}}]
    assert invoke(tool, {"key": "max_age_days"}) == "max_age_days: 7"
    hunt = importlib.import_module(plugin.__name__ + ".search.hunt")
    assert hunt.filters(captured)["max_age_days"] == 7
    assert live["locations"] == "remote"


@pytest.mark.parametrize("failure", [RuntimeError("save failed"), False, {"error": "save failed"}])
def test_failed_host_save_reports_session_only(plugin, iso, monkeypatch, failure):
    reg, tool = _settings_tool(plugin, {"max_age_days": 30})
    live = dict(reg.config)
    reg.live_config = lambda: live

    async def set_config(updates, *, ctx=None, apply_settings=None):
        if isinstance(failure, Exception):
            raise failure
        return failure

    install_host_writer(monkeypatch, set_config)
    out = invoke(tool, {"action": "set", "key": "max_age_days", "value": "7"})
    assert "didn't confirm a durable save" in out
    assert live["max_age_days"] == reg.config["max_age_days"] == 7


@pytest.mark.parametrize("asynchronous", [False, True])
def test_registry_writer_is_awaited_when_needed(plugin, monkeypatch, asynchronous):
    reg = FakeRegistry()
    calls = []

    def write(key, value):
        calls.append((key, value))

    reg.set_config = AsyncMock(side_effect=write) if asynchronous else write
    assert asyncio.run(plugin._write_plugin_config(reg, "max_age_days", 7))
    assert calls == [("max_age_days", 7)]


def test_watch_uses_live_enable_interval_and_score(plugin, iso, monkeypatch):
    import sys
    import time
    from types import SimpleNamespace

    now = [0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    work = []
    stopped = []

    def supervise(scan, **kwargs):
        work.append(scan)
        return SimpleNamespace(start=lambda: None, stop=lambda: stopped.append(True))

    monkeypatch.setattr(sys.modules["graph.sdk"], "supervise", supervise, raising=False)
    tick = AsyncMock()
    monkeypatch.setattr(plugin, "_watch_tick", tick)
    reg, tool = _settings_tool(plugin, {"watch_enabled": False, "watch_interval_min": 180})
    live = dict(reg.config)
    reg.live_config = lambda: live
    install_host_writer(monkeypatch, AsyncMock(return_value={"ok": True}))
    start, stop, _reload = reg.surface_specs["careercoach-watch"]
    asyncio.run(start())
    scan = work[0]
    asyncio.run(scan())
    tick.assert_not_awaited()
    invoke(tool, {"action": "set", "key": "watch_enabled", "value": "true"})
    asyncio.run(scan())
    assert tick.await_count == 1
    now[0] = 300
    asyncio.run(scan())
    assert tick.await_count == 1
    invoke(tool, {"action": "set", "key": "watch_interval_min", "value": "5"})
    invoke(tool, {"action": "set", "key": "watch_min_score", "value": "0"})
    asyncio.run(scan())
    assert tick.await_count == 2
    assert tick.call_args.args[2] == 0
    invoke(tool, {"action": "set", "key": "watch_interval_min", "value": "60"})
    now[0] = 600
    asyncio.run(scan())
    assert tick.await_count == 2
    invoke(tool, {"action": "set", "key": "watch_enabled", "value": "false"})
    now[0] = 5000
    asyncio.run(scan())
    assert tick.await_count == 2
    asyncio.run(stop())
    assert stopped == [True]


def test_registry_writer_failure_falls_back_to_host(plugin, monkeypatch):
    reg = FakeRegistry(plugin_id="careercoach")
    reg.set_config = AsyncMock(side_effect=RuntimeError("unavailable"))
    writer = AsyncMock(return_value={"ok": True})
    install_host_writer(monkeypatch, writer)
    assert asyncio.run(plugin._write_plugin_config(reg, "max_age_days", 7))
    writer.assert_awaited_once_with({"plugin_config": {"careercoach": {"max_age_days": 7}}})


def test_no_host_save_is_explicitly_session_only(plugin, iso, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "ops.config", None)
    reg, tool = _settings_tool(plugin, {})
    out = invoke(tool, {"action": "set", "key": "max_age_days", "value": "7"})
    assert "didn't confirm a durable save" in out
    assert reg.config["max_age_days"] == 7
