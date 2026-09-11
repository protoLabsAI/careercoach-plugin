"""One rule for the career record: the operator profile is the single source of truth.

Everything that reads the operator's history — ``careercoach_read_profile("experience")``, the
drafting and scoring crew, the skills — reads the profile. A workspace ``Resume/Experience.md`` is
never a competing read source: it flows INTO the profile through an explicit, previewed import
(``careercoach_import_experience``) and is never written by the coach; the profile flows OUT to
``Resume/Experience (profile export).md``. The three ``test_p_precedence`` reproducers from the
second review are ported here to that rule.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from _plugin_testkit import FakeRegistry

ROOT = Path(__file__).resolve().parent.parent

RICH = (
    "# Experience\n\n## Roles\n\n### Staff Engineer, Acme (2019-2024)\n"
    "- Cut p99 checkout latency 840ms -> 210ms (baseline: Q3-2021 Grafana board)\n"
    "- Led 6-person payments team; owned PCI scope reduction\n"
)


def _seed_profile(profile):
    for k, v in {
        "name": "Ada Lovelace",
        "location": "London",
        "roles": "### Analyst, Engine Co 1842-1843\n- Wrote Note G",
        "skills": "Expert: symbolic computation",
        "do_not_claim": "- No PhD",
    }.items():
        profile.update_field(k, v)


def _experience(iso) -> Path:
    return iso / "ws" / "Resume" / "Experience.md"


# ── reading: always the profile ───────────────────────────────────────────────────────
def test_read_profile_serves_the_profile_never_experience_md(tools, profile, iso):
    tools["careercoach_init_workspace"].invoke({})
    _seed_profile(profile)
    _experience(iso).write_text(RICH, encoding="utf-8")  # an operator-filled file…

    out = tools["careercoach_read_profile"].invoke({"doc": "experience"})

    assert "Wrote Note G" in out and "No PhD" in out  # …doesn't compete with the profile
    assert "840ms" not in out


def test_a_partly_filled_template_never_reaches_a_draft(tools, profile, iso):
    """Review 2, test_one_keystroke…: filling only the Name line used to flip precedence to the
    template, example bullets and all."""
    tools["careercoach_init_workspace"].invoke({})
    _seed_profile(profile)
    exp = _experience(iso)
    exp.write_text(exp.read_text().replace("- **Name:**", "- **Name:** Ada Lovelace", 1), encoding="utf-8")

    out = tools["careercoach_read_profile"].invoke({"doc": "experience"})
    assert "Wrote Note G" in out and "grew ARR" not in out
    # And importing that file brings in exactly what the operator typed — never the hint text.
    preview = tools["careercoach_import_experience"].invoke({})
    assert "name: unchanged" in preview and "grew ARR" not in preview and "roles" not in preview


def test_a_profile_update_reaches_the_drafting_read_path(tools, profile, iso):
    """Review 2, test_profile_updates_reach…: once Experience.md 'won', a role recorded later in
    chat never reached a CV."""
    tools["careercoach_init_workspace"].invoke({})
    _seed_profile(profile)
    _experience(iso).write_text(RICH, encoding="utf-8")
    tools["careercoach_import_experience"].invoke({"apply": True})

    profile.update_field("roles", "### Staff Engineer, Initech 2024-now")  # a new job, recorded in chat

    out = tools["careercoach_read_profile"].invoke({"doc": "experience"})
    assert "Staff Engineer, Initech" in out and "840ms -> 210ms" in out  # the import landed in the profile too


def test_the_documented_write_back_is_a_profile_update_not_an_ignored_file(tools, profile, iso):
    """Review 2, test_confirmed_write_back…: 'read it, merge, write it back' wrote Experience.md,
    which was then never read. Now the coach doesn't write that file at all — it says where the
    fact goes — and the fact written there reaches every later read."""
    tools["careercoach_init_workspace"].invoke({})
    _seed_profile(profile)
    before = _experience(iso).read_text()
    out = tools["careercoach_read_profile"].invoke({"doc": "experience"})
    merged = out.split("\n\n", 1)[1].replace("- Wrote Note G", "- Wrote Note G\n- CONFIRMED: taught (1843)")

    ack = tools["careercoach_write_profile"].invoke({"doc": "experience", "content": merged})
    assert ack.startswith("Not written") and "careercoach_update_profile" in ack
    assert _experience(iso).read_text() == before

    roles = tools["careercoach_get_profile"].invoke({"field": "roles"})
    tools["careercoach_update_profile"].invoke(
        {"field": "roles", "content": roles + "\n- CONFIRMED: taught (1843)", "mode": "replace"}
    )
    assert "CONFIRMED: taught" in tools["careercoach_read_profile"].invoke({"doc": "experience"})


def test_an_empty_profile_is_never_papered_over_with_the_file(tools, iso):
    tools["careercoach_init_workspace"].invoke({})
    _experience(iso).write_text(RICH, encoding="utf-8")
    out = tools["careercoach_read_profile"].invoke({"doc": "experience"})
    assert "No operator profile recorded yet" in out and "do NOT" in out
    assert "840ms" not in out and "careercoach_import_experience" in out  # it points at the import instead


# ── the import: Experience.md → profile, previewed, under the profile's own rules ─────
TEMPLATE_FILLED = """# Experience — source of truth

