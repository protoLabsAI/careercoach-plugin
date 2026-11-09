"""Hard submit gate — one-shot operator grants + submit-click detection (#4032 phase 3c).

Phase 3's non-goal is "submitting without explicit operator consent". A line in a skill can't
enforce that, and neither can a tool the agent decides to call on its own: consent has to come
from the operator through a channel the model can't forge, and submit clicks have to be *blocked*
unless that consent exists. This module is the enforceable half.

Two pure, host-free pieces:

* **grant state** — an in-process, one-shot, time-limited authorization bound to a
  ``(session_id, verification_id)`` PAIR: the fill session (``formfill.py``) plus the specific
  read-back event (``formfill.current_verification``) the operator approved. ``careercoach_request_submit``
  records one ONLY after an operator approves an interrupt; the submit-gate middleware spends it on
  the next submit-like browser call — but only while that session's CURRENT verification is still the
  exact one granted (the middleware rechecks ``formfill``, so a grant can't authorize a submit after
  its plan was re-planned, re-verified, or displaced) — and nothing else creates one. Binding to the
  verification_id, not the session id alone, is what makes a grant exclusive to ONE verification: two
  forms can collide on a single session id, but each clean read-back mints its own fresh
  verification_id, so a grant for form A can never be spent on a colliding form B. It lives in memory
  on purpose: an authorization to click submit should not survive a restart, a checkpoint, or a copy
  to another turn.
* **submit detection** — ``is_submit_like(tool_name, args)`` recognises the browser calls that
  would send an application (a submit/finish click, an Enter press, a ``.submit()`` eval), so the
  middleware knows which calls to gate. It does NOT gate a job board's "Apply" / "Apply now" button
  (that OPENS the form) or an "Apply filters" listing control — those are not submits.

* **the core gate seam** — on a core whose agent_browser offers ``agent_browser.add_gate_policy``, the
  middleware registers ``core_policy_decision`` with it instead of judging argument text: the core
  resolves what a click / Enter / eval really hits (a ``@eN`` ref included), and the policy allows a
  submit only by spending the same grant under the same verification check. See the section at the end.

**Limits, stated plainly.** This is defense in depth *over the browser tools* — it does NOT
sandbox the browser. It stops the *agent* from clicking submit without an unforgeable operator
approval; the operator can still submit by hand in the visible browser whenever they choose.

No host imports, so every function is unit-testable with nothing but monotonic time.
"""

from __future__ import annotations

import contextvars
import re
import threading
import time

__all__ = [
    "GRANT_TTL_S",
    "BLOCKED",
    "grant",
    "consume",
    "active_grant",
    "clear",
    "is_submit_like",
    "core_policy_decision",
    "ensure_core_policy",
    "core_policy_installed",
    "core_gate_call",
    "live_grant_for_current_verification",
]

# What a blocked submit returns — the same words from the fallback middleware and the core-gate policy.
BLOCKED = (
    "Blocked by careercoach submit gate: no operator authorization. Run careercoach_verify_fill "
    "until VERIFIED, then careercoach_request_submit."
)

# Default life of a grant. A one-shot authorization is meant to cover an imminent click, not sit
# around: if the agent doesn't click within this window, the operator approves again.
GRANT_TTL_S = 120

_lock = threading.Lock()
# session_id -> (verification_id, monotonic expiry deadline). At most one grant is ever live
# (``grant`` supersedes any prior one). Each grant names BOTH the authorized session and the specific
# read-back event (``verification_id``) the operator approved, and a later approval supersedes an
# earlier one. It does NOT, on its own, bind a particular browser click to a session: a
# ``browser_click`` carries no session id, so the middleware can only spend the single live grant on
# the next submit-like call. The binding to a specific verified plan is enforced one level up, in the
# middleware, which spends a grant ONLY while ``formfill.current_verification(session_id)`` still
# equals the granted ``verification_id`` — so a grant can't survive a re-plan (which clears the id),
# a re-verification (which mints a NEW id), or a colliding form B's read-back (also a new id) to
# authorize a submit against anything but the one event approved. See ``_register_submit_middleware``.
_grants: dict[str, tuple[str, float]] = {}


def _now() -> float:
    """Monotonic seconds — immune to wall-clock jumps, and patchable in tests."""
    return time.monotonic()


def _prune(now: float) -> None:
    """Drop every expired grant (caller holds ``_lock``)."""
    for sid in [s for s, (_vid, deadline) in _grants.items() if deadline <= now]:
        _grants.pop(sid, None)


