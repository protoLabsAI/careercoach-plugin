"""Auto-apply readiness — the findings of the 2026-11-08 live eval on 13 real postings.

Fixtures under ``fixtures/eval_2026_11/`` are the ``browser_form_read`` arrays read back from those
forms AFTER the agent filled them (nothing was submitted; the persona is a test one). A "before"
read is the same array with the values blanked, which is what the live forms showed when opened.

A  values outside the plan block VERIFIED, the approval card shows the full read-back, and legal /
   eligibility / attestation questions are never answered from anything but the operator.
B  verify accepts what a widget legitimately commits: a unique-prefix option, a checkbox, a location
   in the widget's spelling, a city derived from the visible address.
C  a bare Enter on the core-gate path defers to the core's resolved verdict.
D  a Greenhouse "Country" picker right before the phone is the phone country.
E  a gh_jid careers page resolves to Greenhouse's embeddable form.
F  name answer keys.
G  a location combobox plans as an autocomplete select.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eval_2026_11"


@pytest.fixture
def formfill(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".applying.formfill")


@pytest.fixture
def answers(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".record.answers")


@pytest.fixture
def ats(plugin):
    return importlib.import_module(plugin.__name__ + ".applying.ats")


@pytest.fixture
def submitgate(plugin):
    return importlib.import_module(plugin.__name__ + ".applying.submitgate")


def after(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["fields"]


def blank(fields: list[dict], keep: tuple[str, ...] = ()) -> list[dict]:
    """The form as opened: every value cleared (checkboxes unticked), except fields whose label is in
    ``keep`` (a value the page itself pre-filled)."""
    out = copy.deepcopy(fields)
    for f in out:
        if f.get("label") in keep:
            continue
        f["value"] = False if f.get("kind") == "checkbox" else ""
    return out


def field(label, kind="text", *, name="", id="", required=False, value="", options=None):
    return {"label": label, "kind": kind, "name": name, "id": id, "required": required, "value": value,
            "options": options or []}  # fmt: skip


def set_value(fields, label, value):
    for f in fields:
        if f.get("label") == label:
            f["value"] = value
            return fields
    raise AssertionError(label)


SUPABASE_CONFIRMED = {
    "full_name": "Testy McTestface",
    "email": "testy.mctestface@example.com",
    "linkedin_url": "https://www.linkedin.com/in/testy-mctestface-example",
}


# ── A: nothing outside the plan reaches VERIFIED ─────────────────────────────────────────────
def test_supabase_unconfirmed_values_block_verified(formfill):
    """The eval run came back VERIFIED with an "over 18" Yes clicked unasked (the UUID checkbox) and a
    passport country nobody confirmed. Both are now mismatches."""
    filled = after("as3-supabase")
    plan = formfill.build_plan(blank(filled), SUPABASE_CONFIRMED, ats="ashby", company="Supabase", role="AI")
    mismatches = formfill.diff(plan, filled)
    unplanned = {m["label"]: m for m in mismatches if m.get("unplanned")}
    assert "Passport Country" in unplanned
    assert "27cb2e41-18c5-45f9-a60e-bfe732273692" in unplanned  # the over-18 Yes, read as a checkbox
    assert unplanned["27cb2e41-18c5-45f9-a60e-bfe732273692"]["actual"] == "true"
    assert "Where did you hear about this vacancy?" in unplanned  # a skip row that now holds a value
    assert "legal" in unplanned["Passport Country"]["reason"]  # the sensitive explanation rides along
    assert "g-recaptcha-response" not in {m["label"] for m in mismatches}  # the page's own token field


def test_supabase_verifies_once_every_value_is_the_operators(formfill):
    filled = after("as3-supabase")
    extra = {
        f["id"] or f["label"]: ("true" if f["kind"] == "checkbox" else f["value"])
        for f in filled
        if f["kind"] != "file" and f["value"] and f["label"] not in {"Name", "Email", "Linkedin"}
    }
    extra["27cb2e41-18c5-45f9-a60e-bfe732273692"] = "yes"  # the operator said they're over 18
    plan = formfill.build_plan(
        blank(filled), SUPABASE_CONFIRMED, extra=extra, ats="ashby", company="Supabase", role="AI"
    )
    mismatches, readback = formfill.review(plan, filled)
    assert mismatches == []
    passport = next(r for r in readback if r["label"] == "Passport Country")
    assert passport["from"] == "extra" and passport["sensitive"] is True


def test_a_replan_after_filling_does_not_launder_an_improvised_value(formfill):
    """A re-plan reads a form the agent may already have filled. The baseline stays the FIRST plan's,
    so a passport country typed between the two plans is still not 'the page's'."""
    filled = after("as3-supabase")
    formfill.build_plan(blank(filled), SUPABASE_CONFIRMED, ats="ashby", company="Supabase", role="AI")
    plan2 = formfill.build_plan(filled, SUPABASE_CONFIRMED, ats="ashby", company="Supabase", role="AI")
    unplanned = {m["label"] for m in formfill.diff(plan2, filled) if m.get("unplanned")}
    assert "Passport Country" in unplanned


