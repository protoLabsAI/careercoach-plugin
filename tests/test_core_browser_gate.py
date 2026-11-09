"""The core browser seams protoAgent's agent_browser added (protoAgent #4923, #4924).

* **Content boundaries.** ``browser_form_read`` output now arrives wrapped in
  ``--- AGENT_BROWSER_PAGE_CONTENT nonce=… ---`` markers, and the model pastes it into
  ``careercoach_plan_fill`` / ``careercoach_verify_fill`` as-is. The parser strips them.
* **The action gate seam.** Where the core offers ``agent_browser.add_gate_policy``, careercoach
  registers a policy instead of judging argument text: the core resolves the real target (a ``@eN``
  ref included) and the policy allows a submit the operator approved on the careercoach card
  (spending the grant, one card) and denies any other submit. Without the service, the old
  middleware gates exactly as before. A fake service stands in for the core here, and one test
  drives a REAL ``create_agent`` to prove the policy is live inside a tool the middleware wrapped.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import types

import pytest
from _plugin_testkit import FakeRegistry
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

NONCE = "1e37acb3c91c903e25ade06b436ccd81"
FORM = [
    {"label": "Email", "kind": "email", "name": "email", "id": "email", "required": True, "value": "", "options": []}
]


def wrapped(inner: str, nonce: str = NONCE, origin: str = "https://boards.greenhouse.io/acme/jobs/1") -> str:
    """``browser_form_read`` output exactly as core's ``boundaries.rewrap`` renders it."""
    return (
        f"--- AGENT_BROWSER_PAGE_CONTENT nonce={nonce} origin={origin} ---\n"
        f"{inner}\n--- END_AGENT_BROWSER_PAGE_CONTENT nonce={nonce} ---"
    )


# ── fakes ───────────────────────────────────────────────────────────────────────────────────
class FakeGateService:
    """``agent_browser.add_gate_policy`` as core ships it: records ``(fn, categories, name)`` and
    returns a remover. ``run(action)`` stands in for core's ``gate.decide`` calling the policies."""

    def __init__(self):
        self.policies: list[tuple] = []
        self.calls = 0

    def __call__(self, fn, categories=("submit",), name=""):
        self.calls += 1
        entry = (fn, tuple(categories), name)
        self.policies.append(entry)

        def remove():
            if entry in self.policies:
                self.policies.remove(entry)

        return remove

    def run(self, action):
        return [fn(action) for fn, _cats, _name in list(self.policies)]


def action(selector="@e42", categories=("submit",), verb="click"):
    """The plain dict core hands a policy after resolving the target."""
    return {
        "tool": f"browser_{verb}",
        "verb": verb,
        "selector": selector,
        "key": "",
        "page_url": "https://boards.greenhouse.io/acme/jobs/1",
        "target": {"found": True, "tag": "button", "name": "Submit application", "submits": True},
        "verdict": {"categories": sorted(categories), "reason": "it submits a form", "description": "Submit the form"},
        "gated": sorted(categories),
    }


class FakeReq:
    def __init__(self, name, args, call_id="call-1"):
        self.tool_call = {"name": name, "args": args, "id": call_id}


