"""The five skill entrypoints, their references, and the preserved application gates."""

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
import yaml

from _plugin_testkit import FakeRegistry
from test_resume_skill import EXTERNAL_TOOLS

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
ENTRYPOINTS = {
    "apply-form": "apply",
    "career-strategy": "strategy",
    "interview-coach": "interview",
    "job-hunt": "job-hunt",
    "resume": "resume",
}
BROWSER_TOOLS = {
    "browser_open",
    "browser_pdf",
    "browser_form_read",
    "browser_select",
    "browser_fill",
    "browser_upload",
    "browser_click",
}
BETA_HEADING = "## Browser fill (opt-in beta)"


def _read(directory):
    text = (SKILLS / directory / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---\n", 2)
    return yaml.safe_load(frontmatter), body


def test_exactly_five_discoverable_skills_in_the_original_directories():
    paths = sorted(SKILLS.rglob("SKILL.md"))
    assert {p.parent for p in paths} == {SKILLS / d for d in ENTRYPOINTS}
    assert len(paths) == 5
    for directory, name in ENTRYPOINTS.items():
        metadata, _ = _read(directory)
        assert metadata["name"] == name
        assert metadata["description"].strip()


@pytest.mark.parametrize("directory", ENTRYPOINTS)
def test_frontmatter_tools_resolve_on_default_surface(plugin, iso, directory):
    registry = FakeRegistry({})
    plugin.register(registry)
    registered = {tool.name for tool in registry.tools}
    metadata, _ = _read(directory)
    # Preserve resume's established ADR 0039 composition contract. Its artifact/host
    # dependencies are explicitly owned and tested, not careercoach registrations.
    external = set(EXTERNAL_TOOLS) if directory == "resume" else BROWSER_TOOLS
    declared = metadata["tools"]
    assert declared and len(declared) == len(set(declared))
    assert set(declared) <= registered | external
    assert {name for name in declared if name.startswith("careercoach_")} <= registered
    assert "company_researcher" not in declared  # a subagent, never a tool


def _relative_links(text):
    # Inline/image links plus reference-style definitions; URLs and local anchors
    # are handled by the caller. Optional titles are outside the destination.
    return re.findall(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)", text) + re.findall(
        r"^\s*\[[^\]\n]+\]:\s*(<[^>]+>|\S+)", text, flags=re.M
    )


def test_every_relative_link_resolves():
    checked = 0
    for path in SKILLS.rglob("*.md"):
        for target in _relative_links(path.read_text(encoding="utf-8")):
            url = urlsplit(target.strip("<>"))
            if url.scheme or url.netloc or not url.path:
                continue
            checked += 1
            assert (path.parent / unquote(url.path)).is_file(), (path.relative_to(ROOT), target)
    assert checked >= 8, "the moved guides must be reachable through real links"


def test_all_apply_guides_are_reachable_from_the_entrypoint():
    pending = [SKILLS / "apply-form/SKILL.md"]
    visited = set()
    while pending:
        path = pending.pop().resolve()
        if path in visited:
            continue
        visited.add(path)
        for target in _relative_links(path.read_text(encoding="utf-8")):
            url = urlsplit(target.strip("<>"))
            if not url.scheme and url.path.endswith(".md"):
                pending.append(path.parent / unquote(url.path))
    assert {p.resolve() for p in (SKILLS / "apply-form").glob("*.md")} <= visited


def test_apply_preserves_kit_default_and_verbatim_browser_beta():
    _, body = _read("apply-form")
    default, beta = body.split(BETA_HEADING, 1)
    assert "careercoach_save_apply_kit" in default
    assert "Never submit without the approval card" in beta
    assert "Never solve or bypass a captcha" in beta
    # Snapshot of the complete S3b beta, including verification, handoff and grant
    # lifetime. A wording change must be reviewed as a deliberate contract change.
    assert (
        hashlib.sha256(beta.encode()).hexdigest() == "c650c911eb9c2db74787db54e70f41acdd4376df5234eb157ab65038c751177b"
    )


def test_absorbed_workflows_keep_their_safeguards():
    _, hunt = _read("job-hunt")
    assert _read("job-hunt")[0]["slash"] == "setup-coach"
    assert "## First run / set me up" in hunt
    for rule in ("Harvest before you ask", "preview_id", "confirm_removal=true", "Only record operator facts after"):
        assert rule in hunt
    _, strategy = _read("career-strategy")
    for step in ("hard-skill diff", "synthesis", "Gap heatmap", "Learning plan", "Never fabricate a resource"):
        assert step in strategy
    packet = (SKILLS / "apply-form/packet-flow.md").read_text()
    phases = re.findall(r"^### Phase (\d+) .*gate: confirm", packet, flags=re.M)
    assert phases == ["1", "2", "3", "4", "5", "6"]
    for rule in ("Gate every phase", "do_not_claim", "interview-backtrack", "you don't submit for them"):
        assert rule in packet


def test_no_operator_specific_names_in_skills():
    for path in SKILLS.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8").lower()
            assert "josh" not in text and "jobcoach" not in text, path
