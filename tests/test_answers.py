"""Saved standard application answers — confirm once, reuse only confirmed (#4032 phase 3a).

Host-free: the store lives under ``CAREERCOACH_DIR`` (a temp dir), and the tools round-trip through
``register()`` with the vendored testkit.
"""

from __future__ import annotations

import importlib

import pytest
from _plugin_testkit import FakeRegistry


@pytest.fixture
def answers(plugin, iso, monkeypatch):
    """The plugin's ``answers`` module, with an isolated store (same ``CAREERCOACH_DIR`` the ``tools``
    fixture uses, so a test may mix the two)."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".answers")


# ── the core state machine: draft → confirmed → (edit) → draft ─────────────────────────
def test_a_draft_is_excluded_from_confirmed(answers):
    answers.propose("phone_number", "555-123-4567")
    assert answers.confirmed() == {}  # r4: form filling draws only from confirmed
    assert answers.load()["phone_number"]["status"] == "draft"


def test_confirm_promotes_a_draft(answers):
    answers.propose("phone_number", "555-123-4567")
    assert answers.confirm(["phone_number"]) == ["phone_number"]
    assert answers.confirmed() == {"phone_number": "555-123-4567"}
    entry = answers.load()["phone_number"]
    assert entry["status"] == "confirmed" and entry["confirmed_at"]


def test_editing_a_confirmed_value_demotes_it(answers):
    answers.propose("current_location", "Austin, Texas, United States")
    answers.confirm(["current_location"])
    assert "current_location" in answers.confirmed()

    answers.propose("current_location", "Dublin, Ireland")  # a correction
    assert answers.confirmed() == {}
    assert answers.load()["current_location"]["status"] == "draft"


def test_re_proposing_the_same_confirmed_value_keeps_it_confirmed(answers):
    answers.propose("email", "ada@example.com")
    answers.confirm(["email"])
    answers.propose("email", "ada@example.com")  # identical — not an edit
    assert answers.confirmed() == {"email": "ada@example.com"}


def test_confirm_skips_a_key_with_no_stored_value(answers):
    assert answers.confirm(["phone_number"]) == []  # nothing proposed yet → nothing to confirm
    assert answers.confirmed() == {}


# ── seeding from the verified profile ──────────────────────────────────────────────────
def test_seed_never_overwrites_an_existing_entry(answers):
    answers.propose("current_location", "Berlin, Germany")
    answers.confirm(["current_location"])
    prof = {
        "identity": {
            "contact": "ada@example.com · +1 555-234-5678 · https://linkedin.com/in/adalovelace",
            "location": "London, United Kingdom",
            "work_auth": "UK citizen",
        }
    }
    answers.seed_from_profile(prof)
    stored = answers.load()

    # the confirmed entry is untouched — not overwritten by the profile's location
    assert stored["current_location"]["value"] == "Berlin, Germany"
    assert stored["current_location"]["status"] == "confirmed"
    # the unambiguous contact pieces seeded as drafts
    assert stored["email"]["value"] == "ada@example.com" and stored["email"]["status"] == "draft"
    assert stored["linkedin_url"]["value"] == "https://linkedin.com/in/adalovelace"
    assert "555-234-5678" in stored["phone_number"]["value"]
    assert stored["authorized_us"]["value"] == "UK citizen" and stored["authorized_us"]["status"] == "draft"


def test_ambiguous_contact_pieces_are_left_for_the_operator(answers):
    answers.seed_from_profile({"identity": {"contact": "ada@x.com, ada@y.com"}})
    assert "email" not in answers.load()  # two emails → neither is unambiguous


def test_self_id_keys_default_to_decline_as_a_draft(answers):
    created = answers.seed_from_profile({"identity": {}})
    stored = answers.load()
    for key in answers.SELF_ID_KEYS:
        assert key in created
        assert stored[key]["value"] == "Decline to self-identify"
        assert stored[key]["status"] == "draft"
    assert answers.confirmed() == {}  # a default decline is still only a draft


# ── unknown keys ───────────────────────────────────────────────────────────────────────
def test_unknown_key_raises_in_the_module_and_errors_in_the_tools(answers, tools):
    with pytest.raises(KeyError):
        answers.propose("favourite_colour", "green")

    out = tools["careercoach_propose_answer"].invoke({"key": "favourite_colour", "value": "green"})
    assert "Unknown answer key" in out and "phone_number" in out  # lists the valid keys

    out = tools["careercoach_confirm_answers"].invoke({"keys": "favourite_colour"})
    assert "Unknown answer key" in out


# ── an unreadable store is never written over (the shared store's refuse-writes rule) ──
def test_an_unreadable_answers_file_is_never_written_over(answers, tools):
    """load() turns a corrupt file into {}; before the fix propose/confirm/seed saved from that and
    one bad byte silently overwrote every saved and confirmed answer. A writer must refuse instead."""
    answers.propose("phone_number", "555-123-4567")
    answers.confirm(["phone_number"])
    answers.propose("email", "ada@example.com")
    path = answers._path()
    # A one-character hand edit breaks the JSON, like a torn write or a fat-fingered edit.
    path.write_text(path.read_text() + ",", encoding="utf-8")
    broken = path.read_bytes()

    # Every writer refuses rather than reading the file as empty and saving {} over it.
    with pytest.raises(answers.StoreUnreadable):
        answers.propose("linkedin_url", "https://linkedin.com/in/ada")
    with pytest.raises(answers.StoreUnreadable):
        answers.confirm(["email"])
    with pytest.raises(answers.StoreUnreadable):
        answers.seed_from_profile({"identity": {"location": "London"}})
    assert path.read_bytes() == broken, "a refused write must leave the file exactly as it was"

    # The degrading reader still never raises, and reports why via load_checked().
    assert answers.load() == {} and answers.confirmed() == {}
    assert "not valid JSON" in answers.load_checked()[1]

    # The agent-facing tools report it instead of raising or silently overwriting.
    assert "unreadable" in tools["careercoach_get_answers"].invoke({})
    assert "Not saved" in tools["careercoach_propose_answer"].invoke({"key": "phone_number", "value": "x"})
    assert "Not confirmed" in tools["careercoach_confirm_answers"].invoke({"keys": "email"})
    assert path.read_bytes() == broken


def test_a_wrongly_shaped_answers_file_counts_as_unreadable(answers, tools):
    """Parses as JSON but holds a shape this module can't read — reading it as empty would report
    "nothing saved yet" and the next write would erase it, so it counts as unreadable."""
    answers.propose("phone_number", "555-9000")
    path = answers._path()
    for broken in ('["not", "an", "object"]', '{"answers": "corrupted"}'):
        path.write_text(broken, encoding="utf-8")

        assert answers.load() == {}  # reads as empty, never raises
        assert answers.load_checked()[1]  # a non-empty reason
        with pytest.raises(answers.StoreUnreadable):
            answers.propose("email", "ada@example.com")
        assert path.read_text(encoding="utf-8") == broken  # the damaged file is left alone
        assert "unreadable" in tools["careercoach_get_answers"].invoke({})


# ── the always-on profile block reports the confirmed/total count (r6) ──────────────────
def test_the_profile_block_reports_the_confirmed_count(plugin, answers):
    answers.propose("phone_number", "555-9000")
    answers.confirm(["phone_number"])
    total = len(answers.STANDARD_KEYS)
    note = plugin._standard_answers_note()
    assert note == f"1/{total} standard answers confirmed; unconfirmed answers are never used to fill a form."

    block = "<operator_profile>\nKNOWN:\n  name: Ada\n</operator_profile>"
    spliced = plugin._with_standard_answers(block)
    assert note in spliced
    assert spliced.count("</operator_profile>") == 1 and spliced.endswith("</operator_profile>")


class _Req:
    """A minimal stand-in for the middleware's model request — just ``messages`` + ``override``."""

    def __init__(self, messages):
        self.messages = messages

    def override(self, *, messages):
        return _Req(messages)


