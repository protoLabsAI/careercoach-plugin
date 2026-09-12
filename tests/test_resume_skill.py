"""The ``resume`` skill — a skill with NO tools of its own, which is exactly what needs testing.

It composes tools other plugins own. We cannot import those plugins (ADR 0039: reference by
name, never import), so the guard is a set of **declared vocabularies**: every external tool
name the skill text reaches for is listed below with the plugin that owns it, every
tool-shaped word that is NOT a tool is listed too, and the tests fail both ways — a word in the
skill that no list knows (a typo, an invented tool) and a listed word nothing uses (a stale
list). Anything ``careercoach_*`` must be really registered.

The templates get the same treatment: they are the skill's only executable asset, so they are
parsed — CSS comments stripped, declarations matched exactly, inline ``style=`` included — and
held to the ATS-critical CSS contract stated in ``SKILL.md``.
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
    "delete_artifact": "artifact",
    "pin_artifact": "artifact",  # artifact plugin 0.18.0+ (protoAgent #3456); used only when present
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

# snake_case words in the skill text that are NOT tools: parameters, config keys, profile
# fields, a plugin id. Listed so a genuine typo can't hide among them.
NOT_TOOLS = {
    "agent_browser",  # the plugin id, named when telling the operator what to enable
    "allowed_domains",
    "artifact_id",
    "do_not_claim",
    "max_blob_kb",
    "max_preview_kb",
    "max_versions",
    "new_string",
    "old_string",
    "output_dir",  # cowork docx skill's default output setting, named to explain why it is overridden
    "preview_id",
    "render_format",
}

# Backticked bare words (no underscore) the skill uses on purpose: kinds, formats, profile
# fields, CSS/HTML names, a quoted extraction failure. A tool name typo'd into one word
# (`showartifact`) is the regression this catches.
BARE_WORDS = {
    "body",
    "req",
    "content",
    "docx",
    "education",
    "efciency",  # the garbled-extraction example in ats-check.md
    "fi",
    "float",
    "h1",
    "h2",
    "history",
    "href",
    "html",
    "kind",
    "latex",
    "markdown",
    "mermaid",
    "notes",
    "path",
    "react",
    "role",
    "roles",
    "serif",
    "skills",
    "stories",
    "svg",
    "upskill",
}

# Anywhere in the text — prose, backticks and fenced blocks alike (fences are where most of
# the concrete calls live).
IDENT = re.compile(r"(?<![\w/.-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![\w-])")
CALL = re.compile(r"(?<![\w.])([a-z][a-z0-9_]*)\(")
BARE = re.compile(r"`([a-z][a-z0-9]*)`")


def _skill_files() -> list[Path]:
    return sorted(SKILL_DIR.glob("*.md"))


def _skill_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _skill_files())


def _flat(text: str) -> str:
    return " ".join(text.split())


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} needs YAML frontmatter"
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


def _registered(plugin, registry) -> set[str]:
    plugin.register(registry)
    return {getattr(t, "name", str(t)) for t in registry.tools}


# ── registration ──────────────────────────────────────────────────────────────────────
def test_the_skill_is_discoverable_and_declares_the_plugin_tools_it_calls(plugin, registry):
    """Same contract the other skills are held to: loadable frontmatter, and every
    ``careercoach_*`` tool it declares is actually registered by ``register()``."""
    registered = _registered(plugin, registry)

    fm, body = _frontmatter(SKILL_DIR / "SKILL.md")
    assert fm["name"] == "resume"
    assert fm["description"].strip(), "the description is what routes the agent here"
    declared = fm["tools"]
    for name in declared:
        if name.startswith("careercoach_"):
            assert name in registered, f"resume declares unknown tool {name!r}"
        assert name in registered or name in EXTERNAL_TOOLS, f"undeclared tool in frontmatter: {name!r}"
    assert "careercoach_read_profile" in declared, "the profile is the source of truth for facts"

    # …and the reverse: a declared tool no step actually calls is a hint the agent will
    # act on with nothing telling it how.
    prose = body + "\n".join(p.read_text(encoding="utf-8") for p in _skill_files() if p.name != "SKILL.md")
    for name in declared:
        assert name in prose, f"resume declares {name!r} but no step tells the agent to use it"


def test_the_router_points_at_every_sub_file_it_names():
    """Progressive disclosure only works if the file the router sends you to exists."""
    _, body = _frontmatter(SKILL_DIR / "SKILL.md")
    reachable = {p.name for p in ROOT.glob("skills/*/*.md")}
    for ref in sorted(set(re.findall(r"`([a-z-]+\.md)`", body))):
        assert ref in reachable, f"SKILL.md points at {ref}, which no skill ships"
    for path in _skill_files():
        if path.name != "SKILL.md":
            assert f"`{path.name}`" in body, f"{path.name} ships but SKILL.md never sends you there"


# ── the composition contract, both directions ─────────────────────────────────────────
def test_every_tool_the_skill_names_exists_somewhere(plugin, registry):
    """A skill that tells the agent to call a tool nobody registers fails silently at runtime
    as 'the agent just didn't do it'. Every tool-shaped word must be known, every call-shaped
    word must be a real tool, and every backticked bare word must be on purpose."""
    registered = _registered(plugin, registry)
    tools = registered | set(EXTERNAL_TOOLS)

    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        for token in set(IDENT.findall(text)):
            assert token in tools | NOT_TOOLS, f"{path.name} names {token!r}, which is neither a tool nor declared"
        for token in set(CALL.findall(text)):
            assert token in tools, f"{path.name} calls {token!r}(…), which no plugin registers"
        for token in set(BARE.findall(text)):
            assert token in tools | BARE_WORDS, f"{path.name} backticks {token!r}, which no list knows"


def test_the_vocabularies_have_no_dead_entries():
    """Keeps the contract honest: a word listed here but used nowhere means a list has
    drifted from what the skill actually says — and a stale entry is where a typo hides."""
    text = _skill_text()
    idents, bares = set(IDENT.findall(text)), set(BARE.findall(text))
    for name, owner in EXTERNAL_TOOLS.items():
        assert name in idents, f"{name!r} ({owner}) is allowlisted but the skill never uses it"
    assert not NOT_TOOLS - idents, f"dead NOT_TOOLS entries: {sorted(NOT_TOOLS - idents)}"
    assert not BARE_WORDS - bares, f"dead BARE_WORDS entries: {sorted(BARE_WORDS - bares)}"


def test_the_skill_states_who_owns_each_external_tool_and_how_it_degrades():
    """The operator has to be told which plugin to enable. Each optional plugin is named,
    and each has a stated fallback rather than a silent switch."""
    text = _skill_text()
    for owner in ("artifact", "agent_browser", "execute_code", "cowork"):
        assert owner in text, f"the skill never names the {owner} plugin"
    assert "#3451" in text, "browser_pdf's arrival must be traceable to the core PR"
    assert "never silently" in text.lower() or "don't silently" in text.lower()


# cowork registers NO tools — only skills (`register_skill_dir`) and a goal verifier — so
# "check your toolset" can't find it. Every skill file that names cowork states the one test
# that works: `execute_code` in the toolset AND `docx` among the available skills.
AVAILABILITY = ("`execute_code` in your toolset", "`docx` in your available skills")


def test_every_file_naming_cowork_uses_the_one_availability_test_that_works():
    routers = {ROOT / "skills/job-application-assistant/SKILL.md", ROOT / "skills/role-packet/SKILL.md"}
    docx_route = re.compile(r"`docx`|load_skill\(['\"]docx")
    naming = sorted(
        {
            p
            for p in ROOT.glob("skills/**/*.md")
            if "cowork" in (s := p.read_text(encoding="utf-8")) and docx_route.search(s)
        }
        | routers
    )
    assert len(naming) > len(routers), "the DOCX route is documented somewhere"
    for path in naming:
        flat = _flat(path.read_text(encoding="utf-8"))
        for phrase in AVAILABILITY:
            assert phrase in flat, f"{path.relative_to(ROOT)} names cowork without the test: {phrase!r}"
        assert "check your toolset)" not in flat, f"{path.relative_to(ROOT)}: cowork adds no tools"


def test_the_skill_does_not_promise_what_the_hosts_cannot_do():
    """Each of these was once stated as fact and wasn't: #3444 is merged, not released;
    browser_pdf prints Letter whatever the @page says; its result isn't a bare path; and
    printing from inside the console prints the console."""
    flat = _flat(_skill_text())
    imports = _flat((SKILL_DIR / "import.md").read_text(encoding="utf-8"))
    assert "#3444" in imports and "not yet in a release" in imports
    export = _flat((SKILL_DIR / "export.md").read_text(encoding="utf-8"))
    assert "US Letter" in export and "Saved to" in export
    assert "Route 1 — DOCX" in export, "DOCX is the route that works today; it leads"
    assert "Print frame" not in flat, "frame-printing was never verified on any host"
    catalogue = _flat((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    assert "print on US Letter through `browser_pdf`" in catalogue


def test_the_skill_survives_the_artifact_panel_evicting_the_resume():
    """The panel keeps 20 artifacts per instance by default. A skill that treats the artifact
    as the record points at nothing after a busy week — so the id is verified, recovery is
    written down, the snapshot is the copy of record, and exports don't burn slots."""
    tailor = _flat((SKILL_DIR / "master-and-tailor.md").read_text(encoding="utf-8"))
    assert "Eviction" in tailor and "list_artifacts" in tailor and "careercoach_list_roles" in tailor
    assert "history" in tailor and "Tell the operator" in tailor
    # every layer that must survive eviction has a home, and recovery reads it back read-only
    export = _flat((SKILL_DIR / "export.md").read_text(encoding="utf-8"))
    assert "careercoach master ·" in tailor, "the master's ids ride in its copy of record, not the profile"
    # same-title postings at one company differ only by req: the title carries it, and a
    # variant's id is read from its own snapshot header before any title lookup
    assert 'title="<Name> — Resume — <Company> <Role - Req>"' in tailor, "same-title postings must not collide"
    assert "<Role - Req> (DOCX)" in export and "<Role - Req> (PDF)" in export
    assert tailor.index("from its snapshot header first") < tailor.index("fall back to the title")
    # recovery must not carry the evicted artifact's id comment into the new one
    assert "Strip the `<!-- careercoach master … -->` line" in tailor
    assert "careercoach master · artifact <id> · version <n>" in tailor, "the comment records the version"
    # edits the operator made in the panel get captured before an eviction can lose them
    assert "wasn't written by you" in tailor and "re-save the copy of record" in tailor
    assert "Updated tailored resume.md" in tailor, "a re-file answers Updated, not Wrote"
    assert "pass the same `req` it was filed under" in tailor, "one folder per role across both flows"
    packet_flow = _flat((ROOT / "skills/role-packet/SKILL.md").read_text(encoding="utf-8"))
    assert "pass the same `req` it was filed under" in packet_flow, "role-packet must reuse the resume's folder"
    assert "careercoach_scaffold_role" not in tailor, "recovery must not use a write tool as a lookup"
    assert "Master Resume.html" in tailor, "the approved master's wording needs a copy of record"
    assert "the approved wording is gone" in tailor, "without one, say so — don't claim otherwise"
    assert "Nothing of record ever lives only in the panel" not in tailor
    assert "Re-file the snapshot after every edit" in tailor and "File its snapshot now" in tailor
    assert "DOCX export:" in tailor and "PDF export:" in tailor, "both export ids live in the header"
    export = _flat((SKILL_DIR / "export.md").read_text(encoding="utf-8"))
    assert "Always write to an absolute path" in export, "the server's cwd is / — relative saves fail"
    assert "Never reuse a DOCX id for a PDF" in export
    for name in ("export.md", "ats-check.md"):
        text = (SKILL_DIR / name).read_text(encoding="utf-8")
        calls = re.findall(r"`save_file_artifact\((.*?)\)`", text, flags=re.S)
        assert calls, f"{name} shows no save_file_artifact call"
        for args in calls:
            assert "artifact_id" in args, f"{name}: a re-save without artifact_id burns a panel slot: {args!r}"


