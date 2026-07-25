"""Operator profile — structured, instance-scoped, always-injected, host-free.

The coach's answer to "who am I working for?". Before this existed the answer lived in a
markdown file the operator had to find in Finder, which meant two failures: the agent
re-interviewed for facts it already held (it had no cheap way to know what it knew), and the
person being described couldn't see the record. So the profile is now *state*, not a document:

  ``profile.json``   structured, beside ``applications.json`` (same convention as ``state.py``)
       ↓ every turn        ``context_block()`` → an ``<operator_profile>`` block via middleware
       ↓ on demand         the full sections, via the profile tools
       ↓ on request        ``to_markdown()`` → a portable ``Experience.md`` export

Two design rules earn their keep:

* **Completeness is first-class.** ``completeness()`` reports what's known *and what isn't*, and
  that goes in front of the model every turn. An agent that can see it already has your contact
  details doesn't ask for them; one that can see ``do_not_claim`` is empty knows to go get it.
* **Guardrails ride along in full.** Identity values and ``do_not_claim`` inject verbatim every
  turn because they're short and load-bearing. The long narrative sections inject as presence +
  size only, and the agent pulls them on demand — always-on context shouldn't scale with the
  length of someone's career.

No host imports, so every function is unit-testable with nothing but a temp dir (set
``CAREERCOACH_DIR``) — same contract as ``state.py`` / ``rubric.py`` / ``packet.py``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Short scalar facts — injected verbatim every turn (cheap, and the ones most often re-asked).
IDENTITY_FIELDS: dict[str, str] = {
    "name": "Full name",
    "location": "Location",
    "work_auth": "Work authorization",
    "contact": "Contact (email · phone · LinkedIn · portfolio)",
    "headlines": "Role-type headlines they'd accept",
}

# Narrative sections — markdown. Injected as presence only; fetched in full when drafting.
SECTIONS: dict[str, str] = {
    "roles": "Roles and impact (most recent first)",
    "education": "Education, certifications, credentials",
    "skills": "Skills inventory, by honest level",
    "do_not_claim": "Lines never to claim on their behalf",
    "stories": "Story bank — pre-vetted STAR proof",
    "notes": "Notes for tailoring (targets, sensitivities, NDAs)",
}

# ``do_not_claim`` is a guardrail, not content: short, and worthless if the agent has to
# remember to go look it up. It injects in full alongside the identity scalars.
ALWAYS_INJECT_SECTIONS: tuple[str, ...] = ("do_not_claim",)

TOTAL_FIELDS = len(IDENTITY_FIELDS) + len(SECTIONS)

# The voice/guardrail gate, injected with the profile on every turn.
#
# It lives here, not in a skill, because of an observed failure: the writing discipline sits in
# ``job-application-assistant/writing-style.md`` and only loads on the tailoring path, so a bare
# "convert my resume to a docx" produced a real deliverable at 16 em-dashes per 1000 words
# against the operator's stated limit of 3, carrying a phrase they had explicitly retired. A rule
# that only binds when you enter through the front door isn't a rule. Always-on is the fix.
WRITE_GATE = (
    "BEFORE WRITING ANYTHING IN THEIR NAME — CV, cover letter, application answer, bio, LinkedIn\n"
    "text, or any docx/pdf/html export — load the writing discipline first (`load_skill`\n"
    "job-application-assistant, then its writing-style.md; plus `my-writing-style` if\n"
    "`list_skills` shows one) and check your draft against DO NOT CLAIM above. This applies to a\n"
    "bare \"make me a resume\" exactly as much as to a tailored application: entering through a\n"
    "side door does not suspend the rules."
)


def _dir() -> Path:
    base = Path(os.environ.get("CAREERCOACH_DIR") or (Path.home() / ".protoagent" / "careercoach"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base


def _path() -> Path:
    return _dir() / "profile.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def empty_profile() -> dict:
    """A well-formed, entirely unfilled profile — the shape every reader can rely on."""
    return {
        "identity": {k: "" for k in IDENTITY_FIELDS},
        "sections": {k: "" for k in SECTIONS},
        "updated": "",
    }


def load_profile() -> dict:
    """The stored profile, normalized. A missing/corrupt file reads as empty rather than
    raising — an unreadable profile must degrade to "I know nothing yet", never break a turn."""
    prof = empty_profile()
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prof
    if not isinstance(raw, dict):
        return prof
    for k in IDENTITY_FIELDS:
        v = (raw.get("identity") or {}).get(k, "")
        prof["identity"][k] = str(v).strip() if v is not None else ""
    for k in SECTIONS:
        v = (raw.get("sections") or {}).get(k, "")
        prof["sections"][k] = str(v).strip() if v is not None else ""
    prof["updated"] = str(raw.get("updated", "") or "")
    return prof


def save_profile(profile: dict) -> dict:
    """Persist a whole profile (normalized + stamped). Returns what was written."""
    out = empty_profile()
    for k in IDENTITY_FIELDS:
        out["identity"][k] = str((profile.get("identity") or {}).get(k, "") or "").strip()
    for k in SECTIONS:
        out["sections"][k] = str((profile.get("sections") or {}).get(k, "") or "").strip()
    out["updated"] = _now()
    _path().write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def update_field(field: str, content: str) -> dict:
    """Set one identity field or one section, leaving everything else untouched.

    Section-at-a-time is the whole point: a long onboarding interview saves as it goes, so
    nothing is lost if it's abandoned, and no write can truncate a career history the way a
    whole-file overwrite can. Raises on an unknown field rather than inventing a slot."""
    if field not in IDENTITY_FIELDS and field not in SECTIONS:
        known = ", ".join(list(IDENTITY_FIELDS) + list(SECTIONS))
        raise KeyError(f"unknown profile field {field!r}; known: {known}")
    prof = load_profile()
    text = (content or "").strip()
    if field in IDENTITY_FIELDS:
        prof["identity"][field] = text
    else:
        prof["sections"][field] = text
    return save_profile(prof)


def completeness(profile: dict | None = None) -> dict:
    """What's known, what isn't, and how much of the picture exists.

    This is the value that stops the agent re-interviewing: ``known`` names the fields it must
    never ask for again, ``missing`` names the only ones still worth asking about."""
    prof = profile if profile is not None else load_profile()
    known: list[str] = []
    missing: list[str] = []
    for k in IDENTITY_FIELDS:
        (known if prof["identity"].get(k) else missing).append(k)
    for k in SECTIONS:
        (known if prof["sections"].get(k) else missing).append(k)
    return {
        "known": known,
        "missing": missing,
        "filled": len(known),
        "total": TOTAL_FIELDS,
        "empty": len(known) == 0,
        "updated": prof.get("updated", ""),
    }


def _summarize(text: str) -> str:
    """A one-line stand-in for a narrative section: enough to prove it exists and hint at
    its weight, without spending always-on context on the body."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return f"recorded ({len(lines)} lines, {len(text)} chars)"


