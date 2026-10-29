"""Grounding post-check (#71): flag draft figures the operator's record doesn't support.

A drafting pass can invent a hard number ("99.9% uptime" with no uptime figure in the profile) or
invert a real one ("cut failed runs by ~70%" → "had a 70% failure rate"). ``grounding.py`` is the
pure check; ``careercoach_read_sources(source="check", text=…)`` is its read-only agent surface.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def grounding(plugin):
    """The plugin's host-free ``grounding`` module (fresh per test — ``load_plugin`` purges it)."""
    return importlib.import_module(plugin.__name__ + ".grounding")


def _kinds(flags):
    return [(f.kind, f.value) for f in flags]


# ── r1: an invented figure is unsupported; a [fill in: …] placeholder is ignored ─────────────────
def test_unsupported_figure_is_flagged(grounding):
    sources = "Staff Engineer at Acme. Led a team of 6 engineers. Skills: Python, Go."
    flags = grounding.check_draft("Maintained 99.9% uptime across services.", sources)
    assert _kinds(flags) == [("unsupported", "99.9%")]


def test_fill_in_placeholder_is_not_flagged(grounding):
    sources = "Staff Engineer at Acme. Led a team of 6 engineers."
    # The placeholder carries a stray "%" and even digits — none of it may be read as a figure.
    assert grounding.check_draft("Maintained [fill in: uptime %] uptime.", sources) == []
    assert grounding.check_draft("Shipped [fill in: 3 features] last quarter.", sources) == []


# ── r2: a figure read the wrong way is a context-mismatch; read the right way, it is clean ────────
def test_inverted_direction_is_a_context_mismatch(grounding):
    sources = "cut failed runs by ~70%"
    flags = grounding.check_draft("had a 70% failure rate", sources)
    assert _kinds(flags) == [("context-mismatch", "70%")]
    assert "cut failed runs by ~70%" in flags[0].source_context


def test_matching_direction_is_clean(grounding):
    sources = "cut failed runs by ~70%"
    assert grounding.check_draft("reduced failed runs by 70%", sources) == []


# ── r3: value normalisation, and the always-ignored numbers ──────────────────────────────────────
def test_tilde_and_thousands_separator_normalise(grounding):
    assert grounding.extract_figures("~70%")[0].normalized == "70%"
    assert grounding.extract_figures("1,200")[0].normalized == "1200"
    # and they therefore MATCH the plain forms, so nothing is flagged
    assert grounding.check_draft("we hit 70% coverage", "achieved ~70% coverage") == []
    assert grounding.check_draft("served 1200 requests", "handled 1,200 requests") == []


def test_standalone_years_and_list_numbering_are_not_flagged(grounding):
    sources = "Staff Engineer at Acme."  # deliberately holds none of these numbers
    assert grounding.check_draft("Joined in 2021 and left in 2024.", sources) == []
    assert grounding.check_draft("1. Improve onboarding\n2) Reduce churn", sources) == []


def test_extract_covers_money_multiplier_duration_and_range(grounding):
    got = {
        f.kind: f.normalized for f in grounding.extract_figures("saved $5M, sped it up 3x in 45 minutes (10-20 runs)")
    }
    assert got == {"money": "$5m", "multiplier": "3x", "duration": "45min", "range": "10-20"}


def test_render_flags_is_plain_words(grounding):
    assert grounding.render_flags([]) == "No unsupported figures."
    out = grounding.render_flags(grounding.check_draft("had a 70% failure rate", "cut failed runs by ~70%"))
    assert "Check the direction: 70%" in out
    assert "cut failed runs by ~70%" in out


# ── r4: the read-only agent surface returns the plain-words flags and writes nothing ──────────────
def _snapshot(root):
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_read_sources_check_reports_and_writes_nothing(tools, profile, iso):
    profile.update_field("roles", "### Staff Engineer, Acme\n- cut failed runs by ~70%", mode="replace")
    rs = tools["careercoach_read_sources"]

    before = _snapshot(iso)
    inverted = rs.invoke({"source": "check", "text": "had a 70% failure rate"})
    unsupported = rs.invoke({"source": "check", "text": "Maintained 99.9% uptime."})
    clean = rs.invoke({"source": "check", "text": "reduced failed runs by 70%"})

    assert "Check the direction: 70%" in inverted
    assert "Not found in your profile: 99.9%" in unsupported
    assert clean == "No unsupported figures."
    # read-only: the check touched no file on disk
    assert _snapshot(iso) == before


