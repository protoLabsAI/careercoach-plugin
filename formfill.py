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
import secrets
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
# The cover-letter file field — matched by label/name/id the same way. Greenhouse labels BOTH the
# résumé and the cover-letter file inputs "Attach"; only the name/id (``resume`` vs ``cover_letter``)
# tells them apart. A cover-letter-ONLY field must NEVER receive the résumé PDF (see
# ``_is_resume_value``): it takes an explicit cover-letter answer or nothing at all. A COMBINED field
# that matches BOTH this and ``_RESUME_FIELD`` (e.g. "Resume/Cover Letter") is a résumé field too and
# does accept the résumé — the drop-the-résumé guard skips it.
_COVER_LETTER_FIELD = re.compile(r"cover[\s_-]?letter")


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


# Punctuation that merely varies between equivalent wordings of the SAME option. Apostrophes are
# DROPPED outright ("don't" → "dont"), so a decline wording can be written apostrophe-free; hyphens,
# periods and commas become spaces so "self-identify" folds to "self identify".
_OPT_DROP = re.compile(r"['’]")
_OPT_SPACE = re.compile(r"[-.,]")


def _norm_option(text: object) -> str:
    """A select option or candidate value folded for matching: lowercased, the punctuation that
    varies between equivalent wordings removed (apostrophes dropped, hyphens/periods/commas → space),
    whitespace collapsed. So "Decline to self-identify" and "Decline To Self Identify" fold to the
    same string."""
    s = _OPT_DROP.sub("", str(text if text is not None else "").lower())
    s = _OPT_SPACE.sub(" ", s)
    return " ".join(s.split())


# Normalized wordings that all mean "I decline to answer this voluntary question". A value whose
# normalized form is in this set is a DECLINE answer, and is matched to an option whose normalized
# form is ALSO in it — so a stored "Decline to self-identify" resolves to a form's "Decline To Self
# Identify" or "I don't wish to answer". This equivalence applies to decline answers ONLY, and only
# when exactly one option qualifies; it is not a general synonym table. It is kept deliberately
# narrow — every entry is unambiguously a refusal to disclose, so a non-decline answer can never
# fold into it.
_DECLINE_FORMS = frozenset(
    {
        "decline to self identify",
        "decline to answer",
        "decline to state",
        "prefer not to say",
        "i dont wish to answer",
        "i do not wish to answer",
        "i dont want to answer",
        "i do not want to answer",
        "prefer not to answer",
        "i prefer not to answer",
        "i prefer not to say",
        "prefer not to disclose",
        "i choose not to disclose",
        "decline",
    }
)


def _match_option(value: str, options: list[str]) -> str | None:
    """The option ``value`` maps to, or ``None`` — NEVER a nearest/fuzzy pick (picking "Afghanistan"
    for an unmatched answer because it sorts first is the failure this prevents).

    Exact match first; then a normalized comparison (``_norm_option``: lowercase, drop the
    apostrophes/hyphens/periods/commas that vary between equivalent wordings, collapse whitespace),
    so "Decline to self-identify" equals "Decline To Self Identify". Finally, ONLY when ``value`` is
    itself a recognized DECLINE answer, the option whose normalized form is ALSO a decline answer is
    chosen — so a stored "Decline to self-identify" resolves to a form's "I don't wish to answer".
    Exactly one decline-equivalent option resolves; zero or several leave it unmatched (ask the
    operator). No other fuzzy matching is done — a non-decline mismatch is never guessed."""
    if value in options:
        return value
    nv = _norm_option(value)
    for opt in options:
        if _norm_option(opt) == nv:
            return opt
    if nv in _DECLINE_FORMS:
        declines = [opt for opt in options if _norm_option(opt) in _DECLINE_FORMS]
        if len(declines) == 1:
            return declines[0]
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


def _is_cover_letter_field(field: dict) -> bool:
    """Whether a file field is the cover letter (matched on its label/name/id). Greenhouse labels both
    the résumé and the cover-letter inputs "Attach", so the label alone can't tell them apart — the
    name/id does. A cover-letter-ONLY field never receives the résumé PDF (see ``_is_resume_value``);
    a COMBINED field also matching ``_is_resume_field`` ("Resume/Cover Letter") is excepted from that
    drop, because it is a résumé field too and the résumé is a valid document for it."""
    return _COVER_LETTER_FIELD.search(_hay(field)) is not None