def test_a_replan_under_a_respelled_role_gets_no_baseline_at_all(formfill):
    filled = after("as3-supabase")
    formfill.build_plan(blank(filled), SUPABASE_CONFIRMED, ats="ashby", company="Supabase", role="AI")
    plan2 = formfill.build_plan(filled, SUPABASE_CONFIRMED, ats="ashby", company="Supabase Inc", role="AI eng")
    assert "Passport Country" in {m["label"] for m in formfill.diff(plan2, filled) if m.get("unplanned")}


def test_a_value_the_page_prefilled_is_allowed_and_marked(formfill):
    form = [field("Email", "email", id="email", required=True), field("Country", "select", id="ctry",
            value="United States", options=["Canada", "United States"])]  # fmt: skip
    plan = formfill.build_plan(form, {"email": "a@b.co"}, company="Acme", role="Eng")
    filled = set_value(copy.deepcopy(form), "Email", "a@b.co")
    mismatches, readback = formfill.review(plan, filled)
    assert mismatches == []
    assert {r["label"]: r["from"] for r in readback} == {"Email": "answer", "Country": "page"}
    # …but changing it from what the page showed is the agent's doing, and blocks
    set_value(filled, "Country", "Canada")
    assert [m["label"] for m in formfill.diff(plan, filled)] == ["Country"]


def test_placeholders_and_unticked_boxes_are_empty(formfill):
    form = [field("Office", "native-select", name="loc", value="Select...", options=["Select...", "London"]),
            field("Consent to marketing", "checkbox", name="mk", value=False)]  # fmt: skip
    plan = formfill.build_plan(form, {}, company="Acme", role="Eng")
    assert formfill.diff(plan, form) == []
    ticked = copy.deepcopy(form)
    ticked[1]["value"] = True
    assert [m["label"] for m in formfill.diff(plan, ticked)] == ["Consent to marketing"]


def test_a_required_unticked_consent_box_is_not_filled(formfill):
    form = [field("I consent to the processing of my data", "checkbox", id="gdpr", required=True, value=False)]
    plan = formfill.build_plan(form, {})
    assert plan["unmapped"][0]["reason"].startswith("a legal")
    assert [m["expected"] for m in formfill.diff(plan, form)] == ["(required — not yet filled)"]


@pytest.mark.parametrize(
    "label",
    [
        "Are you over the age of 18?",
        "I confirm that everything in this application is true and my own",
        "Are you a U.S. person under export control regulations (ITAR/EAR)?",
        "Passport Country",
        "Are you authorized to work in the EU?",
        "Have you ever been convicted of a felony?",
    ],
)
def test_legal_questions_are_never_answered_from_other_answers(formfill, label):
    confirmed = {"authorized_us": "Yes", "current_location": "Portland, Oregon, United States", "pronouns": "x"}
    form = [field(label, "select", id="q", required=True, options=["Yes", "No"])]
    plan = formfill.build_plan(form, confirmed)
    assert plan["rows"] == []
    assert "legal" in plan["unmapped"][0]["reason"]


def test_a_confirmed_us_authorization_still_fills_its_own_question_and_is_marked(formfill):
    form = [field("Are you legally authorized to work in the United States?", "select", id="q",
                  required=True, options=["Yes", "No"])]  # fmt: skip
    plan = formfill.build_plan(form, {"authorized_us": "Yes"})
    assert plan["rows"][0]["value"] == "Yes" and plan["rows"][0]["sensitive"] is True


def test_eu_authorization_never_classifies_as_the_us_answer(formfill):
    assert formfill.classify(field("Are you authorized to work in the EU?")) is None
    assert formfill.classify(field("Are you legally authorized to work in the US?")) == "authorized_us"


def test_the_approval_card_shows_the_full_readback_with_marks(plugin):
    session = {
        "company": "Supabase",
        "role": "AI",
        "rows": [{"label": "Email", "value": "a@b.co", "action": "fill"}],
        "last_readback": [
            {"label": "Email", "value": "a@b.co", "from": "answer", "sensitive": False},
            {"label": "Country", "value": "United States", "from": "page", "sensitive": False},
            {"label": "Passport Country", "value": "United States", "from": "extra", "sensitive": True},
        ],
    }
    card = plugin._submit_approval_card(session, "sid")
    assert [r[0] for r in card["display"]["props"]["rows"]] == ["Email", "Country", "Passport Country"]
    assert "NOT from your answers" in card["detail"] and "LEGAL / ELIGIBILITY" in card["detail"]
    assert "Check the 2 marked value(s)" in card["prompt"]


