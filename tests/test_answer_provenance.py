"""Answer provenance and verify's own read — the findings of the 2026-11-09 eval re-run.

1  An extra answer is the OPERATOR's only when they said it in this conversation; anything else is
   marked "entered by the agent — not confirmed by you" on the approval card, and an agent-written
   answer to a legal / attestation question never verifies. Replays the Supabase X-field episode and
   the Hugging Face attestation episode.
2  Verify checks the page's own read: the browser (core #4957 ``for_agent_session().form_read()``),
   else the newest ``browser_form_read`` result in the conversation; a model-passed copy that lost
   fields is refused.
3  The name answers come from the operator's own profile / Settings, CONFIRMED.
4  "pronounce" is not "pronouns"; a disability SIGNATURE's Name / Date is an attestation.
5  "United States +1" is "United States" for a phone country.
7  A one-option consent group takes yes/no — from the operator only.
8  A checkbox group whose label another field shares is addressed by a CSS selector.
"""

from __future__ import annotations

import copy
import importlib
import json

import pytest


@pytest.fixture
def formfill(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".applying.formfill")


@pytest.fixture
def prov(plugin):
    return importlib.import_module(plugin.__name__ + ".applying.provenance")


@pytest.fixture
def answers(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".record.answers")


def field(label, kind="text", *, id="", name="", required=False, value="", options=None):
    return {"label": label, "kind": kind, "name": name or id, "id": id, "required": required, "value": value,
            "options": options or []}  # fmt: skip


def human(text):
    return {"type": "human", "content": text}


def asked(tool, args, answer, call_id):
    """An AI tool call to a HITL tool plus the operator's reply to it."""
    return [
        {"type": "ai", "content": "", "tool_calls": [{"id": call_id, "name": tool, "args": args}]},
        {"type": "tool", "name": tool, "tool_call_id": call_id, "content": answer},
    ]


def browser_call(tool, content, call_id, args=None):
    return [
        {"type": "ai", "content": "", "tool_calls": [{"id": call_id, "name": tool, "args": args or {}}]},
        {"type": "tool", "name": tool, "tool_call_id": call_id, "content": content},
    ]


def set_values(fields, values):
    out = copy.deepcopy(fields)
    for f in out:
        key = f.get("id") or f.get("name")
        if key in values:
            f["value"] = values[key]
    return out


# The operator's words in both episodes (cceval-r2 transcripts).
BRIEF = (
    "Answers: No to any yes/no question about prior employment at this company, employment agreements or "
    "non-competes, relatives working there, prior applications, or criminal history. GitHub: "
    "https://github.com/testy-mctestface-example. How did you hear about us: the company's careers page. "
    "For essay or 'tell us about' questions, draft two or three honest sentences using only my résumé, and "
    "fill them in; I approve that. You may tick required privacy-policy / data-processing consent boxes; I "
    "agree to them. Leave other optional questions blank. Go ahead and fill the form and verify it; don't submit."
)


# ── 1a: the Supabase X-field episode ──────────────────────────────────────────────────────────
SUPABASE = [
    field("Name", id="_systemfield_name", required=True),
    field("Email", "email", id="_systemfield_email", required=True),
    field("Passport Country", id="passport", required=True),
    field("Are you over the age of 18?", "checkbox", id="over18", value=False),
    field("Twitter / X profile", id="twitter"),
    field("Github Profile", id="github", required=True),
    field("Where did you hear about this vacancy?", id="heard"),
    field("Tell us about your experience working in an async and/or remote environment.", "textarea",
          id="essay", required=True),
]  # fmt: skip
SUPABASE_STATE = {
    "messages": [
        human(
            "Fill in the application for https://jobs.ashbyhq.com/supabase/3b5d with my profile; don't submit. "
            "Extra answers from me: yes, I'm over 18 (tick it); passport country: United States; confirm Testy "
            "McTestface as my full name. When the form is VERIFIED, request my approval card so I can review it."
        ),
        *asked("request_user_input", {"title": "Supabase application", "steps": []}, BRIEF, "c1"),
        human(
            "[round-governor] You are 25 model rounds into this turn. Re-ground before continuing: "
            "https://x.com/testy_unplanned"
        ),  # a runtime notice, never the operator's words — even when it happens to hold the value
        human("I reviewed it in the browser. Re-read the form, verify it, and request my approval card again."),
        *asked(
            "request_decision",
            {
                "question": "The Twitter / X field holds https://x.com/testy_unplanned. Keep it or clear it?",
                "options": [{"id": "keep", "label": "Keep it"}, {"id": "clear", "label": "Clear it"}],
            },
            "Please continue with that: finish filling every field you can, then read it back and verify it.",
            "c2",
        ),  # fmt: skip
    ]
}
ESSAY = "I built an internal tooling platform that 40 teams use, and I cut CI time from 30 to 12 minutes."
SUPABASE_EXTRAS = {
    "_systemfield_name": "Testy McTestface",
    "passport": "United States",
    "over18": "yes",
    "twitter": "https://x.com/testy_unplanned",  # found on the page; "I assumed you typed it"
    "github": "https://github.com/testy-mctestface-example",
    "heard": "the company's careers page",
    "essay": ESSAY,
}


