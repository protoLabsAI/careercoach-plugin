"""Tests for Career Coach — host-free (no protoAgent running).

Two layers:
- pure modules (``rubric``, ``state``) tested directly;
- ``register()`` tested both host-free (guards skip the host-only knobs/subagents) and with
  lightweight host stubs monkeypatched in (the full surface wires up).
"""

from __future__ import annotations

import importlib
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
    assert {s["key"] for s in m["settings"]} >= {
        "full_name",
        "location",
        "target_roles",
        "render_format",
        "packet_root",
    }
    assert m["views"][0]["path"] == "/plugins/careercoach/view"


# ── the dashboard view honors the DS theme contract ──────────────────────────
def test_dashboard_honors_theme_contract(plugin):
    """The rail view must load the DS plugin-kit JS (not just its CSS) so the kit owns the
    protoagent:init handshake and applies the console's live theme (data-theme on :root). A
    CSS-only page with a hand-rolled, token-only listener renders dark in every theme — the
    regression this guards. It must also not reference --pl-color-surface* tokens the kit
    doesn't define (they'd fall back to hardcoded dark even once the theme is applied)."""
    html = plugin._DASHBOARD_HTML
    assert "/_ds/plugin-kit.css" in html  # tokens
    assert "/_ds/plugin-kit.js" in html  # handshake + live theme (the fix)
    assert "initPluginView" in html  # kit drives the handshake, not a hand-rolled message listener
    assert "--pl-color-surface" not in html  # undefined token → hardcoded-dark fallback


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


# ── the role-packet workspace (folder-per-role artifact tree) ─────────────────
def test_packet_paths_and_scaffold(plugin, tmp_path):
    packet = importlib.import_module(plugin.__name__ + ".packet")

    # safe_name preserves human-readable names, strips only path-hostile chars.
    assert packet.safe_name("Associate Director, Technical PM") == "Associate Director, Technical PM"
    assert "/" not in packet.safe_name("Data/ML: Eng")
    assert packet.role_dirname("Staff Engineer", "R407969") == "Staff Engineer - R407969"

    p = packet.role_path(tmp_path, "Merck", "AD Technical PM", "R407969")
    assert p == tmp_path / "Companies" / "Merck" / "Roles" / "AD Technical PM - R407969"

    out = packet.scaffold_role(tmp_path, "Merck", "AD Technical PM", "R407969", raw_jd="Build the platform.")
    assert out["existed"] is False
    folder = tmp_path / "Companies" / "Merck" / "Roles" / "AD Technical PM - R407969"
    assert (folder / "job description (raw).md").read_text().strip() == "Build the platform."
    assert (folder / "process_log.md").exists()

    # Idempotent: a second scaffold reuses the folder and never clobbers the raw JD.
    again = packet.scaffold_role(tmp_path, "Merck", "AD Technical PM", "R407969", raw_jd="DIFFERENT")
    assert again["existed"] is True
    assert (folder / "job description (raw).md").read_text().strip() == "Build the platform."


def test_packet_write_assemble_and_status(plugin, tmp_path):
    packet = importlib.import_module(plugin.__name__ + ".packet")
    packet.scaffold_role(tmp_path, "Acme", "ML Engineer")

    packet.write_artifact(tmp_path, "Acme", "ML Engineer", "", "evidence-map", "## Map\nreq → proof")
    res = packet.write_artifact(tmp_path, "Acme", "ML Engineer", "", "tailored-resume", "# CV")
    folder = tmp_path / "Companies" / "Acme" / "Roles" / "ML Engineer"
    assert (folder / "role evidence map.md").exists()
    assert res["replaced"] is False
    # The process log records each write.
    assert "role evidence map.md" in (folder / "process_log.md").read_text()

    # An unknown artifact slug is rejected, not silently written.
    try:
        packet.write_artifact(tmp_path, "Acme", "ML Engineer", "", "bogus", "x")
        raise AssertionError("expected KeyError for unknown artifact")
    except KeyError:
        pass

    status = packet.role_status(tmp_path, "Acme", "ML Engineer")
    assert "evidence-map" in status["present"] and "cover-letter" in status["missing"]

    asm = packet.assemble_packet(tmp_path, "Acme", "ML Engineer")
    body = (folder / "role packet.md").read_text()
    assert "evidence-map" in asm["present"] and "cover-letter" in asm["missing"]
    assert "req → proof" in body  # present body artifact is concatenated in
    assert "cover letter.md" in body  # missing artifacts listed under "Not yet produced"
    assert (folder / "orchestration log.md").exists()

    roles = packet.list_roles(tmp_path)
    assert roles and roles[0]["company"] == "Acme" and roles[0]["artifacts"] >= 4


