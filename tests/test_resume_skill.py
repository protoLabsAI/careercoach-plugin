"""The ``resume`` skill — a skill with NO tools of its own, which is exactly what needs testing.

It composes tools other plugins own. We cannot import those plugins (ADR 0039: reference by
name, never import), so the guard is a **declared allowlist**: every external tool name the
skill text reaches for is listed below with the plugin that owns it, and the tests fail both
ways — an undeclared name in the skill (a typo, or an invented tool) and a declared name
nothing references (a stale allowlist). Anything ``careercoach_*`` must be really registered.

The templates get the same treatment: they are the skill's only executable asset, so they are
parsed and asserted against the ATS-critical CSS contract stated in ``SKILL.md``.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "resume"
TEMPLATE_DIR = SKILL_DIR / "templates"


# ── the composition contract ──────────────────────────────────────────────────────────
# Every tool the resume skill names that this plugin does NOT register, with its owner.
# Reference-by-name only: none of these can be imported here, so this list IS the contract.
EXTERNAL_TOOLS: dict[str, str] = {
    # artifact plugin (bundled with protoAgent, enabled by default) — plugins/artifact/_tools.py
    "show_artifact": "artifact",
    "get_artifact": "artifact",
    "update_artifact": "artifact",
    "rewrite_artifact": "artifact",
    "list_artifacts": "artifact",
    "check_artifact": "artifact",
    "save_file_artifact": "artifact",
    # agent_browser plugin (off by default; browser_pdf lands with protoAgent PR #3451)
    "browser_open": "agent_browser",
    "browser_pdf": "agent_browser",
    # execute_code plugin (off by default — enabling it is a code-execution trust decision)
    "execute_code": "execute_code",
    # host core — tools/lg_tools.py, tools/fs_tools.py
    "show_component": "host core",
    "load_skill": "host core",
    "fetch_url": "host core",
    "knowledge_ingest": "host core",
    "write_file": "host core",
}

# snake_case tokens in the skill text that are NOT tools: parameters, config keys, profile
# fields, artifact-plugin settings. Listed so a genuine typo can't hide among them.
NOT_TOOLS = {
    "allowed_domains",
    "artifact_id",
    "do_not_claim",
    "max_blob_kb",
    "max_preview_kb",
    "new_string",
    "old_string",
    "preview_id",
    "process_log",
    "render_format",
    "sans_serif",
    "tailored_resume",
}

# A backticked token with an underscore in it — the shape every tool name here has.
TOKEN = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)")


def _skill_files() -> list[Path]:
    return sorted(SKILL_DIR.glob("*.md"))


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} needs YAML frontmatter"
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


# ── registration ──────────────────────────────────────────────────────────────────────
def test_the_skill_is_discoverable_and_declares_the_plugin_tools_it_calls(plugin, registry):
    """Same contract the other skills are held to: loadable frontmatter, and every
    ``careercoach_*`` tool it declares is actually registered by ``register()``."""
    plugin.register(registry)
    registered = {getattr(t, "name", str(t)) for t in registry.tools}

    fm, body = _frontmatter(SKILL_DIR / "SKILL.md")
    assert fm["name"] == "resume"
    assert fm["description"].strip(), "the description is what routes the agent here"
    declared = fm["tools"]
    for name in declared:
        if name.startswith("careercoach_"):
            assert name in registered, f"resume declares unknown tool {name!r}"

    # It adds no tools of its own — the whole point. The tool count is asserted in
    # test_careercoach.py; here we assert the skill reaches only for what exists.
    for name in declared:
        assert name in registered or name in EXTERNAL_TOOLS, f"undeclared tool in frontmatter: {name!r}"
    assert "careercoach_read_profile" in declared, "the profile is the source of truth for facts"

    # …and the reverse: a declared tool no step actually calls is a hint the agent will
    # act on with nothing telling it how.
    prose = body + "\n".join(p.read_text(encoding="utf-8") for p in _skill_files() if p.name != "SKILL.md")
    for name in declared:
        assert name in prose, f"resume declares {name!r} but no step tells the agent to use it"


def test_the_router_points_at_every_sub_file_it_names(plugin):
    """Progressive disclosure only works if the file the router sends you to exists."""
    _, body = _frontmatter(SKILL_DIR / "SKILL.md")
    reachable = {p.name for p in ROOT.glob("skills/*/*.md")}
    for ref in sorted(set(re.findall(r"`([a-z-]+\.md)`", body))):
        assert ref in reachable, f"SKILL.md points at {ref}, which no skill ships"
    # and every sub-file it ships is reachable from the router
    for path in _skill_files():
        if path.name != "SKILL.md":
            assert f"`{path.name}`" in body, f"{path.name} ships but SKILL.md never sends you there"


# ── the composition contract, both directions ─────────────────────────────────────────
def test_every_tool_the_skill_names_exists_somewhere(plugin, registry):
    """A skill that tells the agent to call a tool nobody registers fails silently at
    runtime as 'the agent just didn't do it'. Every tool-shaped name in the skill text must
    be a registered careercoach tool or a declared external one."""
    plugin.register(registry)
    registered = {getattr(t, "name", str(t)) for t in registry.tools}
    known = registered | set(EXTERNAL_TOOLS) | NOT_TOOLS

    for path in _skill_files():
        for token in set(TOKEN.findall(path.read_text(encoding="utf-8"))):
            assert token in known, f"{path.name} names {token!r}, which is neither registered nor declared"


def test_the_external_allowlist_has_no_dead_entries():
    """Keeps the contract honest: a tool listed here but referenced nowhere means the
    allowlist has drifted from what the skill actually composes."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in _skill_files())
    for name, owner in EXTERNAL_TOOLS.items():
        assert name in text, f"{name!r} ({owner}) is allowlisted but the skill never uses it"