def test_verify_records_the_readback_for_the_card(formfill):
    form = [field("Email", "email", id="email", required=True)]
    plan = formfill.build_plan(form, {"email": "a@b.co"}, company="Acme", role="Eng")
    filled = set_value(copy.deepcopy(form), "Email", "a@b.co")
    mismatches, readback = formfill.review(plan, filled)
    rec = formfill.record_verification(plan["session_id"], mismatches, readback)
    assert rec["verified"] and rec["last_readback"][0]["value"] == "a@b.co"
    formfill.invalidate_verification(plan["session_id"])
    assert formfill.load_session(plan["session_id"])["last_readback"] is None


# ── B1: the option browser_select committed ─────────────────────────────────────────────────
AUTH_YES = "Yes, I am currently legally authorized to work in the country where the job is located."
AUTH_NO = "No, I am not currently legally authorized to work in the country where the job is located."
SPONSOR_NO = "No, I do not and will not require sponsorship"


@pytest.mark.parametrize(
    "expected,actual,ok",
    [
        # with no options seen, only known spellings of one choice pass
        ("they/them", "they/them/theirs", True),
        ("United States", "United States of America", True),
        ("Yes", AUTH_YES, False),  # a bare prefix proves nothing about WHICH option was picked
        ("United", "United States", False),
        ("they/them", "they", False),
        ("Man", "Mango", False),
    ],
)
def test_without_options_only_known_spellings_of_one_choice_pass(formfill, expected, actual, ok):
    row = {"action": "select", "options_unknown": True, "value": expected}
    assert formfill._values_equal(expected, actual, row) is ok


@pytest.mark.parametrize(
    "expected,actual,options,ok",
    [
        ("Yes", AUTH_YES, [AUTH_YES, AUTH_NO], True),
        ("No", SPONSOR_NO, ["Yes, I will require sponsorship", SPONSOR_NO], True),
        ("No", "No, but I will in the future", ["Yes", "No, but I will in the future"], False),
        ("Yes", "Yes, but I will require sponsorship", ["Yes, but I will require sponsorship", "No"], False),
    ],
)
def test_with_the_readback_options_a_unique_prefix_passes(formfill, expected, actual, options, ok):
    row = {"action": "select", "options_unknown": True, "value": expected}
    assert formfill._values_equal(expected, actual, row, options) is ok


def test_a_prefix_commit_is_not_accepted_where_the_plan_matched_a_known_option(formfill):
    row = {"action": "select", "value": "United States"}
    assert not formfill._values_equal("United States", "United States Minor Outlying Islands", row)


def test_figma_and_gitlab_choice_rewrites_verify(formfill):
    form = [field("Pronouns", "combobox", id="question_20374544004"),
            field("What is your current country of residence?", "combobox", id="question_38115715002", required=True)]  # fmt: skip
    plan = formfill.build_plan(form, {}, extra={"question_20374544004": "they/them",
                                                 "question_38115715002": "United States"})  # fmt: skip
    filled = set_value(copy.deepcopy(form), "Pronouns", "they/them/theirs")
    set_value(filled, "What is your current country of residence?", "United States of America")
    assert formfill.diff(plan, filled) == []


# ── B2: checkboxes ───────────────────────────────────────────────────────────────────────────
def test_spotify_pronoun_checkboxes_plan_one_check_row(formfill):
    filled = after("lv2-spotify")
    plan = formfill.build_plan(blank(filled), {"pronouns": "they/them"}, ats="lever", company="Spotify", role="X")
    row = next(r for r in plan["rows"] if r.get("group") == "pronouns")
    assert row["action"] == "check" and [c["label"] for c in row["checks"]] == ["They/them"]
    assert not any(r.get("label") == "They/them" and r.get("action") == "fill" for r in plan["rows"])
    mismatches = formfill.diff(plan, filled)
    assert not any(m["label"].startswith("pronouns") for m in mismatches)
    # ticking a second pronoun is caught
    two = copy.deepcopy(filled)
    next(f for f in two if f["label"] == "He/him")["value"] = True
    assert "pronouns: He/him" in {m["label"] for m in formfill.diff(plan, two)}


def test_notion_how_did_you_hear_checkboxes_never_receive_urls(formfill):
    filled = after("as2-notion")
    confirmed = {"linkedin_url": "https://www.linkedin.com/in/x", "portfolio_url": "https://x.example"}
    plan = formfill.build_plan(blank(filled), confirmed, ats="ashby", company="Notion", role="X")
    assert not any(r.get("value", "").startswith("http") and "checkbox" in r.get("id", "") for r in plan["rows"])
    stem = "eec5f04b-2a21-4232-b705-e1a4da94a750_0b3b7773-f6d9-4032-9ab1-368c4164e95a-labeled-checkbox"
    plan = formfill.build_plan(
        blank(filled), confirmed, extra={stem: "LinkedIn"}, ats="ashby", company="Notion", role="X"
    )
    row = next(r for r in plan["rows"] if r.get("group") == stem)
    assert [c["label"] for c in row["checks"]] == ["LinkedIn"]
    # the eval's read-back ticked "Notion Website", not LinkedIn
    labels = {m["label"]: m for m in formfill.diff(plan, filled)}
    assert labels[f"{stem}: LinkedIn"]["expected"] == "ticked"
    assert labels[f"{stem}: Notion Website"]["expected"] == "unticked"


