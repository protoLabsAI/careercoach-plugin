"""Saved standard application answers — confirm once, reuse only confirmed. Host-free.

Every ATS form asks the same handful of questions — phone, work authorization, sponsorship,
voluntary self-ID, LinkedIn — and before this module the agent improvised them per form and got
several wrong ("Yes, Ireland Highly Skilled Worker Visa"; a country of "Afghanistan"). The fix is
to hold those answers as *state*: store each one once, have the operator confirm it once, and let
form filling draw ONLY from the confirmed set. An unconfirmed draft is a proposal the operator has
not yet seen or approved, so it never fills a form.

Each key stores ``{value, status: "draft"|"confirmed", confirmed_at}``:

* ``propose(key, value)`` writes a draft. Changing a *confirmed* value demotes it back to draft
  (an edit always needs a fresh confirmation), while re-proposing the identical confirmed value is
  a no-op that leaves it confirmed.
* ``confirm(keys)`` promotes the named drafts to confirmed — call it only after the operator has
  seen the exact values and said yes.
* ``confirmed()`` returns only the confirmed values; that's the dict form filling reads.
* ``seed_from_profile(prof, full_name)`` proposes drafts from the verified profile (the unambiguous
  pieces of the contact line, the location, the recorded work authorization) and defaults the four
  self-ID keys to "Decline to self-identify". It NEVER overwrites an existing entry. The NAME keys
  are the exception: ``seed_names`` stores the operator's own name (profile name and/or the
  ``full_name`` setting) as CONFIRMED when the two agree, and as DRAFTS when they disagree — so the
  ``full_name`` setting is threaded through here rather than dropped.

**Where it lives.** ``answers.json`` in the plugin's per-instance store, beside ``profile.json`` and
``applications.json``, through the same helpers (``profile.py``): the host's instance root (ADR
0004), ``CAREERCOACH_DIR`` to override. Writes go through the profile's serialized, atomic
``_write_store`` under an ``answers`` lock, so parallel tool calls can't lose each other's edits.
No host imports, so every function is unit-testable with nothing but a temp dir.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from . import profile as _store

StoreUnreadable = _store.StoreUnreadable
log = _store.log

DRAFT = "draft"
CONFIRMED = "confirmed"

# The questions every ATS form asks — key → a one-line description shown to the agent/operator.
STANDARD_KEYS: dict[str, str] = {
    "full_name": "Full name as you write it on an application",
    "first_name": "First (given) name",
    "last_name": "Last (family) name",
    "email": "Email address",
    "phone_country": "Phone country — the country NAME as shown in form pickers (e.g. 'United States')",
    "phone_number": "Phone number in national format, no country code",
    "linkedin_url": "LinkedIn profile URL",
    "portfolio_url": "Portfolio / personal website URL",
    "current_location": "Current location (city, region, country)",
    "authorized_us": "Authorized to work in the US (yes/no)",
    "requires_sponsorship": "Requires visa sponsorship now or in the future (yes/no)",
    "willing_to_relocate": "Willing to relocate (yes/no)",
    "gender": "Gender — voluntary self-identification",
    "race_ethnicity": "Race / ethnicity — voluntary self-identification",
    "veteran_status": "Veteran status — voluntary self-identification",
    "disability_status": "Disability status — voluntary self-identification",
    "pronouns": "Pronouns",
}

# The voluntary EEO self-ID questions. Defaulted to a decline so the agent never guesses a value
# the operator didn't give — declining is a valid, operator-safe answer they can change.
SELF_ID_KEYS: tuple[str, ...] = ("gender", "race_ethnicity", "veteran_status", "disability_status")
DECLINE = "Decline to self-identify"


def _path() -> Path:
    return _store._dir() / "answers.json"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _now_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry(value: str, status: str = DRAFT, confirmed_at: str = "") -> dict:
    return {"value": value, "status": status, "confirmed_at": confirmed_at}


def _norm_entry(raw: object) -> dict | None:
    """One stored entry, normalized to ``{value, status, confirmed_at}``, or ``None`` if it isn't a
    JSON object. An unknown status reads as a draft, and only a confirmed entry keeps a timestamp."""
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    status = status if status in (DRAFT, CONFIRMED) else DRAFT
    confirmed_at = str(raw.get("confirmed_at", "") or "") if status == CONFIRMED else ""
    return _entry(str(raw.get("value", "") or ""), status, confirmed_at)


def _shape_error(raw: object) -> str:
    """Why this parsed JSON can't be read as the answers store, or ``""``.

    A present top level that isn't an object, or an ``answers`` value that isn't an object, holds
    answers this module can't interpret. Reading it as empty would report "nothing saved yet" and
    the next write would erase it, so the store counts as unreadable — reported to the agent, and
    writes refused. Mirrors ``profile._shape_error`` (ADR: the shared store refuses writes over an
    unreadable file)."""
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        return "not a JSON object"
    stored = raw.get("answers")
    if stored is not None and not isinstance(stored, dict):
        return "'answers' is not a JSON object"
    return ""


def _read_store() -> tuple[dict, str]:
    """``(answers_by_key, error)`` — the one place the file is parsed and shape-checked.

    ``error`` is "" when the store is fine, absent or empty, else why it's unreadable (and
    ``answers_by_key`` is empty then). Only keys this module knows are returned; a single entry that
    isn't a JSON object is skipped, mirroring the profile's per-field normalization."""
    raw, err = _store._read_json(_path())
    err = err or _shape_error(raw)
    if err:
        return {}, err
    stored = raw.get("answers") if isinstance(raw, dict) else None
    out: dict[str, dict] = {}
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in STANDARD_KEYS:
                entry = _norm_entry(value)
                if entry is not None:
                    out[key] = entry
    return out, ""


def load_checked() -> tuple[dict, str]:
    """``(answers_by_key, error)``: the stored answers plus why the file is unreadable, or "" when it
    is fine, absent or empty. Readers that can tell the operator the file is damaged use this; plain
    ``load`` below drops the error. Logs LOUDLY when unreadable — a silent "nothing saved yet" reads
    as the feature not working rather than the data being damaged."""
    stored, err = _read_store()
    if err:
        log.warning("[careercoach] answers at %s is unreadable (%s) — reading as empty, refusing writes", _path(), err)
    return stored, err


def load() -> dict:
    """Every stored answer by key, normalized. A missing, empty or unreadable file reads as an empty
    store (a turn never breaks); only keys this module knows are returned. A writer uses
    ``_load_strict`` instead, which refuses to overwrite an unreadable file rather than read it as
    empty."""
    return load_checked()[0]


def _load_strict() -> dict:
    """The stored answers for a writer: an unreadable store raises ``StoreUnreadable`` rather than
    reading as empty. Without this, ``propose`` / ``confirm`` / ``seed_from_profile`` would load
    ``{}`` from one corrupt byte and then save it — overwriting every saved and confirmed answer, and
    replacing the ``.bak`` so the last good copy is lost too."""
    stored, err = _read_store()
    if err:
        raise StoreUnreadable(_path(), err)
    return stored


def _save(stored: dict) -> None:
    """Atomic write (callers hold the ``answers`` lock)."""
    payload = {"answers": stored, "updated": _today()}
    _store._write_store(_path(), json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def propose(key: str, value: str) -> dict:
    """Write a DRAFT value for one standard key; returns the stored entry.

    Changing a value that was already confirmed demotes it back to draft, so an edit always has to
    be confirmed again. Re-proposing the identical confirmed value is a no-op that leaves it
    confirmed. An unknown key raises ``KeyError`` (the tool maps it to a readable error)."""
    if key not in STANDARD_KEYS:
        raise KeyError(key)
    value = (value or "").strip()
    with _store._locked("answers"):
        stored = _load_strict()  # refuse to write over an unreadable file (don't erase the rest)
        current = stored.get(key)
        if current and current.get("status") == CONFIRMED and current.get("value") == value:
            return current  # an identical re-propose leaves a confirmed value confirmed
        stored[key] = _entry(value, DRAFT)
        _save(stored)
        return stored[key]


def confirm(keys) -> list[str]:
    """Promote the named drafts to confirmed; returns the keys actually promoted.

    A key with no stored value is skipped (there's nothing to confirm). An unknown key raises
    ``KeyError``. Confirm only after the operator has seen the exact values and said yes."""
    wanted: list[str] = []
    for key in keys:
        key = (key or "").strip()
        if not key:
            continue
        if key not in STANDARD_KEYS:
            raise KeyError(key)
        wanted.append(key)
    promoted: list[str] = []
    with _store._locked("answers"):
        stored = _load_strict()  # refuse to write over an unreadable file (don't erase the rest)
        for key in wanted:
            entry = stored.get(key)
            if entry is None or not entry.get("value"):
                continue
            entry["status"] = CONFIRMED
            entry["confirmed_at"] = _now_ts()
            promoted.append(key)
        if promoted:
            _save(stored)
    return promoted


def confirmed() -> dict:
    """Only the confirmed values, ``{key: value}`` — the one set form filling may draw from."""
    return {k: e["value"] for k, e in load().items() if e["status"] == CONFIRMED and e["value"]}


# ── seeding from the verified profile ──────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[A-Za-z0-9-]+\.)?linkedin\.com/[^\s,;|]+", re.I)
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{5,}\d")


def _from_contact(contact: str) -> dict[str, str]:
    """The unambiguous pieces of a free-text contact line — an email, a LinkedIn URL, a phone
    number — each only when exactly one candidate is present. A line with two emails seeds neither
    and leaves it for the operator rather than guessing. The phone is read from the text with the
    email and LinkedIn removed first, so digits inside those can't be mistaken for one."""
    out: dict[str, str] = {}
    text = contact or ""
    emails = list(dict.fromkeys(_EMAIL_RE.findall(text)))
    if len(emails) == 1:
        out["email"] = emails[0]
    links = list(dict.fromkeys(m.rstrip(" ·.,;|") for m in _LINKEDIN_RE.findall(text)))
    if len(links) == 1:
        out["linkedin_url"] = links[0]
    remainder = text
    for token in _EMAIL_RE.findall(text) + _LINKEDIN_RE.findall(text):
        remainder = remainder.replace(token, " ")
    phones = list(dict.fromkeys(p.strip() for p in _PHONE_RE.findall(remainder) if sum(c.isdigit() for c in p) >= 7))
    if len(phones) == 1:
        out["phone_number"] = phones[0]
    return out


# The name keys, seeded from the profile's ``name`` (or the ``full_name`` setting) as CONFIRMED: the
# operator typed that name themselves, so asking them to confirm it again only stalls a form (the
# 2026-11-09 eval stalled Airbnb and Linear on draft names).
NAME_KEYS: tuple[str, ...] = ("full_name", "first_name", "last_name")


def _name_seeds(name: str) -> dict[str, str]:
    """Drafts for the name keys from one full name. First and last are split only when the name is
    exactly two words: "Mary Ann Smith" or "Juan de la Cruz" can't be split safely, so only
    ``full_name`` is seeded and the operator gives the parts."""
    name = " ".join(str(name or "").split())
    if not name:
        return {}
    out = {"full_name": name}
    parts = name.split(" ")
    if len(parts) == 2:
        out["first_name"], out["last_name"] = parts
    return out


def seed_names(prof: dict, fallback_name: str = "") -> list[str]:
    """Seed the name keys from the operator's own name — the profile's ``name``, else
    ``fallback_name`` (the ``full_name`` setting) — as CONFIRMED answers, and return the keys created
    or promoted. A missing key is created; a DRAFT holding exactly that name is promoted (a store
    seeded before names were confirmed). Any other entry is left alone: a confirmed value, or a draft
    with a different name. When the profile and the setting name two DIFFERENT people, the names are
    only proposed as drafts, for the operator to settle. Safe to run on every read."""
    identity = prof.get("identity") if isinstance(prof, dict) else None
    identity = identity if isinstance(identity, dict) else {}
    from_profile = " ".join(str(identity.get("name", "") or "").split())
    from_setting = " ".join(str(fallback_name or "").split())
    seeds = _name_seeds(from_profile or from_setting)
    if not seeds:
        return []
    agree = not (from_profile and from_setting) or from_profile.casefold() == from_setting.casefold()
    status = CONFIRMED if agree else DRAFT
    changed: list[str] = []
    with _store._locked("answers"):
        stored = _load_strict()
        for key, value in seeds.items():
            entry = stored.get(key)
            if entry is None:
                stored[key] = _entry(value, status, _now_ts() if status == CONFIRMED else "")
                changed.append(key)
            elif status == CONFIRMED and entry.get("status") == DRAFT and entry.get("value") == value:
                stored[key] = _entry(value, CONFIRMED, _now_ts())
                changed.append(key)
        if changed:
            _save(stored)
    return changed


def seed_from_profile(prof: dict, full_name: str = "") -> list[str]:
    """Propose starter drafts from the verified profile, returning the keys created or promoted.

    Pulls email / phone / LinkedIn out of the contact line (only where unambiguous), the location,
    and the recorded work authorization, and defaults the four self-ID keys to "Decline to
    self-identify". The name keys are seeded via ``seed_names`` from the profile name / the
    ``full_name`` setting — CONFIRMED when the two agree, DRAFTS when they name two different people
    (the ``full_name`` setting is threaded through so that disagreement isn't silently confirmed).
    NEVER overwrites, demotes, or touches a confirmed entry, so it's safe to run in any order and
    on every read — a confirmed value, or a draft the operator already touched, is left exactly as
    it was."""
    identity = prof.get("identity") if isinstance(prof, dict) else None
    identity = identity if isinstance(identity, dict) else {}
    seeds: dict[str, str] = {}
    seeds.update(_from_contact(str(identity.get("contact", "") or "")))
    location = str(identity.get("location", "") or "").strip()
    if location:
        seeds["current_location"] = location
    work_auth = str(identity.get("work_auth", "") or "").strip()
    if work_auth:
        seeds["authorized_us"] = work_auth
    for key in SELF_ID_KEYS:
        seeds.setdefault(key, DECLINE)
    # The name keys (confirmed only when the profile name and the full_name setting agree — else
    # drafts, for the operator to settle). The setting must be threaded through: without it
    # ``seed_names`` has nothing to disagree with and would confirm the profile name unconditionally.
    created: list[str] = seed_names(prof, full_name)
    drafted: list[str] = []
    with _store._locked("answers"):
        stored = _load_strict()  # refuse to write over an unreadable file (don't erase the rest)
        for key, value in seeds.items():
            value = (value or "").strip()
            if key in stored or not value:
                continue  # never overwrite an existing entry; never seed a blank
            stored[key] = _entry(value, DRAFT)
            drafted.append(key)
        if drafted:
            _save(stored)
    created += drafted
    return created