def _is_resume_value(value: object) -> bool:
    """Whether a value is the résumé PDF (or its placeholder instruction) — something that must never be
    planned onto a cover-letter file field. Matches the Ashby résumé instruction and any path/URL whose
    file name starts with ``resume`` (the shape a rendered résumé takes, e.g. ``resume-gitlab.pdf``)."""
    s = str(value if value is not None else "").strip()
    if not s:
        return False
    if s == RESUME_UPLOAD_VALUE:
        return True
    return _basename(s).lower().startswith("resume")


# STANDARD answer keys split by the SHAPE of their answer. A CHOICE key's answer is one of a fixed
# set (Yes/No, a self-ID bucket, a country name) and may legitimately land on a select/combobox. A
# FREE-TEXT key's answer is prose — an email, a URL, a city, a phone number — and must NEVER be
# planned onto a choice field: a confirmed ``current_location`` of "Portland, Oregon, USA" is not an
# answer to a Yes/No "do you currently live here?" question. (An operator's EXPLICIT extra answer is
# exempt — they chose to answer that exact field, so it is honoured whatever its shape.)
_CHOICE_KEYS = frozenset(
    {
        "requires_sponsorship",
        "authorized_us",
        "willing_to_relocate",
        "gender",
        "race_ethnicity",
        "veteran_status",
        "disability_status",
        "phone_country",
        "pronouns",
    }
)
_FREE_TEXT_KEYS = frozenset(_answers.STANDARD_KEYS) - _CHOICE_KEYS


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


