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


# ── extra beats standard; free-text never onto a choice field; decline matching (bugs 5+6) ──
def live_location_question(*, required=True):
    """The GitLab-Greenhouse field that broke: a Yes/No CHOICE whose label contains "location", so it
    classifies to ``current_location`` — and the confirmed location string used to be planned onto it."""
    return field(
        "Do you currently live in this location?",
        "select",
        name="q_live",
        id="q_live",
        required=required,
        options=["Yes", "No"],
    )


def test_extra_answer_beats_a_classified_standard_answer(formfill):
    # r1: the confirmed current_location classifies to this Yes/No field, but the operator's one-off
    # "Yes" must win and the row must record source "extra" — the precedence the bug had backwards.
    confirmed = {"current_location": "Portland, Oregon, USA"}
    extra = {"Do you currently live in this location?": "Yes"}
    plan = formfill.build_plan([live_location_question()], confirmed, extra)
    row = next(r for r in plan["rows"] if r["label"] == "Do you currently live in this location?")
    assert row["value"] == "Yes" and row["source"] == "extra" and row["action"] == "select"
    assert plan["unmapped"] == []
    assert "Portland, Oregon, USA" not in [r.get("value") for r in plan["rows"]]


def test_an_extra_keyed_by_id_or_name_wins_over_the_standard_answer(formfill):
    # r1: extra keyed by the field's id, and by its name, both beat the classified standard answer.
    confirmed = {"current_location": "Portland, Oregon, USA"}
    by_id = formfill.build_plan([live_location_question()], confirmed, {"q_live": "No"})
    row = next(r for r in by_id["rows"] if r.get("id") == "q_live")
    assert row["value"] == "No" and row["source"] == "extra"

    form = [
        field(
            "Do you currently live in this location?",
            "select",
            name="live_q",
            id="x1",
            required=True,
            options=["Yes", "No"],
        )
    ]
    by_name = formfill.build_plan(form, confirmed, {"live_q": "Yes"})
    row = next(r for r in by_name["rows"] if r.get("name") == "live_q")
    assert row["value"] == "Yes" and row["source"] == "extra"


def test_a_free_text_standard_answer_is_never_planned_onto_a_choice_field(formfill):
    # r2: only the confirmed current_location classifies here (no extra). A free-text answer is not a
    # choice value, so the required Yes/No field is asked about — never filled with "Portland, Oregon".
    confirmed = {"current_location": "Portland, Oregon, USA"}
    plan = formfill.build_plan([live_location_question()], confirmed)
    assert plan["rows"] == [], "a free-text standard answer never fills a choice field"
    um = next(u for u in plan["unmapped"] if u["label"] == "Do you currently live in this location?")
    assert um["required"] is True and "free text" in um["reason"]
    assert "Portland, Oregon, USA" not in [r.get("value") for r in plan["rows"]]


def test_an_optional_free_text_on_a_choice_field_is_skipped_not_asked(formfill):
    # r2: the same field, but OPTIONAL — a free-text standard answer leaves it a blank skip row.
    confirmed = {"current_location": "Portland, Oregon, USA"}
    plan = formfill.build_plan([live_location_question(required=False)], confirmed)
    assert plan["unmapped"] == []
    row = next(r for r in plan["rows"] if r["label"] == "Do you currently live in this location?")
    assert row["action"] == "skip" and row["value"] == ""


def test_a_no_option_combobox_with_only_a_free_text_standard_answer_is_unmapped(formfill):
    # r2: a react-select combobox reads back with NO options; its only candidate is the free-text
    # current_location. It must be asked about, not planned verbatim.
    form = [field("Current location", "combobox", name="location", id="location", required=True)]
    plan = formfill.build_plan(form, {"current_location": "Portland, Oregon, USA"})
    assert plan["rows"] == []
    um = next(u for u in plan["unmapped"] if u["target"] == "#location")
    assert um["required"] is True and "free text" in um["reason"]
    assert "Portland, Oregon, USA" not in [r.get("value") for r in plan["rows"]]