def _plan(tools, form, extras, state, company="Supabase", ats="ashby"):
    return tools["careercoach_plan_fill"].invoke(
        {"form_json": json.dumps(form), "company": company, "role": "AI Platform Engineer", "ats": ats,
         "extra_answers": json.dumps(extras), "state": state}
    )  # fmt: skip


def _sid(out: str) -> str:
    return out.split("session_id: ", 1)[1].split("\n", 1)[0].strip()


def test_supabase_x_field_reaches_the_card_as_the_agents_answer(plugin, tools, formfill, answers):
    answers.propose("email", "testy.mctestface@example.com")
    answers.confirm(["email"])
    _plan(tools, SUPABASE, {}, SUPABASE_STATE)  # the form as opened: the page's baseline, X empty
    page = set_values(SUPABASE, {"twitter": "https://x.com/testy_unplanned"})  # typed by someone, later
    out = _plan(tools, page, SUPABASE_EXTRAS, SUPABASE_STATE)
    sid = _sid(out)
    rows = {r.get("id"): r for r in formfill.load_session(sid)["rows"]}
    assert rows["twitter"]["provenance"] == "agent"
    assert rows["essay"]["provenance"] == "agent"  # the agent's draft, however approved in spirit
    for key in ("passport", "github", "heard", "_systemfield_name"):
        assert rows[key]["provenance"] in {"operator", "answers"}, key
    assert rows["over18"]["provenance"] == "operator"  # "yes, I'm over 18 (tick it)" names the question
    assert "YOUR answer, not the operator's" in out

    filled = set_values(
        page,
        {"_systemfield_name": "Testy McTestface", "_systemfield_email": "testy.mctestface@example.com",
         "passport": "United States", "over18": True, "github": "https://github.com/testy-mctestface-example",
         "heard": "the company's careers page", "essay": ESSAY},
    )  # fmt: skip
    verdict = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": json.dumps(filled)})
    assert verdict.startswith("VERIFIED")
    assert "not ones the operator gave" in verdict and "testy_unplanned" in verdict

    card = plugin._submit_approval_card(formfill.load_session(sid), sid)
    lines = {ln.split(" -> ")[0].strip(): ln for ln in card["detail"].splitlines()}
    assert "entered by the agent — not confirmed by you" in lines["Twitter / X profile"]
    assert "your answer" not in lines["Twitter / X profile"]
    assert "your answer for this application" in lines["Passport Country"]
    assert (
        "entered by the agent" in lines["Tell us about your experience working in an async and/or remote environment."]
    )


def test_without_the_conversation_every_extra_is_the_agents(tools, formfill):
    """A call with no state (an old host, a direct call) fails CLOSED: the passport answer can't plan."""
    out = _plan(tools, SUPABASE, SUPABASE_EXTRAS, None)
    rows = {r.get("id"): r for r in formfill.load_session(_sid(out))["rows"]}
    assert "passport" not in rows
    assert "'United States' is your answer, not theirs" in out
    assert rows["twitter"]["provenance"] == "agent"


