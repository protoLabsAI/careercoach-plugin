"""Tests for ``resume.py`` and the ``careercoach_render_resume`` tool — host-free.

The résumé is where fabrication and privacy leaks break in first, so the contract under test is
narrow and strict: the rendered HTML is built from the operator-approved ``resume`` body and the
operator's ``name`` and NOTHING ELSE — never the coach's working record (``location`` / ``contact`` /
``headlines`` / ``roles`` / ``skills`` / ``education`` / ``do_not_claim`` / ``stories`` / ``notes``),
whose fields carry internal coaching annotations (the 2026-10-05 GitLab PDF leak). Every value is
escaped, the tool refuses (writing nothing) when there is no clean body or an annotation is present,
and the file it writes is self-contained so a ``browser_pdf`` print of the ``file://`` page is
faithful.
"""

from __future__ import annotations

import copy
import importlib
import re
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def resume(plugin):
    """The plugin's host-free ``resume`` module (no store needed for the pure functions)."""
    return importlib.import_module(plugin.__name__ + ".resume")


# A clean, employer-facing résumé body — the ONLY thing (plus the name) a résumé is built from. No
# coaching annotations; the content tokens here use plain spelling so they can be told apart from the
# hyphenated working-record sentinels below.
CLEAN_RESUME = (
    "Headline: Analyst and Mathematician\n"
    "Location: London, United Kingdom\n"
    "Contact: ada-clean@example.com · +44 20 7946 0000 · linkedin.com/in/ada\n"
    "\n"
    "Mathematician and analyst working on symbolic computation.\n"
    "\n"
    "## Experience\n"
    "### Analyst — Analytical Engine · 1842–1843\n"
    "- Owned the operational notes & tables\n"
    "- Published the first algorithm for a general-purpose machine\n"
    "\n"
    "## Skills\n"
    "- **Can lead on:** symbolic computation, data tabulation\n"
    "- **Working:** mechanical drafting\n"
    "\n"
    "## Education\n"
    "- BSc Mathematics, University of London, 1850"
)

# A complete, well-formed profile. The working-record fields carry UNIQUE SENTINEL tokens AND
# realistic annotations modelled on the 2026-10-05 leak — none of which may reach a résumé, because
# the renderer reads only `name` and the clean `resume` body. work_auth is an identity field a résumé
# never uses either.
FULL = {
    "identity": {
        "name": "Ada Lovelace",
        "location": (
            "Remote only — NOT relocating for a job. This is a hard constraint as of 2026-08-17 … "
            "do not re-litigate it. loc-sentinel"
        ),
        "work_auth": "UK-citizen-work-authorization-sentinel",
        "contact": "ada-raw-sentinel@example.com · +44 00 0000 0000",
        "headlines": "Senior Analyst — TENURE RULING applies · headline-sentinel",
    },
    "sections": {
        "roles": (
            "### Staff Analyst — BigCo\n"
            "- ★ TWO DESIGN SYSTEMS shipped, verified in-browser 2026-09-11 · role-sentinel\n"
            "- INHERITED stack, not Josh's architecture call. Do not let him claim otherwise.\n"
            "- DATE RESOLVED 2026-09-11 by Josh: Dec 2024 on ALL surfaces\n"
            "- TENURE NOTE: claim '8+ years' UNCONFIRMED by Josh\n"
            "- Start date confirmed by Josh"
        ),
        "education": (
            "- No college degree … does not hold copies of transcripts · edu-sentinel\n"
            "- POSITIONING: Do not apologise for this"
        ),
        "skills": (
            "- Not evidenced / do not claim: GraphQL. Go. · skills-sentinel\n- Portfolio he can SHOW on request"
        ),
        "do_not_claim": "NEVER-imply-hands-on-manufacture-of-the-Engine · dnc-sentinel",
        "stories": "## STAR\nSituation: a translation; Result: published-method-sentinel",
        "notes": "Target research-institutions-sentinel; NDA-with-the-workshop-sentinel",
        "resume": CLEAN_RESUME,
    },
    "updated": "2026-10-04",
}

# The unique tokens that live only in the working record — none may appear in a rendered résumé.
WORKING_RECORD_SENTINELS = (
    "loc-sentinel",
    "UK-citizen-work-authorization-sentinel",
    "ada-raw-sentinel@example.com",
    "headline-sentinel",
    "role-sentinel",
    "edu-sentinel",
    "skills-sentinel",
    "dnc-sentinel",
    "published-method-sentinel",
    "research-institutions-sentinel",
    "NDA-with-the-workshop-sentinel",
)

