"""Agent-facing apply defaults to a saved kit; browser automation stays opt-in."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BETA_HEADING = "## Browser fill (opt-in beta)"
TOOL_TOKEN = re.compile(r"\b(?:careercoach|browser)_[a-z_]+\b")


def _skill():
    text = (ROOT / "skills/apply-form/SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---\n", 2)
    default, beta = body.split(BETA_HEADING, 1)
    return yaml.safe_load(frontmatter), default, beta


def test_default_builds_saves_and_hands_off_without_browser_form_tools():
    metadata, default, _ = _skill()
    assert "apply kit" in metadata["description"]
    for tool in ("careercoach_render_resume", "browser_pdf", "careercoach_get_answers", "careercoach_save_apply_kit"):
        assert tool in metadata["tools"]
        assert tool in default
    assert default.index("## 1. Evaluate") < default.index("## 2. Build")
    assert default.index("## 2. Build") < default.index("## 3. Save") < default.index("## 4. Hand off")
    for tool in ("browser_fill", "browser_click", "browser_form_read", "browser_upload", "browser_select"):
        assert tool not in default
    assert "Career Coach view" in default
    assert "copy buttons" in default
    assert "Mark applied" in default
    assert "cover letter ONLY if the posting asks for one" in default
    assert "drafts stay drafts" in default
    assert "Ask the operator" in default
    assert "Nothing auto-submits" in default


def test_beta_requires_opt_in_and_preserves_submit_and_captcha_safeguards():
    _, _, beta = _skill()
    flat = " ".join(beta.split())
    assert "ONLY when the posting's ATS is explicitly enabled in the `browser_fill`" in flat
    assert "Blank means off" in flat
    assert "Never submit without the approval card" in beta
    assert "Never solve or bypass a captcha" in beta
    for tool in (
        "careercoach_plan_fill",
        "careercoach_verify_fill",
        "careercoach_handoff",
        "careercoach_request_submit",
    ):
        assert tool in beta
    assert "go back to step 7" in beta
    assert "exactly ONE submit click for 120 seconds" in beta
    assert "refuses on a headless" in beta


def test_workflow_final_step_saves_apply_kit_and_hands_off():
    workflow = yaml.safe_load((ROOT / "workflows/apply.yaml").read_text(encoding="utf-8"))
    step = workflow["steps"][-1]
    prompt = step["prompt"]
    assert step["depends_on"] == ["evaluate", "research"]
    for phrase in (
        "apply kit",
        "careercoach_render_resume",
        "browser_pdf",
        "careercoach_get_answers",
        "careercoach_save_apply_kit",
        "cover letter ONLY if the posting asks for one",
        "keep drafts as drafts",
        "new or missing questions",
        "Career Coach view",
        "copy buttons",
        "Mark applied",
        "Nothing auto-submits",
        "{{inputs.posting}}",
    ):
        assert phrase in prompt
    for tool in ("browser_fill", "browser_click", "careercoach_plan_fill", "careercoach_request_submit"):
        assert tool not in prompt
    assert "Apply kit" in workflow["output"]
    assert "Mark applied" in workflow["output"]


def test_application_writer_owns_its_autonomous_contract_and_no_write_or_submit_tool(plugin):
    """The apply workflow runs application_writer with NO operator in the loop. Its contract is its
    own system prompt plus the workflow's write step — not the full interactive skill flow, whose
    operator-gated writes (careercoach_update_profile, careercoach_confirm_answers) belong to a live
    chat where the operator approves each one. Skill frontmatter does not grant tools, so the
    delegate must own every tool it is told to call — and must own none that writes the record,
    promotes an answer in the operator's name, or drives/submits a form."""
    # The writer is granted FINE-GRAINED read/draft tools by their legacy names (careercoach_get_profile
    # / read_profile / get_answers / track_application) — deliberately NOT the merged façades, which
    # would also expose write/confirm actions this autonomous crew must never hold. Those names live on
    # the extended surface (S4b), so register with extended_tools to resolve the grant.
    from _plugin_testkit import FakeRegistry

    reg = FakeRegistry({"extended_tools": True})
    plugin.register(reg)
    writer = next(c for c in reg.subagents if c.name == "application_writer")
    registered = {t.name for t in reg.tools}

    workflow = yaml.safe_load((ROOT / "workflows/apply.yaml").read_text(encoding="utf-8"))
    write_step = workflow["steps"][-1]["prompt"]
    named = set(TOOL_TOKEN.findall(write_step + writer.system_prompt))
    assert {
        "careercoach_render_resume",
        "browser_open",
        "browser_pdf",
        "careercoach_get_answers",
        "careercoach_save_apply_kit",
        "careercoach_prepare_application",
    } <= named  # guards the test itself: the tools the autonomous contract relies on
    missing = named - set(writer.tools)
    assert not missing, f"application_writer lacks tools its autonomous contract calls: {sorted(missing)}"

    # No operator gates a workflow run, so the drafter owns NO tool that writes the record,
    # confirms an answer in the operator's name, or drives/submits the employer's form.
    forbidden = {
        "careercoach_update_profile",
        "careercoach_write_profile",
        "careercoach_import_experience",
        "careercoach_confirm_answers",
        "careercoach_plan_fill",
        "careercoach_verify_fill",
        "careercoach_handoff",
        "careercoach_request_submit",
        "browser_fill",
        "browser_click",
        "browser_form_read",
        "browser_upload",
        "browser_select",
    }
    assert not forbidden & set(writer.tools), sorted(forbidden & set(writer.tools))

    # Every careercoach_* tool the subagent is granted is one the plugin actually registers,
    # and the kit-first prompt no longer hard-codes a cover letter for every application.
    assert not {t for t in writer.tools if t.startswith("careercoach_")} - registered
    assert "ONLY if the posting asks" in writer.system_prompt
    assert "produce a tailored CV and a cover letter" not in writer.system_prompt