def test_the_skill_states_who_owns_each_external_tool_and_how_it_degrades():
    """The operator has to be told which plugin to enable. Each optional plugin is named,
    and each has a stated fallback rather than a silent switch."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in _skill_files())
    for owner in ("artifact", "agent_browser", "execute_code", "cowork"):
        assert owner in text, f"the skill never names the {owner} plugin"
    assert "#3451" in text, "browser_pdf's arrival must be traceable to the core PR"
    assert "never silently" in text.lower() or "don't silently" in text.lower()


def test_the_profile_stays_the_source_of_truth():
    """A resume is where fabrication breaks in first. The rule is restated in the skill."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "careercoach_read_profile" in body and "do_not_claim" in body
    assert "/setup-coach" in body, "a thin profile must route to the interview, not to invention"
    imports = (SKILL_DIR / "import.md").read_text(encoding="utf-8")
    assert "must appear in the source text" in imports


# ── the templates ─────────────────────────────────────────────────────────────────────
class _Parse(HTMLParser):
    """Collects tags and text so a template can be asserted on without a dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.style = ""
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self._in_style = tag == "style"

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.style += data


def _templates() -> list[Path]:
    found = sorted(TEMPLATE_DIR.glob("*.html"))
    assert len(found) >= 2, "the catalogue promises more than one ATS-safe starting point"
    return found


def test_every_template_parses_and_is_a_whole_self_contained_document():
    for path in _templates():
        p = _Parse()
        p.feed(path.read_text(encoding="utf-8"))
        for tag in ("html", "head", "style", "body", "h1", "h2"):
            assert tag in p.tags, f"{path.name} has no <{tag}>"
        assert p.style.strip(), f"{path.name} has no inline CSS — it must be one self-contained file"
        assert "link" not in p.tags and "script" not in p.tags, f"{path.name} loads something external"


def test_every_template_holds_the_ats_critical_css_contract():
    """The contract stated in SKILL.md. These are the four that decide whether a parser can
    read the document at all — page geometry, no split entries, no ligatures, real fallbacks."""
    for path in _templates():
        p = _Parse()
        p.feed(path.read_text(encoding="utf-8"))
        css = re.sub(r"\s+", " ", p.style)

        assert re.search(r"@page\s*\{[^}]*size:\s*(Letter|A4)", css), f"{path.name}: no @page size"
        assert re.search(r"@page\s*\{[^}]*margin:", css), f"{path.name}: @page declares no margin"
        assert "break-inside: avoid" in css, f"{path.name}: entries can split across a page"
        assert "page-break-inside: avoid" in css, f"{path.name}: no legacy break-inside alias"
        assert "font-variant-ligatures: none" in css, f"{path.name}: ligatures reach the parser as U+FB0x"
        assert re.search(r"font-family:[^;]*\b(sans-serif|serif|monospace)\s*;", css), (
            f"{path.name}: font stack has no generic fallback"
        )


def test_no_template_uses_a_layout_a_parser_reads_out_of_order():
    """Single column, always. Two columns interleave into nonsense on extraction; a table,
    a float or an absolutely-positioned block does the same."""
    for path in _templates():
        p = _Parse()
        p.feed(path.read_text(encoding="utf-8"))
        css = re.sub(r"\s+", " ", p.style)

        assert "table" not in p.tags, f"{path.name} uses a <table>"
        assert "img" not in p.tags, f"{path.name} embeds an image"
        assert "header" not in p.tags and "footer" not in p.tags, (
            f"{path.name}: contact details belong in the body, not in page furniture"
        )
        for banned in ("float:", "position: absolute", "grid-template-columns", "column-count", "columns:"):
            assert banned not in css, f"{path.name} uses {banned!r} — that is a multi-column layout"


# What the artifact panel injects into EVERY html artifact, ahead of the artifact's own code:
# the console's DS plugin-kit stylesheet (protoAgent apps/web/public/_ds/plugin-kit.css —
# element rules on `body`, `:where(h1, h2, h3, h4)` and `a`) plus the shell's theme style
# (plugins/artifact/shell.js `base()`: `html,body{margin:0;background;color}`). A print through
# `browser_pdf` opens the file standalone and gets NONE of it. So a template renders the same in
# both places only if it declares every one of these properties itself, on the same element —
# later-in-source at equal-or-higher specificity wins, and `:where()` has zero specificity.
# (-webkit-font-smoothing is left out on purpose: a screen hint that affects neither layout
# nor print.) Refresh this map if the kit grows a new element-level rule.
INJECTED = {
    "html": {"background"},
    "body": {"margin", "background", "color", "font-family", "font-size", "font-weight", "line-height"},
    "h1": {"margin", "font-weight", "line-height", "letter-spacing"},
    "h2": {"margin", "font-weight", "line-height", "letter-spacing"},
    "a": {"color"},
}


def _declared(css: str) -> dict[str, set[str]]:
    """``selector → declared properties`` for every plain rule in ``css`` (nested @media rules
    included, comments stripped). Enough CSS parsing for a contract test, no dependency."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: dict[str, set[str]] = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        props = {d.split(":", 1)[0].strip() for d in body.split(";") if ":" in d}
        for sel in selectors.split(","):
            out.setdefault(sel.strip(), set()).update(props)
    return out


