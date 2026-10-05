"""The HARD submit gate (#4032 phase 3c).

Four layers, all host-free:
  * submit detection (``is_submit_like``) — pure;
  * one-shot, time-limited grant state — pure, with monotonic time monkeypatched so expiry is
    tested without sleeping;
  * the blocking middleware — exercised with fake request/handler objects (no graph run);
  * ``careercoach_request_submit`` — its verify refusal, its headless refusal (no interrupt), and
    the grant it creates only on an approve answer from a monkeypatched interrupt.
"""

from __future__ import annotations

import importlib
import types

import pytest
from _plugin_testkit import FakeRegistry


# ── fixtures ────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def submitgate(plugin):
    return importlib.import_module(plugin.__name__ + ".submitgate")


@pytest.fixture
def gate(plugin, iso, monkeypatch):
    """The whole wired gate in one isolated store: the request_submit tool, the submit-gate
    middleware instance, and the live ``formfill`` / ``submitgate`` modules that back them."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    reg = FakeRegistry({"packet_root": str(iso / "ws")})
    plugin.register(reg)
    tools = {t.name: t for t in reg.tools}
    middleware = next(m for m in (f(None) for f in reg.middlewares) if type(m).__name__ == "_SubmitGateMiddleware")
    return types.SimpleNamespace(
        plugin=plugin,
        tool=tools["careercoach_request_submit"],
        middleware=middleware,
        formfill=importlib.import_module(plugin.__name__ + ".formfill"),
        submitgate=importlib.import_module(plugin.__name__ + ".submitgate"),
    )


class FakeReq:
    """A stand-in for langchain's ``ToolCallRequest`` — only ``.tool_call`` is read by the gate."""

    def __init__(self, name, args, call_id="call-1"):
        self.tool_call = {"name": name, "args": args, "id": call_id}


def _verified_session(ff, *, company="Acme", role="ML Engineer"):
    """Plan + verify a one-field form, returning the session id of a VERIFIED, unchanged plan."""
    plan = ff.build_plan([{"label": "Email", "kind": "text", "required": True}], {"email": "ada@example.com"}, {})
    sid = plan["session_id"]
    ff.update_session(sid, company=company, role=role)
    ff.record_verification(sid, [])  # empty mismatches → verified, plan hash recorded == sid
    return sid


# ── submit detection ──────────────────────────────────────────────────────────────────────
def test_is_submit_like_positives(submitgate):
    assert submitgate.is_submit_like("browser_click", {"selector": "Submit application"})
    assert submitgate.is_submit_like("browser_click", {"selector": "button[type=submit]"})
    assert submitgate.is_submit_like("browser_press", {"key": "Enter"})
    assert submitgate.is_submit_like("browser_eval", {"script": "document.forms[0].requestSubmit()"})
    # case-insensitive, the other send verbs, and the quoted type attribute
    assert submitgate.is_submit_like("browser_click", {"selector": "SUBMIT YOUR APPLICATION"})
    assert submitgate.is_submit_like("browser_click", {"selector": "Send your application"})
    assert submitgate.is_submit_like("browser_click", {"selector": "Complete application"})
    assert submitgate.is_submit_like("browser_click", {"selector": "Finish"})
    assert submitgate.is_submit_like("browser_click", {"selector": 'input[type="submit"]'})
    assert submitgate.is_submit_like("browser_press", {"key": "Return"})
    assert submitgate.is_submit_like("browser_eval", {"script": "form.submit()"})


def test_is_submit_like_negatives(submitgate):
    assert not submitgate.is_submit_like("browser_click", {"selector": "Attach"})
    assert not submitgate.is_submit_like("browser_click", {"selector": "Country"})
    assert not submitgate.is_submit_like("browser_press", {"key": "Tab"})
    assert not submitgate.is_submit_like("browser_eval", {"script": "document.querySelector('#name').value"})
    # a bare programmatic click with no send word is not a submit
    assert not submitgate.is_submit_like("browser_eval", {"script": "document.querySelector('.menu').click()"})
    # "Apply" OPENS the application form (before any fill session exists) and "Apply filters" is a
    # listing control — neither is the final submit, so neither is gated. Regression: a bare
    # \bapply\b matched "Apply filters" and the open-form "Apply", which dead-ended the normal flow
    # and could spend the one-shot grant on a harmless click, refusing the real submit.
    assert not submitgate.is_submit_like("browser_click", {"selector": "Apply"})
    assert not submitgate.is_submit_like("browser_click", {"selector": "Apply now"})
    assert not submitgate.is_submit_like("browser_click", {"selector": "Apply filters"})
    assert not submitgate.is_submit_like("browser_eval", {"script": "document.querySelector('.apply-btn').click()"})
    # other browser tools never read as submit, whatever their args
    assert not submitgate.is_submit_like("browser_fill", {"selector": "Submit", "value": "x"})
    assert not submitgate.is_submit_like("browser_upload", {"selector": "Submit"})
    assert not submitgate.is_submit_like("careercoach_request_submit", {"session_id": "abc"})