def build_plan(
    form_fields, confirmed, extra=None, ats=None, resume_ready=True, company="", role="", posting=""
) -> dict:
    """Plan how to fill a form read by ``browser_form_read``: ``{session_id, rows, unmapped,
    unconfirmed}`` (plus a ``note`` for Ashby), persisted so ``verify_fill`` can diff against it later.

    ``confirmed`` is ``answers.confirmed()`` (the only values ever typed, besides ``extra``).
    ``extra`` maps a field KEY to an operator-supplied answer for a one-off question in this session.
    The key is matched to a field by its ``id`` first, then ``name``, then ``label`` — so an answer
    keyed by id/name fills exactly that field. A key that is a LABEL shared by several fields (and not
    pinned to one of them by id/name) is ambiguous: that EXTRA fills NONE of them — but a confirmed
    standard answer that classifies to the field still fills it (the shared-label ambiguity drops the
    extra, not the fallback). Only a shared-label field with no confirmed answer either is handed to
    ``unmapped`` asking the operator to re-key by id or name. ``extra`` WINS over a classified
    standard answer: when both a one-off ``extra`` and a confirmed standard answer would fill a field,
    the ``extra`` is used (the standard answer is only the fallback), and each row records its
    ``source`` (``"extra"`` or ``"standard"``). ``ats`` names the applicant-tracking
    system (e.g. ``"ashby"``) so the plan can apply
    system-specific rules. ``company`` / ``role`` / ``posting`` are the identity of THIS
    application — the operator-facing company and role, plus the posting itself (its URL, when the
    caller has one). All three are folded into the ``session_id`` (see ``_session_id``) so two
    different applications USUALLY get distinct ids. This is a convenience, NOT the submit-safety
    guarantee: ids can still collide (e.g. two blank-identity plans through
    ``careercoach_plan_fill``, which passes no posting). Submit safety does not rest on id
    uniqueness — a submit grant is bound to one verification EVENT (``verification_id``, see
    ``record_verification`` / ``current_verification``), so even when B collides onto A's id, B's
    read-back mints a new verification_id and A's grant can no longer authorize a submit
    (bd-4t17 / bd-ywmm.4). For each field:

    Every row and every ``unmapped`` entry carries the field's identity — its ``label`` plus ``id``
    and ``name`` (when present) and a ``target`` locator, the most specific selector for the field
    (``#<id>`` when it has an id, else the label). This is what keeps two file fields both labelled
    "Attach" (résumé vs cover letter) distinguishable downstream — the plan, the fill and the
    read-back diff all key off the identity, not the shared label. For each field:

    * a row ``{label, kind, id?, name?, target, key, value, action, source?}`` is emitted when a
      value is known — from ``extra`` (by id, name, then label) or, failing that, from ``confirmed``
      (via ``classify``). ``action`` is ``fill`` / ``select`` / ``upload`` by field kind. A SELECT
      value must resolve to one of the field's options via ``_match_option`` (exact, then a
      normalized comparison that folds case/punctuation, then — for a decline answer only — a single
      decline-equivalent option), else the field goes to ``unmapped`` with its options; never a fuzzy
      pick. A FREE-TEXT standard answer (email, phone number, a URL, ``current_location``) is NOT a
      choice value: on a SELECT field it is never planned — the field is asked about (required) or
      left blank (optional) — so a confirmed location never lands on a Yes/No question; an explicit
      ``extra`` answer is exempt. A SELECT field whose reader listed NO options (a react-select
      combobox) is planned verbatim and flagged ``options_unknown`` for the read-back to guard. A
      Yes/No button group reads as ``select`` and keeps its
      exact options. A cover-letter-ONLY file field NEVER receives the résumé PDF: a résumé-looking
      value routed to it is dropped, and the field takes only an explicit cover-letter answer. A
      COMBINED field also matching ``_is_resume_field`` (e.g. "Resume/Cover Letter") is excepted —
      it is a résumé field too, so the résumé routing (Ashby auto-upload / Greenhouse ask) applies.
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

    **Greenhouse / generic** (no ``ats``): a résumé file field (matched by ``_is_resume_field``) with
    no supplied path is handed to ``unmapped`` asking for the résumé PDF — it is NEVER auto-filled
    here (the auto-upload is Ashby-only). The operator renders the PDF and supplies its path keyed by
    the field's id or name.
    """
    confirmed = dict(confirmed or {})
    extra_norm = {_norm(k): str(v if v is not None else "") for k, v in (extra or {}).items()}
    drafts = _draft_values()
    is_ashby = _norm(ats) == "ashby"

    fields = form_fields if isinstance(form_fields, list) else []
    # How many fields each normalized label covers — an extra answer keyed by a label shared by more
    # than one field can't be routed to a single field, so it fills none of them (asked about instead).
    label_counts: dict[str, int] = {}
    for f in fields:
        if isinstance(f, dict):
            lab = _norm(str(f.get("label") or f.get("name") or f.get("id") or ""))
            if lab:
                label_counts[lab] = label_counts.get(lab, 0) + 1
    rows: list[dict] = []
    unmapped: list[dict] = []
    unconfirmed: list[dict] = []
    seen_unconfirmed: set[str] = set()

    for field in fields:
        if not isinstance(field, dict):
            continue
        fid = str(field.get("id") or "").strip()
        fname = str(field.get("name") or "").strip()
        label = str(field.get("label") or field.get("name") or field.get("id") or "").strip()
        kind = str(field.get("kind") or "").strip()
        options = _options(field)
        action = _action_for(field, options)
        required = _is_required(field)
        key = classify(field)

        # Identity carried on every row / unmapped entry so same-label fields stay distinguishable:
        # id and name when present, and ``target`` — the most specific locator (``#id`` else the label).
        ident = {"label": label, "kind": kind}
        if fid:
            ident["id"] = fid
        if fname:
            ident["name"] = fname
        ident["target"] = f"#{fid}" if fid else label

        # Where the value comes from: the operator's EXTRA answer WINS — keyed by the field's id, then
        # name, then label — and a confirmed standard answer is only the fallback when no non-empty
        # extra exists. (The precedence was backwards before: a classified standard answer beat the
        # operator's explicit one-off, so a confirmed location landed on a Yes/No "do you live here?"
        # question.) An extra keyed by a LABEL shared by several fields (and not pinned to one of them
        # by id/name) is ambiguous: it can't fill ANY of them, so ``shared_label`` marks it so that
        # the extra is dropped — but the ambiguity is the EXTRA's alone. A confirmed standard answer
        # classifies to THIS field unambiguously, so it still fills in as the fallback (``shared_label``
        # must NOT gate the confirmed branch: doing so silently dropped a confirmed answer whenever its
        # field's label happened to be shared). Only when there is no confirmed answer either does the
        # shared-label field go to ``unmapped`` asking the operator to re-key by id/name. ``source``
        # ("extra" / "standard") is recorded on the row so the read-back and the operator can see which
        # answer was used.
        value = None
        source = None
        shared_label = False
        nid, nname, nlabel = _norm(fid), _norm(fname), _norm(label)
        if nid and extra_norm.get(nid, "").strip():
            value, source = extra_norm[nid].strip(), "extra"
        elif nname and extra_norm.get(nname, "").strip():
            value, source = extra_norm[nname].strip(), "extra"
        elif nlabel and extra_norm.get(nlabel, "").strip():
            if label_counts.get(nlabel, 0) > 1:
                shared_label = True  # ambiguous: the key names a label several fields share
            else:
                value, source = extra_norm[nlabel].strip(), "extra"
        if value is None and key is not None and key in confirmed:
            value, source = confirmed[key], "standard"

        # A cover-letter-ONLY file field NEVER receives the résumé PDF (or its placeholder): that is
        # the wrong document, so the value is dropped and the field is treated as having no answer.
        # A COMBINED field (its label/name/id matches BOTH ``cover letter`` AND ``_is_resume_field`` —
        # e.g. "Resume/Cover Letter") is a résumé field too and legitimately takes the résumé, so the
        # guard skips it; the résumé routing below (Ashby auto-upload / Greenhouse ask) then applies.
        if (
            action == UPLOAD
            and _is_cover_letter_field(field)
            and not _is_resume_field(field)
            and value is not None
            and _is_resume_value(value)
        ):
            value = None

        if value is None:
            # An extra keyed by a shared label can't be routed to one field — ask the operator to
            # re-key it by this field's id or name rather than applying it to all the look-alikes.
            if shared_label:
                unmapped.append(
                    {
                        **ident,
                        "key": key,
                        "required": required,
                        "reason": "label shared by several fields — key the answer by id or name",
                    }
                )
                continue
            # Ashby: the required RÉSUMÉ file always uploads the generated résumé — the operator never
            # has to supply a path. Scoped to the résumé field itself (``_is_resume_field``): a required
            # cover letter / transcript is a different document and must NOT get the résumé PDF, so it
            # falls through to the normal required→ask path below. Needs a renderable profile; without
            # one, the résumé is asked about (unmapped) rather than guessed.
            if is_ashby and action == UPLOAD and required and _is_resume_field(field):
                if resume_ready:
                    rows.append(
                        {
                            **ident,
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
                            **ident,
                            "key": key,
                            "required": True,
                            "reason": "a résumé is required but none can be rendered yet — complete the "
                            "profile (run careercoach_render_resume; it names the missing fields)",
                        }
                    )
                continue
            # Greenhouse / generic: a résumé file field with no supplied path is asked about — never
            # auto-filled here (the auto-upload is Ashby-only). The operator renders the PDF and
            # supplies its path keyed by the field's id or name.
            if not is_ashby and action == UPLOAD and _is_resume_field(field):
                unmapped.append(
                    {
                        **ident,
                        "key": key,
                        "required": required,
                        "reason": "a résumé PDF is required — render it with careercoach_render_resume → "
                        "browser_pdf, then supply its path as an extra answer keyed by this field's id or name",
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
                unmapped.append({**ident, "key": key, "required": True, "reason": "no confirmed answer"})
            else:
                rows.append({**ident, "key": key, "value": "", "action": SKIP})
            continue

        if action == SELECT:
            # A FREE-TEXT standard answer (an email, a URL, a city, a phone number) is not a choice —
            # it must never be planned onto a select/combobox (the confirmed location landing on a
            # Yes/No "do you live here?" question). Only an operator's EXPLICIT extra, or a
            # CHOICE-typed standard answer, belongs on a choice field; a free-text standard answer is
            # asked about (required) or left blank (optional), never typed.
            if source == "standard" and key in _FREE_TEXT_KEYS:
                if required:
                    unmapped.append(
                        {
                            **ident,
                            "key": key,
                            "required": True,
                            "reason": "standard answer is free text; this is a choice question",
                        }
                    )
                else:
                    rows.append({**ident, "key": key, "value": "", "action": SKIP})
                continue
            if options:
                # A select WITH options must resolve to one of them — exact, normalized, or a single
                # decline-equivalent — whatever the source; otherwise ask the operator, never guess.
                picked = _match_option(value, options)
                if picked is None:
                    unmapped.append(
                        {
                            **ident,
                            "key": key,
                            "required": required,
                            "reason": "answer matches no option",
                            "answer": value,
                            "options": options,
                        }
                    )
                    continue
                value = picked

        row = {**ident, "key": key, "value": value, "action": action}
        if source is not None:
            row["source"] = source
        # A select/combobox whose options the reader didn't list (a react-select combobox reads back
        # with none) is planned verbatim and flagged ``options_unknown`` — the browser_select
        # read-back then guards that the chosen value actually took.
        if action == SELECT and not options:
            row["options_unknown"] = True
        rows.append(row)

    _order_phone(rows)
    if is_ashby:
        _order_resume_first(rows)
    session_id = _session_id(rows, company, role, posting)
    plan = {"session_id": session_id, "rows": rows, "unmapped": unmapped, "unconfirmed": unconfirmed}
    if is_ashby:
        plan["note"] = ASHBY_PLAN_NOTE
    _save_session(session_id, {**plan, "rows_hash": _rows_hash(rows)})
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
    # A self-ID SELECT the reader gave no options for (``options_unknown`` — a react-select combobox)
    # was planned with the stored decline wording VERBATIM, so the filler picks the form's OWN decline
    # option, whose text differs ("Decline To Self Identify", "I don't wish to answer"). When the plan
    # value is itself a recognized decline, any decline-equivalent read-back counts as equal — but ONLY
    # decline↔decline: an empty actual ("") and a non-decline one ("Male", "I am not a protected
    # veteran") are not in ``_DECLINE_FORMS`` and fall through to the exact comparison below, staying
    # mismatches. Rows WITH known options never reach here (``build_plan`` already resolved them to the
    # real option), so they keep exact matching.
    if (
        row.get("action") == SELECT
        and row.get("options_unknown")
        and _norm_option(expected) in _DECLINE_FORMS
        and _norm_option(actual) in _DECLINE_FORMS
    ):
        return True
    return _norm(expected) == _norm(actual)


def _indexes(fields) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Three lookups over the read-back fields — by id, by name, by (resolved) label — so a plan row
    can be matched to its OWN field by the most specific identity it carries. ``setdefault`` keeps the
    first field for any collision; two fields that share only a label stay separable by id/name."""
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    by_label: dict[str, dict] = {}
    if isinstance(fields, list):
        for f in fields:
            if not isinstance(f, dict):
                continue
            fid, fname = _norm(f.get("id")), _norm(f.get("name"))
            flabel = _norm(f.get("label") or f.get("name") or f.get("id"))
            if fid:
                by_id.setdefault(fid, f)
            if fname:
                by_name.setdefault(fname, f)
            if flabel:
                by_label.setdefault(flabel, f)
    return by_id, by_name, by_label


def _match_field(row: dict, by_id: dict, by_name: dict, by_label: dict) -> dict | None:
    """The read-back field a plan row refers to, matched by id first, then name, then label — so two
    fields that share a label (a résumé and a cover letter both labelled "Attach") are compared each
    against its own field, not collapsed onto whichever came first."""
    rid, rname, rlabel = _norm(row.get("id")), _norm(row.get("name")), _norm(row.get("label"))
    if rid and rid in by_id:
        return by_id[rid]
    if rname and rname in by_name:
        return by_name[rname]
    if rlabel and rlabel in by_label:
        return by_label[rlabel]
    return None


def diff(plan: dict, form_fields_after) -> list[dict]:
    """Compare a plan to the form read back after filling: a list of ``{label, target, expected, actual}``.

    Every planned row (except ``skip``) whose read-back value differs — normalized per field type
    — is a mismatch. Each row is matched to its OWN read-back field by identity (id, then name, then
    label), so two fields sharing a label are compared independently and ``target`` names which one.
    A required field that is still empty is reported too (so an unmapped-but-required field the
    operator never filled is caught); the required-empty pass uses the same identity, so a field a
    row already covered is not double-reported. An empty list means every planned field landed and
    nothing required is blank."""
    rows = plan.get("rows", []) if isinstance(plan, dict) else []
    by_id, by_name, by_label = _indexes(form_fields_after)
    mismatches: list[dict] = []
    covered: set[int] = set()  # id() of each matched read-back field, so a covered field isn't re-reported

    for row in rows:
        if not isinstance(row, dict) or row.get("action") == SKIP:
            continue
        label = row.get("label", "")
        target = row.get("target") or label
        field = _match_field(row, by_id, by_name, by_label)
        if field is not None:
            covered.add(id(field))
        actual = str((field or {}).get("value") or "") if field else ""
        expected = str(row.get("value") or "")
        if not _values_equal(expected, actual, row):
            mismatches.append({"label": label, "target": target, "expected": expected, "actual": actual})

    for field in form_fields_after if isinstance(form_fields_after, list) else []:
        if not isinstance(field, dict) or not _is_required(field):
            continue
        if id(field) in covered:
            continue  # already flagged as a planned-row mismatch above
        label = str(field.get("label") or field.get("name") or field.get("id") or "").strip()
        raw_id = str(field.get("id") or "").strip()
        target = f"#{raw_id}" if raw_id else label
        value = str(field.get("value") or "")
        if value.strip():
            continue
        mismatches.append({"label": label, "target": target, "expected": "(required — not yet filled)", "actual": ""})

    return mismatches


# ── session storage: fill_sessions.json ───────────────────────────────────────────────────
def _path():
    return _store._dir() / "fill_sessions.json"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _now_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows_hash(rows: list[dict]) -> str:
    """A stable short hash of the plan rows — the fingerprint ``record_verification`` stores so a
    diff is tied to the plan it verified. (The SESSION ID is derived separately, by ``_session_id``,
    which also folds in the application's identity — company/role and the posting URL.)"""
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _session_id(rows: list[dict], company: str = "", role: str = "", posting: str = "") -> str:
    """The session id for a plan: a short hash of the plan rows AND the application's identity — the
    operator-facing company + role and, when known, the POSTING itself (its URL / board+job id),
    all normalized. Folding the identity in keeps two different applications on distinct ids in the
    common case — a different company, role, or posting each yields a different id even when the
    fields and answers are byte-for-byte identical — which is handy for labelling and lets a re-plan
    of the SAME application (same company, role, posting and rows) keep its id, so ``_save_session``
    resets exactly that one session's verification on a re-plan.

    But this is NOT where submit safety lives, and ids CAN still collide — most plainly when
    ``careercoach_plan_fill`` is called with blank company/role and no posting for two forms that
    read back identically. The guarantee that a grant approved for form A can't authorize a submit on
    a colliding form B is carried by the per-read-back ``verification_id`` (``record_verification`` /
    ``current_verification``), not by this id being unique: even on a collision, B's clean read-back
    mints a brand-new verification_id that A's grant does not hold (bd-4t17 / bd-ywmm.4)."""
    payload = json.dumps(
        {"rows": rows, "company": _norm(company), "role": _norm(role), "posting": _norm(posting)},
        sort_keys=True,
        ensure_ascii=False,
    )
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


def _clear_other_verifications(sessions: dict, keep: str) -> None:
    """Mark every stored session EXCEPT ``keep`` unverified (caller holds the ``fill_sessions``
    lock). Verification is EXCLUSIVE to the newest plan/read-back — at most one session is verified
    at a time — so writing or verifying one session strips the ``verified`` flag off all the others
    in the SAME atomic write. ``verified`` / ``verified_at`` drop and ``verification_id`` is cleared
    to "" (so no stale verification event can match a grant); each session's ``last_diff`` is left as
    is (its last read-back result stands, it just no longer authorizes a submit)."""
    for sid, rec in sessions.items():
        if sid == keep or not isinstance(rec, dict):
            continue
        rec["verified"] = False
        rec["verified_at"] = ""
        rec["verification_id"] = ""


def _save_session(session_id: str, record: dict) -> None:
    """Persist one planning session. A (re)built plan ALWAYS starts UNVERIFIED: ``verified``
    describes one fill-then-read-back cycle, so writing a plan — even an identical one whose rows
    hash to the same id — cannot carry a prior ``verified`` flag forward. It also clears the
    ``verification_id`` to "", retiring whatever verification event a prior clean read-back had
    stamped (see ``record_verification``): a submit grant binds to that exact id, so a plan write
    makes any grant for this session inert even before a new read-back happens. The browser must be
    filled and read back (``verify_fill``) again before the plan counts as verified; this is what
    stops a re-plan from arriving already-verified and being submitted without a read-back. Only
    ``created`` survives a rewrite.

    Verification is EXCLUSIVE to the newest plan: writing session S also clears ``verified`` /
    ``verification_id`` on every OTHER stored session in the same atomic write, so at most one session
    is ever verified — always the latest one planned or read back.

    These two facts are what close the bd-4t17 hole, and neither depends on session ids being unique
    per application. A submit grant is bound to the ``(session_id, verification_id)`` pair current
    when the operator approved. Planning any form — including a form B that COLLIDES onto A's session
    id — writes a plan here, which clears that session's ``verification_id`` (and, until B is read
    back, leaves it ``""``). A later clean read-back of B mints a BRAND-NEW ``verification_id``
    (``secrets.token_hex``), never the one A's grant holds. So an id collision can no longer authorize
    a submit: the grant's verification_id simply won't match the session's current one."""
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
            "verification_id": "",  # a plan write retires any prior verification event / grant
        }
        _clear_other_verifications(sessions, session_id)  # at most one verified session, ever
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
    only when the diff is empty. Returns the updated record (an empty one if the session is gone).

    An EMPTY diff stamps a fresh random ``verification_id`` (``secrets.token_hex(16)``) on the record
    together with ``verified=True``: that token identifies this ONE read-back event, and a submit
    grant binds to it. Two clean read-backs of the SAME session therefore get two DIFFERENT
    verification_ids, so re-verifying a session after a grant was approved makes that grant inert.
    A NON-EMPTY diff clears ``verification_id`` to "" along with ``verified`` — a failed read-back
    authorizes nothing.

    Verification is EXCLUSIVE to the newest read-back: an empty diff marks THIS session verified and
    clears ``verified`` / ``verification_id`` on every OTHER stored session in the same atomic write,
    so at most one session is verified at a time. A non-empty diff leaves the others alone — it only
    unverifies this one. Reading back a form B can never revive form A's grant: whether B is a
    distinct session or one that collides onto A's id, B's clean read-back mints its OWN fresh
    verification_id, which is not the one A's grant holds."""
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
        rec["verification_id"] = secrets.token_hex(16) if verified else ""
        if verified:
            _clear_other_verifications(sessions, session_id)  # at most one verified session, ever
        _save(sessions)
        return rec


def invalidate_verification(session_id: str) -> None:
    """Drop a session's ``verified`` flag (and its stored diff) WITHOUT rewriting the plan, so it
    reads as unverified until it is filled and read back again. Used after a visible-browser handoff:
    a captcha / login / attestation step can re-render the form and clear filled fields, so the
    verification made BEFORE the handoff no longer describes the live form and must not authorize a
    submit. The ``verification_id`` is cleared too, retiring the verification EVENT a submit grant
    binds to (``current_verification``) — so the handoff revokes any live grant for this session at
    its source, not only through the coarse ``verified`` flag. No-op if the session is gone."""
    with _store._locked("fill_sessions"):
        sessions = _load_strict()
        rec = sessions.get(session_id)
        if rec is None:
            return
        rec["verified"] = False
        rec["verified_at"] = ""
        rec["verification_id"] = ""
        rec["last_diff"] = None
        _save(sessions)


def is_verified(session_id: str) -> bool:
    """Whether the session's latest diff was empty (``False`` if there's no session or no diff).

    Verification is EXCLUSIVE: at most one session is verified at a time — always the latest one
    planned or read back (see ``_save_session`` / ``record_verification``). So planning or verifying
    ANY other form flips this session back to ``False``. This is a coarse signal; the submit gate does
    NOT rely on it alone, because two forms can collide on one session id. The gate binds to the
    session's ``verification_id`` (``current_verification``) instead, so an id collision cannot keep a
    stale grant alive."""
    rec = load_session(session_id)
    return bool(rec and rec.get("verified"))


def current_verification(session_id: str) -> str:
    """The ``verification_id`` of the session's latest clean read-back, or "" when the session is not
    currently verified (no session, an empty read-back never happened, or a later plan/dirty read-back
    retired it).

    This is the identity a submit grant is bound to. ``record_verification`` mints a fresh random
    token on every clean read-back and ``_save_session`` / a dirty diff clear it, so the value names
    exactly ONE verification event. ``careercoach_request_submit`` reads it before the approval
    interrupt and grants with that exact token; the submit-gate middleware allows a submit only while
    this still returns that same non-empty token. A re-plan or a re-verification (including one from a
    form B that collides onto this session id) changes or clears it, so the old grant stops matching —
    the guarantee the bd-4t17 hole needed, with no dependence on session ids being unique."""
    rec = load_session(session_id)
    if not (rec and rec.get("verified")):
        return ""
    return str(rec.get("verification_id") or "")
