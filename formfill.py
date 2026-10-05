"""Form fill PLAN + read-back DIFF — the deterministic half of ATS form filling (#4032 phase 3b).

The browser plugin reads a form with ``browser_form_read`` (one object per field: ``label, kind,
name, id, required, value, options``) and fills it with ``browser_fill`` / ``browser_select`` /
``browser_upload``. Those tools belong to the *agent*, not this plugin. So Career Coach owns the
two steps on either side of the fill, where improvisation is the enemy:

* **a PLAN before filling** — ``build_plan`` maps each field to a saved standard answer (via
  ``classify``) and emits the exact value to type or pick, drawing ONLY from
  ``answers.confirmed()`` plus the session's operator-supplied ``extra``. A draft never fills a
  form; a required field with no confirmed answer, or a ``<select>`` whose answer matches no
  option, is handed back for the operator to resolve rather than guessed. This is the module that
  stops "Afghanistan" being picked because it sorted first.
* **a DIFF after filling** — ``diff`` compares the plan to what ``browser_form_read`` reads back,
  normalizing whitespace/case (digits only for phone, basename for a file). A react-select that
  silently reverted to "Afghanistan" after a planned "United States", or a required field left
  empty, shows up here. A session is ``verified`` only once its latest diff is empty.

The agent runs the browser steps between the two; this module never touches a browser.

**Where it lives.** ``fill_sessions.json`` in the plugin's per-instance store, beside
``profile.json`` / ``answers.json`` / ``applications.json`` and through the same helpers
(``profile.py``): the host's instance root (ADR 0004), ``CAREERCOACH_DIR`` to override. Writes go
through the shared serialized, atomic ``_write_store`` under a ``fill_sessions`` lock, and refuse
to overwrite an unreadable file — the same rules the other stores obey. No host imports, so every
function is unit-testable with nothing but a temp dir.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from . import answers as _answers
from . import profile as _store

StoreUnreadable = _store.StoreUnreadable
log = _store.log

# How a field's action is carried out downstream. ``fill`` → browser_fill a text/email/tel/number
# input; ``select`` → browser_select a native select, combobox or radio-group; ``upload`` →
# browser_upload a file; ``skip`` → an optional field with no answer, left blank on purpose.
FILL, SELECT, UPLOAD, SKIP = "fill", "select", "upload", "skip"

# ``kind`` values (from browser_form_read) that pick an option rather than type free text. Ashby
# renders Yes/No questions as a button GROUP, read back as ``radio-group`` / ``button-group`` (or a
# generic ``other`` carrying options) — all of which pick an option, so they classify as ``select``.
_SELECT_KINDS = frozenset(
    {
        "select",
        "combobox",
        "radio-group",
        "radio",
        "dropdown",
        "listbox",
        "multiselect",
        "button-group",
        "buttongroup",
        "buttons",
    }
)
# ``kind`` values that take a file.
_FILE_KINDS = frozenset({"file", "upload"})

# ── Ashby (#4032 phase 4b) ──────────────────────────────────────────────────────────────────
# Ashby forms ALWAYS require a résumé FILE and the plan can't know its final path, so the upload row
# carries this INSTRUCTION as its value instead of a path. The agent renders the PDF, uploads it, and
# the read-back diff only checks that a file landed (see ``_values_equal``), not that it equals this
# string.
RESUME_UPLOAD_VALUE = "PDF from careercoach_render_resume → browser_pdf"
# Why the upload goes first and every field is re-read after it: Ashby's "autofill from résumé" widget
# can overwrite values the moment the résumé is attached, so filling then uploading would silently
# lose typed answers — upload first, then read EVERY field back to catch any rewrite.
ASHBY_PLAN_NOTE = (
    "Ashby: UPLOAD THE RÉSUMÉ FIRST (the first row), then fill the rest — and re-read EVERY field with "
    "browser_form_read AFTER the upload. Ashby's 'autofill from résumé' can overwrite values you typed, "
    "so a field that changed after the upload is a mismatch the read-back diff must catch."
)
# The ONLY file field the Ashby rule auto-fills with the generated résumé — identified by its
# label/name/id, NOT merely "a required file field". A required cover letter, transcript or writing
# sample is a DIFFERENT document: it must never receive the résumé PDF, so it falls through to the
# normal "no confirmed answer → ask the operator" path instead of being mislabelled a résumé upload.
_RESUME_FIELD = re.compile(r"r[eé]sum[eé]|\bcv\b|curriculum\s+vitae")


def _norm(text: object) -> str:
    """A label/name/value folded for matching: lowercased, whitespace collapsed."""
    return " ".join(str(text if text is not None else "").split()).lower()


def _hay(field: dict) -> str:
    """The searchable text for one field — its label, name and id joined — for ``classify``."""
    parts = [_norm(field.get(k)) for k in ("label", "name", "id")]
    return " ".join(p for p in parts if p)


# ── classify: form field → a STANDARD_KEYS key (answers.py), or None ──────────────────────
# One pattern per key, matched against the normalized label/name/id. Phone is handled separately
# (country-vs-number depends on phone context), so it isn't in this table. First match wins; if a
# field matches TWO different keys the classification is ambiguous and returns None — this never
# guesses between keys. Patterns run against already-lowercased text.
_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"e-?mail")),
    ("linkedin_url", re.compile(r"linked\s?in")),
    ("portfolio_url", re.compile(r"portfolio|personal (?:web)?site|\bwebsite\b|\bweb site\b")),
    ("requires_sponsorship", re.compile(r"sponsor")),
    (
        "authorized_us",
        re.compile(r"authori[sz]ed to work|legally authori[sz]ed|work authori[sz]ation|right to work"),
    ),
    ("willing_to_relocate", re.compile(r"relocat")),
    ("current_location", re.compile(r"\blocation\b|\bcity\b|where are you (?:based|located)|current city")),
    ("gender", re.compile(r"\bgender\b")),
    ("race_ethnicity", re.compile(r"\brace\b|ethnic")),
    ("veteran_status", re.compile(r"veteran")),
    ("disability_status", re.compile(r"disab")),
    ("pronouns", re.compile(r"pronoun")),
)

# Hints that a field is the dial-code / number half of a phone input. The WORD-like terms are
# matched with WORD BOUNDARIES, never as bare substrings: a label that merely contains them —
# "Tell us why…", "Hotel", "Miscellaneous", "excellent" — must NOT read as a phone field, or its
# free-text answer would be overwritten with the confirmed phone number. The MARKERS are
# intl-tel-input widget signatures that legitimately appear as substrings of a name/id.
_PHONE_WORD = re.compile(r"\b(?:tele)?phone\b|\btel\b|\bmobile\b|\bcell\b|\bdial\b")
_PHONE_MARKERS = ("intl-tel", "intltel", " iti", "iti-", "iti_")
_COUNTRY = re.compile(r"\bcountry\b")


def _phone_key(hay: str) -> str | None:
    """``phone_country`` / ``phone_number`` / ``None`` for a field, from its phone context. A
    "country" field is the dial-code half only inside a phone widget (an intl-tel-input, or a
    "country code" select); a phone/mobile/tel field with no country is the number."""
    phone_ctx = _PHONE_WORD.search(hay) is not None or any(m in hay for m in _PHONE_MARKERS)
    has_country = _COUNTRY.search(hay) is not None or "countrycode" in hay
    if has_country and (phone_ctx or "country code" in hay or "countrycode" in hay):
        return "phone_country"
    if phone_ctx and not has_country:
        return "phone_number"
    return None


def classify(field: dict) -> str | None:
    """Map one ``browser_form_read`` field to a ``answers.STANDARD_KEYS`` key, or ``None``.

    Normalized label/name/id matching against a per-key pattern table, plus a context-dependent
    phone rule (country-in-a-phone-widget → ``phone_country``, phone/mobile/tel → ``phone_number``).
    The phone match is folded into the SAME candidate set as the table, so a field that reads as
    both a phone input AND another key is ambiguous and returns ``None``. A field that matches no
    key, or two different keys, returns ``None`` — it never guesses between two keys, and an
    ambiguous field is one for the operator to resolve."""
    if not isinstance(field, dict):
        return None
    hay = _hay(field)
    if not hay:
        return None

    matched = {key for key, pat in _KEY_PATTERNS if pat.search(hay)}
    phone_key = _phone_key(hay)
    if phone_key is not None:
        matched.add(phone_key)

    if len(matched) == 1:
        return next(iter(matched))
    return None  # no match, or ambiguous between keys → never guess


# ── options ───────────────────────────────────────────────────────────────────────────────
def _option_label(option: object) -> str:
    """One select option's visible text. An option is usually a string; some readers emit
    ``{value, label}`` (or ``text`` / ``name``) objects — take the human label then."""
    if isinstance(option, dict):
        for k in ("label", "text", "name", "value"):
            v = option.get(k)
            if v:
                return str(v)
        return ""
    return str(option if option is not None else "")


def _options(field: dict) -> list[str]:
    raw = field.get("options")
    if not isinstance(raw, list):
        return []
    return [lbl for lbl in (_option_label(o) for o in raw) if lbl]


def _match_option(value: str, options: list[str]) -> str | None:
    """The option ``value`` maps to: an exact match, else a case-insensitive exact match, else
    ``None``. NEVER a nearest/fuzzy pick — picking "Afghanistan" for an unmatched answer because it
    sorts first is exactly the failure this prevents."""
    if value in options:
        return value
    low = value.strip().lower()
    for opt in options:
        if opt.strip().lower() == low:
            return opt
    return None


def _action_for(field: dict, options: list[str]) -> str:
    kind = _norm(field.get("kind"))
    if kind in _FILE_KINDS:
        return UPLOAD
    if kind in _SELECT_KINDS or options:
        return SELECT
    return FILL


def _is_required(field: dict) -> bool:
    return bool(field.get("required"))


def _is_resume_field(field: dict) -> bool:
    """Whether a file field is the résumé/CV itself (matched on its label/name/id), as opposed to some
    other required document (a cover letter, transcript, writing sample). Only this field is auto-filled
    with the generated résumé by the Ashby rule — everything else is handed back for the operator."""
    return _RESUME_FIELD.search(_hay(field)) is not None


# ── build_plan ──────────────────────────────────────────────────────────────────────────────
def _draft_values() -> dict[str, str]:
    """Keys that have a non-empty DRAFT value (proposed but not confirmed). Used ONLY to tell the
    operator which answers still need confirming — a draft never supplies a value to a plan."""
    try:
        stored = _answers.load()
    except Exception:  # noqa: BLE001 — an unreadable answers store must not break planning
        return {}
    return {
        k: str(e.get("value", "") or "")
        for k, e in stored.items()
        if e.get("status") == _answers.DRAFT and e.get("value")
    }


def _order_phone(rows: list[dict]) -> list[dict]:
    """Ensure the phone COUNTRY row precedes the phone NUMBER row: an intl-tel-input resets the
    number when the country changes, so the country must be chosen first."""
    ci = next((i for i, r in enumerate(rows) if r.get("key") == "phone_country"), None)
    ni = next((i for i, r in enumerate(rows) if r.get("key") == "phone_number"), None)
    if ci is not None and ni is not None and ci > ni:
        rows.insert(ni, rows.pop(ci))
    return rows


def _order_resume_first(rows: list[dict]) -> list[dict]:
    """Move the Ashby résumé upload row to the FRONT: Ashby's autofill fires on upload, so the résumé
    must be attached before anything is typed, and the row the operator uploads first must read first."""
    ri = next((i for i, r in enumerate(rows) if r.get("resume")), None)
    if ri is not None and ri > 0:
        rows.insert(0, rows.pop(ri))
    return rows


def build_plan(form_fields, confirmed, extra=None, ats=None, resume_ready=True) -> dict:
    """Plan how to fill a form read by ``browser_form_read``: ``{session_id, rows, unmapped,
    unconfirmed}`` (plus a ``note`` for Ashby), persisted so ``verify_fill`` can diff against it later.

    ``confirmed`` is ``answers.confirmed()`` (the only values ever typed, besides ``extra``).
    ``extra`` maps a field LABEL to an operator-supplied answer for a one-off question in this
    session. ``ats`` names the applicant-tracking system (e.g. ``"ashby"``) so the plan can apply
    system-specific rules. For each field:

    * a row ``{label, kind, key, value, action}`` is emitted when a value is known — from
      ``confirmed`` (via ``classify``) or from ``extra`` by label. ``action`` is ``fill`` /
      ``select`` / ``upload`` by field kind; a ``select`` value must match one of the field's
      options (exact, then case-insensitive), else the field goes to ``unmapped`` with its options
      — never a fuzzy pick. A Yes/No button group reads as ``select`` and keeps its exact options.
    * the phone COUNTRY row is ordered before the phone NUMBER row.
    * a REQUIRED field with no known value goes to ``unmapped`` (ask the operator). An OPTIONAL one
      with no value becomes a ``skip`` row.
    * a field whose answer exists only as a DRAFT is listed in ``unconfirmed`` — the operator
      confirms it with ``careercoach_confirm_answers`` before it can fill.

    **Ashby** (``ats == "ashby"``): the required RÉSUMÉ file field (matched by label/name/id via
    ``_is_resume_field``) with no supplied path ALWAYS becomes an ``upload`` row — ordered FIRST —
    whose value is the résumé instruction, because Ashby forms always require a résumé file; the plan
    carries a ``note`` that the upload goes first and every field must be read back after it (its
    autofill widget can overwrite typed values). If the résumé prerequisites are missing
    (``resume_ready`` is false — no renderable profile), that field is listed under ``unmapped``
    instead of guessing a file. Any OTHER required file field (a cover letter, transcript, writing
    sample) is NOT the résumé: it takes the ordinary path — a supplied path uploads, otherwise it is
    asked about — so the résumé PDF is never uploaded into the wrong field.
    """
    confirmed = dict(confirmed or {})
    extra_by_label = {_norm(k): str(v if v is not None else "") for k, v in (extra or {}).items()}
    drafts = _draft_values()
    is_ashby = _norm(ats) == "ashby"

    fields = form_fields if isinstance(form_fields, list) else []
    rows: list[dict] = []
    unmapped: list[dict] = []
    unconfirmed: list[dict] = []
    seen_unconfirmed: set[str] = set()

    for field in fields:
        if not isinstance(field, dict):
            continue
        label = str(field.get("label") or field.get("name") or field.get("id") or "").strip()
        kind = str(field.get("kind") or "").strip()
        options = _options(field)
        action = _action_for(field, options)
        required = _is_required(field)
        key = classify(field)

        # Where the value comes from: a confirmed standard answer first, then the operator's extra.
        value = None
        if key is not None and key in confirmed:
            value = confirmed[key]
        elif _norm(label) in extra_by_label and extra_by_label[_norm(label)].strip():
            value = extra_by_label[_norm(label)].strip()

        if value is None:
            # Ashby: the required RÉSUMÉ file always uploads the generated résumé — the operator never
            # has to supply a path. Scoped to the résumé field itself (``_is_resume_field``): a required
            # cover letter / transcript is a different document and must NOT get the résumé PDF, so it
            # falls through to the normal required→ask path below. Needs a renderable profile; without
            # one, the résumé is asked about (unmapped) rather than guessed.
            if is_ashby and action == UPLOAD and required and _is_resume_field(field):
                if resume_ready:
                    rows.append(
                        {
                            "label": label,
                            "kind": kind,
                            "key": key,
                            "value": RESUME_UPLOAD_VALUE,
                            "action": UPLOAD,
                            "required": True,
                            "resume": True,
                        }
                    )
                else:
                    unmapped.append(
                        {
                            "label": label,
                            "kind": kind,
                            "key": key,
                            "required": True,
                            "reason": "a résumé is required but none can be rendered yet — complete the "
                            "profile (run careercoach_render_resume; it names the missing fields)",
                        }
                    )
                continue
            # No value to fill. A draft is a confirm-me; otherwise required→ask, optional→skip.
            if key is not None and key in drafts:
                if key not in seen_unconfirmed:
                    seen_unconfirmed.add(key)
                    unconfirmed.append({"label": label, "key": key, "draft": drafts[key]})
                continue
            if required:
                unmapped.append(
                    {"label": label, "kind": kind, "key": key, "required": True, "reason": "no confirmed answer"}
                )
            else:
                rows.append({"label": label, "kind": kind, "key": key, "value": "", "action": SKIP})
            continue

        if action == SELECT and options:
            picked = _match_option(value, options)
            if picked is None:
                unmapped.append(
                    {
                        "label": label,
                        "kind": kind,
                        "key": key,
                        "required": required,
                        "reason": "answer matches no option",
                        "answer": value,
                        "options": options,
                    }
                )
                continue
            value = picked

        rows.append({"label": label, "kind": kind, "key": key, "value": value, "action": action})

    _order_phone(rows)
    if is_ashby:
        _order_resume_first(rows)
    session_id = _rows_hash(rows)
    plan = {"session_id": session_id, "rows": rows, "unmapped": unmapped, "unconfirmed": unconfirmed}
    if is_ashby:
        plan["note"] = ASHBY_PLAN_NOTE
    _save_session(session_id, {**plan, "rows_hash": session_id})
    return plan


# ── diff: plan vs. the form read back ─────────────────────────────────────────────────────
def _digits(text: object) -> str:
    return re.sub(r"\D", "", str(text if text is not None else ""))


def _basename(text: object) -> str:
    """The file name of a path, however it's spelled (posix or windows separators) or a bare URL."""
    s = str(text if text is not None else "").strip()
    if not s:
        return ""
    name = PurePosixPath(PureWindowsPath(s).as_posix()).name
    return name or PurePath(s).name