def test_a_group_answer_matching_no_option_is_asked_about(formfill):
    form = [
        field("A", "checkbox", name="q[]", required=True, value=False),
        field("B", "checkbox", name="q[]", value=False),
    ]
    plan = formfill.build_plan(form, {}, extra={"q[]": "C"})
    assert plan["unmapped"][0]["reason"] == "answer matches no option"
    assert plan["unmapped"][0]["options"] == ["A", "B"]


def test_a_single_checkbox_takes_yes_or_no(formfill):
    form = [field("Send me job alerts", "checkbox", id="alerts", value=False)]
    assert formfill.build_plan(form, {}, extra={"alerts": "yes"})["rows"][0]["checks"][0]["id"] == "alerts"
    assert formfill.build_plan(form, {}, extra={"alerts": "no"})["rows"][0]["checks"] == []
    assert (
        formfill.build_plan(form, {}, extra={"alerts": "maybe"})["unmapped"][0]["reason"]
        == "a checkbox takes yes or no"
    )


def test_a_required_checkbox_reads_unticked_as_empty(formfill):
    form = [field("Agree", "checkbox", id="a", required=True, value=False)]
    plan = {"rows": [], "baseline": {}}
    assert formfill.diff(plan, form)[0]["expected"] == "(required — not yet filled)"


# ── B3 / B4: locations ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "actual,ok",
    [
        ("Portland, OR, USA", True),
        ("Portland, Oregon, United States, 97201", True),
        ("Portland, Oregon, US", True),
        ("Portland, ME, USA", False),
        ("Portland", False),
        ("South Portland, Oregon, United States", False),
    ],
)
def test_location_autocomplete_rewrites(formfill, actual, ok):
    row = {"key": "current_location", "action": "fill", "value": "Portland, Oregon, United States"}
    assert formfill._values_equal("Portland, Oregon, United States", actual, row) is ok


def test_zoox_location_verifies(formfill):
    form = [field("Current location", "text", name="location", id="location-input", required=True)]
    plan = formfill.build_plan(form, {"current_location": "Portland, Oregon, United States"}, ats="lever")
    assert formfill.diff(plan, set_value(copy.deepcopy(form), "Current location", "Portland, OR, USA")) == []


def test_blueground_city_is_satisfied_by_the_visible_address(formfill):
    filled = after("wk2-blueground")
    confirmed = {"current_location": "Portland, Oregon, United States"}
    form = blank(filled)
    plan = formfill.build_plan(form, confirmed, ats="workable", company="Blueground", role="X")
    assert any(r.get("id") == "city" and r.get("value") == "Portland" for r in plan["rows"])
    mismatches = formfill.diff(plan, filled)
    assert not any(m["label"] == "city" for m in mismatches)
    # without the companion it's still a mismatch
    no_address = set_value(copy.deepcopy(filled), "Address (Optional)SVGs not supported by this browser.", "")
    assert any(m["label"] == "city" for m in formfill.diff(plan, no_address))


def test_address_parts_filled_by_the_picker_are_derived(formfill):
    form = [field("Location", "text", id="loc", required=True), field("postcode", "text", id="postcode")]
    # a postcode counts as derived only when the planned location itself names it (QA, PR #121)
    plan = formfill.build_plan(form, {"current_location": "Portland, Oregon 97201, United States"})
    filled = set_value(copy.deepcopy(form), "Location", "Portland, Oregon 97201, United States")
    set_value(filled, "postcode", "97201")
    mismatches, readback = formfill.review(plan, filled)
    assert mismatches == [] and {r["label"]: r["from"] for r in readback}["postcode"] == "derived"


# ── C: Enter on the core-gate path ───────────────────────────────────────────────────────────
def test_a_bare_enter_is_not_a_floor_submit(submitgate):
    assert submitgate.is_submit_like("browser_press", {"key": "Enter"})  # the fallback keeps it
    assert not submitgate.is_floor_submit("browser_press", {"key": "Enter"})
    assert submitgate.is_floor_submit("browser_press", {"key": "Enter", "selector": "button[type=submit]"})
    assert submitgate.is_floor_submit("browser_click", {"selector": "Submit application"})


# ── D: the Greenhouse phone country ──────────────────────────────────────────────────────────
def test_gitlab_country_before_the_phone_is_the_phone_country(formfill):
    form = blank(after("gh1-gitlab"))
    plan = formfill.build_plan(form, {"phone_country": "United States", "phone_number": "5550100199"})
    rows = [r for r in plan["rows"] if r.get("key") in {"phone_country", "phone_number"}]
    assert [(r["id"], r["key"]) for r in rows] == [("country", "phone_country"), ("phone", "phone_number")]