def context_block(profile: dict | None = None) -> str:
    """The ``<operator_profile>`` block injected before every model call.

    Deliberately framed as first-party state rather than recalled memory: this is the operator's
    own verified record, maintained by the agent, and it should carry more authority than a RAG
    hit. Returns "" when nothing is known — an empty block is noise, and the onboarding skill
    handles the cold-start case on its own."""
    prof = profile if profile is not None else load_profile()
    cov = completeness(prof)
    if cov["empty"]:
        return ""

    lines = [
        "<operator_profile>",
        "Your operator's verified profile — first-party state you maintain, not recalled memory.",
        "NEVER ask for anything listed under KNOWN; you already have it. Ask only about MISSING,",
        "and only when the task at hand actually needs it. Use the profile tools to read a full",
        "section before drafting; what follows is the index, not the content.",
        "",
        "KNOWN:",
    ]
    for k in IDENTITY_FIELDS:
        v = prof["identity"].get(k)
        if v:
            lines.append(f"  {k}: {v}")
    for k in SECTIONS:
        v = prof["sections"].get(k)
        if v and k not in ALWAYS_INJECT_SECTIONS:
            lines.append(f"  {k}: {_summarize(v)}")

    if cov["missing"]:
        lines.append("")
        lines.append("MISSING (not yet recorded): " + ", ".join(cov["missing"]))
    lines.append("")
    lines.append(f"COVERAGE: {cov['filled']} of {cov['total']} fields"
                 + (f", last updated {cov['updated']}" if cov["updated"] else ""))

    for k in ALWAYS_INJECT_SECTIONS:
        body = prof["sections"].get(k)
        if body:
            lines.append("")
            lines.append(f"{SECTIONS[k].upper()} — hard stops when writing in their name:")
            lines.append(body)

    lines += ["", WRITE_GATE, "</operator_profile>"]
    return "\n".join(lines)


def to_markdown(profile: dict | None = None) -> str:
    """Render the profile as a portable ``Experience.md``-shaped document.

    The export exists so the record stays the operator's: readable, diffable, and handable to
    anyone, including a different agent. It is generated *from* the profile, never read back as
    truth — one write direction, so the two can't drift."""
    prof = profile if profile is not None else load_profile()
    ident, sect = prof["identity"], prof["sections"]
    out = [
        "# Experience — source of truth",
        "",
        "> Generated from the Career Coach operator profile"
        + (f" on {prof.get('updated')}" if prof.get("updated") else "")
        + ". Edit the profile in the Career Coach console view; this file is an export.",
        "",
        "## Identity",
    ]
    for k, label in IDENTITY_FIELDS.items():
        out.append(f"- **{label}:** {ident.get(k) or '_(not recorded)_'}")
    for k, label in SECTIONS.items():
        out += ["", f"## {label}", "", sect.get(k) or "_(not recorded)_"]
    return "\n".join(out) + "\n"