def test_a_no_option_select_plans_an_extra_answer_verbatim_and_flags_it(formfill):
    # A combobox with no options takes an operator's EXPLICIT extra verbatim and flags the row
    # options_unknown — the browser_select read-back then guards that the value actually took.
    form = [field("Role level", "combobox", name="level", id="level", required=True)]
    plan = formfill.build_plan(form, {}, {"level": "Senior"})
    row = next(r for r in plan["rows"] if r.get("id") == "level")
    assert row["value"] == "Senior" and row["action"] == "select"
    assert row["source"] == "extra" and row["options_unknown"] is True
    assert plan["unmapped"] == []


def test_a_choice_key_standard_answer_fills_a_no_option_select_verbatim(formfill):
    # A CHOICE-typed standard answer (unlike a free-text one) MAY fill a no-option select: planned
    # verbatim and flagged options_unknown for the read-back to guard.
    form = [field("Are you authorized to work in the US?", "combobox", name="auth", id="auth", required=True)]
    plan = formfill.build_plan(form, {"authorized_us": "Yes"})
    row = next(r for r in plan["rows"] if r.get("id") == "auth")
    assert row["value"] == "Yes" and row["source"] == "standard" and row["options_unknown"] is True


def test_a_plain_fill_records_its_source(formfill):
    # source is recorded on ordinary fill rows too — standard for the classified answer, extra for the one-off.
    plan = formfill.build_plan(greenhouse_form(), {"email": "ada@example.com"}, {"LinkedIn Profile": "x"})
    assert next(r for r in plan["rows"] if r["label"] == "Email")["source"] == "standard"
    assert next(r for r in plan["rows"] if r["label"] == "LinkedIn Profile")["source"] == "extra"


GENDER_OPTIONS = ["Male", "Female", "Non-binary", "Decline To Self Identify"]
VETERAN_OPTIONS = ["I am a veteran", "I am not a protected veteran", "I don't wish to answer"]


def test_a_stored_decline_resolves_to_each_forms_decline_wording(formfill):
    # r4: the seeded "Decline to self-identify" must resolve to "Decline To Self Identify" (gender,
    # via the case/punctuation-folding comparison) and "I don't wish to answer" (veteran, via the
    # decline-equivalence step) — the real wordings these forms use.
    gender_form = [field("Gender", "select", name="gender", id="gender", options=GENDER_OPTIONS)]
    gplan = formfill.build_plan(gender_form, {"gender": "Decline to self-identify"})
    assert gplan["unmapped"] == []
    assert next(r for r in gplan["rows"] if r["key"] == "gender")["value"] == "Decline To Self Identify"

    veteran_form = [field("Veteran Status", "select", name="veteran", id="veteran", options=VETERAN_OPTIONS)]
    vplan = formfill.build_plan(veteran_form, {"veteran_status": "Decline to self-identify"})
    assert vplan["unmapped"] == []
    assert next(r for r in vplan["rows"] if r["key"] == "veteran_status")["value"] == "I don't wish to answer"


def test_several_decline_equivalent_options_are_unmapped_not_guessed(formfill):
    # r4: when MORE THAN ONE option is a decline wording, the stored decline is ambiguous — asked about.
    options = ["Male", "Female", "Prefer not to say", "Decline to answer"]
    form = [field("Gender", "select", name="gender", id="gender", required=True, options=options)]
    plan = formfill.build_plan(form, {"gender": "Decline to self-identify"})
    assert plan["rows"] == []
    um = next(u for u in plan["unmapped"] if u["key"] == "gender")
    assert um["options"] == options and um["answer"] == "Decline to self-identify"


def test_a_decline_with_no_decline_option_is_unmapped(formfill):
    # r4: ZERO decline-equivalent options → unmatched (ask the operator), never a guess.
    options = ["Male", "Female", "Non-binary"]
    form = [field("Gender", "select", name="gender", id="gender", required=True, options=options)]
    plan = formfill.build_plan(form, {"gender": "Decline to self-identify"})
    assert plan["rows"] == []
    assert any(u["key"] == "gender" and u["options"] == options for u in plan["unmapped"])


