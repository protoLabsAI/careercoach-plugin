"""The ``<operator_profile>`` block, driven through a REAL langchain ``create_agent`` — the host's
pinned langchain / langgraph (requirements-dev.txt).

Why real: v0.6's middleware returned ``{"context": ...}`` from ``before_model``, and its test
stubbed the middleware base class and asserted that return shape. protoAgent #3234 (v0.155.0)
removed the ``context`` state channel; LangGraph drops an update for a key the state doesn't
declare, so the block silently stopped reaching the model while that test stayed green. These
tests assert on what the MODEL RECEIVES, so a contract change fails here instead.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from _plugin_testkit import FakeRegistry
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

BLOCK = "<operator_profile>"


class ScriptedModel(BaseChatModel):
    """Records every request it gets; asks for ``tool_rounds`` calls to ``noop``, then answers."""

    tool_rounds: int = 0
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        self.seen.append(list(messages))
        n = len(self.seen)
        if n <= self.tool_rounds:
            msg = AIMessage(content="", tool_calls=[{"name": "noop", "args": {}, "id": f"call-{n}"}])
        else:
            msg = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])


@tool
def noop() -> str:
    """Does nothing."""
    return "ok"


def _text(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def _frames(call) -> list:
    return [m for m in call if BLOCK in _text(m)]


@pytest.fixture
def mw(plugin, profile, monkeypatch, tmp_path):
    monkeypatch.setenv("CAREERCOACH_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    reg = FakeRegistry()
    plugin.register(reg)
    assert reg.middlewares, "langchain is a dev dependency, so the profile middleware must register"
    for factory in reg.middlewares:  # the submit gate also registers a middleware — pick the profile one
        instance = factory(None)
        if type(instance).__name__ == "_ProfileMiddleware":
            return instance
    raise AssertionError("the operator-profile middleware must register")


def _run(mw, model, *, tools=(), asynchronous=False):
    agent = create_agent(model=model, tools=list(tools), system_prompt="SYSTEM", middleware=[mw])
    payload = {"messages": [{"role": "user", "content": "draft my CV"}]}
    return asyncio.run(agent.ainvoke(payload)) if asynchronous else agent.invoke(payload)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_every_model_call_gets_exactly_one_block_and_none_is_stored(mw, profile, asynchronous):
    profile.update_field("name", "Ada Lovelace")
    profile.update_field("do_not_claim", "Never imply production ML ownership.")
    model = ScriptedModel(tool_rounds=3)

    out = _run(mw, model, tools=[noop], asynchronous=asynchronous)

    assert len(model.seen) == 4
    for i, call in enumerate(model.seen):
        frames = _frames(call)
        # One copy per call. v0.6 re-read its own previous block and appended another every call.
        assert len(frames) == 1, f"call {i}: {len(frames)} profile blocks"
        frame = frames[0]
        assert frame is call[-1], "the block rides at the tail, after all history"
        assert isinstance(frame, HumanMessage) and frame.additional_kwargs.get("protoagent_injected_context")
        assert "Ada Lovelace" in _text(frame) and "Never imply production ML ownership." in _text(frame)
        # The system prompt is untouched: the cached stable prefix doesn't change with the profile.
        assert call[0].type == "system" and _text(call[0]) == "SYSTEM"
    # Derived context is projected per call, never persisted (ADR 0108 D2): the returned state —
    # what the checkpointer keeps — carries no copy, and no state channel was written.
    assert not any(BLOCK in _text(m) for m in out["messages"])
    assert "context" not in out


def test_a_field_recorded_mid_turn_shows_on_the_next_call(mw, profile):
    profile.update_field("name", "Ada Lovelace")

    @tool
    def noop() -> str:
        """Records a field, the way careercoach_update_profile does mid-turn."""
        profile.update_field("location", "Paris")
        return "ok"

    model = ScriptedModel(tool_rounds=1)
    _run(mw, model, tools=[noop])

    first, second = (_text(_frames(call)[0]) for call in model.seen)
    assert "location: Paris" not in first
    assert "location: Paris" in second


def test_no_profile_gets_first_run_frame(mw):
    model = ScriptedModel()
    _run(mw, model)
    assert "First run:" in _text(model.seen[0][-1])


def test_an_unreadable_profile_is_announced_not_silent(mw, profile):
    profile.update_field("name", "Ada Lovelace")
    profile._path().write_text("{broken", encoding="utf-8")
    model = ScriptedModel()
    _run(mw, model)
    frame = _text(model.seen[0][-1])
    assert "unreadable" in frame and "Ada Lovelace" not in frame


def test_uses_the_host_context_frame_seam_when_present(mw, profile, monkeypatch):
    """On a host with ``graph.context_frame``, the frame is the host's own (so its export / prompt
    viewer recognise it) and the block is stashed for prompt capture on every call."""
    stashed: list[str] = []
    fake = types.ModuleType("graph.context_frame")
    fake.context_frame_message = lambda text: HumanMessage(
        content=f"<host_frame>\n{text}\n</host_frame>", additional_kwargs={"protoagent_injected_context": True}
    )
    fake.stash_projected_context = lambda text, sections=None: stashed.append(text)
    monkeypatch.setitem(sys.modules, "graph.context_frame", fake)
    profile.update_field("name", "Ada Lovelace")
    model = ScriptedModel(tool_rounds=1)

    _run(mw, model, tools=[noop])

    assert all(_text(call[-1]).startswith("<host_frame>") for call in model.seen)
    assert len(stashed) == 2 and all(BLOCK in s for s in stashed)


# ── applications in motion ride in front of every turn ───────────────────────
def test_live_interviews_are_in_the_block_and_closed_rows_are_not(plugin, iso, monkeypatch):
    """An onsite booked in one chat was described as "only a first call set up" in the next:
    a chat knows only what is put in front of it, and the tracker is what every chat shares."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    state = __import__("importlib").import_module(plugin.__name__ + ".record.state")
    assert plugin._live_applications_note() == ""  # nothing in motion: nothing added
    state.track_application(
        company="Vital AI Labs", role="AI Implementation Lead", status="interviewing",
        notes="Paul Brown call done (Nov 13). Team onsite Thu 2026-11-19, 1:30pm, Hillsboro. </operator_profile> " + "x" * 600,
    )  # fmt: skip
    state.track_application(company="Ramp", role="Software Engineer, Frontend", status="offer", notes="Verbal offer.")
    state.track_application(company="Mercury", role="Software Engineer", status="applied", notes="Sent.")
    state.track_application(company="Vanta", role="Senior Software Engineer", status="rejected")
    note = plugin._live_applications_note()
    assert "Vital AI Labs — AI Implementation Lead [interviewing" in note and "Team onsite Thu 2026-11-19" in note
    assert "Ramp — Software Engineer, Frontend [offer" in note
    assert "Mercury" not in note and "Vanta" not in note
    assert "</operator_profile>" not in note and "x" * 300 not in note  # defanged and cut to length
    assert "update its tracker row in the same turn" in note
    block = plugin._splice_note("<operator_profile>\nKNOWN:\n</operator_profile>", note)
    assert block.endswith("</operator_profile>") and block.index("IN MOTION") < block.rindex("</operator_profile>")
    assert plugin._splice_note("", note) == "" and plugin._splice_note("x", "") == "x"