# ── 1b: the Hugging Face attestation episode ──────────────────────────────────────────────────
ATTEST = (
    "Can you confirm that everything in this application is true and your own, including your experience and identity?"
)
HF = [
    field("First name", id="firstname", required=True),
    field(ATTEST, "radio-group", name="QA_12487827", required=True, options=["YES", "NO"]),
    field("Did you start your first written answer below with the exact phrase we asked for?", "radio-group",
          name="QA_12487828", required=True, options=["YES", "NO"]),
]  # fmt: skip
HF_STATE = {
    "messages": [
        human("Fill in the application for https://apply.workable.com/huggingface/j/F88446C814 with my profile; "
              "don't submit. I'm here if you need me; use my confirmed answers and the résumé from my profile."),
        *asked("request_user_input", {"title": "Hugging Face application", "steps": []}, BRIEF, "h1"),
        *asked(
            "ask_human",
            {"question": f'1. Attestation: "{ATTEST}" Should I answer YES or NO? Only your own answer counts.'},
            "Please continue with that: finish filling every field you can, then read it back and verify it.",
            "h2",
        ),
    ]
}  # fmt: skip


def test_hf_agent_attestation_never_plans_and_never_verifies(tools, formfill):
    out = _plan(tools, HF, {"firstname": "Testy", "QA_12487827": "YES", "QA_12487828": "YES"}, HF_STATE,
                company="Hugging Face", ats="workable")  # fmt: skip
    sid = _sid(out)
    session = formfill.load_session(sid)
    assert not any(r.get("name") == "QA_12487827" for r in session["rows"])
    entry = next(u for u in session["unmapped"] if u.get("name") == "QA_12487827")
    assert entry["agent_answer"] == "YES" and "hasn't given that answer" in entry["reason"]
    assert "'YES' is your answer, not theirs" in out
    # The agent ticks YES anyway: outside the plan, so it can't verify.
    filled = set_values(HF, {"firstname": "Testy", "QA_12487827": "YES", "QA_12487828": "YES"})
    for f in filled:
        if f["kind"] == "radio-group":
            f["checked"] = True
    verdict = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": json.dumps(filled)})
    assert verdict.startswith("NOT verified") and "QA_12487827" not in verdict.split("\n")[0]
    assert ATTEST[:40] in verdict


def test_hf_operator_attestation_plans_as_theirs(tools, formfill):
    state = {"messages": [human("Extra answers from me: 'everything is true and my own': YES, I attest to that.")]}
    out = _plan(tools, HF, {"QA_12487827": "YES"}, state, company="Hugging Face", ats="workable")
    row = next(r for r in formfill.load_session(_sid(out))["rows"] if r.get("name") == "QA_12487827")
    assert row["provenance"] == "operator" and row["sensitive"]


# ── provenance rules ──────────────────────────────────────────────────────────────────────────
def test_a_short_answer_needs_the_question_named(prov):
    said = prov.operator_utterances({"messages": [human(BRIEF)]})
    # "yes/no" appears in the brief, but nothing in it names the attestation question
    assert not prov.said_by_operator("YES", ATTEST, said)
    # the brief names non-competes and says No to them
    assert prov.said_by_operator("No", "Are you subject to a non-compete agreement?", said)
    # consent: "I agree to them" about privacy-policy boxes
    assert prov.said_by_operator("yes", "I agree to the privacy policy", said)
    assert prov.said_by_operator("the company's careers page", "How did you hear?", said)
    assert not prov.said_by_operator("https://x.com/someone", "Twitter", said)


def test_a_form_answer_is_paired_with_its_field_title(prov):
    schema = {"type": "object", "properties": {"truthful": {"type": "string", "title": f'"{ATTEST}"'}}}
    msgs = asked("request_user_input", {"title": "t", "steps": [{"schema": schema}]}, '{"truthful": "YES"}', "u1")
    said = prov.operator_utterances({"messages": msgs})
    assert prov.said_by_operator("YES", ATTEST, said)
    assert not prov.said_by_operator("YES", "Are you over the age of 18?", said)


def test_runtime_notices_and_agent_text_are_not_the_operators(prov):
    msgs = [
        human("[round-governor] passport: Canada"),
        {"type": "ai", "content": "Your passport country is Canada, I assume."},
    ]
    assert prov.operator_utterances({"messages": msgs}) == []
    assert prov.operator_utterances(None) == []


# ── 2: verify reads the page itself ───────────────────────────────────────────────────────────
FORM = [field("Email", "email", id="email", required=True), field("Twitter", id="twitter")]