def test_a_non_decline_answer_absent_from_options_is_never_fuzzy_matched(formfill):
    # r5: a value that is not a decline answer and equals no option is unmapped — no nearest/synonym
    # pick, whatever the source. "Woman" is close to "Female" but must NOT be guessed to it.
    options = ["Male", "Female", "Non-binary"]
    form = [field("Gender", "select", name="gender", id="gender", required=True, options=options)]
    plan = formfill.build_plan(form, {}, {"gender": "Woman"})
    assert plan["rows"] == []
    um = next(u for u in plan["unmapped"] if u["key"] == "gender")
    assert um["answer"] == "Woman" and um["options"] == options


def test_match_option_folds_case_and_punctuation_but_not_synonyms(formfill):
    # The matcher directly: exact, then case/punctuation-folded, then decline-equivalence only.
    assert formfill._match_option("United States", ["united states"]) == "united states"
    assert (
        formfill._match_option("Decline to self-identify", ["Decline To Self Identify"]) == "Decline To Self Identify"
    )
    assert formfill._match_option("Decline to self-identify", ["I don't wish to answer"]) == "I don't wish to answer"
    # not a decline answer, not an exact/normalized match → no pick
    assert formfill._match_option("Woman", ["Female"]) is None
    # a decline value but no decline option → no pick
    assert formfill._match_option("Prefer not to say", ["Male", "Female"]) is None


# ── verify_fill: a decline on an options_unknown self-ID select accepts any decline wording (bug 3) ──
def combobox_gender(*, required=False):
    """The GitLab-Greenhouse shape that broke: a react-select Gender combobox browser_form_read gives
    NO options for, so build_plan carries the stored decline VERBATIM and flags it options_unknown."""
    return field("Gender", "combobox", name="gender", id="gender", required=required)


def test_verify_accepts_any_decline_wording_for_an_options_unknown_select(formfill):
    # r1: the Gender combobox read back with no options, so the plan holds "Decline to self-identify"
    # verbatim and flags options_unknown. A read-back of EITHER real form wording — the
    # case/punctuation-different "Decline To Self Identify" or the wholly different "I don't wish to
    # answer" — verifies clean, with no exact-text or extra_answers workaround.
    plan = formfill.build_plan([combobox_gender()], {"gender": "Decline to self-identify"})
    row = next(r for r in plan["rows"] if r["key"] == "gender")
    assert row["value"] == "Decline to self-identify" and row["options_unknown"] is True

    for wording in ("Decline To Self Identify", "I don't wish to answer"):
        after = [field("Gender", "combobox", name="gender", id="gender", value=wording)]
        assert formfill.diff(plan, after) == [], wording


def test_a_non_decline_or_empty_readback_of_a_decline_select_is_still_a_mismatch(formfill):
    # r2: the equivalence is decline↔decline ONLY. A non-decline wording ("Male") and an empty field
    # each stay mismatches — the self-ID question did NOT end up answered "decline".
    plan = formfill.build_plan([combobox_gender()], {"gender": "Decline to self-identify"})
    for actual in ("Male", ""):
        after = [field("Gender", "combobox", name="gender", id="gender", value=actual)]
        mism = formfill.diff(plan, after)
        assert len(mism) == 1 and mism[0]["actual"] == actual, actual


def test_a_known_option_select_still_requires_the_exact_planned_option(formfill):
    # r3: when the reader DID list options, build_plan resolved the decline to the form's real option
    # and the row is NOT options_unknown — so verification is exact. A DIFFERENT decline wording in the
    # read-back ("I don't wish to answer" where the form's option was "Decline To Self Identify") is a
    # mismatch: the options_unknown tolerance does not apply to a row whose options were known.
    form = [field("Gender", "select", name="gender", id="gender", options=GENDER_OPTIONS)]
    plan = formfill.build_plan(form, {"gender": "Decline to self-identify"})
    row = next(r for r in plan["rows"] if r["key"] == "gender")
    assert row["value"] == "Decline To Self Identify" and "options_unknown" not in row

    after = [
        field("Gender", "select", name="gender", id="gender", value="I don't wish to answer", options=GENDER_OPTIONS)
    ]
    mism = formfill.diff(plan, after)
    assert len(mism) == 1 and mism[0]["actual"] == "I don't wish to answer"