def test_the_profile_stays_the_source_of_truth():
    """A resume is where fabrication breaks in first. The rule is restated in the skill."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "careercoach_read_profile" in body and "do_not_claim" in body
    assert "/setup-coach" in body, "a thin profile must route to the interview, not to invention"
    imports = (SKILL_DIR / "import.md").read_text(encoding="utf-8")
    assert "must appear in the source text" in imports


# ── the templates ─────────────────────────────────────────────────────────────────────
class _Parse(HTMLParser):
    """Collects tags, the <style> text, inline styles, attribute values and <h2> text, so a
    template can be asserted on without a dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.style = ""
        self.inline: list[str] = []
        self.attr_values: list[str] = []
        self.h2: list[str] = []
        self._in: str | None = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self._in = tag if tag in ("style", "h2") else None
        if tag == "h2":
            self.h2.append("")
        for name, value in attrs:
            self.attr_values.append(value or "")
            if name == "style":
                self.inline.append(value or "")

    def handle_endtag(self, tag):
        if tag == self._in:
            self._in = None

    def handle_data(self, data):
        if self._in == "style":
            self.style += data
        elif self._in == "h2":
            self.h2[-1] += data


def _parse(path: Path) -> _Parse:
    p = _Parse()
    p.feed(path.read_text(encoding="utf-8"))
    return p