def _values_equal(expected: str, actual: str, row: dict) -> bool:
    """Whether a read-back value matches what was planned, tolerant of presentation only. Phone
    compares digits (a country code prefix on one side is allowed); a file compares basenames;
    everything else compares whitespace/case-normalized text."""
    if row.get("resume"):
        # The Ashby résumé row's planned value is an INSTRUCTION, not a real path; it's satisfied once
        # a file is present after the upload (an empty file field means the required résumé didn't land).
        return bool(str(actual if actual is not None else "").strip())
    if row.get("key") == "phone_number":
        a, b = _digits(expected), _digits(actual)
        if not a or not b:
            return a == b
        return a == b or (len(min(a, b, key=len)) >= 7 and (a.endswith(b) or b.endswith(a)))
    if row.get("action") == UPLOAD:
        return _basename(expected).lower() == _basename(actual).lower()
    return _norm(expected) == _norm(actual)


def _index_by_label(fields) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if isinstance(fields, list):
        for f in fields:
            if isinstance(f, dict):
                out.setdefault(_norm(f.get("label") or f.get("name") or f.get("id")), f)
    return out


def diff(plan: dict, form_fields_after) -> list[dict]:
    """Compare a plan to the form read back after filling: a list of ``{label, expected, actual}``.

    Every planned row (except ``skip``) whose read-back value differs — normalized per field type
    — is a mismatch. A required field that is still empty is reported too (so an unmapped-but-
    required field the operator never filled is caught). An empty list means every planned field
    landed and nothing required is blank."""
    rows = plan.get("rows", []) if isinstance(plan, dict) else []
    after = _index_by_label(form_fields_after)
    mismatches: list[dict] = []
    covered: set[str] = set()

    for row in rows:
        if not isinstance(row, dict) or row.get("action") == SKIP:
            continue
        label = row.get("label", "")
        covered.add(_norm(label))
        field = after.get(_norm(label))
        actual = str((field or {}).get("value") or "") if field else ""
        expected = str(row.get("value") or "")
        if not _values_equal(expected, actual, row):
            mismatches.append({"label": label, "expected": expected, "actual": actual})

    for field in form_fields_after if isinstance(form_fields_after, list) else []:
        if not isinstance(field, dict) or not _is_required(field):
            continue
        label = str(field.get("label") or field.get("name") or field.get("id") or "").strip()
        value = str(field.get("value") or "")
        if value.strip():
            continue
        if _norm(label) in covered:
            continue  # already flagged as a planned-row mismatch above
        mismatches.append({"label": label, "expected": "(required — not yet filled)", "actual": ""})

    return mismatches