def grant(session_id: str, verification_id: str, ttl_s: int = GRANT_TTL_S) -> None:
    """Record a ONE-SHOT, time-limited grant for the ``(session_id, verification_id)`` PAIR.

    ``session_id`` is a ``formfill`` fill session; ``verification_id`` is the token that session's
    clean read-back minted (``formfill.current_verification``) — together they name the ONE
    verification event the operator approved. The next submit-like tool call consumes it
    (``consume``), but only on an EXACT pair match; it expires ``ttl_s`` seconds from now even if
    never used. A fresh grant supersedes any earlier one, so authorizations can't accumulate. Only
    ``careercoach_request_submit``'s approved-interrupt path calls this — nothing else in the plugin
    does.

    Both halves must be non-empty: a session with no current verification can't be granted, so there
    is never a grant that a mere session-id collision could satisfy."""
    sid = str(session_id or "").strip()
    vid = str(verification_id or "").strip()
    if not sid or not vid:
        return
    with _lock:
        _grants.clear()  # one live grant at a time
        _grants[sid] = (vid, _now() + max(0, int(ttl_s)))


def consume(session_id: str, verification_id: str) -> bool:
    """Spend the grant for the ``(session_id, verification_id)`` pair: ``True`` if a live (unexpired)
    grant for that EXACT pair existed and is now gone, else ``False``. One-shot — a second call for
    the same pair returns ``False`` — and a mismatch on EITHER half (wrong session, or the right
    session but a different/empty verification) returns ``False`` without spending anything, so a
    grant for one verification is never consumed by another."""
    sid = str(session_id or "").strip()
    vid = str(verification_id or "").strip()
    with _lock:
        now = _now()
        _prune(now)
        entry = _grants.get(sid)
        if entry is not None and vid and entry[0] == vid and entry[1] > now:
            _grants.pop(sid, None)
            return True
        return False


def active_grant() -> tuple[str, str] | None:
    """The ``(session_id, verification_id)`` of the live (unexpired) grant, or ``None``. Prunes
    expired grants."""
    with _lock:
        now = _now()
        _prune(now)
        for sid, (vid, deadline) in _grants.items():
            if deadline > now:
                return (sid, vid)
        return None


def clear() -> None:
    """Drop every grant. Not part of the consent path — a reset hook for tests / teardown."""
    with _lock:
        _grants.clear()


