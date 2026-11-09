"""Who gave an extra answer: the operator, in this conversation, or the agent. Host-free.

``careercoach_plan_fill(extra_answers=…)`` is an argument the MODEL fills in. Before this module the
approval card labelled every extra answer "your answer", so an agent could launder its own guess
into the operator's name. Seen live (2026-11-09 eval): the agent found a value already typed into an
optional X field, "assumed you typed it", passed it as an extra answer and reached VERIFIED with the
card claiming the operator gave it; an attestation "YES" the agent wrote got the same label.

The rule here: an extra answer counts as the OPERATOR's only when its value appears in something the
operator said in the current conversation — a human message, or their reply to ``ask_human`` /
``request_user_input`` / ``request_decision``. Everything else is the agent's, and the approval card
says so. A legal / eligibility / attestation answer that is the agent's never verifies (formfill).

**Short values** ("Yes", "No", "18", "US") appear in almost any message, so containment proves
nothing. For those the operator's words must ALSO name the question: enough of the question's
content words in the same message, or in the question the operator was answering (the ``ask_human``
question, the ``request_user_input`` field title). A yes-ish value is found as any of a few yes words
("yes", "agree", "consent", "tick" …) so "I agree to them" can answer a consent box.

This is a heuristic over free text. It errs toward "the agent's": an answer the operator gave in
words the check can't match (a blanket "No to any question about prior employment") is marked
"entered by the agent — not confirmed by you" on the card, which the operator can still approve.
A HITL ``default`` the agent prefilled counts as the operator's once they submit the form unchanged,
as the core documents. Runtime notices injected as human messages (``[round-governor] …``) are not
the operator's words and are skipped.
"""

from __future__ import annotations

import json
import re
import unicodedata

# The core HITL tools whose result is the operator's reply (protoAgent ``tools/lg_tools.py``).
HITL_TOOLS = frozenset({"ask_human", "request_user_input", "request_decision"})

# A human message the RUNTIME injected, not one the operator typed: "[round-governor] You are 25
# model rounds into this turn…". The operator never opens a message with a bracketed tag like this.
_RUNTIME_NOTICE = re.compile(r"^\s*\[[a-z][a-z0-9_.:-]*\]", re.I)

_YES_WORDS = frozenset(
    {"yes", "y", "yep", "yeah", "true", "agree", "agreed", "consent", "accept", "confirm", "confirmed",
     "tick", "ticked", "check", "checked", "attest", "certify"}
)  # fmt: skip
_NO_WORDS = frozenset({"no", "n", "nope", "false", "none", "never", "decline", "untick", "unticked"})
_YES_VALUES = frozenset({"yes", "y", "true", "checked", "check", "ticked", "tick", "on", "x", "1", "agree", "i agree"})
_NO_VALUES = frozenset({"no", "n", "false", "unchecked", "unticked", "off", "0"})

# Words that say nothing about WHICH question a message answers.
_STOP = frozenset(
    """a an the and or but if of to in on at by for from with without into onto over under about as is are was
    were be been being am do does did done have has had having you your yours we our us they them their it its
    this that these those there here what which who whom whose when where why how will would can could should
    shall may might must please kindly any all some each every no not yes than then so such also just only
    ever now currently current previously prior yet i me my mine he she him her his hers o e g eg ie etc via
    per one two let tell know answer question questions select choose pick option options below above following""".split()
)  # fmt: skip


def _fold(text: object) -> str:
    """Text folded for containment: NFKC, case-folded, curly quotes straightened, whitespace collapsed."""
    s = unicodedata.normalize("NFKC", str(text if text is not None else ""))
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(s.casefold().split())


def _words(text: object) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(text))


def _stem(word: str) -> str:
    """A crude plural fold, so "agreements" names "agreement" and "applications" names "application"."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _content(text: object) -> set[str]:
    return {_stem(w) for w in _words(text) if w not in _STOP and (len(w) >= 3 or w.isdigit())}


def _text_of(content: object) -> str:
    """A message's text: a string, or the text parts of a multimodal content list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _get(msg: object, key: str, default=None):
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def _kind(msg: object) -> str:
    """``human`` / ``ai`` / ``tool`` / … for a LangChain message object or its dict form."""
    kind = _get(msg, "type") or _get(msg, "role") or ""
    kind = str(kind).lower()
    return {"user": "human", "assistant": "ai"}.get(kind, kind)


def _messages(state: object) -> list:
    if isinstance(state, dict):
        msgs = state.get("messages")
    else:
        msgs = getattr(state, "messages", None)
    return list(msgs) if isinstance(msgs, (list, tuple)) else []