def test_a_non_decline_options_unknown_select_still_mismatches_on_change(formfill):
    # r2: options_unknown does NOT loosen matching for a non-decline value. An operator's one-off
    # "Senior" planned on a no-option combobox must read back exactly — "Junior" is a mismatch.
    form = [field("Role level", "combobox", name="level", id="level", required=True)]
    plan = formfill.build_plan(form, {}, {"level": "Senior"})
    row = next(r for r in plan["rows"] if r.get("id") == "level")
    assert row["options_unknown"] is True
    after = [field("Role level", "combobox", name="level", id="level", required=True, value="Junior")]
    mism = formfill.diff(plan, after)
    assert len(mism) == 1 and mism[0]["expected"] == "Senior" and mism[0]["actual"] == "Junior"


def test_plan_tells_the_filler_to_pick_the_forms_decline_option(tools, answers):
    # r4: the rendered plan line for an options_unknown decline row instructs the filler to pick the
    # form's OWN decline option and says verify_fill accepts any decline wording for the field.
    import json

    answers.propose("gender", "Decline to self-identify")
    answers.confirm(["gender"])
    form = [field("Gender", "combobox", name="gender", id="gender", required=True)]
    out = tools["careercoach_plan_fill"].invoke({"form_json": json.dumps(form), "company": "Acme", "role": "SWE"})
    assert "decline-to-answer option" in out
    assert "accepts any decline wording" in out


# ── same-label file fields: route the résumé, never onto the cover letter ───────────────────
def two_attach_fields(*, resume_required=True, cover_required=False):
    """The GitLab-Greenhouse shape that broke: BOTH file inputs are labelled "Attach"; only the
    name/id (``resume`` vs ``cover_letter``) tells the résumé apart from the cover letter."""
    return [
        field("Attach", "file", name="resume", id="resume", required=resume_required),
        field("Attach", "file", name="cover_letter", id="cover_letter", required=cover_required),
    ]


def test_every_plan_row_carries_an_identity_and_target(formfill):
    # r3: every row carries the field's id/name (when present) and a `target` locator, `#id` preferred.
    confirmed = {"email": "ada@example.com", "linkedin_url": "https://linkedin.com/in/ada"}
    plan = formfill.build_plan(greenhouse_form(), confirmed)
    assert all(r.get("target") for r in plan["rows"]), "every row has a target locator"
    email = next(r for r in plan["rows"] if r["label"] == "Email")
    assert email["id"] == "email" and email["name"] == "email" and email["target"] == "#email"


def test_an_extra_keyed_by_id_routes_the_resume_only_to_the_resume_field(formfill):
    # The live bug: the plan uploaded the résumé to BOTH "Attach" fields. Keyed by id "resume", the
    # answer fills the résumé field ALONE — the cover-letter "Attach" never receives the résumé.
    plan = formfill.build_plan(two_attach_fields(), {}, {"resume": "resume-gitlab.pdf"})

    uploads = [r for r in plan["rows"] if r["action"] == "upload"]
    assert len(uploads) == 1, "exactly one upload — the résumé"
    assert uploads[0]["target"] == "#resume" and uploads[0]["id"] == "resume"
    assert uploads[0]["value"] == "resume-gitlab.pdf"
    assert all(r.get("target") != "#cover_letter" for r in uploads)  # never the cover letter

    # the cover letter is optional here → a blank skip row, never the résumé upload
    cover = next(r for r in plan["rows"] if r.get("target") == "#cover_letter")
    assert cover["action"] == "skip" and cover["value"] == ""
    assert "resume-gitlab.pdf" not in [r.get("value") for r in plan["rows"] if r.get("target") == "#cover_letter"]


def test_an_extra_keyed_by_a_shared_label_fills_none_and_lists_each(formfill):
    # Keyed by the shared label "Attach" (not an id/name): the answer can't be routed to one field, so
    # it fills NEITHER — each look-alike is handed back asking the operator to re-key by id or name.
    plan = formfill.build_plan(
        two_attach_fields(resume_required=True, cover_required=True), {}, {"Attach": "resume-gitlab.pdf"}
    )
    assert not any(r["action"] == "upload" for r in plan["rows"]), "a shared-label answer fills no field"

    attach = [u for u in plan["unmapped"] if u["label"] == "Attach"]
    assert len(attach) == 2
    assert {u["target"] for u in attach} == {"#resume", "#cover_letter"}
    assert all("key the answer by id or name" in u["reason"] for u in attach)


