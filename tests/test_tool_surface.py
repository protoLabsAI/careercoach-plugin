"""S4b: the consolidated default tool surface.

The plugin registered ~31 tools. The DEFAULT set is now lean (10-12) around the core loop — find
roles → evaluate → apply kit → track → interview prep — with every pre-consolidation ("legacy") tool
name reachable behind ``extended_tools`` as a thin wrapper over the SAME implementation, and the four
browser-fill tools gated on ``browser_fill`` (S3a). These tests pin that contract; the behaviour of
each underlying tool is covered by the existing suites, reached through the extended legacy names.
"""

from __future__ import annotations

import asyncio

import pytest

from _plugin_testkit import FakeRegistry

# The rubric Knobs tools are a separate, agent-tunable host surface (graph.sdk.Knobs) — excluded from
# the default-surface count. The host-free testkit registers them as record-only placeholders.
KNOBS = {"careercoach_knobs", "careercoach_tune", "careercoach_preset"}

# The four browser-fill tools — register only when browser_fill enables at least one ATS.
BROWSER_FILL_TOOLS = {
    "careercoach_plan_fill",
    "careercoach_verify_fill",
    "careercoach_handoff",
    "careercoach_request_submit",
}
ALL_ATS = "greenhouse,lever,ashby,workable"

# The default-surface names that kept their EXACT pre-consolidation name (careercoach_digest keeps its
# name, signature and output — the jobCoach 7:30 report — exactly).
KEPT_ON_DEFAULT = frozenset(
    {
        "careercoach_save_apply_kit",
        "careercoach_watchlist",
        "careercoach_digest",
        "careercoach_log_contact",
        "careercoach_render_resume",
        "careercoach_prepare_application",
    }
)

# Every non-knob tool name registered BEFORE S4b (the pre-consolidation surface). With
# extended_tools=true — plus browser_fill for the four fill tools — every one must register again, so
# jobCoach's schedules and prompts that name the old tools keep working.
PRE_CHANGE_TOOL_NAMES = frozenset(
    {
        "careercoach_track_application",
        "careercoach_list_applications",
        "careercoach_save_apply_kit",
        "careercoach_search_jobs",
        "careercoach_watchlist",
        "careercoach_scan_boards",
        "careercoach_import_history",
        "careercoach_import_connections",
        "careercoach_find_referrals",
        "careercoach_log_contact",
        "careercoach_digest",
        "careercoach_init_workspace",
        "careercoach_get_profile",
        "careercoach_update_profile",
        "careercoach_export_experience",
        "careercoach_import_experience",
        "careercoach_read_profile",
        "careercoach_write_profile",
        "careercoach_scaffold_role",
        "careercoach_write_artifact",
        "careercoach_assemble_packet",
        "careercoach_list_roles",
        "careercoach_render_resume",
        "careercoach_get_answers",
        "careercoach_propose_answer",
        "careercoach_confirm_answers",
        "careercoach_plan_fill",
        "careercoach_verify_fill",
        "careercoach_prepare_application",
        "careercoach_request_submit",
        "careercoach_handoff",
    }
)


def _register(plugin, cfg):
    reg = FakeRegistry(cfg)
    plugin.register(reg)
    return reg


def _names(plugin, cfg):
    return {t.name for t in _register(plugin, cfg).tools}


def _tools(plugin, cfg):
    return {t.name: t for t in _register(plugin, cfg).tools}


# ── r1: the lean default surface ───────────────────────────────────────────────────────────────
def test_default_surface_is_lean_and_keeps_the_digest(plugin, iso):
    names = _names(plugin, {})
    non_knob = names - KNOBS
    assert 10 <= len(non_knob) <= 12, sorted(non_knob)
    assert "careercoach_digest" in non_knob  # the jobCoach 7:30 report, by its exact name
    # the merged façades are all present
    assert {
        "careercoach_find_roles",
        "careercoach_profile",
        "careercoach_track",
        "careercoach_answers",
        "careercoach_role_packet",
    } <= non_knob
    # no pre-consolidation legacy-only name leaks into the default surface
    legacy_only = PRE_CHANGE_TOOL_NAMES - KEPT_ON_DEFAULT
    assert not (legacy_only & non_knob), sorted(legacy_only & non_knob)


def test_default_surface_hides_the_legacy_names(plugin, iso):
    names = _names(plugin, {})
    sample = {
        "careercoach_scan_boards",
        "careercoach_search_jobs",
        "careercoach_get_profile",
        "careercoach_update_profile",
        "careercoach_track_application",
        "careercoach_get_answers",
        "careercoach_import_history",
        "careercoach_import_connections",
        "careercoach_find_referrals",
        "careercoach_scaffold_role",
    }
    assert not (sample & names), sorted(sample & names)


# ── r2: extended_tools re-registers the whole pre-change surface ─────────────────────────────────
def test_extended_tools_reregisters_every_pre_change_name(plugin, iso):
    names = _names(plugin, {"extended_tools": True, "browser_fill": ALL_ATS})
    missing = PRE_CHANGE_TOOL_NAMES - names
    assert not missing, sorted(missing)
    # the merged façades stay registered alongside the legacy names
    assert {"careercoach_find_roles", "careercoach_profile", "careercoach_track"} <= names


