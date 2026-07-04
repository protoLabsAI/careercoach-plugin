"""Tests for Career Coach — host-free (no protoAgent running).

Two layers:
- pure modules (``rubric``, ``state``) tested directly;
- ``register()`` tested both host-free (guards skip the host-only knobs/subagents) and with
  lightweight host stubs monkeypatched in (the full surface wires up).
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


# ── manifest ──────────────────────────────────────────────────────────────────
def test_manifest_is_valid():
    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert m["id"] == "careercoach" and m["version"]
    assert m["config_section"] == "careercoach"
    assert "jobs_api_key" in m["secrets"]
    assert {s["key"] for s in m["settings"]} >= {"full_name", "location", "target_roles", "render_format"}
    assert m["views"][0]["path"] == "/plugins/careercoach/view"


# ── the pure fit rubric ───────────────────────────────────────────────────────
def test_rubric_defaults_and_presets_sum_to_100(plugin):
    rubric = importlib.import_module(plugin.__name__ + ".rubric")
    assert sum(rubric.DEFAULT_WEIGHTS.values()) == 100
    for name, preset in rubric.PRESETS.items():
        assert sum(preset.values()) == 100, name
    assert set(rubric.DIMENSIONS) == set(rubric.DEFAULT_WEIGHTS)


def test_rubric_weighted_overall_and_verdict(plugin):
    rubric = importlib.import_module(plugin.__name__ + ".rubric")
    # All-80s → 80 regardless of weights; verdict Strong Fit.
    assert rubric.weighted_overall({d: 80 for d in rubric.DIMENSIONS}) == 80.0
    assert rubric.verdict(80.0)[0] == "Strong Fit"
    assert rubric.verdict(50.0)[0] == "Moderate Fit"
    assert rubric.verdict(10.0)[0] == "Poor Fit"
    # Partial evaluation still yields a sensible number (only scored dims count).
    assert rubric.weighted_overall({"technical": 90}) == 90.0
    # Zero weights fall back to defaults, never divides by zero.
    assert rubric.normalize_weights({d: 0 for d in rubric.DIMENSIONS}) == {
        d: float(v) for d, v in rubric.DEFAULT_WEIGHTS.items()
    }


# ── the application tracker (instance-scoped JSON) ────────────────────────────
def test_state_roundtrip_and_dedupe(plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("CAREERCOACH_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    state = importlib.import_module(plugin.__name__ + ".state")

    state.track_application(company="Acme", role="ML Engineer", fit_score=82, status="considering")
    state.track_application(company="Globex", role="Data Scientist", fit_score=61, status="applied")
    assert len(state.load_applications()) == 2

    # Same (company, role) updates in place, not a duplicate; blank notes preserved.
    state.track_application(company="acme", role="ml engineer", fit_score=88, status="interviewing")
    rows = state.load_applications()
    assert len(rows) == 2
    acme = next(r for r in rows if r["company"].lower() == "acme")
    assert acme["fit_score"] == 88 and acme["status"] == "interviewing"

    assert len(state.load_applications(status="applied")) == 1


# ── register() — host-free (guards skip host-only knobs + subagents) ──────────
def test_register_runs_host_free(plugin, registry):
    plugin.register(registry)  # must not raise with no host present
    assert len(registry.tools) == 2  # the two tracker tools; knobs skipped host-free
    prefixes = {p for p, _ in registry.routers}
    assert "/api/plugins/careercoach" in prefixes  # gated DATA route
    assert "/plugins/careercoach" in prefixes  # public PAGE
    assert registry.subagents == []  # subagent registration skipped without the host


# ── register() — full surface with lightweight host stubs ─────────────────────
def test_full_surface_with_host_stubs(plugin, registry, monkeypatch):
    """With graph.sdk (Knobs) + graph.subagents.config (SubagentConfig) present, the guarded
    paths wire up: the rubric knobs become tools and the 3-subagent crew registers."""
    import graph.sdk  # the testkit stub module

    class _FakeKnobs:
        def __init__(self):
            self.defined = {}

        def define(self, key, value, **_):
            self.defined[key] = value
            return self

        def preset(self, *_a, **_k):
            return self

    monkeypatch.setattr(graph.sdk, "Knobs", _FakeKnobs, raising=False)
    monkeypatch.setattr(
        graph.sdk, "make_knob_tools", lambda knobs, prefix: [f"{prefix}_knobs", f"{prefix}_tune"], raising=False
    )

    # Stand in for graph.subagents.config (not in the testkit's default stubs).
    subpkg = types.ModuleType("graph.subagents")
    subpkg.__path__ = []  # type: ignore[attr-defined]
    cfgmod = types.ModuleType("graph.subagents.config")

    class _FakeSubagentConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    cfgmod.SubagentConfig = _FakeSubagentConfig
    monkeypatch.setitem(sys.modules, "graph.subagents", subpkg)
    monkeypatch.setitem(sys.modules, "graph.subagents.config", cfgmod)

    plugin.register(registry)

    # 2 tracker tools + 2 knob tools.
    assert len(registry.tools) == 4
    assert any("careercoach_knobs" == str(t) for t in registry.tools)
    # The research → evaluate → write crew.
    names = {c.name for c in registry.subagents}
    assert names == {"company_researcher", "job_evaluator", "application_writer"}
    for c in registry.subagents:
        assert "load_skill" in c.tools or c.name == "company_researcher"