def _email_plan(tools, answers):
    answers.propose("email", "a@b.co")
    answers.confirm(["email"])
    return _sid(_plan(tools, FORM, {}, {"messages": []}, company="Acme", ats="greenhouse"))


def test_verify_uses_the_latest_browser_read_not_the_copy(tools, answers):
    sid = _email_plan(tools, answers)
    page = set_values(FORM, {"email": "a@b.co", "twitter": "https://x.com/unplanned"})
    copy_ = set_values(FORM, {"email": "a@b.co"})  # the model's edited copy hides the X value
    state = {"messages": browser_call("browser_fill", "Filled", "f1") + browser_call(
        "browser_form_read", json.dumps(page), "r1")}  # fmt: skip
    out = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": json.dumps(copy_), "state": state})
    assert out.startswith("NOT verified") and "x.com/unplanned" in out
    assert "not the form_json you passed" in out


def test_verify_refuses_a_read_older_than_the_last_browser_change(tools, answers):
    sid = _email_plan(tools, answers)
    page = set_values(FORM, {"email": "a@b.co"})
    state = {"messages": browser_call("browser_form_read", json.dumps(page), "r1")
             + browser_call("browser_fill", "Filled", "f1", {"selector": "Twitter"})}  # fmt: skip
    out = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": json.dumps(page), "state": state})
    assert out.startswith("NOT verified") and "after your last browser_form_read" in out


def test_verify_on_an_old_core_refuses_a_copy_that_lost_fields(tools, answers):
    sid = _email_plan(tools, answers)
    trimmed = [set_values(FORM, {"email": "a@b.co"})[0]]  # the Twitter field dropped from the copy
    out = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": json.dumps(trimmed)})
    assert out.startswith("NOT verified") and "missing 1 field" in out and "Twitter" in out
    full = set_values(FORM, {"email": "a@b.co"})
    assert (
        tools["careercoach_verify_fill"]
        .invoke({"session_id": sid, "form_json": json.dumps(full)})
        .startswith("VERIFIED")
    )


class _Result:
    def __init__(self, value, ok=True):
        self.value, self.ok, self.kind, self.error = value, ok, "ok" if ok else "cli_error", ""


def test_verify_reads_the_browser_through_the_core_client(plugin, tools, answers, monkeypatch):
    sid = _email_plan(tools, answers)
    page = set_values(FORM, {"email": "a@b.co", "twitter": "https://x.com/unplanned"})
    seen = {}

    class Client:
        def form_read(self, scope=""):
            return _Result(page)

    def make(**_):
        raise AssertionError("verify must use the agent's session, not a session of its own")

    def for_agent_session(state=None, **_):
        seen["state"] = state
        return Client()

    make.for_agent_session = for_agent_session
    from graph import sdk

    monkeypatch.setattr(sdk, "service", lambda name: make if name == "agent_browser.client" else None, raising=False)
    state = {"session_id": "chat-1", "messages": []}
    copy_ = set_values(FORM, {"email": "a@b.co"})
    out = tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": json.dumps(copy_), "state": state})
    assert seen["state"] == state
    assert out.startswith("NOT verified") and "x.com/unplanned" in out and "browser's own read" in out


# ── 3: names from the operator's own profile / Settings are confirmed ─────────────────────────
def test_draft_names_matching_the_profile_are_promoted(answers):
    answers.propose("full_name", "Testy McTestface")  # a store seeded when names were drafts
    answers.propose("first_name", "Someone")  # a draft the operator changed: left alone
    changed = answers.seed_names({"identity": {"name": "Testy McTestface"}})
    stored = answers.load()
    assert "full_name" in changed and stored["full_name"]["status"] == answers.CONFIRMED
    assert stored["first_name"] == {**stored["first_name"], "value": "Someone", "status": answers.DRAFT}
    assert stored["last_name"]["status"] == answers.CONFIRMED


def test_profile_and_setting_that_disagree_only_propose(answers):
    answers.seed_names({"identity": {"name": "Testy McTestface"}}, fallback_name="Other Person")
    assert answers.load()["full_name"]["status"] == answers.DRAFT