# ── session storage: fill_sessions.json ───────────────────────────────────────────────────
def _path():
    return _store._dir() / "fill_sessions.json"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _now_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows_hash(rows: list[dict]) -> str:
    """A stable short hash of the plan rows — the session id, and the fingerprint
    ``record_verification`` stores so a diff is tied to the plan it verified."""
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _shape_error(raw: object) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        return "not a JSON object"
    stored = raw.get("sessions")
    if stored is not None and not isinstance(stored, dict):
        return "'sessions' is not a JSON object"
    return ""


def _read_sessions() -> tuple[dict, str]:
    raw, err = _store._read_json(_path())
    err = err or _shape_error(raw)
    if err:
        return {}, err
    stored = raw.get("sessions") if isinstance(raw, dict) else None
    out: dict[str, dict] = {}
    if isinstance(stored, dict):
        for sid, rec in stored.items():
            if isinstance(rec, dict):
                out[sid] = rec
    return out, ""


def load_sessions_checked() -> tuple[dict, str]:
    stored, err = _read_sessions()
    if err:
        log.warning(
            "[careercoach] fill_sessions at %s is unreadable (%s) — reading as empty, refusing writes", _path(), err
        )
    return stored, err


def _load_strict() -> dict:
    stored, err = _read_sessions()
    if err:
        raise StoreUnreadable(_path(), err)
    return stored