# The leak's annotation phrases, as the acceptance criteria name them (case-insensitive).
LEAK_ANNOTATIONS = (
    "do not",
    "date resolved",
    "tenure",
    "★",
    "do not claim",
    "positioning",
    "confirmed by josh",
    "re-litigate",
    "inherited stack",
)


def _profile(**overrides) -> dict:
    """A deep copy of FULL with identity/section overrides (value ``None`` clears the field)."""
    prof = copy.deepcopy(FULL)
    for key, value in overrides.items():
        slot = "identity" if key in prof["identity"] else "sections"
        prof[slot][key] = "" if value is None else value
    return prof


# ── render_resume_html: everything on the page comes from `name` + the `resume` body ────
def test_every_rendered_field_comes_from_the_resume_body(resume):
    doc = resume.render_resume_html(FULL)

    # name (identity) as the <h1> and <title>; everything else is from the clean body
    assert "<h1>Ada Lovelace</h1>" in doc and "Ada Lovelace — Resume" in doc
    assert '<p class="headline">Analyst and Mathematician</p>' in doc  # Headline: header line
    assert "London, United Kingdom" in doc and "ada-clean@example.com" in doc  # Location + Contact line
    assert "Mathematician and analyst working on symbolic computation." in doc  # summary paragraph

    # the body's `## ` sections render as <section><h2>…</h2>, with the label as written
    assert "<h2>Experience</h2>" in doc and "<h2>Skills</h2>" in doc and "<h2>Education</h2>" in doc
    assert "<h3>Analyst — Analytical Engine · 1842–1843</h3>" in doc  # `### ` is a role heading
    assert "<li>Published the first algorithm for a general-purpose machine</li>" in doc  # `-` bullet
    assert "University of London" in doc  # education, from the body

    # markdown became real HTML, not literal markers, and `&` is escaped
    assert "<strong>Can lead on:</strong>" in doc and "**" not in doc
    assert "Owned the operational notes &amp; tables" in doc


def test_r1_no_working_record_sentinel_reaches_the_resume(resume):
    """r1: a profile whose working-record fields all carry unique sentinels renders a résumé that
    contains NONE of them — the renderer reads only `name` and the `resume` body."""
    doc = resume.render_resume_html(FULL)
    for sentinel in WORKING_RECORD_SENTINELS:
        assert sentinel not in doc, f"{sentinel!r} leaked into the résumé"


def test_r2_no_leak_annotation_reaches_the_resume(resume):
    """r2: the working record is full of realistic coaching annotations (the 2026-10-05 leak); none
    of them appears in the rendered HTML, case-insensitively."""
    low = resume.render_resume_html(FULL).lower()
    for phrase in LEAK_ANNOTATIONS:
        assert phrase.lower() not in low, f"{phrase!r} leaked into the résumé"


def test_r3_contact_line_is_exactly_the_bodys_location(resume):
    """r3: the contact line shows exactly the body's `Location:` text and no other location text."""
    prof = _profile(
        location="SOME-OTHER-LOCATION-sentinel",  # working record — must not appear
        resume=(
            "Location: Portland, Oregon · Remote\n"
            "Contact: josh@example.com · linkedin.com/in/josh\n"
            "\n## Experience\n- Did the work"
        ),
    )
    doc = resume.render_resume_html(prof)
    assert '<p class="contact">Portland, Oregon · Remote | josh@example.com · linkedin.com/in/josh</p>' in doc
    assert "Portland, Oregon · Remote" in doc
    assert "SOME-OTHER-LOCATION-sentinel" not in doc


def test_a_headline_renders_and_bare_summary_text_becomes_a_paragraph(resume):
    doc = resume.render_resume_html(
        _profile(resume="Headline: Staff Engineer\nContact: a@b.com\n\nSeasoned builder.\n\n## Experience\n- X")
    )
    assert '<p class="headline">Staff Engineer</p>' in doc
    assert "<p>Seasoned builder.</p>" in doc


def test_markup_in_the_body_is_escaped_not_rendered(resume):
    """r9: the XSS/escape contract, now exercised against the `resume` body."""
    doc = resume.render_resume_html(
        _profile(resume="Contact: a@b.com\n\n## Role\n- <script>alert('xss')</script> and <b>bold</b>")
    )
    assert "<script>" not in doc and "<b>bold</b>" not in doc
    assert "&lt;script&gt;alert('xss')&lt;/script&gt;" in doc
    assert "&lt;b&gt;bold&lt;/b&gt;" in doc