def _schema_titles(args: dict) -> dict[str, str]:
    """``{property: "title description"}`` across a ``request_user_input`` call's steps — the question
    each submitted field answers."""
    out: dict[str, str] = {}
    steps = args.get("steps") if isinstance(args, dict) else None
    for step in steps if isinstance(steps, list) else []:
        schema = step.get("schema") if isinstance(step, dict) else None
        props = schema.get("properties") if isinstance(schema, dict) else None
        for key, spec in (props or {}).items() if isinstance(props, dict) else []:
            if isinstance(spec, dict):
                out[str(key)] = " ".join(str(spec.get(k) or "") for k in ("title", "description")).strip()
    return out


def operator_utterances(state: object) -> list[tuple[str, str]]:
    """``[(said, about)]`` — each thing the operator said in this conversation, with the question it
    answered (``about``, empty for a plain message). ``about`` is the AGENT's text, so it only ever
    helps a short answer name its question; a value is never found in it.

    Read from the graph state's ``messages``: human messages (minus runtime notices), and the result
    of each core HITL tool, paired with the question from the AI tool call that asked it. A
    ``request_user_input`` result that parses as an object contributes each submitted field on its own
    — paired with that field's title — plus the whole reply."""
    msgs = _messages(state)
    calls: dict[str, tuple[str, dict]] = {}
    for m in msgs:
        if _kind(m) != "ai":
            continue
        for tc in _get(m, "tool_calls") or []:
            if isinstance(tc, dict) and tc.get("id"):
                calls[str(tc["id"])] = (
                    str(tc.get("name") or ""),
                    tc.get("args") if isinstance(tc.get("args"), dict) else {},
                )
    out: list[tuple[str, str]] = []
    for m in msgs:
        kind = _kind(m)
        text = _text_of(_get(m, "content"))
        if kind == "human":
            if text.strip() and not _RUNTIME_NOTICE.match(text):
                out.append((text, ""))
            continue
        if kind != "tool":
            continue
        name, args = calls.get(str(_get(m, "tool_call_id") or ""), ("", {}))
        name = name or str(_get(m, "name") or "")
        if name not in HITL_TOOLS or not text.strip():
            continue
        if name == "ask_human":
            out.append((text, str(args.get("question") or "")))
        elif name == "request_decision":
            out.append((text, " ".join(str(args.get(k) or "") for k in ("question", "context"))))
        else:  # request_user_input — the submitted form as JSON
            titles = _schema_titles(args)
            whole_about = " ".join([str(args.get("title") or ""), str(args.get("description") or "")])
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, (list, tuple)):
                        value = ", ".join(str(v) for v in value)
                    out.append((str(value), titles.get(str(key), "")))
            out.append((text, whole_about))
    return out


def is_short(value: object) -> bool:
    """A value too generic to prove anything by appearing in a message: a yes/no word, a number, or
    anything under five characters ("Yes", "No", "US", "18")."""
    v = _fold(value)
    return len(v) < 5 or v in _YES_VALUES or v in _NO_VALUES or v.replace(" ", "").isdigit()


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and needle in haystack


def _names_question(said: str, about: str, question: str) -> bool:
    """Whether a message (or the question it answered) names ``question``: at least half of the
    question's content words, capped at three."""
    want = _content(question)
    if not want:
        return False
    have = _content(said) | _content(about)
    need = min(3, max(1, (len(want) + 1) // 2))
    return len(want & have) >= need


def _short_value_present(value: str, said: str) -> bool:
    v = _fold(value)
    words = set(_words(said))
    if v in _YES_VALUES:
        return bool(words & _YES_WORDS)
    if v in _NO_VALUES:
        return bool(words & _NO_WORDS)
    vw = _words(v)
    if not vw:
        return False
    # the value's words, in order, as whole words of the message
    return re.search(r"(?<![a-z0-9])" + r"\W+".join(map(re.escape, vw)) + r"(?![a-z0-9])", _fold(said)) is not None


def said_by_operator(value: object, question: object, utterances: list[tuple[str, str]]) -> bool:
    """Whether the operator gave ``value`` (an extra answer for the field asking ``question``) in this
    conversation. A longer value must appear (folded) in one thing they said. A short value must
    appear there as a whole word AND that same utterance — or the question it answered — must name
    the field's question."""
    v = _fold(value)
    if not v:
        return False
    for said, about in utterances or []:
        if is_short(v):
            if _short_value_present(v, said) and _names_question(said, about, str(question or "")):
                return True
        elif _contains(_fold(said), v):
            return True
    return False