def test_a_country_question_elsewhere_is_not_the_phone_country(formfill):
    form = [field("Country", "combobox", id="country"), field("Why us?", "textarea", id="q1"),
            field("Phone", "tel", id="phone")]  # fmt: skip
    plan = formfill.build_plan(form, {"phone_country": "United States"})
    assert not any(r.get("key") == "phone_country" and r["action"] != "skip" for r in plan["rows"])


# ── E: gh_jid careers pages ──────────────────────────────────────────────────────────────────
def test_detect_reads_the_embeddable_form(ats):
    got = ats.detect("https://job-boards.greenhouse.io/embed/job_app?for=airbnb&token=7826337")
    assert got["board"] == "airbnb" and got["job_id"] == "7826337"
    assert got["apply_url"] == "https://job-boards.greenhouse.io/embed/job_app?for=airbnb&token=7826337"


def test_board_candidates_start_with_the_careers_host(ats):
    assert ats.board_candidates("https://careers.airbnb.com/positions/7826337?gh_jid=7826337")[0] == "airbnb"
    assert "gitlab" in ats.board_candidates("https://about.gitlab.com/jobs/apply/?gh_jid=1", "GitLab")
    assert len(ats.board_candidates("https://careers.x.com/?gh_jid=1", "Some Long Company Name")) <= 6