@pytest.fixture
def gate(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    reg = FakeRegistry({"packet_root": str(iso / "ws"), "browser_fill": "greenhouse"})
    plugin.register(reg)
    sg = importlib.import_module(plugin.__name__ + ".applying.submitgate")
    ff = importlib.import_module(plugin.__name__ + ".applying.formfill")
    sg.clear()
    sg.ensure_core_policy(None, ff.current_verification)  # no policy left over from another test
    service = FakeGateService()
    holder = {"service": service}
    monkeypatch.setattr(plugin, "_core_gate_service", lambda: holder["service"])
    tools = {t.name: t for t in reg.tools}
    mws = [f(None) for f in reg.middlewares]
    yield types.SimpleNamespace(
        plugin=plugin,
        reg=reg,
        tools=tools,
        middleware=next(m for m in mws if type(m).__name__ == "_SubmitGateMiddleware"),
        submitgate=sg,
        formfill=ff,
        service=service,
        holder=holder,
    )
    sg.clear()
    sg.ensure_core_policy(None, ff.current_verification)


def _verified(ff):
    plan = ff.build_plan(FORM, {"email": "ada@example.com"}, {})
    sid = plan["session_id"]
    ff.record_verification(sid, [])
    return sid


def _grant(gate, sid):
    gate.submitgate.grant(sid, gate.formfill.current_verification(sid))


def _through_core(gate, act):
    """A tool call that the middleware wraps and whose 'tool' is core: it runs the policies."""
    seen = {}

    def handler(req):
        seen["votes"] = gate.service.run(act)
        return "RAN"

    out = gate.middleware.wrap_tool_call(FakeReq("browser_click", {"selector": act["selector"]}), handler)
    return out, seen.get("votes")


# ── A: content boundaries ────────────────────────────────────────────────────────────────────
def test_strip_page_boundaries(gate):
    ff = gate.formfill
    inner = json.dumps(FORM, indent=2)
    assert ff.strip_page_boundaries(wrapped(inner)) == inner
    assert ff.strip_page_boundaries("\n  " + wrapped(inner) + "\n\n") == inner  # pasted with blank lines
    assert ff.strip_page_boundaries(inner) == inner  # unwrapped passes through untouched
    # A cut-off block (no closing line) loses only its opening line; the rest is left to fail parsing.
    assert ff.strip_page_boundaries(wrapped(inner).rsplit("\n", 1)[0]) == inner
    # A closing line with ANOTHER nonce isn't ours, so it stays (and the JSON then fails to parse).
    other = wrapped(inner).replace(
        f"END_AGENT_BROWSER_PAGE_CONTENT nonce={NONCE}", "END_AGENT_BROWSER_PAGE_CONTENT nonce=ff"
    )
    assert ff.strip_page_boundaries(other).endswith("nonce=ff ---")


def test_plan_fill_and_verify_fill_accept_marker_wrapped_form_read(gate):
    answers = importlib.import_module(gate.plugin.__name__ + ".record.answers")
    answers.propose("email", "ada@example.com")
    answers.confirm(["email"])
    plan = gate.tools["careercoach_plan_fill"].invoke(
        {
            "form_json": wrapped(json.dumps(FORM, indent=2)),
            "company": "Acme",
            "role": "ML Engineer",
            "ats": "greenhouse",
        }
    )
    assert "could not be parsed" not in plan and "isn't a list" not in plan, plan
    sid = next(line.split(":", 1)[1].strip() for line in plan.splitlines() if line.strip().startswith("session_id:"))
    filled = [dict(FORM[0], value="ada@example.com")]
    out = gate.tools["careercoach_verify_fill"].invoke({"session_id": sid, "form_json": wrapped(json.dumps(filled))})
    assert out.startswith("VERIFIED"), out


def test_wrapped_garbage_still_gets_the_readable_error(gate):
    out = gate.tools["careercoach_plan_fill"].invoke(
        {"form_json": wrapped("not json"), "company": "A", "role": "R", "ats": "greenhouse"}
    )
    assert "could not be parsed" in out


# ── B: the core gate seam ──────────────────────────────────────────────────────────────────
def test_with_the_service_one_policy_registers_and_browser_calls_reach_core(gate):
    out, votes = _through_core(gate, action(selector="@e42"))
    assert out == "RAN"  # the middleware let the call reach the core, which resolves the ref and decides
    assert len(gate.service.policies) == 1
    _fn, cats, name = gate.service.policies[0]
    assert cats == ("submit",) and "careercoach" in name
    assert votes == [{"decision": "deny", "reason": gate.submitgate.BLOCKED}]  # no grant → core refuses
    # Many calls (and a second register()) still leave exactly one policy: two would race for the grant.
    gate.plugin.register(FakeRegistry({"browser_fill": "greenhouse"}))
    for _ in range(3):
        _through_core(gate, action())
    assert len(gate.service.policies) == 1 and gate.service.calls == 1


def test_a_ref_submit_without_a_grant_is_denied(gate):
    # The hole this closes: argument text "@e42" never read as a submit to the old matcher.
    assert not gate.submitgate.is_submit_like("browser_click", {"selector": "@e42"})
    _out, votes = _through_core(gate, action(selector="@e42"))
    assert votes[0]["decision"] == "deny"


def test_a_submit_off_any_job_application_is_left_to_the_core_card(gate):
    # QA (#120): a submit on an unrelated page (a newsletter, a settings form) with no grant live
    # must not get careercoach's job-application BLOCKED — the core's own approval card decides it.
    act = action(selector="@e7")
    act["page_url"] = "https://example.com/newsletter"
    _out, votes = _through_core(gate, act)
    assert votes == [None]


def test_a_submit_on_an_application_page_still_needs_the_grant(gate):
    sg, ff = gate.submitgate, gate.formfill
    from_ats = importlib.import_module(gate.plugin.__name__ + ".applying.ats")
    assert from_ats.detect("https://boards.greenhouse.io/acme/jobs/1")
    assert sg.core_policy_decision(action(), ff.current_verification, from_ats.detect)["decision"] == "deny"

    # can't tell what page it is → keep the hard gate
    def boom(url):
        raise ValueError(url)

    assert sg.core_policy_decision(action(), ff.current_verification, boom)["decision"] == "deny"


def test_an_approved_submit_is_allowed_once_by_core_with_no_second_card(gate):
    sid = _verified(gate.formfill)
    _grant(gate, sid)
    _out, votes = _through_core(gate, action(selector="@e42"))
    assert votes == [{"decision": "allow", "reason": gate.submitgate.ALLOWED_REASON}]  # core skips its card
    assert gate.submitgate.active_grant() is None  # one-shot: spent
    _out, votes = _through_core(gate, action(selector="@e42"))
    assert votes[0]["decision"] == "deny"


def test_a_non_submit_gets_no_opinion_and_keeps_the_grant(gate):
    sid = _verified(gate.formfill)
    _grant(gate, sid)
    # core reads "Apply now" (opens the form) and a plain eval as no submit; download is another category
    for cats in ((), ("eval",), ("download",)):
        _out, votes = _through_core(gate, action(selector="Apply now", categories=cats))
        assert votes == [None]
    assert gate.submitgate.active_grant() is not None


def test_a_stale_grant_is_denied_and_not_spent(gate):
    sid = _verified(gate.formfill)
    _grant(gate, sid)
    gate.formfill.record_verification(sid, [])  # re-verified: a NEW verification id
    _out, votes = _through_core(gate, action())
    assert votes[0]["decision"] == "deny"
    assert gate.submitgate.active_grant() is not None  # mismatched → never consumed


def test_the_policy_is_inert_outside_a_careercoach_wrapped_call(gate):
    sid = _verified(gate.formfill)
    _grant(gate, sid)
    gate.middleware.wrap_tool_call(FakeReq("browser_snapshot", {}), lambda req: "ok")  # installs the policy
    # A call the careercoach middleware didn't wrap (a stale generation, a graph without plugin middleware)
    assert gate.service.run(action()) == [None]
    assert gate.submitgate.active_grant() is not None


def test_async_path_runs_the_policy_live(gate):
    sid = _verified(gate.formfill)
    _grant(gate, sid)
    seen = {}

    async def handler(req):
        seen["votes"] = gate.service.run(action())
        return "RAN"

    out = asyncio.run(gate.middleware.awrap_tool_call(FakeReq("browser_click", {"selector": "@e42"}), handler))
    assert out == "RAN" and seen["votes"][0]["decision"] == "allow"


def test_the_argument_text_floor_still_blocks_a_worded_submit_without_a_grant(gate):
    # The core's probe can fail to read a page and then see no submit at all, so a call that reads as a
    # submit by its own arguments still needs a grant, exactly as before, even with the core gate on.
    ran = []
    out = gate.middleware.wrap_tool_call(
        FakeReq("browser_click", {"selector": "Submit application"}, call_id="c5"), lambda req: ran.append(1) or "RAN"
    )
    assert ran == [] and out.content == gate.submitgate.BLOCKED and out.tool_call_id == "c5"
    assert len(gate.service.policies) == 1  # the core path was taken; the floor answered first


def test_a_worded_submit_the_core_missed_still_spends_the_grant(gate):
    sid = _verified(gate.formfill)
    _grant(gate, sid)
    # The core resolved nothing (probe failed): its policy sees no submit and gives no opinion…
    out, votes = _through_core(gate, action(selector="Submit application", categories=()))
    assert out == "RAN" and votes == [None]
    # …but the one-shot grant is still spent by that one submit-like call.
    assert gate.submitgate.active_grant() is None


def test_a_worded_submit_the_core_allowed_spends_the_grant_once(gate):
    sid = _verified(gate.formfill)
    _grant(gate, sid)
    out, votes = _through_core(gate, action(selector="Submit application"))
    assert out == "RAN" and votes[0]["decision"] == "allow"
    assert gate.submitgate.active_grant() is None
    out = gate.middleware.wrap_tool_call(FakeReq("browser_click", {"selector": "Submit application"}), lambda r: "RAN")
    assert out.content == gate.submitgate.BLOCKED


def test_a_reloaded_provider_gets_a_fresh_registration(gate):
    _through_core(gate, action())
    first = gate.service
    second = FakeGateService()
    gate.holder["service"] = second
    _through_core(gate, action())
    assert first.policies == [] and len(second.policies) == 1


def test_without_the_service_the_old_middleware_gates_unchanged(gate):
    _through_core(gate, action())  # a policy registered while the service was there…
    service = gate.service
    gate.holder["service"] = None  # …then agent_browser goes away (or an older core)
    ran = []
    out = gate.middleware.wrap_tool_call(
        FakeReq("browser_click", {"selector": "Submit application"}, call_id="c9"), lambda req: ran.append(1) or "RAN"
    )
    assert ran == [] and out.content == gate.submitgate.BLOCKED and out.tool_call_id == "c9"
    assert service.policies == []  # the stale registration was removed
    assert not gate.submitgate.core_policy_installed()
    # …and with a grant, the text matcher lets exactly one through, as before.
    sid = _verified(gate.formfill)
    _grant(gate, sid)
    assert gate.middleware.wrap_tool_call(FakeReq("browser_press", {"key": "Enter"}), lambda req: "RAN") == "RAN"
    assert gate.submitgate.active_grant() is None


def test_a_broken_service_falls_back_to_the_middleware(gate):
    def broken(fn, categories=("submit",), name=""):
        raise RuntimeError("seam changed")

    gate.holder["service"] = broken
    out = gate.middleware.wrap_tool_call(FakeReq("browser_click", {"selector": "Submit"}), lambda req: "RAN")
    assert out.content == gate.submitgate.BLOCKED


def test_only_request_submit_mints_the_grant_the_policy_spends(gate):
    # The policy never creates a grant: without careercoach_request_submit's approved interrupt it only denies.
    _verified(gate.formfill)
    for _ in range(3):
        _out, votes = _through_core(gate, action())
        assert votes[0]["decision"] == "deny"
    assert gate.submitgate.active_grant() is None


# ── a REAL create_agent: the policy is live inside the tool the middleware wrapped ────────────
class ScriptedModel(BaseChatModel):
    """Asks for one ``browser_click("@e42")``, then answers."""

    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        self.seen.append(1)
        if len(self.seen) == 1:
            msg = AIMessage(
                content="", tool_calls=[{"name": "browser_click", "args": {"selector": "@e42"}, "id": "c1"}]
            )
        else:
            msg = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _fake_core_click(service, asynchronous):
    """A ``browser_click`` that behaves like core's: resolve (here: always a submit), ask the policies."""

    def decide(selector):
        votes = [v for v in service.run(action(selector=selector)) if v]
        if any(v["decision"] == "deny" for v in votes):
            return "Error: denied by policy"
        if any(v["decision"] == "allow" for v in votes):
            return "CLICKED"
        return "Error: core would ask the operator"

    if asynchronous:

        @tool
        async def browser_click(selector: str) -> str:
            """Click."""
            await asyncio.sleep(0)
            return decide(selector)

    else:

        @tool
        def browser_click(selector: str) -> str:
            """Click."""
            return decide(selector)

    return browser_click


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("granted", [True, False], ids=["granted", "no-grant"])
def test_real_agent_one_card_with_a_grant_and_a_refusal_without(gate, asynchronous, granted):
    if granted:
        _grant(gate, _verified(gate.formfill))
    agent = create_agent(
        model=ScriptedModel(),
        tools=[_fake_core_click(gate.service, asynchronous)],
        system_prompt="SYSTEM",
        middleware=[gate.middleware],
    )
    payload = {"messages": [{"role": "user", "content": "submit it"}]}
    out = asyncio.run(agent.ainvoke(payload)) if asynchronous else agent.invoke(payload)
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == ("CLICKED" if granted else "Error: denied by policy")
    assert gate.submitgate.active_grant() is None