def test_the_document_is_self_contained(resume):
    """r9: no external resource URLs — inline CSS only — so a ``browser_pdf`` print of the
    ``file://`` page matches what was laid out. Exercised against the clean `resume` body."""
    doc = resume.render_resume_html(FULL)  # CLEAN_RESUME's contact has no URL scheme in it
    low = doc.lower()
    assert low.startswith("<!doctype html>")  # standalone print must not go quirks mode
    assert "<style>" in low and "</style>" in low  # the CSS is inline
    assert "@page" in low and "letter" in low  # laid out for US Letter (what browser_pdf prints)
    for forbidden in ("<link", "<script", "src=", "url(", "@import", "http"):
        assert forbidden not in low, f"the résumé pulls in an external resource: {forbidden!r}"


# ── missing_for_resume: required gaps + a résumé with no way to reach the operator ──────
def test_missing_for_resume_reports_name_resume_and_the_contact_line(resume):
    assert resume.missing_for_resume(FULL) == []
    # name present, roles + contact present, but the `resume` body is empty → ["resume"] (r4)
    assert resume.missing_for_resume(_profile(resume=None)) == ["resume"]
    assert resume.missing_for_resume(_profile(name=None)) == ["name"]
    # a body with `## ` sections but no `Contact:` header line → the contact line is missing (r6)
    assert resume.missing_for_resume(_profile(resume="## Experience\n- Did a thing")) == ["contact"]
    # a body with a Contact: line and sections is buildable
    assert resume.missing_for_resume(_profile(resume="Contact: a@b.com\n\n## Experience\n- X")) == []
    # a header-only body (no `## ` section) is not forced to carry a contact line — it's just "resume"-less…
    assert resume.missing_for_resume(_profile(resume="Headline: Engineer")) == []


def test_pdf_name_slugs_the_company(resume):
    assert resume.pdf_name("Acme Corp") == "resume-acme-corp.pdf"
    assert resume.pdf_name("") == "resume.pdf"
    assert resume.pdf_name("  ") == "resume.pdf"


# ── annotation_lines: defense in depth against coach notes in the body ──────────────────
def test_annotation_lines_is_clean_for_a_clean_body(resume):
    assert resume.annotation_lines(_profile(name="Ada Lovelace")) == []


def test_r5_annotation_lines_flags_every_marker(resume):
    """r5: every marker in ANNOTATION_MARKERS is detected on a line of the résumé body."""
    assert resume.ANNOTATION_MARKERS, "the marker list must not be empty"
    for marker in resume.ANNOTATION_MARKERS:
        prof = _profile(resume=f"Contact: a@b.com\n\n## Experience\n- A line that happens to say {marker} here")
        hits = resume.annotation_lines(prof)
        assert any(marker in h.lower() for h in hits), f"marker {marker!r} not flagged"


def test_annotation_lines_scans_the_name_too(resume):
    hits = resume.annotation_lines(
        _profile(name="Ada (TENURE NOTE: unconfirmed)", resume="Contact: a@b.com\n\n## X\n- y")
    )
    assert hits and "tenure note" in hits[0].lower()


def test_annotation_lines_truncates_a_long_line(resume):
    long = "do not " + "x" * 300
    hits = resume.annotation_lines(_profile(resume=f"Contact: a@b.com\n\n## X\n- {long}"))
    assert hits and len(hits[0]) <= 120 and hits[0].endswith("…")


# ── the module honours the host-free contract ──────────────────────────────────────────
def test_resume_module_imports_no_host():
    src = (ROOT / "resume.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+graph(\.|\s|$)", src, re.M), "resume.py imports the host"


# ── the tool: write + next steps, or refuse ─────────────────────────────────────────────
def _seed_clean(tools):
    tools["careercoach_update_profile"].invoke({"field": "name", "content": "Ada Lovelace"})
    tools["careercoach_update_profile"].invoke({"field": "resume", "content": CLEAN_RESUME, "mode": "replace"})