> An old banner.

## Identity
- **Name:** Grace Hopper
- **Location / work authorization:** Arlington, VA
- **Contact:** grace@navy.example
- **Headline(s):** 2-3 role-type framings you'd accept (e.g. "Technical PM", "ML Eng")

## Roles (most recent first)

### Rear Admiral — US Navy · 1983–1986
- **Scope:** Navy-wide data automation.
- **Tools / methods:** the stack, frameworks, and practices you actually used hands-on.

_(Duplicate the block above per role. Older roles can be terser.)_

## Education
- **PhD, Mathematics** — Yale, 1934.

## Certifications & credentials
- <Cert> — <issuer>, <year>. (Link if verifiable.)

## Skills inventory (be honest about level)
- **Expert / can lead on:** COBOL, compilers
- **Proficient / independent:**
- **Explicitly do NOT claim:** Never claim hardware design.

## Publications
- A Manual of Operation for the Automatic Sequence Controlled Calculator (1946)
"""


def test_import_previews_then_applies_only_what_the_operator_wrote(tools, profile, iso):
    tools["careercoach_init_workspace"].invoke({})
    _experience(iso).write_text(TEMPLATE_FILLED, encoding="utf-8")

    preview = tools["careercoach_import_experience"].invoke({})
    assert preview.startswith("Import preview — nothing written yet") and "apply=true" in preview
    assert profile.completeness()["empty"], "a preview must not write"
    assert "A Manual of Operation" in preview  # an unmapped section is raised, not guessed into a field

    applied = tools["careercoach_import_experience"].invoke({"apply": True})
    assert applied.startswith("Imported")
    prof = profile.load_profile()
    assert prof["identity"]["name"] == "Grace Hopper"
    assert prof["identity"]["location"] == "Arlington, VA"
    assert prof["identity"]["contact"] == "grace@navy.example"
    assert prof["identity"]["headlines"] == ""  # still the template's hint text: not the operator's
    assert "### Rear Admiral — US Navy" in prof["sections"]["roles"] and "Navy-wide" in prof["sections"]["roles"]
    assert "actually used hands-on" not in prof["sections"]["roles"]  # template hint lines dropped
    assert prof["sections"]["education"] == "- **PhD, Mathematics** — Yale, 1934."
    assert "COBOL" in prof["sections"]["skills"] and "do NOT" not in prof["sections"]["skills"]
    assert prof["sections"]["do_not_claim"] == "Never claim hardware design."  # the template's DNC slot


def test_import_obeys_append_replace_and_the_hard_stops(tools, profile, iso):
    tools["careercoach_init_workspace"].invoke({})
    _seed_profile(profile)
    exp = _experience(iso)
    exp.write_text(
        "## Identity\n- **Name:** Ada King\n\n## Roles\n### Analyst, Engine Co 1842-1843\n- Wrote Note G, corrected\n\n"
        "## Lines never to claim\n- No Nobel\n",
        encoding="utf-8",
    )

    # append (default): the differing name is kept, roles and hard stops only grow.
    tools["careercoach_import_experience"].invoke({"apply": True})
    prof = profile.load_profile()
    assert prof["identity"]["name"] == "Ada Lovelace"
    assert "- Wrote Note G\n" in prof["sections"]["roles"] + "\n" and "corrected" in prof["sections"]["roles"]
    assert prof["sections"]["do_not_claim"] == "- No PhD\n\n- No Nobel"

    # replace: sections match the file — except a hard stop still needs the operator's say-so.
    out = tools["careercoach_import_experience"].invoke({"apply": True, "mode": "replace"})
    prof = profile.load_profile()
    assert prof["identity"]["name"] == "Ada King"
    assert prof["sections"]["roles"] == "### Analyst, Engine Co 1842-1843\n- Wrote Note G, corrected"
    assert "do_not_claim: refused" in out and "No PhD" in prof["sections"]["do_not_claim"]
    tools["careercoach_import_experience"].invoke({"apply": True, "mode": "replace", "confirm_removal": True})
    assert profile.load_profile()["sections"]["do_not_claim"] == "- No Nobel"


def test_importing_the_export_back_changes_nothing(tools, profile, iso):
    """A copy of the export dropped into Experience.md is recognised by content, not by banner:
    everything in it is already in the profile."""
    _seed_profile(profile)
    tools["careercoach_export_experience"].invoke({})
    export = iso / "ws" / "Resume" / "Experience (profile export).md"
    shutil.copyfile(export, _experience(iso))
    before = profile._path().read_bytes()

    out = tools["careercoach_import_experience"].invoke({"apply": True})

    assert "Nothing new to import" in out and profile._path().read_bytes() == before


def test_import_with_nothing_to_import(tools, iso):
    assert "nothing to import" in tools["careercoach_import_experience"].invoke({})
    tools["careercoach_init_workspace"].invoke({})
    assert "untouched template" in tools["careercoach_import_experience"].invoke({})


# ── the export: profile → its own file, never over Experience.md ──────────────────────
def test_export_never_overwrites_the_operators_experience_file(tools, profile, iso):
    tools["careercoach_init_workspace"].invoke({})
    _experience(iso).write_text(RICH, encoding="utf-8")
    _seed_profile(profile)
    out = tools["careercoach_export_experience"].invoke({})

    assert _experience(iso).read_text() == RICH
    export = iso / "ws" / "Resume" / "Experience (profile export).md"
    assert str(export) in out and "Wrote Note G" in export.read_text()


def test_the_export_says_where_to_change_the_record(profile):
    md = profile.to_markdown({"identity": {"name": "Ada"}, "sections": {}, "updated": "2026-09-11"})
    assert md.startswith("# Experience (profile export)")
    assert "tell" in md and "chat" in md and "read-only" in md  # the panel can't edit anything


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


# ── the crew and the skills read the same record ──────────────────────────────────────
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
    assert not {"careercoach_update_profile", "careercoach_write_profile", "careercoach_import_experience"} & set(
        writer.tools
    )


def _skill(name: str) -> tuple[dict, str]:
    text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


PROFILE_TOOLS = (
    "careercoach_get_profile",
    "careercoach_read_profile",
    "careercoach_update_profile",
    "careercoach_import_experience",
)


def test_profile_skills_declare_the_profile_tools_their_steps_call():
    for name in ("job-application-assistant", "setup-coach", "role-packet"):
        fm, body = _skill(name)
        for tool_name in PROFILE_TOOLS:
            if f"`{tool_name}" in body:
                assert tool_name in fm["tools"], f"{name} calls {tool_name} but doesn't declare it"


def test_nothing_still_names_experience_md_as_a_source():
    """The one rule, stated everywhere: no skill, guide, template or persona tells the agent to
    draft from Experience.md."""
    paths = [ROOT / "SOUL.md", *ROOT.glob("skills/**/*.md"), *ROOT.glob("templates/**/*.md")]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for stale in (
            "source of truth — from",
            "Experience.md` (the source of truth)",
            "traces to a\n   verified entry in `Resume/Experience.md`",
            "wins over the profile",
        ):
            assert stale not in text, f"{path.relative_to(ROOT)} still says {stale!r}"


def test_setup_coach_does_not_promise_an_editable_panel():
    _, body = _skill("setup-coach")
    assert "edit it there" not in body
    assert "read-only" in body