# ── submit detection ──────────────────────────────────────────────────────────────────────
# A click target (selector or visible text) that SENDS the application. We deliberately do NOT match
# a bare "apply": on a job board "Apply" / "Apply now" OPENS the form (it happens before any fill
# session exists, so it could never be authorized) and "Apply filters" is a listing control — a
# \bapply\b would match both, so gating them would dead-end the normal flow AND could spend the
# one-shot grant on a harmless click, refusing the real submit. The send verbs are "submit", "send
# application", "complete application" and "finish". The phrases allow flexible whitespace and an
# intervening "your"/"my"/"the" ("Send your application").
_SUBMIT_WORDS = re.compile(
    r"\bsubmit\b"
    r"|send\s+(?:your\s+|my\s+|the\s+)?application"
    r"|complete\s+(?:your\s+|my\s+|the\s+)?application"
    r"|\bfinish\b",
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
    # A programmatic click counts only when it targets a submit control — a plain ``.click()`` on
    # some other element (or a read-only eval) is not a submit.
    if ".click(" in low and (_SUBMIT_WORDS.search(script) or _SUBMIT_TYPE.search(script)):
        return True
    return False


def is_submit_like(tool_name: object, args: object) -> bool:
    """Whether a tool call would submit a job application.

    ``True`` for: a ``browser_click`` whose target text matches submit / send application /
    complete application / finish (case-insensitive) or carries ``type=submit``; a ``browser_press``
    of Enter / Return; a ``browser_eval`` whose script contains ``.submit(``, ``requestSubmit``, or
    ``.click()`` alongside one of those send verbs. A bare "Apply" / "Apply now" (opens the form) or
    "Apply filters" (a listing control) is NOT a submit and is ``False``. Every other call —
    including ``browser_fill`` / ``browser_select`` / ``browser_upload`` / ``browser_form_read`` — is
    ``False`` and passes the gate untouched."""
    name = _text(tool_name).strip().lower()
    if name == "browser_click":
        return _is_submit_click(args)
    if name == "browser_press":
        return _is_enter_press(args)
    if name == "browser_eval":
        return _is_submit_eval(args)
    return False


# ── the core browser gate seam (protoAgent agent_browser, ``agent_browser.add_gate_policy``) ─────
# Newer cores resolve what a click / Enter / eval will REALLY hit (a ``@eN`` ref included) and ask the
# operator on their own approval card for a submit. Matching words in a tool's argument text can't do
# that — ``browser_click("@e42")`` on the real Submit button reads as nothing — so where the core offers
# the seam, careercoach decides on the core's verdict instead: a policy the core runs inside the browser
# tool, after it resolved the target. It ALLOWS a submit the operator already approved on the
# careercoach card (spending the one-shot grant, under exactly the middleware's verification check, so
# the operator sees ONE card, not two) and DENIES any other submit. Everything else gets no opinion.
#
# The policy is live only inside a tool call the careercoach middleware wrapped (``core_gate_call``).
# That keeps a stale policy inert: core keeps its policy list across a careercoach reload, and an old
# generation's policy holds the old, empty grant table — left live it would deny every approved submit
# (deny beats allow). Outside a careercoach-wrapped call (a disabled plugin, a subagent graph without
# plugin middleware) it returns no opinion and the core gate decides on its own.
_CORE_CALL: contextvars.ContextVar[dict | None] = contextvars.ContextVar("careercoach_core_gate_call", default=None)
_core_lock = threading.Lock()
# The add_gate_policy service the policy is registered with, and the function that removes it. One
# policy per module generation: two would race for the single grant and the loser's deny would win.
_core: dict = {"service": None, "remove": None}

SUBMIT_CATEGORY = "submit"
ALLOWED_REASON = "the operator approved this submit on the careercoach approval card"


def core_policy_decision(action: object, current_verification, is_application_page=None) -> dict | None:
    """The careercoach rule over one core-resolved browser action. Pure apart from the grant table.

    ``action`` is the plain dict the core gate passes a policy; only ``action["verdict"]["categories"]``
    is read, so the decision rests on what the core resolved the target to be, never on argument text.
    ``current_verification(session_id)`` is ``formfill.current_verification``. Returns ``None`` (no
    opinion) unless the core reads the action as a submit; then ``{"decision": "allow"}`` when the live
    grant still matches its session's CURRENT verification and was spent, else ``{"decision": "deny"}``
    with :data:`BLOCKED`. A stale or mismatched grant is never spent.

    ``is_application_page(page_url)`` (``ats.detect``) scopes the hard deny to job applications: a
    submit on some other page, with no grant live, gets ``None`` — the core's own operator approval
    card decides it — rather than careercoach's job-application BLOCKED message (QA, PR #120)."""
    verdict = action.get("verdict") if isinstance(action, dict) else None
    categories = verdict.get("categories") if isinstance(verdict, dict) else None
    if not isinstance(categories, (list, tuple, set, frozenset)) or SUBMIT_CATEGORY not in categories:
        return None
    live = active_grant()
    if live is not None:
        sid, vid = live
        if vid and current_verification(sid) == vid and consume(sid, vid):
            return {"decision": "allow", "reason": ALLOWED_REASON}
    elif callable(is_application_page):
        page_url = action.get("page_url") if isinstance(action, dict) else ""
        try:
            on_application = bool(is_application_page(str(page_url or "")))
        except Exception:  # noqa: BLE001 — can't tell: keep the hard gate
            on_application = True
        if not on_application:
            return None  # not a job application: the core's operator card decides
    return {"decision": "deny", "reason": BLOCKED}


def ensure_core_policy(service, current_verification, is_application_page=None) -> bool:
    """Make sure exactly one careercoach policy is registered with ``service`` (the core's
    ``agent_browser.add_gate_policy``), and report whether one is.

    ``service`` is ``None`` on a core without the seam or with agent_browser off: any policy this
    module registered earlier is removed and the answer is ``False`` (the fallback middleware gates).
    A service that changed (agent_browser reloaded) gets a fresh registration and the old one is
    removed. A registration that fails also answers ``False``, so the fallback still gates."""
    with _core_lock:
        if service is not None and _core["service"] is service:
            return True
        old = _core["remove"]
        _core["service"] = _core["remove"] = None
        if callable(old):
            try:
                old()
            except Exception:  # noqa: BLE001 — a provider that went away can't fail the gate
                pass
        if service is None:
            return False

        def careercoach_submit_policy(action):
            call = _CORE_CALL.get()
            if call is None:
                return None  # not a careercoach-wrapped call (or a stale generation): no opinion
            decision = core_policy_decision(action, current_verification, is_application_page)
            if decision is not None and decision.get("decision") == "allow":
                call["allowed"] = True  # this call spent the grant (see core_gate_call)
            return decision

        try:
            remove = service(careercoach_submit_policy, categories=(SUBMIT_CATEGORY,), name="careercoach submit gate")
        except Exception:  # noqa: BLE001 — a broken seam falls back to the middleware's own gate
            return False
        _core["service"] = service
        _core["remove"] = remove
        return True


def core_policy_installed() -> bool:
    """Whether a careercoach policy is registered with the core gate right now."""
    with _core_lock:
        return _core["service"] is not None


class core_gate_call:
    """Context manager the middleware wraps a tool call in: the careercoach policy is live inside it.

    ``allowed`` turns ``True`` when the policy allowed (and so spent the grant) during the call."""

    def __enter__(self):
        self._record = {"allowed": False}
        self._token = _CORE_CALL.set(self._record)
        return self

    @property
    def allowed(self) -> bool:
        return bool(self._record.get("allowed"))

    def __exit__(self, *exc):
        _CORE_CALL.reset(self._token)
        return False


def live_grant_for_current_verification(current_verification) -> tuple[str, str] | None:
    """The live grant, if it still matches its session's CURRENT verification, else ``None``. Spends
    nothing — the middleware's argument-text floor checks with this before letting a call reach the core."""
    live = active_grant()
    if live is None:
        return None
    sid, vid = live
    return live if vid and current_verification(sid) == vid else None