def test_packet_init_workspace_never_clobbers(plugin, tmp_path):
    packet = importlib.import_module(plugin.__name__ + ".packet")
    templates = ROOT / "templates"

    first = packet.init_workspace(tmp_path, templates)
    assert first["created"], "templates should be copied into a fresh workspace"
    assert any(rel.endswith("SKILL.md") or "Experience" in rel for rel in first["created"])

    # A candidate edit must survive a re-init.
    exp = next(tmp_path.rglob("Experience.md"), None)
    assert exp is not None
    exp.write_text("MY REAL EXPERIENCE", encoding="utf-8")
    second = packet.init_workspace(tmp_path, templates)
    assert not second["created"] and second["skipped"]  # nothing new, all skipped
    assert exp.read_text() == "MY REAL EXPERIENCE"


def test_packet_source_read_write_and_guards(plugin, tmp_path):
    """The source-of-truth seam: the coach can reach Experience.md without a managed fs project,
    can tell a seeded-but-untouched template from a filled-in one, and cannot rewrite the
    discipline files that bind it."""
    packet = importlib.import_module(plugin.__name__ + ".packet")
    templates = ROOT / "templates"

    # Unseeded workspace — reported missing, not silently empty.
    missing = packet.read_source(tmp_path, "experience", templates)
    assert missing["exists"] is False and missing["text"] == ""

    packet.init_workspace(tmp_path, templates)

    # Seeded but untouched: the file exists and has plenty of text, but is NOT edited — this is
    # the distinction that stops the coach drafting a career out of template hint text.
    seeded = packet.read_source(tmp_path, "experience", templates)
    assert seeded["exists"] is True
    assert seeded["edited"] is False
    assert "source of truth" in seeded["text"]

    res = packet.write_source(tmp_path, "experience", "# Experience\n\n- Staff Eng @ Acme, 6y")
    assert res["replaced"] is True  # the template counts as content being replaced
    filled = packet.read_source(tmp_path, "experience", templates)
    assert filled["edited"] is True and "Acme" in filled["text"]

    # Story bank is writable too; the discipline files are not.
    assert packet.write_source(tmp_path, "story-bank", "## Story: migration")["doc"] == "story-bank"
    for readonly in ("reviewer", "humanize", "improvements"):
        try:
            packet.write_source(tmp_path, readonly, "rewriting my own guardrails")
            raise AssertionError(f"expected PermissionError writing {readonly}")
        except PermissionError:
            pass
        assert packet.read_source(tmp_path, readonly, templates)["exists"] is True  # still readable

    # An unknown slug is rejected on both paths, not silently created.
    for call in (lambda: packet.read_source(tmp_path, "nope"), lambda: packet.write_source(tmp_path, "nope", "x")):
        try:
            call()
            raise AssertionError("expected KeyError for unknown source")
        except KeyError:
            pass

    # Without a templates_dir there's nothing to compare against — an existing file reads as edited.
    assert packet.read_source(tmp_path, "reviewer")["edited"] is True


