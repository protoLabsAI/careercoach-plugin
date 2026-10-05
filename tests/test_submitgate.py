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
    # case-insensitive, and the other verbs / the quoted type attribute
    assert submitgate.is_submit_like("browser_click", {"selector": "APPLY NOW"})
    assert submitgate.is_submit_like("browser_click", {"selector": 'input[type="submit"]'})
    assert submitgate.is_submit_like("browser_press", {"key": "Return"})
    assert submitgate.is_submit_like("browser_eval", {"script": "form.submit()"})


def test_is_submit_like_negatives(submitgate):
    assert not submitgate.is_submit_like("browser_click", {"selector": "Attach"})
    assert not submitgate.is_submit_like("browser_click", {"selector": "Country"})
    assert not submitgate.is_submit_like("browser_press", {"key": "Tab"})
    assert not submitgate.is_submit_like("browser_eval", {"script": "document.querySelector('#name').value"})
    # a bare programmatic click with no submit/apply word is not a submit
    assert not submitgate.is_submit_like("browser_eval", {"script": "document.querySelector('.menu').click()"})
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
    gate.submitgate.grant("sid-1")
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
    gate.submitgate.grant("sid-1")
    gate.middleware.wrap_tool_call(FakeReq("browser_form_read", {}), lambda req: "RAN")
    assert gate.submitgate.active_grant() == "sid-1"


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


def test_request_submit_refuses_when_plan_changed(gate):
    # The plan-hash guard, as a unit: verified but the recorded hash no longer matches the id.
    ok, _ = gate.plugin._submit_ready({"verified": True, "rows_hash": "sid-xyz"}, "sid-xyz")
    assert ok is True
    changed, reason = gate.plugin._submit_ready({"verified": True, "rows_hash": "STALE"}, "sid-xyz")
    assert changed is False and "changed" in reason


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