def _decls(block: str) -> dict[str, str]:
    out = {}
    for d in block.split(";"):
        if ":" in d:
            prop, value = d.split(":", 1)
            out[prop.strip().lower()] = " ".join(value.split()).lower()
    return out


def _rules(css: str) -> list[tuple[list[str], dict[str, str], str | None]]:
    """``(selectors, declarations, enclosing @media or None)`` for every rule in ``css``,
    comments stripped first — a commented-out rule is not a rule."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: list[tuple[list[str], dict[str, str], str | None]] = []

    def walk(block: str, media: str | None) -> None:
        pos = 0
        while (start := block.find("{", pos)) >= 0:
            prelude, depth, end = block[pos:start].strip(), 1, start + 1
            while depth and end < len(block):
                depth += {"{": 1, "}": -1}.get(block[end], 0)
                end += 1
            inner = block[start + 1 : end - 1]
            if prelude.startswith("@media"):
                walk(inner, prelude)
            else:
                out.append(([s.strip() for s in prelude.split(",")], _decls(inner), media))
            pos = end

    walk(css, None)
    return out


def _screen(rules) -> dict[str, dict[str, str]]:
    """``selector → declarations`` as the panel renders it: print-only rules excluded."""
    out: dict[str, dict[str, str]] = {}
    for selectors, decls, media in rules:
        if media and "print" in media:
            continue
        for sel in selectors:
            out.setdefault(sel, {}).update(decls)
    return out


def _templates() -> list[Path]:
    found = sorted(TEMPLATE_DIR.glob("*.html"))
    assert len(found) >= 2, "the catalogue promises more than one ATS-safe starting point"
    return found


def test_every_template_parses_and_is_a_whole_self_contained_document():
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        assert text.lower().startswith("<!doctype html>"), f"{path.name}: no doctype — standalone print goes quirks"
        p = _parse(path)
        for tag in ("html", "head", "style", "body", "h1", "h2"):
            assert tag in p.tags, f"{path.name} has no <{tag}>"
        assert p.style.strip(), f"{path.name} has no inline CSS — it must be one self-contained file"
        assert "link" not in p.tags and "script" not in p.tags, f"{path.name} loads something external"


def test_every_template_holds_the_ats_critical_css_contract():
    """The contract stated in SKILL.md, matched as real declarations — page geometry, entries
    that never split, no ligatures, a font stack with a real generic fallback."""
    for path in _templates():
        rules = _rules(_parse(path).style)
        screen = _screen(rules)

        page = screen.get("@page", {})
        assert page.get("size", "").split()[:1] in (["letter"], ["a4"]), f"{path.name}: no @page size"
        assert page.get("margin"), f"{path.name}: @page declares no margin"
        entry = screen.get(".entry", {})
        assert entry.get("break-inside") == "avoid", f"{path.name}: .entry can split across a page"
        assert entry.get("page-break-inside") == "avoid", f"{path.name}: .entry has no legacy alias"
        body = screen.get("body", {})
        assert body.get("font-variant-ligatures") == "none", f"{path.name}: ligatures reach the parser as U+FB0x"
        generic = body.get("font-family", "").split(",")[-1].strip()
        assert generic in ("sans-serif", "serif", "monospace"), f"{path.name}: font stack has no generic fallback"


BANNED = {
    "float": lambda v: v != "none",
    "position": lambda v: v in ("absolute", "fixed"),
    "display": lambda v: (
        v in ("flex", "inline-flex", "grid", "inline-grid", "inline-block", "table", "table-row", "table-cell")
    ),
    # `width: 48%` beside a sibling is a two-column layout without a single flex/grid keyword
    "width": lambda v: v.endswith("%") and float(v[:-1] or 0) < 100,
    "columns": lambda v: True,
    "column-count": lambda v: True,
    "column-width": lambda v: True,
    "grid-template-columns": lambda v: True,
    "grid-template-areas": lambda v: True,
}


def test_no_template_uses_a_layout_a_parser_reads_out_of_order():
    """Single column, always — in the stylesheet AND in any inline style attribute. Two
    columns interleave into nonsense on extraction; a table, a float, a flex/grid row or an
    absolutely-positioned block does the same."""
    for path in _templates():
        p = _parse(path)
        decls = [d for _, d, _ in _rules(p.style)] + [_decls(s) for s in p.inline]
        for d in decls:
            for prop, bad in BANNED.items():
                if prop in d:
                    assert not bad(d[prop]), f"{path.name} uses {prop}: {d[prop]} — a multi-column layout"
        for tag in ("table", "img", "svg", "picture", "canvas", "iframe", "object"):
            assert tag not in p.tags, f"{path.name} uses <{tag}>"
        assert "header" not in p.tags and "footer" not in p.tags, (
            f"{path.name}: contact details belong in the body, not in page furniture"
        )


STANDARD_HEADINGS = {"Summary", "Skills", "Experience", "Education", "Certifications", "Publications"}


def test_every_template_uses_only_the_standard_headings_its_own_checklist_demands():
    """ats-check.md rule 2. A template that fails the skill's own checklist teaches the agent
    to fail it too."""
    for path in _templates():
        for heading in _parse(path).h2:
            assert " ".join(heading.split()) in STANDARD_HEADINGS, f"{path.name}: non-standard heading {heading!r}"


def test_no_placeholder_hides_where_the_reader_cannot_see_it():
    """A placeholder inside an attribute (an href) survives into a sent resume unnoticed — it
    renders as nothing and extracts as nothing. Every placeholder is visible text."""
    for path in _templates():
        for value in _parse(path).attr_values:
            assert "[" not in value, f"{path.name}: placeholder inside an attribute: {value!r}"


# What the artifact panel injects into EVERY html artifact, ahead of the artifact's own code:
# the console's DS plugin-kit stylesheet (protoAgent apps/web/public/_ds/plugin-kit.css —
# element rules on `body`, `:where(h1, h2, h3, h4)` and `a`) plus the shell's theme style
# (plugins/artifact/shell.js `base()`: `html,body{margin:0;background;color}`). A print through
# `browser_pdf` or a downloaded file opens standalone and gets NONE of it. So a template renders
# the same in both places only if it declares each of these itself, on the same element, in its
# SCREEN rules — later-in-source wins at equal specificity and `:where()` has none.
# (-webkit-font-smoothing is left out on purpose: a screen hint that affects neither layout nor
# print.) NOT covered, because no stylesheet can win it: after a live theme change the shell's
# re-theme shim writes the theme's text colour and background onto <body> as INLINE styles,
# which beat any template rule — one reason the panel is not a print preview. (A srcdoc iframe
# is never in quirks mode, so where the injection sits relative to the doctype doesn't matter.)
# Refresh this map if the kit grows a new element-level rule.
INJECTED = {
    "html": {"background"},
    "body": {"margin", "background", "color", "font-family", "font-size", "font-weight", "line-height"},
    "h1": {"margin", "font-weight", "line-height", "letter-spacing"},
    "h2": {"margin", "font-weight", "line-height", "letter-spacing"},
    "a": {"color"},
}


def test_every_template_isolates_itself_from_the_stylesheet_the_panel_injects():
    """The panel render and the exported file must agree: a heading that's weight 500 in the
    panel and 700 on paper is a template bug."""
    for path in _templates():
        p = _parse(path)
        screen = _screen(_rules(p.style))
        for element, props in INJECTED.items():
            if element == "a":
                if "a" not in p.tags:
                    continue
                own = set().union(*(set(d) for sel, d in screen.items() if sel == "a" or sel.endswith(" a")))
            else:
                own = set(screen.get(element, {}))
            missing = props - own
            assert not missing, f"{path.name}: <{element}> inherits {sorted(missing)} from the panel's DS kit"
        assert "box-sizing" in screen.get("*", {}), f"{path.name}: box model left to the host"


def test_no_template_ships_a_ligature_or_private_use_character():
    """What the rules checklist looks for in a finished resume, enforced at the source."""
    for path in _templates():
        for ch in path.read_text(encoding="utf-8"):
            assert not ("\ufb00" <= ch <= "\ufb06"), f"{path.name} contains a ligature glyph {ch!r}"
            assert not ("\ue000" <= ch <= "\uf8ff"), f"{path.name} contains a private-use glyph {ch!r}"


def test_the_catalogue_and_the_template_directory_agree():
    """A template nobody can find is a template nobody uses, and a catalogue row pointing at
    a missing file sends the agent to author its own shell instead."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    on_disk = {p.name for p in _templates()}
    listed = set(re.findall(r"templates/([a-z0-9-]+\.html)", body))
    assert listed == on_disk, f"catalogue lists {sorted(listed)}, disk has {sorted(on_disk)}"


