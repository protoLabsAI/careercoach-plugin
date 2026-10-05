"""Greenhouse ATS adapter — fetch a job's PUBLIC question schema to prepare answers (#4032 phase 4a).

Host-free, no live network: ``ats.detect`` + ``ats.greenhouse_fields`` are pure and exercised against
a fixture saved from the real Greenhouse Job Board API (GitLab job 8698330002), and the
``careercoach_prepare_application`` tool is driven with ``ats.fetch_schema`` monkeypatched — so the
suite never calls out. The fixture is the verbatim ``?questions=true`` response, so a change in the
API's shape shows up here rather than in production.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "greenhouse_gitlab_8698330002.json"


@pytest.fixture
def ats(plugin):
    """The plugin's ``ats`` module (pure — no store, no network)."""
    return importlib.import_module(plugin.__name__ + ".ats")


@pytest.fixture
def gitlab_schema():
    """The saved live Greenhouse response for GitLab job 8698330002 (``?questions=true``)."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ── detect: a job URL → {ats, board, job_id} | None ───────────────────────────────────────
def test_detect_recognises_every_greenhouse_url_form(ats):
    # current job-boards host — board token + id from the path
    assert ats.detect("https://job-boards.greenhouse.io/gitlab/jobs/8698330002") == {
        "ats": "greenhouse",
        "board": "gitlab",
        "job_id": "8698330002",
    }
    # older boards host, with a trailing query — still resolved
    assert ats.detect("https://boards.greenhouse.io/gitlab/jobs/8698330002?utm_source=x") == {
        "ats": "greenhouse",
        "board": "gitlab",
        "job_id": "8698330002",
    }
    # a company careers page embedding the job via gh_jid — board token isn't in the URL
    assert ats.detect("https://about.gitlab.com/jobs/apply/?gh_jid=8698330002") == {
        "ats": "greenhouse",
        "board": None,
        "job_id": "8698330002",
    }


def test_detect_returns_none_for_a_non_greenhouse_url(ats):
    assert ats.detect("https://example.com/careers/123") is None
    assert ats.detect("https://jobs.lever.co/acme/abc-123") is None  # Lever is deferred, not detected
    assert ats.detect("") is None
    assert ats.detect(None) is None


# ── greenhouse_fields: the API's questions → browser_form_read field dicts ──────────────────
def test_schema_converts_to_browser_form_read_fields(ats, gitlab_schema):
    fields = ats.greenhouse_fields(gitlab_schema)
    by_name = {f["name"]: f for f in fields}

    # every field carries the browser_form_read shape
    for f in fields:
        assert {"label", "kind", "name", "id", "required", "value", "options"} <= set(f)

    # required flags preserved (Email required, Phone optional)
    assert by_name["email"]["required"] is True and by_name["email"]["kind"] == "text"
    assert by_name["phone"]["required"] is False

    # file field kind — the Resume/CV upload. Its companion paste-text box is NOT independently
    # required (satisfying either input satisfies the question), so it isn't asked about.
    assert by_name["resume"]["kind"] == "file" and by_name["resume"]["required"] is True
    assert by_name["resume_text"]["kind"] == "textarea" and by_name["resume_text"]["required"] is False

    # select options preserved (the 7-way sponsorship question; the Yes/No experience question)
    sponsorship = next(f for f in fields if "sponsorship" in f["label"].lower())
    assert sponsorship["kind"] == "select" and len(sponsorship["options"]) == 7
    yes_no = next(f for f in fields if "3 years" in f["label"])
    assert yes_no["options"] == ["Yes", "No"]

    # the compliance (EEOC) self-ID questions are included
    labels = {f["label"] for f in fields}
    assert {"Gender", "Race", "VeteranStatus"} <= labels


def test_hidden_fields_are_skipped_and_every_type_maps(ats):
    # The live GitLab fixture has no input_hidden / multi_value_multi_select fields, so exercise both
    # the skip and the full type map on a synthetic payload.
    api = {
        "questions": [
            {"label": "Secret token", "required": True, "fields": [{"name": "tok", "type": "input_hidden"}]},
            {
                "label": "Which stacks?",
                "required": True,
                "fields": [
                    {
                        "name": "stacks",
                        "type": "multi_value_multi_select",
                        "values": [{"label": "Backend", "value": 1}, {"label": "Frontend", "value": 2}],
                    }
                ],
            },
            {"label": "Notes", "required": False, "fields": [{"name": "notes", "type": "textarea"}]},
        ]
    }
    fields = ats.greenhouse_fields(api)
    names = {f["name"] for f in fields}
    assert "tok" not in names  # input_hidden dropped entirely
    stacks = next(f for f in fields if f["name"] == "stacks")
    assert stacks["kind"] == "multiselect" and stacks["options"] == ["Backend", "Frontend"]
    assert next(f for f in fields if f["name"] == "notes")["kind"] == "textarea"


def test_greenhouse_fields_tolerates_junk_payloads(ats):
    assert ats.greenhouse_fields(None) == []
    assert ats.greenhouse_fields({}) == []
    assert ats.greenhouse_fields({"questions": "nope"}) == []


# ── careercoach_prepare_application: detect → fetch → build_plan → ask-about list ───────────
def _invoke(tool, **kwargs):
    return asyncio.run(tool.ainvoke(kwargs))


def test_prepare_returns_the_plan_and_only_the_unmapped_required_questions(tools, ats, gitlab_schema, monkeypatch):
    async def fake_fetch(board, job_id):
        assert board == "gitlab" and job_id == "8698330002"
        return gitlab_schema, ""

    monkeypatch.setattr(ats, "fetch_schema", fake_fetch)
    out = _invoke(
        tools["careercoach_prepare_application"],
        url="https://job-boards.greenhouse.io/gitlab/jobs/8698330002",
        company="GitLab",
        role="Fullstack Engineer",
    )
    assert "PREPARATION" in out  # the docstring's contract: this is prep, not verification
    assert "session_id:" in out  # the id for the later read-back verify
    assert "Ask the operator about ONLY these:" in out
    # a genuinely new required free-text question with no confirmed answer is surfaced to ask about
    assert "What is your time split" in out


def test_prepare_on_a_non_greenhouse_url_points_to_the_live_path(tools):
    out = _invoke(tools["careercoach_prepare_application"], url="https://example.com/careers/123")
    assert "isn't a recognised Greenhouse job URL" in out
    assert "careercoach_plan_fill" in out  # the live-form path to use instead
    assert not out.startswith("Could not prepare")  # a readable message, not a caught exception


def test_prepare_on_a_gh_jid_careers_page_cannot_fetch_and_says_so(tools):
    out = _invoke(
        tools["careercoach_prepare_application"], url="https://about.gitlab.com/jobs/apply/?gh_jid=8698330002"
    )
    assert "not the board token" in out and "8698330002" in out
    assert "careercoach_plan_fill" in out  # fall back to the live-form path


def test_prepare_surfaces_a_fetch_error_as_a_readable_message(tools, ats, monkeypatch):
    async def boom(board, job_id):
        return None, (
            f"The Greenhouse job board API returned HTTP 404 for board {board!r} job {job_id!r} — "
            "the job may be closed or the board token may be wrong."
        )

    monkeypatch.setattr(ats, "fetch_schema", boom)
    out = _invoke(
        tools["careercoach_prepare_application"], url="https://job-boards.greenhouse.io/gitlab/jobs/404404404"
    )
    assert "Couldn't prepare from the Greenhouse schema" in out
    assert "HTTP 404" in out
    assert "browser_form_read" in out  # still points to the live-form path
    assert not out.startswith("Could not prepare the application")  # the err path, not an exception


# ── the module is host-free ────────────────────────────────────────────────────────────────
def test_ats_has_no_host_imports():
    src = (Path(__file__).resolve().parent.parent / "ats.py").read_text(encoding="utf-8")
    assert "import graph" not in src and "from graph" not in src