def test_prepare_on_a_gh_jid_page_returns_the_embed_url(tools, ats, monkeypatch):
    seen = []

    async def fake_fetch(board, job_id):
        seen.append(board)
        if board == "airbnb":
            return {"title": "ML Engineer", "questions": [
                {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text"}]}]}, ""  # fmt: skip
        return None, "HTTP 404"

    monkeypatch.setattr(ats, "fetch_schema", fake_fetch)
    out = asyncio.run(
        tools["careercoach_prepare_application"].ainvoke(
            {"url": "https://careers.airbnb.com/positions/7826337?gh_jid=7826337", "company": "Airbnb"}
        )
    )
    assert seen[0] == "airbnb"
    assert "https://job-boards.greenhouse.io/embed/job_app?for=airbnb&token=7826337" in out
    assert "Prepared from the Greenhouse public schema" in out


# ── F: name keys ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "f,key",
    [
        (field("First Name", id="first_name"), "first_name"),
        (field("Last Name", id="last_name"), "last_name"),
        (field("Full name✱", name="name"), "full_name"),
        (field("Name", id="_systemfield_name"), "full_name"),
        (field("", name="firstname", id="firstname"), "first_name"),
        (field("Company name", id="org"), None),
        (field("Preferred first name", id="pref"), None),
        (field("Name of the employee who referred you", id="ref"), None),
    ],
)
def test_name_fields_classify(formfill, f, key):
    assert formfill.classify(f) == key


def test_name_keys_seed_from_the_profile_or_the_setting(answers):
    assert set(answers.seed_names({"identity": {"name": "Testy McTestface"}})) == {
        "full_name",
        "first_name",
        "last_name",
    }
    stored = answers.load()
    assert stored["first_name"]["value"] == "Testy" and stored["first_name"]["status"] == answers.DRAFT
    assert answers.seed_names({"identity": {"name": "Someone Else"}}) == []  # never overwrites


def test_a_three_word_name_seeds_only_the_full_name(answers):
    assert answers.seed_names({}, fallback_name="Mary Ann Smith") == ["full_name"]


def test_get_answers_seeds_missing_name_keys_for_an_existing_store(tools, answers):
    answers.propose("email", "a@b.co")
    out = tools["careercoach_get_answers"].invoke({})
    assert "full_name" in out  # listed as missing, or seeded when the profile has a name


# ── G: location autocomplete ─────────────────────────────────────────────────────────────────
def test_a_location_combobox_plans_an_autocomplete_select(formfill):
    form = [field("Location (City)", "combobox", id="candidate-location", required=True)]
    plan = formfill.build_plan(form, {"current_location": "Portland, Oregon, United States"})
    row = plan["rows"][0]
    assert row["action"] == "select" and row["autocomplete"] and row["search"] == "Portland"
    filled = set_value(copy.deepcopy(form), "Location (City)", "Portland, Oregon, United States")
    assert formfill.diff(plan, filled) == []


def test_the_plan_render_names_the_autocomplete_and_check_steps(tools):
    form = [
        field("Location (City)", "combobox", id="candidate-location", required=True),
        field("He/him", "checkbox", name="pronouns", value=False),
        field("They/them", "checkbox", name="pronouns", value=False),
    ]
    out = tools["careercoach_plan_fill"].invoke(
        {"form_json": json.dumps(form), "company": "A", "role": "B", "ats": "greenhouse",
         "extra_answers": json.dumps({"candidate-location": "Portland, Oregon, United States", "pronouns": "They/them"})}
    )  # fmt: skip
    assert "browser_click to tick: They/them" in out
    assert "Type, pick or tick NOTHING" in out


# ── QA panel, PR #121: one regression per finding (each fails on 5e98902) ─────────────────────
def _press_action(target, categories=()):
    return {
        "tool": "browser_press",
        "verb": "press",
        "selector": "",
        "key": "Enter",
        "page_url": "https://boards.greenhouse.io/acme/jobs/1",
        "target": target,
        "verdict": {"categories": sorted(categories), "reason": "", "description": ""},
        "gated": [],
    }


def _never_verified(_sid):
    return ""


@pytest.mark.parametrize(
    "target",
    [None, {"found": False}, {"found": True, "tag": "", "note": "frame"}, {"found": True}],
)
def test_qa1_a_bare_enter_the_core_could_not_resolve_still_needs_the_grant(submitgate, target):
    submitgate.clear()
    decision = submitgate.core_policy_decision(_press_action(target), _never_verified, None, enter_floor=True)
    assert decision == {"decision": "deny", "reason": submitgate.BLOCKED}


def test_qa1_a_bare_enter_on_a_resolved_non_submit_target_defers_to_the_core(submitgate):
    target = {"found": True, "tag": "input", "role": "combobox", "name": "Location (City)"}
    decision = submitgate.core_policy_decision(_press_action(target), _never_verified, None, enter_floor=True)
    assert decision is None


def test_qa1_an_unresolved_bare_enter_spends_a_live_grant_once(submitgate):
    submitgate.clear()
    submitgate.grant("sid", "vid")
    current = {"sid": "vid"}.get
    first = submitgate.core_policy_decision(_press_action(None), current, None, enter_floor=True)
    second = submitgate.core_policy_decision(_press_action(None), current, None, enter_floor=True)
    assert first["decision"] == "allow" and second["decision"] == "deny"
    submitgate.clear()


def test_qa2_a_city_only_plan_never_verifies_another_state(formfill):
    row = {"key": "current_location", "action": "fill", "value": "Portland"}
    assert not formfill._values_equal("Portland", "Portland, Maine, United States", row)
    assert formfill._values_equal("Portland", "Portland", row)


def test_qa3_a_prefix_option_is_not_another_country(formfill):
    row = {"action": "select", "options_unknown": True, "value": "United States"}
    assert not formfill._values_equal("United States", "United States Minor Outlying Islands", row)
    assert not formfill._values_equal("Congo", "Congo, the Democratic Republic of the", row)
    both = ["United States", "United States Minor Outlying Islands"]
    assert not formfill._values_equal("United", "United States Minor Outlying Islands", row, both)


def test_qa4_an_address_part_is_derived_only_when_it_matches_the_location(formfill):
    form = [field("Location", "text", id="loc", required=True), field("country", "text", id="country"),
            field("postcode", "text", id="postcode")]  # fmt: skip
    plan = formfill.build_plan(form, {"current_location": "Portland, Oregon, United States"}, company="A", role="B")
    filled = set_value(copy.deepcopy(form), "Location", "Portland, Oregon, United States")
    set_value(filled, "country", "Canada")
    set_value(filled, "postcode", "97201")
    unplanned = {m["label"] for m in formfill.diff(plan, filled) if m.get("unplanned")}
    assert unplanned == {"country", "postcode"}  # neither is named by the planned location
    set_value(filled, "country", "USA")
    set_value(filled, "postcode", "")
    mismatches, readback = formfill.review(plan, filled)
    assert mismatches == [] and {r["label"]: r["from"] for r in readback}["country"] == "derived"


def test_qa4_the_hidden_city_companion_must_name_the_same_city(formfill):
    form = [field("Address", "text", id="address"), field("city", "text", id="city")]
    plan = formfill.build_plan(form, {"current_location": "Portland, Oregon, United States"}, company="A", role="B")
    filled = set_value(copy.deepcopy(form), "Address", "South Portland, Maine, United States")
    assert any(m["label"] == "city" for m in formfill.diff(plan, filled))


def test_qa5_another_application_never_inherits_a_baseline(formfill):
    form = [field("Email", "email", id="email"), field("Country", "text", id="country"),
            field("Phone", "tel", id="phone"), field("City", "text", id="cityq")]  # fmt: skip
    prefilled_a = set_value(copy.deepcopy(form), "Country", "Greece")
    formfill.build_plan(prefilled_a, {}, company="Acme", role="Eng")
    # Beta's own page pre-fills "Canada"; Acme's "Greece" must not pass as Beta's page value
    plan_b = formfill.build_plan(form, {}, company="Beta", role="Data")
    assert plan_b["baseline"] == {}
    filled = set_value(copy.deepcopy(form), "Country", "Greece")
    assert [m["label"] for m in formfill.diff(plan_b, filled)] == ["Country"]


def test_qa5_a_plan_with_no_identity_gets_no_baseline(formfill):
    form = [field("Country", "text", id="country", value="United States")]
    plan = formfill.build_plan(form, {})
    assert plan["baseline"] == {}
    assert [m["label"] for m in formfill.diff(plan, form)] == ["Country"]


def test_qa6_the_handoff_done_message_says_to_replan_with_what_the_operator_set(plugin):
    import inspect

    src = inspect.getsource(plugin._register_handoff)
    assert "re-plan with those values in" in src and "Never clear what they set" in src
    skill = (Path(__file__).resolve().parent.parent / "skills/apply-form/SKILL.md").read_text(encoding="utf-8")
    assert "Never clear what the operator set" in skill


def test_qa6_an_attestation_the_operator_ticked_verifies_once_replanned(formfill):
    form = [field("Email", "email", id="email", required=True),
            field("I certify that the information above is true", "checkbox", id="attest", required=True, value=False)]  # fmt: skip
    formfill.build_plan(form, {"email": "a@b.co"}, company="A", role="B")
    filled = set_value(copy.deepcopy(form), "Email", "a@b.co")
    filled[1]["value"] = True  # the operator ticked it in the visible browser during the handoff
    replan = formfill.build_plan(filled, {"email": "a@b.co"}, {"attest": "yes"}, company="A", role="B")
    mismatches, readback = formfill.review(replan, filled)
    assert mismatches == []
    assert next(r for r in readback if r["target"] == "#attest")["sensitive"] is True


def test_qa7_pyproject_documents_why_host_packages_are_undeclared():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "No [project.dependencies] on purpose" in text and "langchain" in text


def test_qa8_board_candidates_docstring_reads_as_one_order(ats):
    doc = " ".join(ats.board_candidates.__doc__.split())
    assert "first the site's own name from its host" in doc and "company name the site's" not in doc


# ── QA re-review, PR #121 at 9e115ca: a checkbox group and its free-text companion ───────────────
def _by_target(plan):
    return {r.get("target"): r for r in plan["rows"]}


def test_qa_rr1_a_matched_pronoun_box_is_not_also_typed_into_custom(formfill):
    """Lever (Spotify eval): "they/them" ticks the They/them box ONCE. The Custom toggle and its
    "Write here..." box (name ``pronouns`` too) are skipped, not given the same answer again."""
    form = blank(after("lv2-spotify"))
    plan = formfill.build_plan(form, {"pronouns": "they/them"}, ats="lever", company="Spotify", role="X")
    rows = _by_target(plan)
    assert rows["#customPronounsTextField"]["action"] == "skip"
    assert rows["#customPronounsOption"]["action"] == "skip"
    answered = [r for r in plan["rows"] if r.get("value") and r["action"] != "skip" and r.get("key") == "pronouns"]
    assert [(r["action"], r.get("group")) for r in answered] == [("check", "pronouns")]


def test_qa_rr1_an_unlisted_pronoun_goes_in_custom_and_ticks_its_toggle(formfill):
    form = blank(after("lv2-spotify"))
    plan = formfill.build_plan(form, {"pronouns": "ze/zir"}, ats="lever", company="Spotify", role="X")
    rows = _by_target(plan)
    assert (rows["#customPronounsTextField"]["action"], rows["#customPronounsTextField"]["value"]) == ("fill", "ze/zir")
    assert [c["id"] for c in rows["#customPronounsOption"]["checks"]] == ["customPronounsOption"]
    group = next(r for r in plan["rows"] if r.get("group") == "pronouns")
    assert group["action"] == "check" and group["checks"] == []
    assert not any(u.get("group") == "pronouns" for u in plan["unmapped"])
    # filled that way, it verifies; also ticking They/them does not
    filled = copy.deepcopy(form)
    next(f for f in filled if f.get("id") == "customPronounsTextField")["value"] = "ze/zir"
    next(f for f in filled if f.get("id") == "customPronounsOption")["value"] = True
    assert not any(
        "pronoun" in m["label"].lower() or "custom" in m["label"].lower() for m in formfill.diff(plan, filled)
    )
    next(f for f in filled if f.get("label") == "They/them")["value"] = True
    assert "pronouns: They/them" in {m["label"] for m in formfill.diff(plan, filled)}


def test_qa_rr1_an_unlisted_answer_ticks_the_groups_own_other_member(formfill):
    form = [
        field("LinkedIn", "checkbox", name="q[f3]", value=False),
        field("Other", "checkbox", name="q[f3]", value=False),
        field("Type your response", "text", name="q[f4]"),
    ]
    plan = formfill.build_plan(form, {}, extra={"q[f3]": "A podcast"})
    group = next(r for r in plan["rows"] if r.get("group") == "q[f3]")
    assert [c["label"] for c in group["checks"]] == ["Other"]
    assert next(r for r in plan["rows"] if r.get("name") == "q[f4]")["value"] == "A podcast"


# ── protoAgent #4951: the checkbox question read as ONE field ────────────────────────────────────
PRONOUN_OPTIONS = ["He/him", "She/her", "They/them", "Xe/xem", "Use name only", "Custom"]


def group_q(label, options, value=None, *, name="", required=False):
    return {"label": label, "kind": "checkbox-group", "name": name, "id": "", "required": required,
            "value": list(value or []), "options": list(options)}  # fmt: skip


def test_core_checkbox_group_plans_one_check_row_by_question(formfill):
    form = [group_q("Pronouns", PRONOUN_OPTIONS, name="pronouns")]
    plan = formfill.build_plan(form, {"pronouns": "they/them"}, company="A", role="B")
    (row,) = plan["rows"]
    assert (row["action"], row["target"], row["ticks"]) == ("check", "Pronouns", ["They/them"])
    filled = [group_q("Pronouns", PRONOUN_OPTIONS, ["They/them"], name="pronouns")]
    mismatches, readback = formfill.review(plan, filled)
    assert mismatches == [] and readback[0]["value"] == "They/them" and readback[0]["from"] == "answer"


def test_core_checkbox_group_extra_ticks_and_an_extra_tick_is_caught(formfill):
    q = "How did you hear about us?"
    opts = ["LinkedIn", "Glassdoor", "Notion Website", "Conference or Meetup"]
    plan = formfill.build_plan([group_q(q, opts)], {"linkedin_url": "https://x"}, {q: "LinkedIn; Conference or Meetup"})
    assert plan["rows"][0]["ticks"] == ["LinkedIn", "Conference or Meetup"]
    assert formfill.diff(plan, [group_q(q, opts, ["Conference or Meetup", "LinkedIn"])]) == []
    (m,) = formfill.diff(plan, [group_q(q, opts, ["LinkedIn", "Notion Website"])])
    assert m["actual"] == "LinkedIn, Notion Website" and "browser_click" in m["reason"]


def test_core_checkbox_group_never_takes_a_free_text_answer(formfill):
    form = [group_q("Which location are you in?", ["Remote", "London"], required=True)]
    plan = formfill.build_plan(form, {"current_location": "Portland, Oregon, United States"})
    assert plan["rows"] == [] and "free text" in plan["unmapped"][0]["reason"]


def test_core_checkbox_group_unmatched_answer_goes_in_the_custom_box(formfill):
    form = [group_q("Pronouns", PRONOUN_OPTIONS, name="pronouns"),
            field("Write here...", "text", name="pronouns", id="customPronounsTextField")]  # fmt: skip
    plan = formfill.build_plan(form, {"pronouns": "ze/zir"}, company="A", role="B")
    rows = {r["target"]: r for r in plan["rows"]}
    assert rows["Pronouns"]["ticks"] == ["Custom"]
    assert (rows["#customPronounsTextField"]["action"], rows["#customPronounsTextField"]["value"]) == ("fill", "ze/zir")
    matched = formfill.build_plan(form, {"pronouns": "they/them"}, company="A", role="C")
    assert {r["target"]: r["action"] for r in matched["rows"]}["#customPronounsTextField"] == "skip"


def test_core_checkbox_group_unticked_required_is_blank_and_unplanned_ticks_are_caught(formfill):
    form = [group_q("I agree to the terms", ["I agree"], required=True)]
    plan = formfill.build_plan(form, {}, company="A", role="B")
    assert "legal" in plan["unmapped"][0]["reason"]
    assert formfill.diff(plan, form)[0]["expected"] == "(required — not yet filled)"
    (m,) = formfill.diff(plan, [group_q("I agree to the terms", ["I agree"], ["I agree"], required=True)])
    assert m["unplanned"] and m["actual"] == "I agree"


def test_the_plan_renders_browser_select_per_option_for_a_core_group(tools):
    form = [group_q("Pronouns", PRONOUN_OPTIONS, name="pronouns")]
    out = tools["careercoach_plan_fill"].invoke(
        {"form_json": json.dumps(form), "company": "A", "role": "B", "ats": "lever",
         "extra_answers": json.dumps({"Pronouns": "They/them"})}
    )  # fmt: skip
    assert 'browser_select(field="Pronouns", option_text="They/them")' in out
    assert "browser_click an option's label unticks it" in out


def test_options_unknown_rows_offer_option_text_alternatives(formfill):
    form = [field("Gender", "combobox", id="gender"), field("Country of residence", "combobox", id="cor")]
    plan = formfill.build_plan(form, {"gender": "Decline to self-identify"}, {"cor": "United States"})
    rows = {r["id"]: r for r in plan["rows"]}
    assert rows["gender"]["option_text"].split("|")[:3] == [
        "Decline to self-identify", "I don't wish to answer", "Prefer not to say"]  # fmt: skip
    assert rows["cor"]["option_text"] == "United States|United States of America"
    assert formfill.option_alternatives("Software engineer") == "Software engineer"


def test_an_autocomplete_row_names_the_core_messages(tools):
    form = [field("Location (City)", "combobox", id="candidate-location", required=True)]
    out = tools["careercoach_plan_fill"].invoke(
        {"form_json": json.dumps(form), "company": "A", "role": "B", "ats": "greenhouse",
         "extra_answers": json.dumps({"candidate-location": "Portland, Oregon, United States"})}
    )  # fmt: skip
    assert "matches more than one suggestion" in out and "no suggestions appeared" in out