# ── the guides no longer promise what no host could do ────────────────────────────────
STALE = (
    "then export/print to PDF",
    "HTML → PDF via the",
    "Render, look at it, iterate",
    "artifact-rendered HTML → PDF",
    "render these to PDF",
    "Print frame",
    "quirks mode",  # a srcdoc iframe is never in quirks mode (HTML spec; measured for core #3457)
)
READ_FIRST = (
    "skills/job-application-assistant/SKILL.md",
    "skills/job-application-assistant/cv-guide.md",
    "skills/job-application-assistant/cover-letter-guide.md",
    "skills/role-packet/SKILL.md",
    "__init__.py",
    "workflows/apply.yaml",
    "CREDITS.md",
    "README.md",
    "SOUL.md",
)


def test_nothing_the_agent_reads_first_still_promises_a_pdf_the_host_cannot_make():
    """Before browser_pdf there was no host path from an HTML artifact to a PDF file, and no
    way for the agent to see a rendered artifact. Both were stated as fact — in the guides,
    the router, the plugin docstring and the apply workflow's closing line."""
    for rel in (*READ_FIRST, *(str(p.relative_to(ROOT)) for p in _skill_files())):
        text = _flat((ROOT / rel).read_text(encoding="utf-8"))
        for phrase in STALE:
            assert phrase not in text, f"{rel} still says {phrase!r}"