# ── grant state: one-shot + expiry ──────────────────────────────────────────────────────────
def test_grant_is_one_shot_and_expires(submitgate, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(submitgate, "_now", lambda: clock["t"])

    submitgate.grant("sid-1", ttl_s=120)
    assert submitgate.active_grant() == "sid-1"

    # One-shot: the first consume wins, the second finds nothing.
    assert submitgate.consume("sid-1") is True
    assert submitgate.consume("sid-1") is False
    assert submitgate.active_grant() is None

    # Expiry: a fresh grant is gone once the clock passes its TTL.
    submitgate.grant("sid-2", ttl_s=120)
    assert submitgate.active_grant() == "sid-2"
    clock["t"] = 1000.0 + 121
    assert submitgate.active_grant() is None
    assert submitgate.consume("sid-2") is False


def test_grant_supersedes_prior_and_is_session_scoped(submitgate, monkeypatch):
    monkeypatch.setattr(submitgate, "_now", lambda: 0.0)
    submitgate.grant("sid-A")
    submitgate.grant("sid-B")  # a fresh grant supersedes the old one
    assert submitgate.active_grant() == "sid-B"
    assert submitgate.consume("sid-A") is False  # the superseded one can't authorize anything
    assert submitgate.consume("sid-B") is True


# ── the blocking middleware ─────────────────────────────────────────────────────────────────
def test_middleware_blocks_without_a_grant(gate):
    ran = []
    result = gate.middleware.wrap_tool_call(
        FakeReq("browser_click", {"selector": "Submit application"}, call_id="c1"),
        lambda req: ran.append(req) or "RAN",
    )
    assert ran == []  # the browser tool never ran
    assert type(result).__name__ == "ToolMessage"
    assert "Blocked by careercoach submit gate" in result.content
    assert result.tool_call_id == "c1"  # same id keeps the transcript valid


def test_middleware_allows_exactly_one_call_with_a_grant(gate):
    sid = _verified_session(gate.formfill)  # a grant is only spent while its session stays VERIFIED
    gate.submitgate.grant(sid)
    calls = []

    def handler(req):
        calls.append(req.tool_call["id"])
        return "RAN"

    first = gate.middleware.wrap_tool_call(FakeReq("browser_press", {"key": "Enter"}, call_id="c1"), handler)
    assert first == "RAN" and calls == ["c1"]  # grant consumed, call ran

    # The grant was one-shot: a second submit-like call is blocked.
    second = gate.middleware.wrap_tool_call(FakeReq("browser_press", {"key": "Enter"}, call_id="c2"), handler)
    assert type(second).__name__ == "ToolMessage" and "Blocked" in second.content
    assert calls == ["c1"]  # the second never ran


def test_middleware_passes_non_submit_calls_untouched(gate):
    ran = []
    result = gate.middleware.wrap_tool_call(
        FakeReq("browser_fill", {"selector": "Email", "value": "a@b.com"}),
        lambda req: ran.append(req) or "RAN",
    )
    assert result == "RAN" and len(ran) == 1
    # A non-submit call does not touch the grant (so it can't starve a real submit of its grant).
    sid = _verified_session(gate.formfill)
    gate.submitgate.grant(sid)
    gate.middleware.wrap_tool_call(FakeReq("browser_form_read", {}), lambda req: "RAN")
    assert gate.submitgate.active_grant() == sid


def test_middleware_lets_an_apply_click_through_without_spending_the_grant(gate):
    # Regression: an "Apply" button opens the form and "Apply filters" is a listing control —
    # neither is a submit, so with a live grant the middleware passes them through untouched and
    # does NOT consume the one-shot grant, leaving it for the real submit click.
    sid = _verified_session(gate.formfill)
    gate.submitgate.grant(sid)
    ran = []
    for label in ("Apply", "Apply now", "Apply filters"):
        out = gate.middleware.wrap_tool_call(
            FakeReq("browser_click", {"selector": label}), lambda req: ran.append(label) or "RAN"
        )
        assert out == "RAN"
    assert ran == ["Apply", "Apply now", "Apply filters"]  # every harmless click ran
    assert gate.submitgate.active_grant() == sid  # the grant is still live for the real submit

    submit = gate.middleware.wrap_tool_call(
        FakeReq("browser_click", {"selector": "Submit application"}), lambda req: "SUBMITTED"
    )
    assert submit == "SUBMITTED" and gate.submitgate.active_grant() is None  # now it's spent


def test_middleware_checks_the_grant_against_its_session_not_just_that_one_is_live(gate):
    # The grant is checked AGAINST the session it was approved for, not merely "is any grant live".
    # A browser click carries no session id, so the middleware enforces the binding by requiring the
    # grant's session to still be VERIFIED: re-planning resets verification, so the still-live grant
    # becomes inert and can't authorize a submit against a form that is no longer the verified one.
    sid = _verified_session(gate.formfill)
    gate.submitgate.grant(sid)
    assert gate.submitgate.active_grant() == sid  # a grant is live…

    gate.formfill.build_plan([{"label": "Email", "kind": "text", "required": True}], {"email": "ada@example.com"}, {})
    assert gate.formfill.is_verified(sid) is False  # …but a re-plan reset verification

    out = gate.middleware.wrap_tool_call(
        FakeReq("browser_click", {"selector": "Submit application"}, call_id="c9"), lambda req: "RAN"
    )
    assert type(out).__name__ == "ToolMessage" and "Blocked" in out.content
    assert out.tool_call_id == "c9"
    assert gate.submitgate.active_grant() == sid  # the inert grant was NOT spent on the blocked call


def test_middleware_blocks_a_submit_after_a_different_form_is_planned(gate):
    # The bd-4t17 hole: the operator approves form A (grant live for 120s), then the agent plans and
    # fills a DIFFERENT form B and clicks submit on B. Verification is exclusive to the newest plan,
    # so planning B unverifies A — A's grant goes inert and the submit on B is BLOCKED, even though
    # A's grant never expired and was never re-planned itself.
    sid_a = _verified_session(gate.formfill, company="Acme", role="ML Engineer")
    gate.submitgate.grant(sid_a)
    assert gate.submitgate.active_grant() == sid_a  # a grant for A is live…

    # Plan a genuinely different form B (different rows → different session id).
    plan_b = gate.formfill.build_plan(
        [{"label": "Phone", "kind": "text", "required": True}], {"phone_number": "555-0100"}, {}
    )
    assert plan_b["session_id"] != sid_a
    assert gate.formfill.is_verified(sid_a) is False  # …but planning B revoked A's verification

    out = gate.middleware.wrap_tool_call(
        FakeReq("browser_click", {"selector": "Submit application"}, call_id="cB"), lambda req: "RAN"
    )
    assert type(out).__name__ == "ToolMessage" and "Blocked" in out.content
    assert out.tool_call_id == "cB"
    assert gate.submitgate.active_grant() == sid_a  # the inert grant was NOT spent on the blocked click


def test_middleware_blocks_a_submit_after_a_same_fields_different_company_form(gate):
    # The exact bd-4t17 collision: form B has the SAME fields and answers as the approved form A, but
    # is a different application (different company). Because the session id folds in company/role, B
    # gets a DIFFERENT id — it does not reuse A's slot and re-verify it — so planning B unverifies A
    # (verification is exclusive) and A's still-live grant can't authorize a submit on B.
    ff = gate.formfill
    rows = [{"label": "Email", "kind": "text", "required": True}]
    confirmed = {"email": "ada@example.com"}

    sid_a = ff.build_plan(rows, confirmed, {}, company="Acme", role="ML Engineer")["session_id"]
    ff.update_session(sid_a, company="Acme", role="ML Engineer")
    ff.record_verification(sid_a, [])  # A verified and approved
    gate.submitgate.grant(sid_a)
    assert gate.submitgate.active_grant() == sid_a and ff.is_verified(sid_a) is True

    # Same fields + answers, DIFFERENT company → a genuinely different application, so a different id.
    sid_b = ff.build_plan(rows, confirmed, {}, company="Globex", role="ML Engineer")["session_id"]
    assert sid_b != sid_a, "same rows but a different company must not collide onto A's id"
    assert ff.is_verified(sid_a) is False  # planning B revoked A's verification
    assert ff.is_verified(sid_b) is False  # and a freshly planned B is unverified

    out = gate.middleware.wrap_tool_call(
        FakeReq("browser_click", {"selector": "Submit application"}, call_id="cC"), lambda req: "RAN"
    )
    assert type(out).__name__ == "ToolMessage" and "Blocked" in out.content
    assert out.tool_call_id == "cC"
    assert gate.submitgate.active_grant() == sid_a  # the inert grant was NOT spent on the blocked click


def test_middleware_blocks_a_submit_after_a_same_fields_different_posting_form(gate):
    # The bd-ywmm.4 residue the review caught: careercoach_prepare_application defaults company/role
    # to "" but carries the posting URL. Two DIFFERENT postings with identical questions and blank
    # company/role must NOT collide onto one slot — else a clean read-back of B would re-verify A and
    # A's live grant would authorize a submit on B. The posting URL is folded into the id, so B is a
    # genuinely different session: planning it unverifies A (verification is exclusive) and the submit
    # on B is BLOCKED.
    ff = gate.formfill
    rows = [{"label": "Email", "kind": "text", "required": True}]
    confirmed = {"email": "ada@example.com"}

    sid_a = ff.build_plan(rows, confirmed, posting="https://job-boards.greenhouse.io/acme/jobs/1")["session_id"]
    ff.record_verification(sid_a, [])  # A verified and approved
    gate.submitgate.grant(sid_a)
    assert gate.submitgate.active_grant() == sid_a and ff.is_verified(sid_a) is True

    # Same fields + answers + blank company/role, DIFFERENT posting → a different application, so a
    # different id — not a silent reuse of A's slot.
    sid_b = ff.build_plan(rows, confirmed, posting="https://job-boards.greenhouse.io/globex/jobs/2")["session_id"]
    assert sid_b != sid_a, "same rows + blank identity but a different posting must not collide onto A"
    assert ff.is_verified(sid_a) is False  # planning B revoked A's verification
    assert ff.is_verified(sid_b) is False  # and a freshly planned B is unverified

    out = gate.middleware.wrap_tool_call(
        FakeReq("browser_click", {"selector": "Submit application"}, call_id="cD"), lambda req: "RAN"
    )
    assert type(out).__name__ == "ToolMessage" and "Blocked" in out.content
    assert out.tool_call_id == "cD"
    assert gate.submitgate.active_grant() == sid_a  # the inert grant was NOT spent on the blocked click


# ── careercoach_request_submit ──────────────────────────────────────────────────────────────
def test_request_submit_refuses_unverified_session(gate, monkeypatch):
    # A planned-but-not-verified session (no read-back diff recorded yet).
    plan = gate.formfill.build_plan([{"label": "Email", "kind": "text", "required": True}], {"email": "a@b.com"}, {})
    sid = plan["session_id"]
    # Guard: even if an operator were present, an unverified session is refused before asking.
    monkeypatch.setattr(gate.plugin, "_turn_is_headless", lambda: False)
    monkeypatch.setattr(
        gate.plugin, "_submitgate_interrupt", lambda payload: pytest.fail("must not interrupt an unverified session")
    )
    out = gate.tool.invoke({"session_id": sid})
    assert "not authorized" in out and "VERIFIED" in out
    assert gate.submitgate.active_grant() is None


def test_submit_ready_requires_verified(gate):
    # _submit_ready as a unit: a verified session is ready; anything unverified is refused with a
    # readable reason. (There is no dead rows-hash compare — "changed since verified" is enforced by
    # formfill resetting verification on every plan write, exercised below.)
    ok, reason = gate.plugin._submit_ready({"verified": True}, "sid-xyz")
    assert ok is True and reason == ""
    blocked, reason = gate.plugin._submit_ready({"verified": False}, "sid-xyz")
    assert blocked is False and "VERIFIED" in reason
    missing, reason = gate.plugin._submit_ready(None, "sid-xyz")
    assert missing is False and reason


def test_request_submit_refuses_after_a_replan(gate, monkeypatch):
    """A verified session that is re-planned comes back UNVERIFIED and is refused WITHOUT asking the
    operator. Re-planning (even the identical form) is a new fill cycle that was never read back, so
    this is the real 'plan changed since it was verified' guard — a formfill reset, not a hash
    compare that can never fail (the old guard: session_id IS the rows hash, which
    record_verification recomputes identically)."""
    ff = gate.formfill
    sid = _verified_session(ff)
    assert ff.is_verified(sid) is True

    # Re-plan the SAME one-field form: identical rows → same session id, but verification is cleared.
    again = ff.build_plan([{"label": "Email", "kind": "text", "required": True}], {"email": "ada@example.com"}, {})
    assert again["session_id"] == sid and ff.is_verified(sid) is False

    monkeypatch.setattr(gate.plugin, "_turn_is_headless", lambda: False)
    monkeypatch.setattr(
        gate.plugin, "_submitgate_interrupt", lambda payload: pytest.fail("must not interrupt a re-planned session")
    )
    out = gate.tool.invoke({"session_id": sid})
    assert "not authorized" in out.lower()
    assert gate.submitgate.active_grant() is None


def test_request_submit_refuses_headless_and_does_not_interrupt(gate, monkeypatch):
    sid = _verified_session(gate.formfill)
    monkeypatch.setattr(gate.plugin, "_turn_is_headless", lambda: True)
    monkeypatch.setattr(
        gate.plugin,
        "_submitgate_interrupt",
        lambda payload: pytest.fail("a headless turn must not interrupt"),
    )
    out = gate.tool.invoke({"session_id": sid})
    assert "headless" in out or "no operator" in out
    assert gate.submitgate.active_grant() is None  # no grant without an operator


def test_request_submit_grants_only_on_approve(gate, monkeypatch):
    sid = _verified_session(gate.formfill, company="Acme", role="ML Engineer")
    monkeypatch.setattr(gate.plugin, "_turn_is_headless", lambda: False)

    seen = {}

    def approve(payload):
        seen["payload"] = payload
        return "approve"

    monkeypatch.setattr(gate.plugin, "_submitgate_interrupt", approve)
    out = gate.tool.invoke({"session_id": sid})

    assert "authorized" in out.lower()
    assert gate.submitgate.active_grant() == sid  # the approve created the one-shot grant
    # The card the operator saw carried company, role and the planned field→value table.
    card = seen["payload"]
    assert card["company"] == "Acme" and card["role"] == "ML Engineer" and card["session_id"] == sid
    assert any(f["label"] == "Email" and f["value"] == "ada@example.com" for f in card["fields"])


def test_request_submit_declines_leave_no_grant(gate, monkeypatch):
    sid = _verified_session(gate.formfill)
    monkeypatch.setattr(gate.plugin, "_turn_is_headless", lambda: False)
    monkeypatch.setattr(gate.plugin, "_submitgate_interrupt", lambda payload: "no")
    out = gate.tool.invoke({"session_id": sid})
    assert "declined" in out.lower()
    assert gate.submitgate.active_grant() is None


def test_request_submit_unknown_session(gate):
    out = gate.tool.invoke({"session_id": "does-not-exist"})
    assert "No fill plan found" in out


def test_request_submit_reraises_the_interrupt_pause(gate, monkeypatch):
    """The langgraph pause signal must reach the runtime — the tool's catch-all must not swallow
    it into a string, or the human-in-the-loop pause would silently break."""
    from langgraph.errors import GraphInterrupt

    sid = _verified_session(gate.formfill)
    monkeypatch.setattr(gate.plugin, "_turn_is_headless", lambda: False)

    def pause(payload):
        raise GraphInterrupt(())

    monkeypatch.setattr(gate.plugin, "_submitgate_interrupt", pause)
    with pytest.raises(GraphInterrupt):
        gate.tool.invoke({"session_id": sid})
    assert gate.submitgate.active_grant() is None  # paused, not granted
