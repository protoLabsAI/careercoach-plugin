"""One rule for the career record: generated output never overwrites the operator's own file.

``Resume/Experience.md`` is the operator's — only they, or ``careercoach_write_profile`` with
their confirmation, write it. ``careercoach_export_experience`` writes its snapshot to its own
file. ``careercoach_read_profile("experience")`` returns Experience.md once the operator has
filled it in (it wins over the profile), and the live profile until then — so a filled-in
profile never sends role-packet back round the "run /setup-coach" loop.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from _plugin_testkit import FakeRegistry

ROOT = Path(__file__).resolve().parent.parent

RICH = (
    "# Experience\n\n### Staff Engineer, Acme (2019-2024)\n"
    "- Cut p99 checkout latency 840ms -> 210ms (baseline: Q3-2021 Grafana board)\n"
    "- Led 6-person payments team; owned PCI scope reduction\n"
)


def test_export_never_overwrites_the_operators_experience_file(tools, iso):
    tools["careercoach_init_workspace"].invoke({})
    tools["careercoach_write_profile"].invoke({"doc": "experience", "content": RICH})
    # setup-coach step 0 harvests Experience.md into the profile (lossily, as a summary)…
    tools["careercoach_update_profile"].invoke({"field": "name", "content": "Ada"})
    tools["careercoach_update_profile"].invoke({"field": "roles", "content": "Staff Engineer, Acme"})
    # …and step 4 exports.
    out = tools["careercoach_export_experience"].invoke({})

    assert "840ms -> 210ms" in tools["careercoach_read_profile"].invoke({"doc": "experience"})
    assert (iso / "ws" / "Resume" / "Experience.md").read_text() == RICH
    export = iso / "ws" / "Resume" / "Experience (profile export).md"
    assert str(export) in out and "Staff Engineer, Acme" in export.read_text()


def test_export_leaves_even_an_untouched_template_alone(tools, iso):
    tools["careercoach_init_workspace"].invoke({})
    template = (iso / "ws" / "Resume" / "Experience.md").read_text()
    tools["careercoach_update_profile"].invoke({"field": "name", "content": "Ada"})
    tools["careercoach_export_experience"].invoke({})
    assert (iso / "ws" / "Resume" / "Experience.md").read_text() == template


def test_read_profile_serves_the_profile_until_experience_md_is_filled_in(tools):
    read = tools["careercoach_read_profile"]
    tools["careercoach_init_workspace"].invoke({})
    # Nothing anywhere: the gate still stops drafting.
    assert "untouched template" in read.invoke({"doc": "experience"})

    tools["careercoach_update_profile"].invoke({"field": "name", "content": "Ada"})
    tools["careercoach_update_profile"].invoke({"field": "roles", "content": "Analyst, Engine Co"})
    # The operator took setup-coach's "go straight to work" exit — no export ran.
    out = read.invoke({"doc": "experience"})
    assert "untouched template" not in out and "Analyst, Engine Co" in out and "hasn't filled in" in out

    tools["careercoach_write_profile"].invoke({"doc": "experience", "content": RICH})
    assert read.invoke({"doc": "experience"}) == RICH  # their own file wins once it exists


def test_a_pre_07_export_over_experience_md_is_not_mistaken_for_the_operators_file(tools, iso):
    """v0.6 exported OVER Experience.md; the upgrade must not keep serving that stale snapshot."""
    tools["careercoach_update_profile"].invoke({"field": "roles", "content": "Current role, Now Co"})
    old = iso / "ws" / "Resume" / "Experience.md"
    old.parent.mkdir(parents=True)
    old.write_text(
        "# Experience — source of truth\n\n> Generated from the Career Coach operator profile on 2026-07-04. "
        "Edit the profile in the Career Coach console view; this file is an export.\n\n## Roles\n\nStale role\n",
        encoding="utf-8",
    )
    out = tools["careercoach_read_profile"].invoke({"doc": "experience"})
    assert "Current role, Now Co" in out and "Stale role" not in out


def test_a_dev_twin_export_does_not_clobber_the_prod_record(plugin, profile, iso, monkeypatch):
    """The dev twin shares the default ~/CareerCoach workspace with prod. Its export may replace the
    shared generated snapshot (regenerable), but never the record prod reads."""
    reg = FakeRegistry({})  # packet_root "" → ~/CareerCoach (HOME is a temp dir)
    plugin.register(reg)
    t = {x.name: x for x in reg.tools}

    profile.update_field("roles", "### Staff Engineer, Acme — PROD HISTORY")  # the default instance
    t["careercoach_export_experience"].invoke({})
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "dev")  # scripts/dev.sh
    profile.update_field("name", "Test User")
    t["careercoach_export_experience"].invoke({})
    monkeypatch.delenv("PROTOAGENT_INSTANCE")

    assert "PROD HISTORY" in t["careercoach_read_profile"].invoke({"doc": "experience"})


def test_the_export_says_where_to_change_the_record(profile):
    md = profile.to_markdown({"identity": {"name": "Ada"}, "sections": {}, "updated": "2026-09-11"})
    assert profile.is_generated_export(md)
    assert "tell" in md and "chat" in md and "read-only" in md  # the panel can't edit anything


# ── the crew that judges and writes about the candidate can read the profile ─────────
def test_drafting_and_scoring_subagents_can_read_the_profile(plugin, registry):
    """The host builds subagents without plugin middleware, so the always-on block never reaches
    them: the ones that write in the operator's name (or score them) need the read tools."""
    plugin.register(registry)
    registered = {t.name for t in registry.tools}
    crew = {c.name: c for c in registry.subagents}
    for name in ("application_writer", "job_evaluator"):
        c = crew[name]
        assert {"careercoach_get_profile", "careercoach_read_profile"} <= set(c.tools), (name, c.tools)
        assert "careercoach_read_profile('experience')" in c.system_prompt
        assert not {t for t in c.tools if t.startswith("careercoach_")} - registered
    writer = crew["application_writer"]
    assert "careercoach_get_profile('do_not_claim')" in writer.system_prompt
    assert "careercoach_update_profile" not in writer.tools and "careercoach_write_profile" not in writer.tools


def _skill(name: str) -> tuple[dict, str]:
    text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


def test_profile_skills_declare_the_profile_tools_their_steps_call():
    for name in ("job-application-assistant", "setup-coach", "role-packet"):
        fm, body = _skill(name)
        for tool_name in ("careercoach_get_profile", "careercoach_read_profile", "careercoach_update_profile"):
            if f"`{tool_name}" in body:
                assert tool_name in fm["tools"], f"{name} calls {tool_name} but doesn't declare it"


def test_setup_coach_does_not_promise_an_editable_panel():
    _, body = _skill("setup-coach")
    assert "edit it there" not in body
    assert "read-only" in body