def test_the_guides_now_state_what_is_true_and_hand_off_to_the_resume_skill():
    cv = _flat((ROOT / "skills/job-application-assistant/cv-guide.md").read_text(encoding="utf-8"))
    assert "You do **not** see the rendered page" in cv
    assert "The artifact plugin does not make PDFs" in cv
    assert 'load_skill("resume")' in cv
    letter = _flat((ROOT / "skills/job-application-assistant/cover-letter-guide.md").read_text(encoding="utf-8"))
    assert "`resume` skill's `export.md`" in letter and "doesn't make PDFs" in letter


# ── the resume keeps its bookkeeping out of the operator's profile ─────────────────────
def test_the_resume_skill_never_writes_into_the_operators_profile_sections():
    """`notes` is the operator's own section (setup-coach: targets, NDAs, sensitivities) and it
    flows into careercoach_read_profile("experience") and the shareable profile export. A
    whole-section replace from the model is one slip from deleting it. Ids and paths live in
    the resume's own files; the only profile writes here are fact corrections during import."""
    write_notes = re.compile(r"careercoach_update_profile\(\s*['\"]notes")
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        assert not write_notes.search(text), f"{path.name} writes to the operator's notes"
        if path.name != "import.md":
            assert not re.search(r"mode\s*=\s*['\"]replace", text), f"{path.name} replaces a profile section"
            assert "`notes`" not in text, f"{path.name} keeps resume bookkeeping in the operator's notes"