def test_a_shared_label_extra_still_falls_back_to_the_confirmed_standard_answer(formfill):
    # Regression (#23 review): an EXTRA keyed by a label several fields share is ambiguous and can't be
    # routed, so it is dropped — but that ambiguity is the EXTRA's alone. A confirmed standard answer
    # that classifies to the field is unambiguous and must still fill it. The `not shared_label` guard
    # on the confirmed fallback used to silently drop the confirmed answer whenever the field's label
    # happened to be shared.
    form = [
        field("Email", "email", name="email_1", id="email_1", required=True),
        field("Email", "email", name="email_2", id="email_2", required=True),
    ]
    confirmed = {"email": "ada@example.com"}
    extra = {"Email": "typo@example.com"}  # keyed by the shared label → ambiguous, dropped
    plan = formfill.build_plan(form, confirmed, extra)
    assert plan["unmapped"] == [], "a confirmed standard answer fills a shared-label field"
    rows = [r for r in plan["rows"] if r["label"] == "Email"]
    assert len(rows) == 2
    assert all(r["value"] == "ada@example.com" and r["source"] == "standard" for r in rows)
    assert "typo@example.com" not in [r.get("value") for r in plan["rows"]]


def test_a_shared_label_field_with_no_standard_answer_is_still_unmapped(formfill):
    # The other half of the regression: with NO confirmed standard answer to fall back to, a
    # shared-label extra still fills none of the look-alikes — each is asked about, as before.
    form = [
        field("Reference", "text", name="ref_1", id="ref_1", required=True),
        field("Reference", "text", name="ref_2", id="ref_2", required=True),
    ]
    plan = formfill.build_plan(form, {}, {"Reference": "Jane Doe"})
    assert not any(r["action"] != "skip" for r in plan["rows"]), "a shared-label extra fills no field"
    refs = [u for u in plan["unmapped"] if u["label"] == "Reference"]
    assert len(refs) == 2
    assert {u["target"] for u in refs} == {"#ref_1", "#ref_2"}
    assert all("key the answer by id or name" in u["reason"] for u in refs)


def test_a_resume_value_is_never_planned_onto_the_cover_letter_field(formfill):
    # Even if the operator mis-keys the résumé onto the cover-letter field by id, the résumé PDF is
    # DROPPED — that field takes only an explicit cover-letter answer, never the résumé.
    plan = formfill.build_plan(two_attach_fields(cover_required=True), {}, {"cover_letter": "resume-gitlab.pdf"})

    assert all("resume-gitlab.pdf" not in str(r.get("value", "")) for r in plan["rows"])
    cover_um = next(u for u in plan["unmapped"] if u.get("target") == "#cover_letter")
    assert cover_um["required"] is True  # dropped résumé + no real cover letter → asked about


def test_a_greenhouse_resume_file_with_no_answer_is_asked_about_not_auto_filled(formfill):
    # Without the Ashby flag a résumé file field is NEVER auto-uploaded; with no supplied path it is
    # handed back asking for the rendered PDF (the same shape as Ashby's missing-résumé message).
    form = [field("Resume/CV", "file", name="resume", id="resume", required=True)]
    plan = formfill.build_plan(form, {})
    assert plan["rows"] == [], "the résumé is not auto-filled on the Greenhouse path"
    assert not any(r.get("resume") for r in plan["rows"])
    um = next(u for u in plan["unmapped"] if u["target"] == "#resume")
    assert um["required"] is True and "résumé" in um["reason"] and "careercoach_render_resume" in um["reason"]


def combined_resume_cover_field(*, required=True):
    """A SINGLE file field whose label/name/id match BOTH the résumé and the cover-letter patterns —
    the "Resume/Cover Letter" combined input some boards render. The drop-the-résumé guard must NOT
    fire on it: it is a résumé field too, so the résumé is a valid document for it."""
    return [field("Resume/Cover Letter", "file", name="resume_cover", id="resume_cover", required=required)]


