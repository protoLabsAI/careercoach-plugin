"""careercoach_settings — get/set the Career Coach search + watch config keys.

The tool reads and writes the allowlisted keys the board scan and the background watch depend on
(target_roles, title_exclude, locations, search_country, max_age_days, watch_enabled,
watch_interval_min, watch_min_score, followup_days). The jobs_api_key secret and every key off the
allowlist are refused and never echoed. A set applies to the in-process config at once — so a
subsequent get and the next ``hunt.filters`` scan read see it with no restart — and persists through
the host's plugin-config store. That durable write is a seam (``_write_plugin_config``); the suite runs
host-free, so these tests monkeypatch it to prove it is (and isn't) reached.
"""

from __future__ import annotations

import importlib

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
    out = tool.invoke({"action": "get"})
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
    out = tool.invoke({"action": "get"})
    assert "max_age_days: 30" in out
    assert "watch_interval_min: 180" in out
    assert "watch_min_score: 75" in out
    assert "followup_days: 10" in out
    assert "watch_enabled: false" in out
    assert "title_exclude: intern, internship, new grad" in out
    assert "locations: (blank)" in out


def test_get_one_key_shows_just_that_value(plugin, iso):
    _reg, tool = _settings_tool(plugin, {"max_age_days": 21})
    assert tool.invoke({"action": "get", "key": "max_age_days"}) == "max_age_days: 21"


def test_get_single_secret_or_unknown_key_is_refused(plugin, iso):
    _reg, tool = _settings_tool(plugin, {"jobs_api_key": "SEEKRIT"})
    out = tool.invoke({"action": "get", "key": "jobs_api_key"})
    assert "secret" in out.lower()
    assert "SEEKRIT" not in out
    assert "search/watch setting" in tool.invoke({"action": "get", "key": "bogus"})


# ── r2: set applies in-process (next scan sees it) and persists ──────────────────────────────────
def test_set_applies_in_process_so_the_next_scan_sees_it(plugin, iso):
    hunt = importlib.import_module(plugin.__name__ + ".search.hunt")
    reg, tool = _settings_tool(plugin, {"target_roles": "AI Engineer", "locations": "remote", "max_age_days": 30})
    out = tool.invoke({"action": "set", "key": "max_age_days", "value": "14"})
    assert "Set max_age_days: 30 → 14" in out
    # a subsequent get sees it
    assert "max_age_days: 14" in tool.invoke({"action": "get"})
    assert tool.invoke({"action": "get", "key": "max_age_days"}) == "max_age_days: 14"
    # the next board scan reads the SAME registry.config dict through hunt.filters
    assert hunt.filters(reg.config)["max_age_days"] == 14
    # a string key, too — stored verbatim (trimmed) and visible to the scan
    tool.invoke({"action": "set", "key": "target_roles", "value": "ML Engineer, Data Scientist"})
    assert reg.config["target_roles"] == "ML Engineer, Data Scientist"
    assert hunt.filters(reg.config)["roles"] == "ML Engineer, Data Scientist"


def test_set_reaches_the_host_persist_path(plugin, iso, monkeypatch):
    reg, tool = _settings_tool(plugin, {"watch_min_score": 75})
    calls = []
    monkeypatch.setattr(plugin, "_write_plugin_config", lambda registry, key, value: calls.append((key, value)) or True)
    out = tool.invoke({"action": "set", "key": "watch_min_score", "value": "80"})
    assert calls == [("watch_min_score", 80)]  # the durable write got the typed value
    assert reg.config["watch_min_score"] == 80
    assert "Set watch_min_score: 75 → 80" in out
    assert "didn't confirm" not in out  # the host confirmed the save, so no fallback note


def test_bool_key_parses_true_false_and_rejects_junk(plugin, iso):
    reg, tool = _settings_tool(plugin, {"watch_enabled": False})
    tool.invoke({"action": "set", "key": "watch_enabled", "value": "true"})
    assert reg.config["watch_enabled"] is True
    tool.invoke({"action": "set", "key": "watch_enabled", "value": "off"})
    assert reg.config["watch_enabled"] is False
    out = tool.invoke({"action": "set", "key": "watch_enabled", "value": "maybe"})
    assert "true/false" in out
    assert reg.config["watch_enabled"] is False  # a bad value changes nothing


# ── r3: the secret and unknown keys are refused and change nothing ───────────────────────────────
def test_secret_is_refused_echoes_nothing_and_does_not_persist(plugin, iso, monkeypatch):
    reg, tool = _settings_tool(plugin, {"jobs_api_key": "SEEKRIT"})
    calls = []
    monkeypatch.setattr(plugin, "_write_plugin_config", lambda *a: calls.append(a) or True)
    out = tool.invoke({"action": "set", "key": "jobs_api_key", "value": "newkey"})
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
        out = tool.invoke({"action": "set", "key": key, "value": "x"})
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
    out = tool.invoke({"action": "set", "key": "max_age_days", "value": "soon"})
    assert "Not changed" in out and "number" in out
    assert reg.config["max_age_days"] == 30  # unchanged
    assert calls == []
    assert "Not changed" in tool.invoke({"action": "set", "key": "watch_interval_min", "value": "abc"})


def test_number_key_rejects_out_of_range_value(plugin, iso):
    reg, tool = _settings_tool(plugin, {})
    assert "between 0 and 100" in tool.invoke({"action": "set", "key": "watch_min_score", "value": "150"})
    assert "at least 5" in tool.invoke({"action": "set", "key": "watch_interval_min", "value": "2"})
    assert "0 or more" in tool.invoke({"action": "set", "key": "max_age_days", "value": "-3"})
    assert "watch_min_score" not in reg.config  # nothing was stored


# ── misc: unknown action and a set with no key both report cleanly ───────────────────────────────
def test_unknown_action_and_missing_key_are_handled(plugin, iso):
    _reg, tool = _settings_tool(plugin, {})
    assert "Unknown action" in tool.invoke({"action": "frobnicate"})
    assert "pass its key and value" in tool.invoke({"action": "set"})


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
    out = tool.invoke({"action": "set", "key": key, "value": value})
    assert out.startswith(f"Set {key}:")
    assert key in reg.config
    assert tool.invoke({"action": "get", "key": key}).startswith(f"{key}:")
