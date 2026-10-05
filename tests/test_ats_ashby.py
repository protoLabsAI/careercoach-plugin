"""Ashby ATS adapter — URL detection + the live-form plan rules (#4032 phase 4b).

Ashby publishes NO public per-job application-form schema (its posting API returns only job-listing
metadata), so — unlike Greenhouse — the coach never fetches or scrapes an Ashby job: it uses the
live-form path (``browser_form_read`` → ``careercoach_plan_fill`` with ``ats="ashby"``). These tests
are host-free and make NO network call: ``ats.detect`` is pure, the plan rules run against a synthetic
Ashby-shaped ``browser_form_read`` array, and ``careercoach_prepare_application`` is driven with
``ats.fetch_schema`` monkeypatched to FAIL if it is ever called — proving the Ashby path doesn't fetch.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

# A standard Ashby job UUID (the second path segment of a jobs.ashbyhq.com job URL).
JOB_UUID = "4b9c2d1e-7a3f-4c8b-9e2d-1f6a8c3b5d47"


@pytest.fixture
def ats(plugin):
    """The plugin's ``ats`` module (pure — no store, no network)."""
    return importlib.import_module(plugin.__name__ + ".ats")


@pytest.fixture
def formfill(plugin, iso, monkeypatch):
    """The plugin's ``formfill`` module with an isolated store (its own ``CAREERCOACH_DIR``)."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".formfill")


# ── synthetic Ashby form helpers ────────────────────────────────────────────────────────────
def field(label, kind="text", *, name="", id="", required=False, value="", options=None):
    return {
        "label": label,
        "kind": kind,
        "name": name,
        "id": id,
        "required": required,
        "value": value,
        "options": options or [],
    }


def ashby_form():
    """An Ashby-shaped ``browser_form_read`` array: a required résumé FILE, two Yes/No button groups
    (one read as ``radio-group``, one as a generic ``other`` carrying options), and the contact
    scalars. The résumé is listed in the middle on purpose, so the upload-first ordering is exercised."""
    return [
        field("Full name", "text", name="_systemfield_name", id="name", required=True),
        field("Email", "email", name="_systemfield_email", id="email", required=True),
        field("Resume", "file", name="_systemfield_resume", id="_systemfield_resume", required=True),
        field(
            "Are you legally authorized to work in the US?",
            "radio-group",
            name="q_auth",
            id="q_auth",
            required=True,
            options=["Yes", "No"],
        ),
        field(
            "Will you now or in the future require sponsorship?",
            "other",
            name="q_sponsor",
            id="q_sponsor",
            required=True,
            options=["Yes", "No"],
        ),
    ]


# ── detect: an Ashby job URL → {ats, board, job_id} | None ──────────────────────────────────
def test_detect_recognises_both_ashby_url_forms(ats):
    assert ats.detect(f"https://jobs.ashbyhq.com/openai/{JOB_UUID}") == {
        "ats": "ashby",
        "board": "openai",
        "job_id": JOB_UUID,
    }
    # the /application suffix form
    assert ats.detect(f"https://jobs.ashbyhq.com/openai/{JOB_UUID}/application") == {
        "ats": "ashby",
        "board": "openai",
        "job_id": JOB_UUID,
    }
    # a trailing query / different org still resolves board + id from the path
    detected = ats.detect(f"https://jobs.ashbyhq.com/ramp/{JOB_UUID}/application?utm_source=x")
    assert detected == {"ats": "ashby", "board": "ramp", "job_id": JOB_UUID}


def test_a_greenhouse_url_is_not_misdetected_as_ashby(ats):
    gh = ats.detect("https://job-boards.greenhouse.io/gitlab/jobs/8698330002")
    assert gh is not None and gh["ats"] == "greenhouse"  # the hosts are distinct — no cross-detection
    # a bare Ashby board (no job UUID) is not a job URL, and a non-UUID second segment doesn't match
    assert ats.detect("https://jobs.ashbyhq.com/openai") is None
    assert ats.detect("https://jobs.ashbyhq.com/openai/not-a-uuid") is None


# ── build_plan: the Ashby résumé-upload-first + Yes/No rules ────────────────────────────────
def test_resume_upload_row_comes_first_and_is_required(formfill):
    # Nothing confirmed, but the profile CAN render a résumé → the required file becomes an upload row.
    plan = formfill.build_plan(ashby_form(), {}, ats="ashby", resume_ready=True)
    rows = plan["rows"]
    assert rows, "expected at least the résumé upload row"
    first = rows[0]
    assert first["action"] == "upload"
    assert first["required"] is True
    assert first["resume"] is True
    assert first["value"] == formfill.RESUME_UPLOAD_VALUE
    assert first["value"] == "PDF from careercoach_render_resume → browser_pdf"
    # the plan carries a note: upload first, read back every field after (autofill can overwrite).
    assert "UPLOAD THE RÉSUMÉ FIRST" in plan["note"]
    assert "autofill" in plan["note"].lower()


def test_yes_no_button_groups_map_to_select_with_exact_options(formfill):
    confirmed = {"authorized_us": "Yes", "requires_sponsorship": "No"}
    plan = formfill.build_plan(ashby_form(), confirmed, ats="ashby", resume_ready=True)
    by_label = {r["label"]: r for r in plan["rows"]}

    auth = by_label["Are you legally authorized to work in the US?"]  # kind radio-group
    assert auth["action"] == "select" and auth["value"] == "Yes"

    sponsor = by_label["Will you now or in the future require sponsorship?"]  # kind "other" + options
    assert sponsor["action"] == "select" and sponsor["value"] == "No"

    # an answer that is not one of the exact Yes/No options is NEVER fuzzy-matched — it is asked about.
    plan2 = formfill.build_plan(ashby_form(), {"authorized_us": "Yep"}, ats="ashby", resume_ready=True)
    um = next(u for u in plan2["unmapped"] if u["label"] == "Are you legally authorized to work in the US?")
    assert um["options"] == ["Yes", "No"] and um["answer"] == "Yep"


def test_resume_is_unmapped_when_the_profile_cannot_render_one(formfill):
    # resume_ready is False (no renderable profile) → the required résumé is asked about, never guessed.
    plan = formfill.build_plan(ashby_form(), {}, ats="ashby", resume_ready=False)
    assert not any(r.get("resume") for r in plan["rows"]), "no upload row without a renderable résumé"
    resume_um = next(u for u in plan["unmapped"] if u["label"] == "Resume")
    assert resume_um["required"] is True
    assert "résumé" in resume_um["reason"]


def test_without_the_ashby_flag_a_required_file_is_not_auto_uploaded(formfill):
    # The Ashby rule is scoped: a generic plan (no ats) leaves a required file with no answer unmapped,
    # exactly as before — it does not invent a résumé upload.
    plan = formfill.build_plan(ashby_form(), {}, resume_ready=True)
    assert not any(r.get("resume") for r in plan["rows"])
    assert "note" not in plan
    assert any(u["label"] == "Resume" for u in plan["unmapped"])


# ── diff: an autofill overwrite is a mismatch; the uploaded résumé is not ───────────────────
def test_autofill_overwrite_shows_up_in_the_diff(formfill):
    confirmed = {"email": "ada@example.com", "authorized_us": "Yes"}
    plan = formfill.build_plan(ashby_form(), confirmed, ats="ashby", resume_ready=True)

    # Read back AFTER the upload: the résumé landed (a real filename), email is as planned, but Ashby's
    # autofill-from-résumé silently flipped the authorization select to "No".
    after = [
        field("Resume", "file", name="_systemfield_resume", required=True, value="resume-acme.pdf"),
        field("Email", "email", name="_systemfield_email", required=True, value="ada@example.com"),
        field(
            "Are you legally authorized to work in the US?",
            "radio-group",
            name="q_auth",
            required=True,
            value="No",
            options=["Yes", "No"],
        ),
    ]
    mism = formfill.diff(plan, after)
    by_label = {m["label"]: m for m in mism}

    assert "Are you legally authorized to work in the US?" in by_label
    overwrite = by_label["Are you legally authorized to work in the US?"]
    assert overwrite["expected"] == "Yes" and overwrite["actual"] == "No"
    # the uploaded résumé is NOT a mismatch — a file landed, which is all the plan can verify.
    assert "Resume" not in by_label


def test_an_unuploaded_resume_is_caught_by_the_diff(formfill):
    plan = formfill.build_plan(ashby_form(), {}, ats="ashby", resume_ready=True)
    after = [field("Resume", "file", name="_systemfield_resume", required=True, value="")]  # upload failed
    mism = formfill.diff(plan, after)
    assert any(m["label"] == "Resume" for m in mism), "an empty required résumé field must be flagged"


# ── careercoach_prepare_application: Ashby → live-form path, NO network call ─────────────────
def _invoke(tool, **kwargs):
    return asyncio.run(tool.ainvoke(kwargs))


def test_prepare_on_an_ashby_url_uses_the_live_path_and_makes_no_network_call(tools, ats, monkeypatch):
    called: list[tuple] = []

    async def must_not_fetch(board, job_id):
        called.append((board, job_id))
        raise AssertionError("careercoach_prepare_application must not fetch a schema for Ashby")

    monkeypatch.setattr(ats, "fetch_schema", must_not_fetch)
    out = _invoke(
        tools["careercoach_prepare_application"],
        url=f"https://jobs.ashbyhq.com/openai/{JOB_UUID}/application",
        company="OpenAI",
        role="Member of Technical Staff",
    )
    assert called == [], "no network call may be made for an Ashby URL"
    assert "Ashby" in out
    assert "no public per-job" in out.lower()  # names exactly why there's no schema to prepare from
    assert "does NOT scrape" in out
    # names the live-form path steps to use instead
    assert "browser_form_read" in out and "careercoach_plan_fill" in out and "careercoach_verify_fill" in out
    assert 'ats="ashby"' in out


def test_plan_fill_tool_threads_the_ashby_flag_and_renders_the_note(tools):
    # Through the tool with an empty profile: the résumé can't be rendered yet, so it's asked about —
    # but the Ashby plan note (upload first, read back after) is still rendered.
    out = tools["careercoach_plan_fill"].invoke(
        {"form_json": json.dumps(ashby_form()), "company": "OpenAI", "role": "MTS", "ats": "ashby"}
    )
    assert "UPLOAD THE RÉSUMÉ FIRST" in out
    assert "Ask the operator about ONLY these:" in out
    assert "Resume" in out  # the required résumé is surfaced, not invented, with no renderable profile


# ── the module is host-free ────────────────────────────────────────────────────────────────
def test_ats_ashby_detection_is_host_free():
    src = (Path(__file__).resolve().parent.parent / "ats.py").read_text(encoding="utf-8")
    assert "import graph" not in src and "from graph" not in src
