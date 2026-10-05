"""Hard submit gate — one-shot operator grants + submit-click detection (#4032 phase 3c).

Phase 3's non-goal is "submitting without explicit operator consent". A line in a skill can't
enforce that, and neither can a tool the agent decides to call on its own: consent has to come
from the operator through a channel the model can't forge, and submit clicks have to be *blocked*
unless that consent exists. This module is the enforceable half.

Two pure, host-free pieces:

* **grant state** — an in-process, one-shot, time-limited authorization keyed by the fill
  ``session_id`` (``formfill.py``). ``careercoach_request_submit`` records one ONLY after an
  operator approves an interrupt; the submit-gate middleware spends it on the next submit-like
  browser call, and nothing else creates one. It lives in memory on purpose: an authorization to
  click submit should not survive a restart, a checkpoint, or a copy to another turn.
* **submit detection** — ``is_submit_like(tool_name, args)`` recognises the browser calls that
  would send an application (a submit/apply click, an Enter press, a ``.submit()`` eval), so the
  middleware knows which calls to gate.

**Limits, stated plainly.** This is defense in depth *over the browser tools* — it does NOT
sandbox the browser. It stops the *agent* from clicking submit without an unforgeable operator
approval; the operator can still submit by hand in the visible browser whenever they choose.

No host imports, so every function is unit-testable with nothing but monotonic time.
"""

from __future__ import annotations

import re
import threading
import time

__all__ = ["GRANT_TTL_S", "grant", "consume", "active_grant", "clear", "is_submit_like"]

# Default life of a grant. A one-shot authorization is meant to cover an imminent click, not sit
# around: if the agent doesn't click within this window, the operator approves again.
GRANT_TTL_S = 120

_lock = threading.Lock()
# session_id -> monotonic expiry deadline. At most one grant is ever live (``grant`` supersedes
# any prior one), but keying by session means a grant issued for one fill plan can't authorize a
# click made against a different one.
_grants: dict[str, float] = {}


def _now() -> float:
    """Monotonic seconds — immune to wall-clock jumps, and patchable in tests."""
    return time.monotonic()


def _prune(now: float) -> None:
    """Drop every expired grant (caller holds ``_lock``)."""
    for sid in [s for s, deadline in _grants.items() if deadline <= now]:
        _grants.pop(sid, None)


def grant(session_id: str, ttl_s: int = GRANT_TTL_S) -> None:
    """Record a ONE-SHOT, time-limited grant for ``session_id`` (a ``formfill`` fill session).

    The next submit-like tool call consumes it (``consume``); it expires ``ttl_s`` seconds from
    now even if never used. A fresh grant supersedes any earlier one, so authorizations can't
    accumulate. Only ``careercoach_request_submit``'s approved-interrupt path calls this — nothing
    else in the plugin does."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _lock:
        _grants.clear()  # one live grant at a time
        _grants[sid] = _now() + max(0, int(ttl_s))


def consume(session_id: str) -> bool:
    """Spend the grant for ``session_id``: ``True`` if a live (unexpired) grant existed and is now
    gone, else ``False``. One-shot — a second call for the same session returns ``False``."""
    sid = str(session_id or "").strip()
    with _lock:
        now = _now()
        _prune(now)
        deadline = _grants.get(sid)
        if deadline is not None and deadline > now:
            _grants.pop(sid, None)
            return True
        return False


def active_grant() -> str | None:
    """The ``session_id`` of the live (unexpired) grant, or ``None``. Prunes expired grants."""
    with _lock:
        now = _now()
        _prune(now)
        for sid, deadline in _grants.items():
            if deadline > now:
                return sid
        return None


def clear() -> None:
    """Drop every grant. Not part of the consent path — a reset hook for tests / teardown."""
    with _lock:
        _grants.clear()


# ── submit detection ──────────────────────────────────────────────────────────────────────
# A click target (selector or visible text) that sends the application. Word boundaries keep
# "Attach" / "Country" / "Apply filters"-style labels from reading as submit — only the real
# verbs trip it. The phrases allow flexible whitespace ("Send  Application").
_SUBMIT_WORDS = re.compile(
    r"\bsubmit\b|\bapply\b|send\s+application|\bfinish\b|complete\s+application",
    re.IGNORECASE,
)
# A submit-typed control, however the selector spells it: type=submit, type="submit",
# [type='submit'].
_SUBMIT_TYPE = re.compile(r"""type\s*=\s*["']?\s*submit""", re.IGNORECASE)

# The key names a press event carries, and the keys that fire a default submit.
_PRESS_KEYS = ("key", "keys", "value")
_ENTER_KEYS = frozenset({"enter", "return", "\n", "\r", "\r\n"})

# The arg a click's target might arrive under (browser readers vary).
_CLICK_KEYS = ("selector", "text", "label", "name", "value", "aria_label", "ariaLabel", "target", "element")
# The arg an eval's script might arrive under.
_EVAL_KEYS = ("script", "code", "expression", "js", "source", "function", "fn")


def _text(value: object) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _gather(args: object, keys: tuple[str, ...]) -> str:
    """Join the string values present under any of ``keys`` — the searchable text for a call."""
    if not isinstance(args, dict):
        return ""
    return " ".join(_text(args[k]) for k in keys if args.get(k) not in (None, ""))


def _is_submit_click(args) -> bool:
    hay = _gather(args, _CLICK_KEYS)
    return bool(_SUBMIT_WORDS.search(hay) or _SUBMIT_TYPE.search(hay))


def _is_enter_press(args) -> bool:
    key = _gather(args, _PRESS_KEYS).strip().lower()
    if not key:
        return False
    return key in _ENTER_KEYS or key.endswith("enter") or key.endswith("return")


def _is_submit_eval(args) -> bool:
    script = _gather(args, _EVAL_KEYS)
    low = script.lower()
    if ".submit(" in low or "requestsubmit" in low:
        return True
    # A programmatic click counts only when it targets a submit/apply control — a plain
    # ``.click()`` on some other element (or a read-only eval) is not a submit.
    if ".click(" in low and (_SUBMIT_WORDS.search(script) or _SUBMIT_TYPE.search(script)):
        return True
    return False


def is_submit_like(tool_name: object, args: object) -> bool:
    """Whether a tool call would submit a job application.

    ``True`` for: a ``browser_click`` whose target text matches submit / apply / send
    application / finish / complete application (case-insensitive) or carries ``type=submit``;
    a ``browser_press`` of Enter / Return; a ``browser_eval`` whose script contains ``.submit(``,
    ``requestSubmit``, or ``.click()`` alongside a submit/apply word. Every other call — including
    ``browser_fill`` / ``browser_select`` / ``browser_upload`` / ``browser_form_read`` — is
    ``False`` and passes the gate untouched."""
    name = _text(tool_name).strip().lower()
    if name == "browser_click":
        return _is_submit_click(args)
    if name == "browser_press":
        return _is_enter_press(args)
    if name == "browser_eval":
        return _is_submit_eval(args)
    return False
