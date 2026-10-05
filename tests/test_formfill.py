"""Form fill PLAN + read-back DIFF (#4032 phase 3b).

Host-free: the module maps a ``browser_form_read`` array to the operator's *confirmed* standard
answers, hands back the unmapped/unconfirmed fields to ask about, and later diffs the read-back
against the plan. The store lives under ``CAREERCOACH_DIR`` (a temp dir), and the tools round-trip
through ``register()`` with the vendored testkit. Synthetic form JSON is shaped like a Greenhouse
application (one object per field: ``label, kind, name, id, required, value, options``).
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def formfill(plugin, iso, monkeypatch):
    """The plugin's ``formfill`` module, with an isolated store (the same ``CAREERCOACH_DIR`` the
    ``tools`` and ``answers`` fixtures use, so a test may mix the three)."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".formfill")


@pytest.fixture
def answers(plugin, iso, monkeypatch):
    """The plugin's ``answers`` module, sharing ``formfill``'s store."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".answers")


# ── synthetic form helpers ──────────────────────────────────────────────────────────────
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


COUNTRIES = ["Afghanistan", "Albania", "Algeria", "Ireland", "United Kingdom", "United States"]


def greenhouse_form():
    """A Greenhouse-shaped form: the phone COUNTRY select sits AFTER the number (so ordering is
    actually exercised), a file upload, a required free-text question, and an optional one."""
    return [
        field("Email", "email", name="email", id="email", required=True),
        field("Phone", "tel", name="phone", id="phone", required=True),
        field("Phone country", "select", name="phone_country", id="iti-0__country", required=True, options=COUNTRIES),
        field("LinkedIn Profile", "text", name="linkedin", id="linkedin"),
        field("Resume/CV", "file", name="resume", id="resume", required=True),
        field("Why do you want to work here?", "textarea", name="q1", id="q1", required=True),
        field("How did you hear about us?", "text", name="q2", id="q2"),
    ]


# ── classify: form field → a STANDARD_KEYS key, or None ──────────────────────────────────
def test_classify_maps_the_standard_fields(formfill):
    assert formfill.classify(field("Email address", "email")) == "email"
    assert formfill.classify(field("LinkedIn Profile")) == "linkedin_url"
    assert formfill.classify(field("Personal website / portfolio")) == "portfolio_url"
    assert formfill.classify(field("Are you legally authorized to work in the US?")) == "authorized_us"
    assert formfill.classify(field("Will you now or in the future require sponsorship?")) == "requires_sponsorship"
    assert formfill.classify(field("Gender")) == "gender"
    assert formfill.classify(field("Race / Ethnicity")) == "race_ethnicity"
    assert formfill.classify(field("Veteran Status")) == "veteran_status"
    assert formfill.classify(field("Disability Status")) == "disability_status"
    # phone: country (in a phone widget) vs number
    assert formfill.classify(field("Phone country", "select", name="phone_country")) == "phone_country"
    assert formfill.classify(field("Phone", "tel", name="phone")) == "phone_number"


def test_classify_never_guesses_between_two_keys(formfill):
    # matches neither table entry
    assert formfill.classify(field("Favourite colour")) is None
    assert formfill.classify(field("")) is None
    # one question touching TWO keys is ambiguous → None, not a coin-flip
    assert formfill.classify(field("Are you authorized to work, or do you require sponsorship?")) is None


def test_phone_substrings_in_free_text_are_not_phone_fields(formfill):
    # "tel"/"cell" as a SUBSTRING of an ordinary word must not classify as phone — otherwise the
    # confirmed phone number would be typed into a free-text question and reported VERIFIED.
    assert formfill.classify(field("Tell us why you want to join")) is None
    assert formfill.classify(field("Which hotel did you stay at?")) is None
    assert formfill.classify(field("Any miscellaneous notes?")) is None
    assert formfill.classify(field("What is your excellent trait?")) is None
    # a free-text country question (no phone context) is not the dial-code field either
    assert formfill.classify(field("Country of residence")) is None
    # the real phone fields still classify
    assert formfill.classify(field("Telephone", "tel", name="telephone")) == "phone_number"
    assert formfill.classify(field("Mobile number", "tel", name="mobile")) == "phone_number"


def test_a_phone_substring_question_is_not_filled_with_the_phone_number(formfill):
    # The confirmed phone number must not leak into a free-text question that merely says "tell".
    confirmed = {"phone_number": "555-123-4567"}
    form = [field("Tell us why you want to join", "textarea", name="motivation", required=True)]
    plan = formfill.build_plan(form, confirmed)
    assert plan["rows"] == [], "a free-text question is never auto-filled from the phone answer"
    assert "555-123-4567" not in [r.get("value") for r in plan["rows"]]
    assert {u["label"] for u in plan["unmapped"]} == {"Tell us why you want to join"}  # asked, not guessed


# ── build_plan ────────────────────────────────────────────────────────────────────────────
def test_phone_country_row_is_ordered_before_the_number(formfill):
    confirmed = {"phone_number": "555-123-4567", "phone_country": "United States"}
    plan = formfill.build_plan(greenhouse_form(), confirmed)
    keys = [r["key"] for r in plan["rows"]]
    assert "phone_country" in keys and "phone_number" in keys
    assert keys.index("phone_country") < keys.index("phone_number"), "country must be chosen before the number"


def test_plan_values_come_only_from_confirmed_and_extra(formfill):
    confirmed = {
        "email": "ada@example.com",
        "phone_number": "555-123-4567",
        "phone_country": "United States",
        "linkedin_url": "https://linkedin.com/in/ada",
    }
    extra = {"Why do you want to work here?": "I admire the team's work on compilers."}
    plan = formfill.build_plan(greenhouse_form(), confirmed, extra)
    by_label = {r["label"]: r for r in plan["rows"]}

    assert by_label["Email"]["value"] == "ada@example.com" and by_label["Email"]["action"] == "fill"
    assert by_label["Phone country"]["value"] == "United States" and by_label["Phone country"]["action"] == "select"
    assert by_label["Why do you want to work here?"]["value"].startswith("I admire")  # the operator's extra answer
    # Every filled value traces back to a confirmed answer or the extra — nothing invented.
    allowed = set(confirmed.values()) | set(extra.values())
    for r in plan["rows"]:
        if r["action"] != "skip":
            assert r["value"] in allowed


def test_a_file_field_is_an_upload_from_an_extra_answer(formfill):
    plan = formfill.build_plan(greenhouse_form(), {}, {"Resume/CV": "/tmp/ada_lovelace_resume.pdf"})
    row = next(r for r in plan["rows"] if r["label"] == "Resume/CV")
    assert row["action"] == "upload" and row["value"] == "/tmp/ada_lovelace_resume.pdf"


def test_a_select_value_absent_from_options_is_unmapped_not_a_nearest_match(formfill):
    # The confirmed country isn't in this form's option list — the "Afghanistan" failure.
    form = [field("Phone country", "select", name="phone_country", required=True, options=["Afghanistan", "Ireland"])]
    plan = formfill.build_plan(form, {"phone_country": "United States"})
    assert plan["rows"] == [], "no value may be filled when the answer matches no option"
    assert len(plan["unmapped"]) == 1
    um = plan["unmapped"][0]
    assert um["label"] == "Phone country" and um["options"] == ["Afghanistan", "Ireland"]
    assert "Afghanistan" not in [r.get("value") for r in plan["rows"]]  # never the nearest/first option


def test_a_select_value_matches_an_option_case_insensitively(formfill):
    form = [field("Phone country", "select", name="phone_country", required=True, options=COUNTRIES)]
    plan = formfill.build_plan(form, {"phone_country": "united states"})
    assert plan["unmapped"] == []
    assert plan["rows"][0]["value"] == "United States"  # canonical casing from the options


def test_draft_answers_are_excluded_and_listed_as_unconfirmed(formfill, answers):
    answers.propose("phone_number", "555-123-4567")  # a DRAFT — never confirmed
    form = [field("Phone", "tel", name="phone", required=True)]
    plan = formfill.build_plan(form, answers.confirmed())  # confirmed() is empty

    assert plan["rows"] == [], "a draft never fills a form"
    assert [u["key"] for u in plan["unconfirmed"]] == ["phone_number"]
    assert not any(u.get("key") == "phone_number" for u in plan["unmapped"])  # a draft is confirm-me, not unmapped
    assert "555-123-4567" not in [r.get("value") for r in plan["rows"]]


def test_optional_unmapped_is_skipped_and_required_unmapped_is_listed(formfill):
    plan = formfill.build_plan(greenhouse_form(), {})  # nothing confirmed, no extras

    skips = {r["label"] for r in plan["rows"] if r["action"] == "skip"}
    assert "How did you hear about us?" in skips  # optional + no answer → skip, left blank

    unmapped_labels = {u["label"] for u in plan["unmapped"]}
    assert {"Email", "Why do you want to work here?"} <= unmapped_labels  # required + no answer → ask
    # Nothing was invented for a required field.
    assert all(r["value"] == "" for r in plan["rows"] if r["action"] == "skip")
    assert not any(r["label"] == "Email" for r in plan["rows"])


# ── diff: plan vs. the form read back ─────────────────────────────────────────────────────
def test_diff_catches_a_react_select_reverting_to_afghanistan(formfill):
    form = [field("Phone country", "select", name="phone_country", required=True, options=COUNTRIES)]
    plan = formfill.build_plan(form, {"phone_country": "United States"})
    # the react-select silently reverted to the first option after the fill
    after = [
        field("Phone country", "select", name="phone_country", required=True, value="Afghanistan", options=COUNTRIES)
    ]
    mism = formfill.diff(plan, after)
    assert len(mism) == 1
    assert mism[0] == {"label": "Phone country", "expected": "United States", "actual": "Afghanistan"}


def test_diff_ignores_phone_formatting_differences(formfill):
    form = [field("Phone", "tel", name="phone", required=True)]
    plan = formfill.build_plan(form, {"phone_number": "555-123-4567"})
    after = [field("Phone", "tel", name="phone", required=True, value="(555) 123-4567")]
    assert formfill.diff(plan, after) == []  # same digits, different punctuation


def test_diff_compares_a_file_by_basename(formfill):
    form = [field("Resume/CV", "file", name="resume", required=True)]
    plan = formfill.build_plan(form, {}, {"Resume/CV": "/tmp/ada_resume.pdf"})
    after = [field("Resume/CV", "file", name="resume", required=True, value="ada_resume.pdf")]
    assert formfill.diff(plan, after) == []  # the browser reports the basename, not the full path


def test_diff_reports_a_required_field_left_empty(formfill):
    form = [field("Email", "email", name="email", required=True)]
    plan = formfill.build_plan(form, {"email": "ada@example.com"})
    after = [field("Email", "email", name="email", required=True, value="")]  # never got filled
    mism = formfill.diff(plan, after)
    assert len(mism) == 1 and mism[0]["label"] == "Email" and mism[0]["actual"] == ""


# ── record_verification: verified only when the latest diff is empty ───────────────────────
def test_an_empty_diff_verifies_and_a_later_mismatch_unverifies(formfill):
    form = [field("Phone country", "select", name="phone_country", required=True, options=COUNTRIES)]
    plan = formfill.build_plan(form, {"phone_country": "United States"})
    sid = plan["session_id"]

    good = [
        field("Phone country", "select", name="phone_country", required=True, value="United States", options=COUNTRIES)
    ]
    formfill.record_verification(sid, formfill.diff(plan, good))
    assert formfill.is_verified(sid) is True

    bad = [
        field("Phone country", "select", name="phone_country", required=True, value="Afghanistan", options=COUNTRIES)
    ]
    rec = formfill.record_verification(sid, formfill.diff(plan, bad))
    assert formfill.is_verified(sid) is False
    assert rec["last_diff"] and rec["last_diff"][0]["actual"] == "Afghanistan"


def test_rebuilding_a_plan_resets_verification(formfill):
    # A verified session that is re-planned — even the identical form, which hashes to the same id —
    # comes back UNVERIFIED: verification describes one fill-then-read-back cycle, so the browser must
    # be filled and read back again before the plan counts as verified. This is what stops a re-plan
    # from being submitted without a fresh read-back. Creation time survives the rewrite.
    form = [field("Email", "email", name="email", required=True)]
    plan = formfill.build_plan(form, {"email": "ada@example.com"})
    sid = plan["session_id"]
    formfill.record_verification(sid, [])
    assert formfill.is_verified(sid) is True
    created = formfill.load_session(sid)["created"]

    again = formfill.build_plan(form, {"email": "ada@example.com"})
    assert again["session_id"] == sid  # identical rows → same session id
    assert formfill.is_verified(sid) is False  # but the prior verification was cleared
    rec = formfill.load_session(sid)
    assert rec["last_diff"] is None and rec["verified_at"] == "" and rec["created"] == created


# ── verification is EXCLUSIVE to the newest plan / read-back ────────────────────────────────
def test_planning_a_different_form_clears_a_prior_sessions_verification(formfill):
    # The bd-4t17 hole: a grant for form A must not survive planning form B. Verification is
    # exclusive to the newest plan, so planning B (different rows → different session id) strips
    # A's verified flag in the same write, even though A was never re-planned or read back.
    form_a = [field("Email", "email", name="email", required=True)]
    plan_a = formfill.build_plan(form_a, {"email": "ada@example.com"})
    sid_a = plan_a["session_id"]
    formfill.record_verification(sid_a, [])  # A is now verified
    assert formfill.is_verified(sid_a) is True

    form_b = [field("Phone", "tel", name="phone", required=True)]
    plan_b = formfill.build_plan(form_b, {"phone_number": "555-123-4567"})
    sid_b = plan_b["session_id"]
    assert sid_b != sid_a, "a different form must hash to a different session id"

    assert formfill.is_verified(sid_a) is False, "planning B revoked A's verification"
    assert formfill.is_verified(sid_b) is False, "a freshly planned B is unverified"
    # A's last read-back result is untouched — it just no longer authorizes a submit.
    assert formfill.load_session(sid_a)["last_diff"] == []


def test_verifying_b_leaves_a_unverified(formfill):
    # Verifying form B marks B verified and keeps A false — the two can never both be verified.
    plan_a = formfill.build_plan([field("Email", "email", name="email", required=True)], {"email": "ada@example.com"})
    sid_a = plan_a["session_id"]
    plan_b = formfill.build_plan([field("Phone", "tel", name="phone", required=True)], {"phone_number": "555-0100"})
    sid_b = plan_b["session_id"]

    formfill.record_verification(sid_b, [])
    assert formfill.is_verified(sid_b) is True
    assert formfill.is_verified(sid_a) is False


def test_at_most_one_session_is_verified_after_any_sequence(formfill):
    def verified_count():
        return sum(1 for rec in formfill.load_sessions_checked()[0].values() if rec.get("verified"))

    forms = {
        "a": [field("Email", "email", name="email", required=True)],
        "b": [field("Phone", "tel", name="phone", required=True)],
        "c": [field("LinkedIn Profile", "text", name="linkedin")],
    }
    sids = {k: formfill.build_plan(f, {}, {f[0]["label"]: "x"})["session_id"] for k, f in forms.items()}
    assert len(set(sids.values())) == 3  # three distinct sessions

    # A sequence of plans and verifications: after every step, never more than one is verified.
    formfill.record_verification(sids["a"], [])
    assert verified_count() == 1 and formfill.is_verified(sids["a"])

    formfill.record_verification(sids["b"], [])
    assert verified_count() == 1 and formfill.is_verified(sids["b"]) and not formfill.is_verified(sids["a"])

    formfill.build_plan(forms["c"], {}, {forms["c"][0]["label"]: "x"})  # re-planning C unverifies everything
    assert verified_count() == 0

    formfill.record_verification(sids["c"], [])
    formfill.record_verification(sids["c"], [{"label": "x", "expected": "y", "actual": "z"}])  # a mismatch
    assert verified_count() == 0  # a non-empty diff unverifies C and touches nothing else


def test_a_different_application_with_identical_fields_gets_a_distinct_session(formfill):
    # The bd-4t17 COLLISION: form B has the SAME fields and answers as the approved form A but is a
    # DIFFERENT application. The session id folds in the operator-facing company/role, so B (different
    # company, or different role) never collides onto A's id — verifying B clears A instead of reviving
    # it. If the id were the rows hash alone, A and B would share a slot and a clean read-back of B
    # would re-verify A, letting A's live grant through on a form the operator never saw.
    rows = [field("Email", "email", name="email", required=True)]
    confirmed = {"email": "ada@example.com"}
    sid_a = formfill.build_plan(rows, confirmed, company="Acme", role="ML Engineer")["session_id"]
    sid_company = formfill.build_plan(rows, confirmed, company="Globex", role="ML Engineer")["session_id"]
    sid_role = formfill.build_plan(rows, confirmed, company="Acme", role="Staff Engineer")["session_id"]
    assert len({sid_a, sid_company, sid_role}) == 3  # same fields/answers, three distinct applications

    formfill.record_verification(sid_a, [])
    assert formfill.is_verified(sid_a) is True
    # Reading back the different-company form must NOT re-verify A; it clears A and verifies only itself.
    formfill.record_verification(sid_company, [])
    assert formfill.is_verified(sid_company) is True
    assert formfill.is_verified(sid_a) is False


def test_blank_identity_postings_with_identical_fields_get_distinct_sessions(formfill):
    # The bd-ywmm.4 residue the review caught: careercoach_prepare_application defaults company and
    # role to "" but always has the posting URL in hand. If the id hashed only rows+company+role, two
    # DIFFERENT postings with identical questions and blank company/role would collide onto one slot,
    # and a clean read-back of B would re-verify A — letting A's live grant through on a form the
    # operator never saw. Folding the posting into the id keeps them distinct even with blank identity.
    rows = [field("Email", "email", name="email", required=True)]
    confirmed = {"email": "ada@example.com"}
    sid_a = formfill.build_plan(rows, confirmed, posting="https://job-boards.greenhouse.io/acme/jobs/1")["session_id"]
    sid_b = formfill.build_plan(rows, confirmed, posting="https://job-boards.greenhouse.io/globex/jobs/2")["session_id"]
    assert sid_a != sid_b, "same rows + blank company/role but a different posting must not collide"

    formfill.record_verification(sid_a, [])
    assert formfill.is_verified(sid_a) is True
    # A clean read-back of the OTHER posting must not revive A: it clears A and verifies only itself.
    formfill.record_verification(sid_b, [])
    assert formfill.is_verified(sid_b) is True
    assert formfill.is_verified(sid_a) is False


def test_re_planning_the_same_application_keeps_its_id(formfill):
    # The flip side of the collision fix: re-planning the SAME application (same company, role and
    # rows) keeps the id, so _save_session resets exactly that session's verification — the existing
    # re-plan-resets-verification guard still holds once company/role are part of the id.
    rows = [field("Email", "email", name="email", required=True)]
    first = formfill.build_plan(rows, {"email": "ada@example.com"}, company="Acme", role="ML Engineer")
    sid = first["session_id"]
    formfill.record_verification(sid, [])
    assert formfill.is_verified(sid) is True

    again = formfill.build_plan(rows, {"email": "ada@example.com"}, company="Acme", role="ML Engineer")
    assert again["session_id"] == sid  # identical application → same id
    assert formfill.is_verified(sid) is False  # and its verification was reset by the re-plan


# ── the module is host-free ────────────────────────────────────────────────────────────────
def test_formfill_has_no_host_imports():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "formfill.py").read_text(encoding="utf-8")
    assert "import graph" not in src and "from graph" not in src


# ── the tools round-trip through register() and never raise ────────────────────────────────
def test_malformed_json_gives_a_readable_error(tools):
    import json

    out = tools["careercoach_plan_fill"].invoke({"form_json": "{not valid", "company": "Acme", "role": "SWE"})
    assert "could not be parsed" in out and "browser_form_read" in out  # a readable string, not a traceback

    out = tools["careercoach_plan_fill"].invoke(
        {"form_json": "[]", "company": "Acme", "role": "SWE", "extra_answers": "{oops"}
    )
    assert "extra_answers could not be parsed" in out

    out = tools["careercoach_verify_fill"].invoke({"session_id": "nope", "form_json": "[]"})
    assert "No fill plan found" in out  # an unknown session is caught before anything else

    # a real session, but garbage read-back JSON → the parse guard, still never a traceback
    plan = tools["careercoach_plan_fill"].invoke(
        {"form_json": json.dumps([field("Email", "email", name="email", required=True)]), "company": "A", "role": "R"}
    )
    import re

    sid = re.search(r"session_id:\s*(\S+)", plan).group(1)
    out = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": "nope"})
    assert "could not be parsed" in out


def test_plan_and_verify_tools_round_trip(tools, answers):
    import json
    import re

    for key, value in {
        "email": "ada@example.com",
        "phone_number": "555-123-4567",
        "phone_country": "United States",
    }.items():
        answers.propose(key, value)
    answers.confirm(["email", "phone_number", "phone_country"])

    form = [
        field("Email", "email", name="email", required=True),
        field("Phone", "tel", name="phone", required=True),
        field("Phone country", "select", name="phone_country", required=True, options=COUNTRIES),
        field("Why do you want to work here?", "textarea", name="q1", required=True),
    ]
    out = tools["careercoach_plan_fill"].invoke({"form_json": json.dumps(form), "company": "Acme", "role": "SWE"})
    assert "ada@example.com" in out and "United States" in out
    assert "Ask the operator about ONLY these:" in out
    assert "Why do you want to work here?" in out  # required, no confirmed answer → asked about

    sid = re.search(r"session_id:\s*(\S+)", out).group(1)

    filled = [
        field("Email", "email", name="email", required=True, value="ada@example.com"),
        field("Phone", "tel", name="phone", required=True, value="(555) 123-4567"),
        field("Phone country", "select", name="phone_country", required=True, value="United States", options=COUNTRIES),
        field("Why do you want to work here?", "textarea", name="q1", required=True, value="Because compilers."),
    ]
    verified = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": json.dumps(filled)})
    assert verified.startswith("VERIFIED")

    wrong = [dict(f) for f in filled]
    wrong[2]["value"] = "Afghanistan"  # the country reverted
    out = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": json.dumps(wrong)})
    assert out.startswith("NOT verified") and "Phone country" in out and "Afghanistan" in out