def test_read_sources_still_rejects_an_unknown_source(tools):
    # adding "check" must not turn a genuinely unknown source into a silent read
    assert "Unknown source" in tools["careercoach_read_sources"].invoke({"source": "confirm"})


@pytest.mark.parametrize(
    "heading",
    ["Lines never to claim on their behalf", "Guardrails (things NOT to claim)", "do_not_claim"],
)
def test_guardrail_sections_are_not_evidence(grounding, heading):
    sources = (
        "## Skills\nBuilt 12 services.\n"
        f"## {heading}\nNever claim 99.9% uptime.\n"
        "### More restrictions\nNever claim $5M savings.\n"
        "## Story: delivery\nShipped 8 projects."
    )
    assert _kinds(grounding.check_draft("99.9% uptime and $5M savings", sources)) == [
        ("unsupported", "99.9%"),
        ("unsupported", "$5m"),
    ]
    assert grounding.check_draft("Built 12 services and shipped 8 projects", sources) == []


@pytest.mark.parametrize(
    "source",
    [
        "Led a team of 6 engineers and set up CI",
        "Led a team of 6 engineers and wrote down notes",
        "Led a team of 6 engineers with more responsibilities",
        "Led a team of 6 engineers with less supervision",
    ],
)
def test_incidental_words_do_not_give_counts_a_direction(grounding, source):
    assert grounding.check_draft("Led a team of 6 engineers", source) == []


@pytest.mark.parametrize("reverse", [False, True])
def test_neutral_source_occurrence_suppresses_direction_warning(grounding, reverse):
    sources = [
        "Reduced failed runs by 70%.",
        "The profile also records an independently verified measurement for coverage: 70%. This was measured across all production services.",
    ]
    if reverse:
        sources.reverse()
    assert grounding.check_draft("Had 70% coverage", "\n\n".join(sources)) == []


def test_opposite_direction_still_flags(grounding):
    assert _kinds(grounding.check_draft("Increased failures by 70%", "Cut failures by 70%")) == [
        ("context-mismatch", "70%")
    ]


def test_money_suffixes_and_identifiers_have_word_boundaries(grounding):
    assert [f.normalized for f in grounding.extract_figures("$40 monthly, $5 by Q3; S3 EC2")] == [
        "$40",
        "$5",
    ]
    assert _kinds(grounding.check_draft("Spent $40 monthly", "Spent $40 million")) == [("unsupported", "$40")]


def test_check_excludes_real_profile_and_story_bank_guardrails(tools, profile, iso):
    profile.update_field("do_not_claim", "Never claim 99.9% uptime", mode="replace")
    profile.update_field("skills", "Built 12 services", mode="replace")
    tools["careercoach_write_profile"].invoke(
        {
            "doc": "story-bank",
            "content": (
                "## Guardrails (things NOT to claim)\nNever claim 45 minutes recovery.\n"
                "### Detail\nNever claim $5M savings.\n"
                "## Story: delivery\nShipped 8 projects."
            ),
        }
    )
    rs = tools["careercoach_read_sources"]
    before = _snapshot(iso)
    out = rs.invoke({"source": "check", "text": "99.9% uptime, 45 minutes recovery, $5M savings"})
    for value in ("99.9%", "45min", "$5m"):
        assert f"Not found in your profile: {value}" in out
    assert rs.invoke({"source": "check", "text": "Built 12 services; shipped 8 projects"}) == (
        "No unsupported figures."
    )
    assert _snapshot(iso) == before
    assert "99.9%" in rs.invoke({"source": "profile", "field": "do_not_claim"})


@pytest.mark.parametrize("text", [None, "", "   "])
def test_check_requires_a_draft(tools, text):
    args = {"source": "check"}
    if text is not None:
        args["text"] = text
    assert "No draft given" in tools["careercoach_read_sources"].invoke(args)