def test_the_middleware_frame_carries_the_answers_note(plugin, profile, answers):
    profile.update_field("name", "Ada Lovelace")  # a non-empty profile → the block is injected
    answers.propose("phone_number", "555-9000")
    answers.confirm(["phone_number"])

    reg = FakeRegistry()
    plugin.register(reg)
    assert len(reg.middlewares) == 1, "langchain is a dev dependency, so the profile middleware registers"
    mw = reg.middlewares[0](None)
    mw.before_agent({}, None)

    captured: dict = {}
    mw.wrap_model_call(_Req([]), lambda req: captured.setdefault("req", req))
    frame = captured["req"].messages[-1]
    text = frame.content if isinstance(frame.content, str) else str(frame.content)

    assert "Ada Lovelace" in text  # the profile block still rides
    assert "standard answers confirmed" in text and "1/" in text
    assert text.count("</operator_profile>") == 1


# ── the tools round-trip through register() with the vendored testkit ───────────────────
def test_the_tools_round_trip_through_register(tools, answers):
    for name in ("careercoach_get_answers", "careercoach_propose_answer", "careercoach_confirm_answers"):
        assert name in tools

    total = len(answers.STANDARD_KEYS)
    first = tools["careercoach_get_answers"].invoke({})  # first use seeds from the (empty) profile
    assert "Decline to self-identify" in first  # self-ID declines appear
    assert f"0/{total} confirmed." in first  # nothing confirmed yet

    saved = tools["careercoach_propose_answer"].invoke({"key": "phone_number", "value": "555-9000"})
    assert "draft" in saved and "careercoach_confirm_answers" in saved  # won't fill a form until confirmed
    assert "DRAFT" in tools["careercoach_get_answers"].invoke({})

    tools["careercoach_confirm_answers"].invoke({"keys": "phone_number"})
    after = tools["careercoach_get_answers"].invoke({})
    assert "phone_number: 555-9000" in after and f"1/{total} confirmed." in after