def test_a_combined_resume_cover_letter_field_accepts_the_resume(formfill):
    # Regression: a combined field matches both patterns, so the cover-letter guard used to drop the
    # résumé and leave it unfillable. A supplied résumé path must upload to it (keyed by id).
    plan = formfill.build_plan(combined_resume_cover_field(), {}, {"resume_cover": "resume-gitlab.pdf"})
    uploads = [r for r in plan["rows"] if r["action"] == "upload"]
    assert len(uploads) == 1, "the combined field uploads the résumé — it is not dropped"
    assert uploads[0]["target"] == "#resume_cover" and uploads[0]["value"] == "resume-gitlab.pdf"
    assert not any(u.get("target") == "#resume_cover" for u in plan["unmapped"])


def test_a_combined_field_with_no_answer_is_asked_about_as_a_resume_not_dropped(formfill):
    # With no supplied path the combined field is treated as a résumé on the Greenhouse path: asked
    # about (render the PDF), NOT silently dropped to a blank skip row by the cover-letter guard.
    plan = formfill.build_plan(combined_resume_cover_field(), {})
    assert plan["rows"] == [], "the combined résumé field is not left as a blank skip"
    um = next(u for u in plan["unmapped"] if u["target"] == "#resume_cover")
    assert um["required"] is True and "careercoach_render_resume" in um["reason"]


def test_an_optional_combined_field_on_ashby_still_accepts_a_supplied_resume(formfill):
    # Regression (the other half of the guard bug): an OPTIONAL combined field on Ashby had its
    # supplied résumé dropped and silently became a skip row. The résumé must upload instead.
    plan = formfill.build_plan(
        combined_resume_cover_field(required=False),
        {},
        {"resume_cover": "resume-acme.pdf"},
        ats="ashby",
        resume_ready=True,
    )
    uploads = [r for r in plan["rows"] if r["action"] == "upload"]
    assert len(uploads) == 1 and uploads[0]["value"] == "resume-acme.pdf"
    assert not any(r["action"] == "skip" and r.get("target") == "#resume_cover" for r in plan["rows"])


# ── diff: plan vs. the form read back ─────────────────────────────────────────────────────
def test_diff_matches_same_label_file_fields_by_identity(formfill):
    # Both "Attach" fields read back: the résumé landed on #resume, the (optional) cover letter is
    # blank. Each row is compared against its OWN field by id, so there is no mismatch.
    plan = formfill.build_plan(two_attach_fields(), {}, {"resume": "resume-gitlab.pdf"})
    after = [
        field("Attach", "file", name="resume", id="resume", required=True, value="resume-gitlab.pdf"),
        field("Attach", "file", name="cover_letter", id="cover_letter", required=False, value=""),
    ]
    assert formfill.diff(plan, after) == []


def test_diff_flags_the_resume_landing_on_the_cover_letter_field(formfill):
    # The résumé was uploaded to the WRONG field: #resume is empty, #cover_letter holds the résumé.
    # Matching rows to fields by id catches it as a mismatch on #resume — the shared label can't mask it.
    plan = formfill.build_plan(two_attach_fields(), {}, {"resume": "resume-gitlab.pdf"})
    after = [
        field("Attach", "file", name="resume", id="resume", required=True, value=""),
        field("Attach", "file", name="cover_letter", id="cover_letter", required=False, value="resume-gitlab.pdf"),
    ]
    mism = formfill.diff(plan, after)
    assert len(mism) == 1
    assert mism[0]["target"] == "#resume"
    assert mism[0]["expected"] == "resume-gitlab.pdf" and mism[0]["actual"] == ""


def test_diff_catches_a_react_select_reverting_to_afghanistan(formfill):
    form = [field("Phone country", "select", name="phone_country", required=True, options=COUNTRIES)]
    plan = formfill.build_plan(form, {"phone_country": "United States"})
    # the react-select silently reverted to the first option after the fill
    after = [
        field("Phone country", "select", name="phone_country", required=True, value="Afghanistan", options=COUNTRIES)
    ]
    mism = formfill.diff(plan, after)
    assert len(mism) == 1
    # the field has no id, so its target locator is the label itself
    assert mism[0] == {
        "label": "Phone country",
        "target": "Phone country",
        "expected": "United States",
        "actual": "Afghanistan",
    }


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