def test_plan_fill_fills_names_from_settings_without_asking(plugin, iso, monkeypatch, formfill):
    from _plugin_testkit import FakeRegistry

    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    reg = FakeRegistry(config={"browser_fill": "greenhouse", "full_name": "Testy McTestface"})
    plugin.register(reg)
    tool = next(t for t in reg.tools if t.name == "careercoach_plan_fill")
    form = [field("First Name", id="first_name", required=True), field("Last Name", id="last_name", required=True)]
    out = tool.invoke({"form_json": json.dumps(form), "company": "Airbnb", "role": "SWE", "ats": "greenhouse",
                       "state": {"messages": []}})  # fmt: skip
    assert "#first_name -> Testy" in out and "#last_name -> McTestface" in out
    assert "DRAFT" not in out


# ── 4: whole-word patterns; signatures are attestations ───────────────────────────────────────
def test_pronounce_and_signature_fields_are_not_pronouns_or_disability(formfill):
    assert formfill.classify(field("How do you pronounce your name?", id="pron")) is None
    assert formfill.classify(field("Pronouns", id="pronouns")) == "pronouns"
    assert formfill.classify(field("Disability status", name="eeo[disability]")) == "disability_status"
    assert formfill.classify(field("Disability Status", id="disability_status")) == "disability_status"
    sig = field("Name", name="eeo[disabilitySignature]")
    date = field("Date", name="eeo[disabilitySignatureDate]")
    assert formfill.classify(date) is None
    assert formfill.is_sensitive(sig) and formfill.is_sensitive(date)
    # the confirmed name never signs for the operator
    plan = formfill.build_plan([sig, date], {"full_name": "Testy McTestface", "disability_status": "Decline"})
    assert all(r["action"] == "skip" for r in plan["rows"])


# ── 5: phone dial codes ───────────────────────────────────────────────────────────────────────
def test_phone_country_ignores_the_dial_code(formfill):
    form = [
        field("Country", "combobox", id="country", options=["United States +1", "Canada +1"]),
        field("Phone", "tel", id="phone"),
    ]
    plan = formfill.build_plan(form, {"phone_country": "United States", "phone_number": "5550100199"})
    row = next(r for r in plan["rows"] if r["id"] == "country")
    assert row["action"] == "select" and row["value"] == "United States +1"
    assert formfill.diff(plan, set_values(form, {"country": "United States +1", "phone": "(555) 010-0199"})) == []
    assert formfill.diff(plan, set_values(form, {"country": "Canada +1", "phone": "(555) 010-0199"}))
    assert formfill.strip_dial_code("+44 United Kingdom") == "United Kingdom"


# ── 7: a one-option consent group ─────────────────────────────────────────────────────────────
CONSENT = field("I agree to the privacy policy", "checkbox-group", name="consent", required=True,
                options=["I agree"], value=[])  # fmt: skip


def test_a_one_option_consent_group_ticks_on_the_operators_yes(formfill, prov):
    said = prov.operator_utterances({"messages": [human(BRIEF)]})
    plan = formfill.build_plan([CONSENT], {}, {"consent": "yes"}, operator_said=said)
    row = plan["rows"][0]
    assert row["action"] == "check" and row["ticks"] == ["I agree"] and row["provenance"] == "operator"
    assert formfill.diff(plan, [{**CONSENT, "value": ["I agree"]}]) == []


def test_a_one_option_consent_group_never_takes_the_agents_yes(formfill, prov):
    said = prov.operator_utterances({"messages": [human("Fill it in, don't submit.")]})
    plan = formfill.build_plan([CONSENT], {}, {"consent": "yes"}, operator_said=said)
    assert plan["rows"] == [] and plan["unmapped"][0]["agent_answer"] == "yes"


# ── 8: the pronouns group is addressed by its checkboxes, not a label 11 fields share ─────────
def test_a_group_whose_label_is_shared_is_selected_by_css(tools):
    form = [
        field("Pronouns", "checkbox-group", name="pronouns", options=["He/him", "She/her", "They/them"], value=[]),
        field("Custom", "checkbox", id="customPronounsOption", value=False),
        field("pronouns", id="customPronounsTextField", name="pronouns"),
    ]
    state = {"messages": [human("My pronouns: They/them.")]}
    out = _plan(tools, form, {"pronouns": "They/them"}, state, company="Spotify", ats="lever")
    assert """browser_select(field='input[type="checkbox"][name="pronouns"]', option_text="They/them")""" in out