def test_r7_tool_writes_the_resume_and_returns_file_url_and_approval_step(tools, iso):
    _seed_clean(tools)

    out = tools["careercoach_render_resume"].invoke({"company": "Acme Corp"})

    # the approval instruction comes before the browser steps (r7)
    assert "approval before browser_upload" in out
    assert "never been sent to an employer unreviewed" in out

    # the exact three-step pipeline, in order
    assert "browser_open file://" in out
    assert 'browser_pdf("resume-acme-corp.pdf")' in out
    assert "browser_upload" in out
    assert "capture" in out.lower()  # the rationale: browser_upload only reads the capture folder

    # the file:// URL points at a file that exists and holds the rendered résumé
    m = re.search(r"browser_open (file://\S+)", out)
    assert m, out
    local = Path(urllib.parse.unquote(urllib.parse.urlparse(m.group(1)).path))
    assert local.is_file()
    assert local.name == "resume.html" and local.parent.name == "Resume"  # under the workspace root
    doc = local.read_text(encoding="utf-8")
    assert doc.lower().startswith("<!doctype html>") and "Ada Lovelace" in doc
    # and the working record never reaches the file
    assert "ada-raw-sentinel@example.com" not in doc and "ada-clean@example.com" in doc


def test_r4_tool_refuses_and_writes_no_file_when_the_resume_body_is_empty(tools, iso):
    # name + contact + roles recorded, but no approved résumé body → refuse, name `resume`.
    tools["careercoach_update_profile"].invoke({"field": "name", "content": "Ada Lovelace"})
    tools["careercoach_update_profile"].invoke({"field": "contact", "content": "ada@example.com"})
    tools["careercoach_update_profile"].invoke({"field": "roles", "content": "### Analyst — Engine\n- Owned notes"})

    out = tools["careercoach_render_resume"].invoke({"company": "Acme"})

    assert "Not rendered" in out and "resume" in out
    assert 'field="resume"' in out and 'mode="replace"' in out  # how to save it
    assert "approve" in out.lower()  # only after the operator approves
    assert not (iso / "ws" / "Resume" / "resume.html").exists(), "nothing must be written on refusal"


def test_r5_tool_refuses_and_quotes_an_annotation_line(tools, iso):
    _seed_clean(tools)
    # a body that is otherwise valid but carries a coaching annotation on one line
    annotated = "Contact: ada@example.com\n\n## Experience\n- Shipped X (TENURE NOTE: unconfirmed)"
    tools["careercoach_update_profile"].invoke({"field": "resume", "content": annotated, "mode": "replace"})

    out = tools["careercoach_render_resume"].invoke({"company": "Acme"})

    assert "Not rendered" in out
    assert "Shipped X (TENURE NOTE: unconfirmed)" in out  # the offending line is quoted
    assert "only employer-facing text" in out.lower() or "employer-facing" in out.lower()
    assert not (iso / "ws" / "Resume" / "resume.html").exists(), "nothing must be written on refusal"


def test_r6_tool_refuses_when_the_body_has_sections_but_no_contact_line(tools, iso):
    tools["careercoach_update_profile"].invoke({"field": "name", "content": "Ada Lovelace"})
    tools["careercoach_update_profile"].invoke(
        {"field": "resume", "content": "## Experience\n- Owned the notes", "mode": "replace"}
    )

    out = tools["careercoach_render_resume"].invoke({"company": "Acme"})

    assert "Not rendered" in out and "Contact" in out
    assert not (iso / "ws" / "Resume" / "resume.html").exists(), "nothing must be written on refusal"


# ── r8: `resume` is a recognised profile field, and survives the export → import round trip ─
def test_r8_update_profile_stores_the_resume_field(tools, profile):
    msg = tools["careercoach_update_profile"].invoke({"field": "resume", "content": CLEAN_RESUME, "mode": "replace"})
    assert "unknown profile field" not in msg
    assert profile.load_profile()["sections"]["resume"] == CLEAN_RESUME.strip()


def test_r8_resume_survives_export_import_round_trip(profile):
    profile.update_field("name", "Ada Lovelace")
    profile.update_field("resume", CLEAN_RESUME, mode="replace")

    exported = profile.to_markdown(profile.load_profile())
    fields, _unmapped, _notes = profile.parse_experience(exported)

    # the résumé body parses straight back out of the export, unchanged…
    assert "resume" in fields
    assert profile._same(fields["resume"], CLEAN_RESUME)
    # …and re-importing it in append mode is a no-op.
    rows, _pid = profile.plan_import(fields, mode="append", source=exported)
    resume_row = next(r for r in rows if r["field"] == "resume")
    assert resume_row["outcome"] == "unchanged", resume_row


def test_resume_is_a_known_section(profile):
    assert "resume" in profile.SECTIONS
    # it is a normal section — not an always-inject guardrail, not guarded against removal
    assert "resume" not in profile.ALWAYS_INJECT_SECTIONS
    assert "resume" not in profile.GUARDED_SECTIONS