def _save(sessions: dict) -> None:
    """Atomic write (callers hold the ``fill_sessions`` lock)."""
    payload = {"sessions": sessions, "updated": _today()}
    _store._write_store(_path(), json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _save_session(session_id: str, record: dict) -> None:
    """Persist one planning session. A (re)built plan ALWAYS starts UNVERIFIED: ``verified``
    describes one fill-then-read-back cycle, so writing a plan — even an identical one whose rows
    hash to the same id — cannot carry a prior ``verified`` flag forward. The browser must be filled
    and read back (``verify_fill``) again before the plan counts as verified; this is what stops a
    re-plan from arriving already-verified and being submitted without a read-back. Only ``created``
    survives a rewrite."""
    with _store._locked("fill_sessions"):
        sessions = _load_strict()  # refuse to write over an unreadable file (don't erase the rest)
        prior = sessions.get(session_id, {})
        merged = {
            **record,
            "session_id": session_id,
            "created": prior.get("created") or _now_ts(),
            "verified": False,
            "last_diff": None,
            "verified_at": "",
        }
        sessions[session_id] = merged
        _save(sessions)


def update_session(session_id: str, **fields) -> None:
    """Attach extra metadata (e.g. company / role) to a stored session; no-op if it's gone."""
    with _store._locked("fill_sessions"):
        sessions = _load_strict()
        rec = sessions.get(session_id)
        if rec is None:
            return
        rec.update(fields)
        _save(sessions)


def load_session(session_id: str) -> dict | None:
    """The stored planning session, or ``None`` if there isn't one."""
    return load_sessions_checked()[0].get(session_id)


def record_verification(session_id: str, mismatches: list[dict]) -> dict:
    """Store the latest diff for a session plus a hash of its plan rows, and mark it ``verified``
    only when the diff is empty. Returns the updated record (an empty one if the session is gone)."""
    verified = not mismatches
    with _store._locked("fill_sessions"):
        sessions = _load_strict()
        rec = sessions.get(session_id)
        if rec is None:
            return {}
        rec["last_diff"] = list(mismatches)
        rec["rows_hash"] = _rows_hash(rec.get("rows", []))
        rec["verified"] = verified
        rec["verified_at"] = _now_ts() if verified else ""
        _save(sessions)
        return rec


def invalidate_verification(session_id: str) -> None:
    """Drop a session's ``verified`` flag (and its stored diff) WITHOUT rewriting the plan, so it
    reads as unverified until it is filled and read back again. Used after a visible-browser handoff:
    a captcha / login / attestation step can re-render the form and clear filled fields, so the
    verification made BEFORE the handoff no longer describes the live form and must not authorize a
    submit. No-op if the session is gone."""
    with _store._locked("fill_sessions"):
        sessions = _load_strict()
        rec = sessions.get(session_id)
        if rec is None:
            return
        rec["verified"] = False
        rec["verified_at"] = ""
        rec["last_diff"] = None
        _save(sessions)


def is_verified(session_id: str) -> bool:
    """Whether the session's latest diff was empty (``False`` if there's no session or no diff)."""
    rec = load_session(session_id)
    return bool(rec and rec.get("verified"))