def test_profile_store_completeness_and_context(plugin, monkeypatch, tmp_path):
    """The operator profile: field-at-a-time writes, an honest completeness picture, and a
    context block that names what's missing — the thing that stops the agent re-interviewing."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    profile = importlib.import_module(plugin.__name__ + ".profile")

    # Cold start: well-formed but empty, and it injects NOTHING (an empty block is noise).
    cov = profile.completeness()
    assert cov["empty"] is True and cov["filled"] == 0
    assert set(cov["missing"]) == set(profile.IDENTITY_FIELDS) | set(profile.SECTIONS)
    assert profile.context_block() == ""

    profile.update_field("name", "Ada Lovelace")
    profile.update_field("location", "London")
    saved = profile.update_field("roles", "### Analyst — Analytical Engine\n- Owned the notes")

    # Field-at-a-time must not disturb its neighbours — a long interview saves as it goes.
    assert saved["identity"]["name"] == "Ada Lovelace"
    assert saved["identity"]["location"] == "London"
    assert "Analytical Engine" in saved["sections"]["roles"]

    cov = profile.completeness()
    assert cov["filled"] == 3 and cov["empty"] is False
    assert set(cov["known"]) == {"name", "location", "roles"}
    assert "work_auth" in cov["missing"] and "do_not_claim" in cov["missing"]

    block = profile.context_block()
    assert "Ada Lovelace" in block and "London" in block  # short facts inject verbatim…
    assert "Owned the notes" not in block  # …long sections do NOT (always-on context stays cheap)
    assert "recorded (" in block  # they appear as presence + size instead
    assert "MISSING" in block and "work_auth" in block
    assert "NEVER ask for anything listed under KNOWN" in block
    assert f"{cov['filled']} of {cov['total']}" in block

    # do_not_claim is a guardrail: short, load-bearing, and injected IN FULL every turn.
    profile.update_field("do_not_claim", "Never imply production ML ownership.")
    block = profile.context_block()
    assert "Never imply production ML ownership." in block

    # The write gate rides along on EVERY turn, so a side-door request ("just make me a docx")
    # can't reach a deliverable without the voice discipline having been loaded first.
    assert "BEFORE WRITING ANYTHING IN THEIR NAME" in block
    assert "writing-style" in block and "my-writing-style" in block
    assert "side door does not suspend the rules" in block

    # Unknown fields are rejected rather than silently creating a slot.
    try:
        profile.update_field("favourite_colour", "green")
        raise AssertionError("expected KeyError for unknown profile field")
    except KeyError:
        pass

    # A corrupt store never raises mid-turn: readers see nothing from it — but the block SAYS it's
    # unreadable instead of reading as a fresh start, so the agent neither re-interviews nor drafts
    # without the hard stops. (The write-side refusal is in test_profile_store.py.)
    (tmp_path / "profile.json").write_text("{not json", encoding="utf-8")
    assert profile.completeness()["empty"] is True
    block = profile.context_block()
    assert "unreadable" in block and "DO NOT CLAIM" in block and "Ada Lovelace" not in block

    # The markdown export is generated FROM the profile (one write direction, no drift).
    md = profile.to_markdown({"identity": {"name": "Ada"}, "sections": {"roles": "R"}, "updated": ""})
    assert md.startswith("# Experience") and "Ada" in md and "_(not recorded)_" in md


def test_profile_concurrent_writes_lose_nothing(plugin, monkeypatch, tmp_path):
    """Parallel ``update_field`` calls must not drop each other's edits or tear the file.

    Not theoretical: models emit tool calls in parallel, and a real run recorded five fields but
    persisted two, leaving valid JSON with the tail of a longer write spliced onto the end. The
    store is load-edit-save, so without a lock the last writer wins."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    profile = importlib.import_module(plugin.__name__ + ".profile")

    import json as _json
    import threading

    # Long values make an interleaved write far likelier to strand a tail.
    fields = {
        "name": "Ada Lovelace",
        "location": "London",
        "work_auth": "UK citizen",
        "contact": "ada@example.com · linkedin.com/in/ada",
        "headlines": "Analyst · Mathematician · " + ("Engine specialist " * 40),
        "roles": "### Analyst — Analytical Engine\n" + ("- Owned the notes\n" * 120),
        "skills": "**Can lead on:** " + ("symbolic computation, " * 60),
        "do_not_claim": "Never imply hands-on manufacture of the Engine.",
    }
    barrier = threading.Barrier(len(fields))

    def write(k, v):
        barrier.wait()  # maximize overlap
        profile.update_field(k, v)

    threads = [threading.Thread(target=write, args=kv) for kv in fields.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The file must still parse — no tail of a longer write left behind.
    text = (tmp_path / "profile.json").read_text(encoding="utf-8")
    _json.loads(text)  # raises if torn

    # And every field must have survived, not just the last writer's.
    prof = profile.load_profile()
    for k, v in fields.items():
        got = prof["identity"].get(k) or prof["sections"].get(k)
        assert got == v.strip(), f"{k} was lost to a concurrent write"
    assert profile.completeness(prof)["filled"] == len(fields)


def test_skills_declare_real_tools(plugin, registry):
    """Every ``careercoach_*`` tool a skill lists in its frontmatter must actually be registered.

    Guards the failure mode this seam was built to fix: a skill instructing the agent to reach for
    something the plugin never exposes, which fails silently at runtime as "the agent just didn't
    do it". Also asserts each skill carries the frontmatter the host indexes on."""
    plugin.register(registry)
    registered = {getattr(t, "name", str(t)) for t in registry.tools}

    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert len(skills) >= 5, "skills should be discovered from skills/*/SKILL.md"

    for path in skills:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{path.name} needs YAML frontmatter"
        fm = yaml.safe_load(text.split("---\n", 2)[1])
        assert fm.get("name") and fm.get("description"), f"{path.parent.name} needs name + description"
        for name in fm.get("tools") or []:
            if name.startswith("careercoach_"):
                assert name in registered, f"{path.parent.name} declares unknown tool {name!r}"

    # The onboarding skill is the entry point a fresh archetype lands on — it must be user-facing.
    setup = yaml.safe_load((ROOT / "skills/setup-coach/SKILL.md").read_text().split("---\n", 2)[1])
    assert setup["user_facing"] is True and setup["slash"] == "setup-coach"


# ── register() — host-free (the testkit's record-only stand-ins let every guarded path run) ──
def test_register_runs_host_free(plugin, registry):
    plugin.register(registry)  # must not raise with no host present
    names = [t.name for t in registry.tools]
    # 3 tracker/search + 10 packet/profile tools + the 3 rubric-knob tools (the vendored testkit
    # stands in for graph.sdk's Knobs/make_knob_tools, so the guarded knob path runs host-free).
    assert len(names) == 16 and len(set(names)) == 16
    assert {"careercoach_knobs", "careercoach_tune", "careercoach_preset"} <= set(names)
    prefixes = {p for p, _ in registry.routers}
    assert "/api/plugins/careercoach" in prefixes  # gated DATA route
    assert "/plugins/careercoach" in prefixes  # public PAGE
    # The research → evaluate → write crew (graph.subagents.config is stood in by the testkit too).
    assert {c.name for c in registry.subagents} == {"company_researcher", "job_evaluator", "application_writer"}
    assert len(registry.middlewares) == 1  # the operator-profile frame (langchain is a dev dep)
    assert "careercoach:new_matches" in registry.verifiers  # goal verifier wired (VerifyResult is stubbed)
    assert "careercoach-watch" not in registry.surfaces  # auto-scan off by default


# ── register() — the rubric knobs, with the host seam recorded ─────────────────
def test_full_surface_with_host_stubs(plugin, registry, monkeypatch):
    """With graph.sdk's Knobs patched to a recording fake, the guarded knob path defines one knob
    per rubric weight at its default (plus every preset), and the crew can pull the full skill."""
    import graph.sdk  # the testkit stub module

    rubric = importlib.import_module(plugin.__name__ + ".rubric")
    made = {}

    class _RecordingKnobs:
        def __init__(self):
            self.defined, self.presets = {}, {}
            made["knobs"] = self

        def define(self, key, value, **_):
            self.defined[key] = value
            return self

        def preset(self, name, overrides, **_):
            self.presets[name] = overrides
            return self

    monkeypatch.setattr(graph.sdk, "Knobs", _RecordingKnobs, raising=False)
    monkeypatch.setattr(
        graph.sdk,
        "make_knob_tools",
        lambda knobs, prefix: [types.SimpleNamespace(name=f"{prefix}_tune")],
        raising=False,
    )

    plugin.register(registry)

    assert made["knobs"].defined == {f"weight_{d}": v for d, v in rubric.DEFAULT_WEIGHTS.items()}
    assert set(made["knobs"].presets) == set(rubric.PRESETS)
    assert "careercoach_tune" in {t.name for t in registry.tools}
    for c in registry.subagents:
        assert "load_skill" in c.tools or c.name == "company_researcher"


# ── Phase 3: live job source (pure parsers + selection + prescore) ────────────
def test_jobsource_parsers(plugin):
    js = importlib.import_module(plugin.__name__ + ".jobsource")
    jsearch = {
        "data": [
            {
                "job_title": "ML Engineer",
                "employer_name": "Acme",
                "job_city": "Berlin",
                "job_country": "DE",
                "job_apply_link": "https://a/1",
                "job_description": "Build models",
            }
        ]
    }
    r = js.parse_jsearch(jsearch)
    assert r[0]["title"] == "ML Engineer" and r[0]["company"] == "Acme"
    assert "Berlin" in r[0]["location"] and r[0]["source"] == "jsearch"

    remotive = {
        "jobs": [
            {
                "title": "Data Scientist",
                "company_name": "Globex",
                "candidate_required_location": "Remote",
                "url": "https://g/2",
                "description": "<p>Do <b>science</b></p>",
            }
        ]
    }
    r2 = js.parse_remotive(remotive)
    assert r2[0]["title"] == "Data Scientist" and r2[0]["source"] == "remotive"
    assert "<" not in r2[0]["snippet"]  # HTML stripped


def test_provider_selection_and_prescore(plugin):
    js = importlib.import_module(plugin.__name__ + ".jobsource")
    assert js.choose_provider("auto", "") == "remotive"
    assert js.choose_provider("auto", "KEY") == "jsearch"
    assert js.choose_provider("jsearch", "") == "jsearch"

    hi = js.prescore({"title": "Senior ML Engineer", "snippet": "ml pipelines"}, "ML engineer")
    lo = js.prescore({"title": "Barista", "snippet": "espresso"}, "ML engineer")
    assert 0 <= lo < hi <= 100
    assert js.prescore({"title": "anything"}, "") == 0  # no target terms → 0


# ── Phase 3: the watch matcher + wiring ───────────────────────────────────────
def test_find_new_matches(plugin):
    watch = importlib.import_module(plugin.__name__ + ".watch")
    jobs = [
        {"title": "ML Engineer", "company": "Acme", "url": "u1", "snippet": "ml"},
        {"title": "ML Engineer", "company": "Globex", "url": "u2", "snippet": "ml"},
        {"title": "Chef", "company": "Foods", "url": "u3", "snippet": "cooking"},
    ]
    seen = {("acme", "ml engineer")}  # Acme already tracked
    out = watch.find_new_matches(jobs, "ML engineer", seen, 50)
    assert [m["company"] for m in out] == ["Globex"]  # Acme seen, Chef below threshold
    assert out[0]["score"] >= 50


def test_watch_surface_and_verifier_when_enabled(plugin):
    from _plugin_testkit import FakeRegistry

    reg = FakeRegistry(config={"watch_enabled": True, "target_roles": "ML engineer"})
    plugin.register(reg)
    assert "careercoach-watch" in reg.surfaces  # auto-scan surface registered when enabled
    assert "careercoach:new_matches" in reg.verifiers  # WATCH/monitor verifier available either way