def test_every_template_isolates_itself_from_the_stylesheet_the_panel_injects():
    """The panel render and the `browser_pdf` print must be the same document, not nearly the
    same: a heading that's weight 500 in the panel and 700 on paper is a template bug."""
    for path in _templates():
        p = _Parse()
        p.feed(path.read_text(encoding="utf-8"))
        rules = _declared(p.style)
        for element, props in INJECTED.items():
            if element == "a":
                if "a" not in p.tags:
                    continue
                own = set().union(*(v for k, v in rules.items() if k == "a" or k.endswith(" a")))
            else:
                own = rules.get(element, set())
            missing = props - own
            assert not missing, f"{path.name}: <{element}> inherits {sorted(missing)} from the panel's DS kit"
        assert "box-sizing" in rules.get("*", set()), f"{path.name}: box model left to the host"


def test_no_template_ships_a_ligature_or_private_use_character():
    """What the rules checklist looks for in a finished resume, enforced at the source."""
    for path in _templates():
        for ch in path.read_text(encoding="utf-8"):
            assert not ("ﬀ" <= ch <= "ﬆ"), f"{path.name} contains a ligature glyph {ch!r}"
            assert not ("" <= ch <= ""), f"{path.name} contains a private-use glyph {ch!r}"


def test_the_catalogue_and_the_template_directory_agree():
    """A template nobody can find is a template nobody uses, and a catalogue row pointing at
    a missing file sends the agent to author its own shell instead."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    on_disk = {p.name for p in _templates()}
    listed = set(re.findall(r"templates/([a-z0-9-]+\.html)", body))
    assert listed == on_disk, f"catalogue lists {sorted(listed)}, disk has {sorted(on_disk)}"


# ── the guides no longer promise what no host could do ────────────────────────────────
def test_the_cv_guide_no_longer_claims_the_agent_renders_and_exports_a_pdf():
    """Before browser_pdf there was no host path from an HTML artifact to a PDF file, and no
    way for the agent to see a rendered artifact. Both were stated as fact in the guides."""
    for rel in ("job-application-assistant/cv-guide.md", "job-application-assistant/cover-letter-guide.md"):
        text = (ROOT / "skills" / rel).read_text(encoding="utf-8")
        assert "then export/print to PDF" not in text, f"{rel} still promises an export the agent can't do"
        assert "HTML → PDF via the artifact" not in text, f"{rel} still credits the artifact plugin with PDFs"
        assert "Render, look at it, iterate" not in text, f"{rel} still claims the agent sees the render"
        assert "resume" in text, f"{rel} should hand the mechanics to the resume skill"