def test_list_roles_prints_each_role_folders_absolute_path(tools):
    """The resume skill finds a variant's snapshot through its role folder, so the read-only
    lister has to say where that folder is."""
    tools["careercoach_scaffold_role"].invoke({"company": "Acme", "role": "Staff Engineer", "req": "R1"})
    out = tools["careercoach_list_roles"].invoke({})
    line = next(ln for ln in out.splitlines() if "Staff Engineer" in ln)
    folder = Path(line.rsplit(" — ", 1)[-1].strip())
    assert folder.is_absolute() and folder.is_dir() and folder.name == "Staff Engineer - R1", out


def test_a_relative_packet_root_lands_under_home_not_the_servers_cwd(plugin, iso, monkeypatch):
    """The desktop server runs with cwd `/` (read-only), so a relative packet_root such as
    "CareerCoach" raised EROFS in careercoach_init_workspace — the step the master's copy of
    record depends on. A relative value is anchored to the home directory."""
    import importlib

    packet = importlib.import_module(plugin.__name__ + ".packet")
    monkeypatch.chdir("/")
    root = packet.resolve_root("CareerCoach")
    assert root == Path.home() / "CareerCoach" and root.is_dir()


def test_pinning_the_master_is_conditional_on_the_tool_being_there():
    """pin_artifact ships in artifact plugin 0.18.0 (protoAgent #3456), not in a core release yet,
    so every pin step is conditional and the copy of record stays the recovery path without it.
    Only the master is pinned, and a refusal at the pin cap goes to the operator, never an unpin."""
    tailor = _flat((SKILL_DIR / "master-and-tailor.md").read_text(encoding="utf-8"))
    assert "if `pin_artifact` is in your toolset" in tailor
    assert "pin_artifact(<id>)" in tailor and "pin_artifact(<new id>)` if you have it" in tailor
    assert "Pin only the master" in tailor and "rather than unpinning one yourself" in tailor
    assert "A pinned master is exempt from this cap" in tailor
    fm, body = _frontmatter(SKILL_DIR / "SKILL.md")
    assert "pin_artifact" in fm["tools"]
    assert "| Keep the master from being evicted | `pin_artifact` |" in body