# ── r3: the browser-fill tools gate on browser_fill ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("browser_fill", "present"),
    [("", False), ("greenhouse", True), (ALL_ATS, True), ("workday", False)],
)
def test_browser_fill_tools_gate_on_browser_fill(plugin, iso, browser_fill, present):
    cfg = {"extended_tools": True}
    if browser_fill:
        cfg["browser_fill"] = browser_fill
    reg = _register(plugin, cfg)
    names = {t.name for t in reg.tools}
    assert (BROWSER_FILL_TOOLS <= names) is present
    if not present:
        assert not (BROWSER_FILL_TOOLS & names)
    # the submit-gate enforcement middleware is ALWAYS registered (the submit gate is unchanged)
    mw_names = {type(f(None)).__name__ for f in reg.middlewares}
    assert "_SubmitGateMiddleware" in mw_names


# ── the façades delegate to the same implementations ─────────────────────────────────────────────
def test_merged_tools_dispatch_to_the_same_implementations(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    tools = _tools(plugin, {"packet_root": str(iso / "ws"), "extended_tools": True})

    # track: an empty list via the façade == via the legacy tool (same implementation)
    assert tools["careercoach_track"].invoke({"action": "list"}) == tools["careercoach_list_applications"].invoke({})
    # track: record a role, then the façade list shows it
    tools["careercoach_track"].invoke({"action": "track", "company": "Acme", "role": "ML Engineer", "fit_score": 80})
    assert "ML Engineer @ Acme" in tools["careercoach_track"].invoke({"action": "list"})
    # track: move it — the new path via state.move
    moved = tools["careercoach_track"].invoke(
        {"action": "move", "company": "Acme", "role": "ML Engineer", "status": "applied"}
    )
    assert "to applied" in moved
    assert "applied" in tools["careercoach_track"].invoke({"action": "list", "status": "applied"})

    # profile / answers / role_packet read the same stores as their legacy names
    assert tools["careercoach_profile"].invoke({"action": "get"}) == tools["careercoach_get_profile"].invoke({})
    assert tools["careercoach_answers"].invoke({"action": "get"}) == tools["careercoach_get_answers"].invoke({})
    assert tools["careercoach_role_packet"].invoke({"action": "list"}) == tools["careercoach_list_roles"].invoke({})


def test_merged_tools_report_unknown_actions(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    tools = _tools(plugin, {"packet_root": str(iso / "ws")})
    for name in ("careercoach_track", "careercoach_profile", "careercoach_answers", "careercoach_role_packet"):
        assert "Unknown action" in tools[name].invoke({"action": "nonsense"})


def test_find_roles_routes_watched_and_market(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    fr = _tools(plugin, {"packet_root": str(iso / "ws")})["careercoach_find_roles"]
    # the no-query, unknown-source and no-target-roles paths each stay offline
    assert "search the market" in asyncio.run(fr.ainvoke({"source": "market"})).lower()
    assert "Unknown source" in asyncio.run(fr.ainvoke({"source": "nope"}))
    assert "target roles" in asyncio.run(fr.ainvoke({"source": "watched"})).lower()


# ── r5: the eval aliases cover every capability with real default-surface tools ──────────────────
def test_prompts_aliases_cover_every_capability_with_default_tools(plugin, iso):
    from evals.tool_selection import run_eval

    _, aliases = run_eval.load_prompts()
    default_names = _names(plugin, {})
    assert aliases, "aliases must not be empty"
    for capability, names in aliases.items():
        assert names, f"{capability} has an empty alias list"
        assert set(names) <= default_names, (capability, set(names) - default_names)


def test_agent_instructions_use_available_tools(plugin, iso):
    """Default instructions must work without either opt-in surface.

    Optional instructions are confined to explicit sections with availability checks;
    their names must resolve when that section's setting is enabled.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    default = _names(plugin, {})
    extended = _names(plugin, {"extended_tools": True})
    fill = _names(plugin, {"browser_fill": "greenhouse"})
    paths = [root / "SOUL.md", *root.glob("workflows/*.yaml"), *root.glob("skills/**/*.md")]
    for path in paths:
        text = path.read_text()
        optional_names = set()
        if path == root / "skills/job-hunt/SKILL.md":
            text, optional = text.split("## Optional extended tools", 1)
            assert "extended_tools: true" in optional
            assert "available tool list before calling" in optional
            optional_names = set(re.findall(r"\bcareercoach_\w+", optional))
            assert optional_names <= extended
            assert {
                "careercoach_import_history",
                "careercoach_import_connections",
                "careercoach_find_referrals",
            } <= optional_names
            assert 'careercoach_track(action="track"' in text
            assert "ask who the operator knows" in text
        elif path == root / "skills/apply-form/SKILL.md":
            text, optional = text.split("## Browser fill (opt-in beta)", 1)
            assert "browser_fill" in optional
            assert "available tool list before calling" in optional
            optional_names = set(re.findall(r"\bcareercoach_\w+", optional))
            assert optional_names <= fill
        names = set(re.findall(r"\bcareercoach_\w+", text))
        assert names <= default, (path.relative_to(root), names - default)


def test_truncated_hard_stops_point_to_default_profile_tool(plugin, profile, iso):
    import ast
    import re

    stored = profile.empty_profile()
    # Simulate an older profile whose hard stops exceed today's injection cap.
    stored["sections"]["do_not_claim"] = "Never claim invented credentials.\n" * 2000
    profile.save_profile(stored)
    block = profile.context_block()
    call = re.search(r"careercoach_\w+\([^)]*\)", block)
    assert call, block
    expression = ast.parse(call.group(), mode="eval").body
    tool = _tools(plugin, {})[expression.func.id]
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in expression.keywords}
    assert kwargs == {"action": "get", "field": "do_not_claim"}
    assert stored["sections"]["do_not_claim"].strip() in tool.invoke(kwargs)
