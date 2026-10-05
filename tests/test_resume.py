"""Tests for ``resume.py`` and the ``careercoach_render_resume`` tool — host-free.

The résumé is where fabrication breaks in first, so the contract under test is narrow and strict:
the rendered HTML is built from the VERIFIED profile and nothing else, every value is escaped, the
guardrail/private sections never leak into it, and the tool refuses (writing nothing) when the
profile can't honestly back a résumé. The file it writes is self-contained so a ``browser_pdf``
print of the ``file://`` page is faithful.
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


# A complete, well-formed profile. The three sections at the bottom (do_not_claim / stories /
# notes) carry text that MUST NOT reach a résumé; work_auth is an identity field the résumé
# doesn't use. Each marker below is unique, so a test can assert it is present or absent verbatim.
FULL = {
    "identity": {
        "name": "Ada Lovelace",
        "location": "London, UK",
        "work_auth": "UK-citizen-work-authorization",
        "contact": "ada@example.com · +44 20 7946 0000",
        "headlines": "Analyst · Mathematician",
    },
    "sections": {
        "roles": (
            "### Analyst — Analytical Engine\n- Owned the operational notes & tables\n- Wrote-the-first-algorithm"
        ),
        "education": "- BSc Mathematics, University-of-London, 1850",
        "skills": "**Can lead on:** symbolic-computation, note-taking\n**Working:** mechanical engineering",
        "do_not_claim": "NEVER-imply-hands-on-manufacture-of-the-Engine",
        "stories": "## STAR\nSituation: a translation; Result: published-method-marker",
        "notes": "Target research-institutions-marker; NDA-with-the-workshop-marker",
    },
    "updated": "2026-10-04",
}


def _profile(**section_overrides) -> dict:
    """A deep copy of FULL with identity/section overrides (value ``None`` clears the field)."""
    prof = copy.deepcopy(FULL)
    for key, value in section_overrides.items():
        slot = "identity" if key in prof["identity"] else "sections"
        prof[slot][key] = "" if value is None else value
    return prof


# ── render_resume_html: everything on the page comes from the profile ──────────────────
def test_every_rendered_field_comes_from_the_used_profile_fields(resume):
    doc = resume.render_resume_html(FULL)

    # identity the résumé uses
    assert "Ada Lovelace" in doc  # name, as the <h1> and the <title>
    assert "Analyst · Mathematician" in doc  # headlines
    assert "London, UK" in doc and "ada@example.com" in doc  # location + contact, one line

    # the three sections it uses, by their standard headings and their content
    assert "Experience" in doc and "Skills" in doc and "Education" in doc
    assert "Analytical Engine" in doc  # a role heading
    assert "Wrote-the-first-algorithm" in doc  # a role bullet
    assert "University-of-London" in doc  # education
    assert "symbolic-computation" in doc  # skills

    # markdown became real HTML, not literal markers
    assert "<h2>Experience</h2>" in doc
    assert "<li>Wrote-the-first-algorithm</li>" in doc
    assert "<strong>Can lead on:</strong>" in doc and "**" not in doc  # bold, no raw asterisks
    # `&` in a bullet is escaped rather than passed through raw
    assert "Owned the operational notes &amp; tables" in doc


def test_guardrail_and_private_sections_never_appear(resume):
    """do_not_claim / stories / notes are never résumé content — and work_auth isn't used either."""
    doc = resume.render_resume_html(FULL)
    for leaked in (
        "NEVER-imply-hands-on-manufacture-of-the-Engine",  # do_not_claim
        "published-method-marker",  # stories
        "research-institutions-marker",  # notes
        "NDA-with-the-workshop-marker",  # notes
        "UK-citizen-work-authorization",  # work_auth (identity, but not a résumé field)
    ):
        assert leaked not in doc, f"{leaked!r} leaked into the résumé"


def test_markup_in_a_role_is_escaped_not_rendered(resume):
    doc = resume.render_resume_html(_profile(roles="### Role\n- <script>alert('xss')</script> and <b>bold</b>"))
    assert "<script>" not in doc and "<b>bold</b>" not in doc
    assert "&lt;script&gt;alert('xss')&lt;/script&gt;" in doc
    assert "&lt;b&gt;bold&lt;/b&gt;" in doc


def test_the_document_is_self_contained(resume):
    """No external resource URLs — inline CSS only, nothing the browser must fetch — so a
    ``browser_pdf`` print of the ``file://`` page matches what was laid out (r5)."""
    doc = resume.render_resume_html(FULL)  # FULL's contact has no URL in it
    low = doc.lower()
    assert low.startswith("<!doctype html>")  # standalone print must not go quirks mode
    assert "<style>" in low and "</style>" in low  # the CSS is inline
    assert "@page" in low and "letter" in low  # laid out for US Letter (what browser_pdf prints)
    for forbidden in ("<link", "<script", "src=", "url(", "@import", "http"):
        assert forbidden not in low, f"the résumé pulls in an external resource: {forbidden!r}"


# ── missing_for_resume: required gaps are reported, not guessed ────────────────────────
def test_missing_for_resume_reports_only_the_empty_required_fields(resume):
    assert resume.missing_for_resume(FULL) == []
    assert resume.missing_for_resume(_profile(name=None)) == ["name"]
    assert resume.missing_for_resume(_profile(contact=None)) == ["contact"]
    assert resume.missing_for_resume(_profile(roles=None)) == ["roles"]
    # order follows REQUIRED_FIELDS; the optional fields (location, headlines, skills) don't count
    assert resume.missing_for_resume(_profile(name=None, contact=None, roles=None)) == ["name", "contact", "roles"]
    assert resume.missing_for_resume(_profile(location=None, headlines=None, skills=None, education=None)) == []


def test_pdf_name_slugs_the_company(resume):
    assert resume.pdf_name("Acme Corp") == "resume-acme-corp.pdf"
    assert resume.pdf_name("") == "resume.pdf"
    assert resume.pdf_name("  ") == "resume.pdf"


# ── the module honours the host-free contract (r6) ─────────────────────────────────────
def test_resume_module_imports_no_host():
    src = (ROOT / "resume.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+graph(\.|\s|$)", src, re.M), "resume.py imports the host"


# ── the tool: write + next steps, or refuse ────────────────────────────────────────────
def test_tool_writes_the_resume_and_returns_a_file_url_to_an_existing_file(tools, iso):
    tools["careercoach_update_profile"].invoke({"field": "name", "content": "Ada Lovelace"})
    tools["careercoach_update_profile"].invoke({"field": "contact", "content": "ada@example.com"})
    tools["careercoach_update_profile"].invoke(
        {"field": "roles", "content": "### Analyst — Engine\n- Owned the operational notes"}
    )

    out = tools["careercoach_render_resume"].invoke({"company": "Acme Corp"})

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


def test_tool_refuses_and_writes_no_file_when_a_required_field_is_missing(tools, iso):
    # name + contact recorded, but no roles → a résumé can't be built honestly.
    tools["careercoach_update_profile"].invoke({"field": "name", "content": "Ada Lovelace"})
    tools["careercoach_update_profile"].invoke({"field": "contact", "content": "ada@example.com"})

    out = tools["careercoach_render_resume"].invoke({"company": "Acme"})

    assert "Not rendered" in out and "roles" in out
    # the recorded fields are not falsely named as gaps (each gap is reported as `field (label)`)
    assert "name (" not in out and "contact (" not in out
    assert "/setup-coach" in out
    assert not (iso / "ws" / "Resume" / "resume.html").exists(), "nothing must be written on refusal"
