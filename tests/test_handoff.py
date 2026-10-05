"""The visible-browser handoff (#4032 phase 4c).

Where a human is genuinely required — a captcha, a login, a legal attestation — the agent fills
everything else and hands over a visible browser for that one step. ``careercoach_handoff`` shares
the submit gate's ``interrupt`` + headless-refusal pattern (bd-ywmm.4), so the tests mirror
``test_submitgate.py``: the headless refusal takes no interrupt, Done/Cancel are driven by a
monkeypatched interrupt, reason validation is checked, and the langgraph pause must re-raise. The
end-to-end ``apply-form`` skill is held to the same contract as the other skills: it parses, and its
frontmatter declares the tools its steps call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "apply-form" / "SKILL.md"


# ── fixtures ────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def handoff(tools, plugin):
    """The ``careercoach_handoff`` tool plus the plugin module whose globals it reads, so a test can
    monkeypatch the interrupt / headless seams the same way the submit-gate tests do."""
    import types

    return types.SimpleNamespace(plugin=plugin, tool=tools["careercoach_handoff"])


# ── careercoach_handoff ───────────────────────────────────────────────────────────────────────
def test_handoff_refuses_headless_and_does_not_interrupt(handoff, monkeypatch):
    monkeypatch.setattr(handoff.plugin, "_turn_is_headless", lambda: True)
    monkeypatch.setattr(
        handoff.plugin,
        "_handoff_interrupt",
        lambda payload: pytest.fail("a headless turn must not interrupt"),
    )
    out = handoff.tool.invoke({"session_id": "sid-1", "reason": "captcha"})
    assert "headless" in out or "no operator" in out
    assert "captcha" in out  # it names the step a human still has to finish
    assert "stop" in out.lower()


def test_handoff_done_requires_a_fresh_read_back_and_verify(handoff, monkeypatch):
    monkeypatch.setattr(handoff.plugin, "_turn_is_headless", lambda: False)

    seen = {}

    def done(payload):
        seen["payload"] = payload
        return "done"

    monkeypatch.setattr(handoff.plugin, "_handoff_interrupt", done)
    out = handoff.tool.invoke({"session_id": "sid-42", "reason": "login"})

    # Done → mandatory read-back + verify before any submit, carrying the session id through.
    assert "browser_form_read" in out
    assert "careercoach_verify_fill" in out and "sid-42" in out
    assert "VERIFIED" in out and "careercoach_request_submit" in out
    # The card the operator saw named the step and pointed at a visible browser.
    card = seen["payload"]
    assert card["reason"] == "login" and card["session_id"] == "sid-42"
    assert card["options"] == ["done", "cancel"]
    assert "Browser panel" in card["prompt"] and "headed" in card["prompt"]


def test_handoff_cancel_returns_the_stop_message(handoff, monkeypatch):
    monkeypatch.setattr(handoff.plugin, "_turn_is_headless", lambda: False)
    monkeypatch.setattr(handoff.plugin, "_handoff_interrupt", lambda payload: "cancel")
    out = handoff.tool.invoke({"session_id": "sid-1", "reason": "attestation"})
    assert out == "Operator cancelled the handoff; stop."


def test_handoff_treats_a_vague_answer_as_cancel(handoff, monkeypatch):
    """Default-deny: anything that isn't an unambiguous Done stops, so an ambiguous answer never
    reads as 'the step is complete' and marches on toward submit."""
    monkeypatch.setattr(handoff.plugin, "_turn_is_headless", lambda: False)
    monkeypatch.setattr(handoff.plugin, "_handoff_interrupt", lambda payload: "")
    out = handoff.tool.invoke({"session_id": "sid-1", "reason": "other"})
    assert out == "Operator cancelled the handoff; stop."


def test_handoff_validates_the_reason(handoff, monkeypatch):
    # An unknown reason is refused with the valid set, and WITHOUT interrupting anyone.
    monkeypatch.setattr(handoff.plugin, "_turn_is_headless", lambda: False)
    monkeypatch.setattr(
        handoff.plugin, "_handoff_interrupt", lambda payload: pytest.fail("a bad reason must not interrupt")
    )
    out = handoff.tool.invoke({"session_id": "sid-1", "reason": "bribe-the-recruiter"})
    assert "Unknown handoff reason" in out
    for valid in ("captcha", "login", "attestation", "other"):
        assert valid in out


@pytest.mark.parametrize("reason", ["captcha", "login", "attestation", "other"])
def test_handoff_accepts_every_valid_reason(handoff, monkeypatch, reason):
    monkeypatch.setattr(handoff.plugin, "_turn_is_headless", lambda: False)
    monkeypatch.setattr(handoff.plugin, "_handoff_interrupt", lambda payload: "done")
    out = handoff.tool.invoke({"session_id": "sid-1", "reason": reason})
    assert "browser_form_read" in out and "careercoach_verify_fill" in out


@pytest.mark.parametrize("answer", ["done", "cancel"])
def test_handoff_invalidates_the_pre_handoff_verification_and_grant(handoff, monkeypatch, answer):
    """The gap the review caught: a captcha / login / attestation can re-render the form and clear
    filled fields, so the verification made BEFORE the handoff no longer describes the live form. The
    handoff must drop that verification — and revoke any live submit grant — so a fresh
    browser_form_read + careercoach_verify_fill (and a fresh approval card) are required before any
    submit can go through. Both the submit gate's tool (``_submit_ready`` reads ``verified``) and its
    middleware (``formfill.is_verified`` + a live grant) must then refuse the stale state."""
    import importlib

    formfill = importlib.import_module(handoff.plugin.__name__ + ".formfill")
    submitgate = importlib.import_module(handoff.plugin.__name__ + ".submitgate")

    plan = formfill.build_plan([{"label": "Email", "kind": "text", "required": True}], {"email": "ada@example.com"}, {})
    sid = plan["session_id"]
    formfill.record_verification(sid, [])  # empty mismatches → verified, as the pre-handoff fill was
    vid = formfill.current_verification(sid)  # the read-back minted a verification event the grant binds to
    submitgate.grant(sid, vid)  # a pre-handoff submit grant, as if request_submit had already been approved
    assert formfill.is_verified(sid) and submitgate.active_grant() == (sid, vid)

    monkeypatch.setattr(handoff.plugin, "_turn_is_headless", lambda: False)
    monkeypatch.setattr(handoff.plugin, "_handoff_interrupt", lambda payload: answer)
    handoff.tool.invoke({"session_id": sid, "reason": "captcha"})

    # Whichever way the operator answered, the stale pre-handoff state can no longer authorize submit.
    assert not formfill.is_verified(sid), "the pre-handoff verification must be cleared"
    assert submitgate.active_grant() is None, "any pre-handoff submit grant must be revoked"


def test_handoff_never_touches_the_captcha_and_its_docstring_says_so(handoff):
    doc = handoff.tool.description
    assert "never" in doc.lower()
    assert "captcha" in doc.lower() and "bypass" in doc.lower()


def test_handoff_reraises_the_interrupt_pause(handoff, monkeypatch):
    """The langgraph pause signal must reach the runtime — the tool's catch-all must not swallow it
    into a string, or the human-in-the-loop pause would silently break."""
    from langgraph.errors import GraphInterrupt

    monkeypatch.setattr(handoff.plugin, "_turn_is_headless", lambda: False)

    def pause(payload):
        raise GraphInterrupt(())

    monkeypatch.setattr(handoff.plugin, "_handoff_interrupt", pause)
    with pytest.raises(GraphInterrupt):
        handoff.tool.invoke({"session_id": "sid-1", "reason": "captcha"})


# ── the apply-form skill ──────────────────────────────────────────────────────────────────────
def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} needs YAML frontmatter"
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


def test_apply_form_skill_parses_and_declares_the_loop_tools(plugin, registry):
    """It parses, carries name + description, and its frontmatter lists the tools the loop turns on —
    the handoff, the submit gate, and the plan/verify read-back pair the card requires. Every
    ``careercoach_*`` tool it declares must actually be registered (the silent-no-op failure mode)."""
    plugin.register(registry)
    registered = {getattr(t, "name", str(t)) for t in registry.tools}

    fm, body = _frontmatter(SKILL)
    assert fm["name"] == "apply-form"
    assert fm["description"].strip(), "the description is what routes the agent here"

    declared = fm["tools"]
    required = {
        "careercoach_handoff",
        "careercoach_request_submit",
        "careercoach_verify_fill",
        "careercoach_plan_fill",
    }
    assert required <= set(declared), f"apply-form must declare {sorted(required)}"

    for name in declared:
        if name.startswith("careercoach_"):
            assert name in registered, f"apply-form declares unknown tool {name!r}"
        # …and a declared tool no step names is a hint the agent will reach for nothing.
        assert name in body, f"apply-form declares {name!r} but no step tells the agent to use it"


def test_apply_form_skill_states_the_two_hard_rules_and_the_support_scope():
    """The doctrine must say plainly: never submit without the approval card, never solve a captcha,
    and Lever/Workday aren't supported yet (use the live-form path with care)."""
    _, body = _frontmatter(SKILL)
    flat = " ".join(body.split())
    assert "Never submit without the approval card" in flat
    assert "Never solve or bypass a captcha" in flat
    # the loop names both the submit gate and the handoff
    assert "careercoach_request_submit" in flat and "careercoach_handoff" in flat
    # support scope: Greenhouse + Ashby supported, Lever/Workday not yet
    assert "Greenhouse" in flat and "Ashby" in flat
    assert "Lever and Workday are NOT supported yet" in flat
    assert "live-form path" in flat
