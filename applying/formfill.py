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
  empty, shows up here. So does any value OUTSIDE the plan — a field the agent filled on its own, a
  box it ticked — unless the page itself showed it when the application was first planned (the
  ``baseline``). A session is ``verified`` only once its latest diff is empty, and the read-back it
  verified is kept for the approval card, which shows the whole form, not just the planned rows.
* **legal / eligibility / attestation questions** (``is_sensitive``) are filled only from a confirmed
  answer on exactly that topic or from the operator's own extra answer — never inferred.

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

from ..record import answers as _answers
from ..record import profile as _store

StoreUnreadable = _store.StoreUnreadable
log = _store.log

# How a field's action is carried out downstream. ``fill`` → browser_fill a text/email/tel/number
# input; ``select`` → browser_select a native select, combobox or radio-group; ``upload`` →
# browser_upload a file; ``skip`` → an optional field with no answer, left blank on purpose.
FILL, SELECT, UPLOAD, SKIP = "fill", "select", "upload", "skip"
# ``check`` → browser_click each checkbox the row lists (``checks``) so it's ticked, and leave every
# other member of the group unticked. A checkbox carries a ticked state, not text, so it is never a
# ``fill``: typing "they/them" into a "They/them" checkbox is meaningless, and a "LinkedIn" option of a
# "how did you hear about us?" group must never receive the LinkedIn URL.
CHECK = "check"

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


_PLAIN_CSS_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


# ── the core browser's untrusted-content boundaries ─────────────────────────────────────────
# protoAgent's agent_browser wraps page-derived output (browser_form_read included) in nonce markers so
# the model can tell page data from instructions:
#
#     --- AGENT_BROWSER_PAGE_CONTENT nonce=<hex> origin=<page URL> ---
#     [ {"label": "Email", ...} ]
#     --- END_AGENT_BROWSER_PAGE_CONTENT nonce=<same hex> ---
#
# The model pastes that output into careercoach_plan_fill / verify_fill as-is, so the parser has to
# take the markers off. Vendored (a few lines) rather than imported from the browser plugin: this
# plugin imports no other plugin's modules, and it has to parse the same JSON on cores without them.
_BOUNDARY_OPEN = re.compile(r"^--- AGENT_BROWSER_PAGE_CONTENT nonce=([0-9A-Za-z]+) origin=.* ---$")
_BOUNDARY_CLOSE = "--- END_AGENT_BROWSER_PAGE_CONTENT nonce={nonce} ---"


def strip_page_boundaries(text: object) -> str:
    """The text inside agent_browser's content-boundary markers, or ``text`` unchanged when it isn't
    wrapped. The opening marker must be the first non-blank line; its closing line (same nonce) is
    dropped when it's the last one. A cut-off block (no closing line) loses only the opening line,
    so a truncated array still fails to parse with a readable error instead of passing silently."""
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    lines = raw.strip().split("\n")
    m = _BOUNDARY_OPEN.match(lines[0].strip())
    if not m:
        return raw
    body = lines[1:]
    if body and body[-1].strip() == _BOUNDARY_CLOSE.format(nonce=m.group(1)):
        body = body[:-1]
    return "\n".join(body)


def id_selector(field_id: str) -> str:
    """A CSS selector that matches the element whose id is exactly ``field_id``.

    A plain id (``email``, ``first_name``) is ``#email``. Anything else (``input-candidate.name-3``
    from Recruitee, ``:r1:`` from React, Workday's ``a/b`` or ``x[0]``, a leading digit, a space)
    would be MISREAD as ``#id``: ``#input-candidate.name-3`` means id ``input-candidate`` plus class
    ``name-3``, and the fill fails with "Element not found". Those use the attribute form
    ``[id="..."]``, with ``\\`` and ``"`` escaped (and a control character as a CSS hex escape)."""
    if _PLAIN_CSS_ID.fullmatch(field_id):
        return f"#{field_id}"
    out = []
    for ch in field_id:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\{ord(ch):x} ")
        else:
            out.append(ch)
    return '[id="' + "".join(out) + '"]'


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


# A field that asks about ANOTHER person — the employee who referred you, a reference, a manager,
# an emergency contact — must never receive the candidate's OWN email / phone / LinkedIn / URL /
# location. Seen live (2026-10-16, Greenhouse): the candidate's own email was planned into "…full
# name and work email of the LaunchDarkly employee who referred you." The third-party noun must OWN
# the contact noun — a mere mention elsewhere in the label is helper text about the CANDIDATE's own
# field ("Phone (for recruiter contact)", "LinkedIn (shared with the hiring manager)"). Ownership is:
#   * a third-party noun followed within two words by a contact noun ("Previous manager phone",
#     "Partner's email", "Ref 1 email", "Referring employee email"), or
#   * "<contact noun> … of the/your/a … <third-party noun>" ("work email of the LaunchDarkly
#     employee"), or "who referred" / "employee who" anywhere.
# All matching is word-bounded ("Preferred name" / "preferences" are not "reference"). Only the
# candidate's personal free-text keys are guarded (``_PERSONAL_KEYS``): a yes/no or EEO question
# ("Willing to relocate? (referral bonus applies)") is never about someone else's details.
_TP_NOUN = (
    r"(?:referr\w*|referees?|refs?\s?#?\d+|references?(?:\s?#?\d+)?|(?:hiring\s)?managers?|supervisors?"
    r"|recruiters?|emergency(?:\scontacts?)?|next\sof\skin|kin|ice(?:\scontacts?)?|spouses?|partners?"
    r"|parents?|guardians?|mentors?|professors?|employees?|interviewers?|contact\spersons?"
    r"|point[\s-]of[\s-]contact|employers?|schools?|advisors?|friends?|colleagues?|landlords?"
    r"|his|her|their)(?:['’]s?)?"
)
_CONTACT_NOUN = (
    r"(?:e-?\s?mails?|(?:tele)?phones?|mobile|cell|numbers?|names?|linked\s?in|locations?|city"
    r"|address|website|portfolio|url|profiles?)"
)
_THIRD_PARTY = re.compile(
    rf"\b{_TP_NOUN}(?:\W+[\w'’]+){{0,2}}?\W+{_CONTACT_NOUN}\b"
    rf"|\b{_CONTACT_NOUN}(?:\W+\w+)?\W+of\s+(?:your|the|a|an|my|our|their|this|that)\s+(?:[\w'’-]+\s+){{0,2}}?{_TP_NOUN}\b"
    r"|\bwho\s+referred\b|\bemployee\s+who\b"
)
_PERSONAL_KEYS = frozenset(
    {"email", "phone_number", "phone_country", "linkedin_url", "portfolio_url", "current_location"}
    | {"full_name", "first_name", "last_name"}
)
# A field whose name/id sits under a work-history / education section (``workExperience-3--location``,
# ``education_0_phone``) describes that employer or school, never the candidate's own location/phone.
_SECTION_PREFIX = re.compile(r"^(?:work experience|education|employment)\b")
_SECTION_KEYS = frozenset({"current_location", "phone_number"})
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_ID_SEPARATORS = re.compile(r"[_\-.\[\]/]+")


def _split_ident(text: object) -> str:
    """A field ``name`` / ``id`` as words: camelCase and letter/digit runs split BEFORE lowercasing
    (``hiringManagerEmail`` → "hiring manager email", ``reference1Email`` → "reference 1 email"),
    and ``_ - . [ ] /`` folded to spaces."""
    raw = str(text if text is not None else "")
    return _norm(_ID_SEPARATORS.sub(" ", _CAMEL.sub(" ", raw)))


def _is_third_party(field: dict) -> bool:
    """True when a field's label, name or id asks for SOMEONE ELSE's contact details — each source
    checked on its own, so a pattern never spans the label into the id."""
    texts = [_norm(field.get("label"))] + [_split_ident(field.get(k)) for k in ("name", "id")]
    return any(t and _THIRD_PARTY.search(t) for t in texts)


def _in_history_section(field: dict) -> bool:
    """True when a field's name or id puts it under a work-experience / education / employment entry."""
    return any(_SECTION_PREFIX.search(_split_ident(field.get(k))) for k in ("name", "id"))


# ── name keys ─────────────────────────────────────────────────────────────────────────────
# Matched on the WHOLE cleaned label (or a field's name/id split into words), never as a substring:
# "Company name", "Name of your referrer", "Preferred first name" and "What's the name you'd prefer us
# to use?" are not the candidate's legal name fields.
_FIRST_NAME = re.compile(r"^(?:legal\s+)?(?:first|given)\s*name$|^forename$|^fname$")
_LAST_NAME = re.compile(r"^(?:legal\s+)?(?:last|family)\s*name$|^surname$|^lname$")
_FULL_NAME = re.compile(
    r"^(?:your\s+)?(?:full\s*)?(?:legal\s+)?name$|^full\s+legal\s+name$|^(?:candidate|applicant)s?\s+(?:full\s+)?name$"
)
_LABEL_NOISE = re.compile(r"[*✱:]+|\(required\)|\(optional\)")


def _clean_label(text: object) -> str:
    """A label without the required/optional markers forms decorate it with ("Full name✱", "Name*")."""
    return _norm(_LABEL_NOISE.sub(" ", str(text if text is not None else "")))


def _name_key(field: dict) -> str | None:
    """``full_name`` / ``first_name`` / ``last_name`` for a field that asks for the candidate's own
    name, judged on the label first and then the name/id (``first_name``, ``_systemfield_name``)."""
    texts = [_clean_label(field.get("label"))]
    texts += [re.sub(r"^systemfield\s+", "", _split_ident(field.get(k))) for k in ("name", "id")]
    for text in texts:
        if not text:
            continue
        if _FIRST_NAME.match(text):
            return "first_name"
        if _LAST_NAME.match(text):
            return "last_name"
        if _FULL_NAME.match(text):
            return "full_name"
    return None


# ── legal, eligibility and attestation questions ──────────────────────────────────────────
# A question that makes the candidate attest to something, or asks about their legal eligibility —
# work authorization, citizenship and export control, age, a criminal record, a binding agreement,
# a consent — is answered ONLY by the operator: from a confirmed standard answer on exactly that
# topic, or in their own words (an extra answer, or a handoff). It is never inferred from the profile.
# Seen live (2026-11 eval): an "over 18" Yes clicked unasked, a passport country inferred from
# "US citizen", "everything is true and your own: YES" answered by the agent, an export-control answer
# inferred, and "EU authorized: NO" after an ambiguous "continue".
_SENSITIVE = re.compile(
    r"attest|certif(?:y|ies|ication that)|\bi\s+(?:hereby\s+)?(?:confirm|declare|acknowledge|agree|consent|understand)"
    r"|acknowledg|declar(?:e|ation)|true\s*(?:,|and|&)\s*(?:complete|correct|accurate)|accurate\s+and\s+complete"
    r"|(?:is|are)\s+(?:all\s+)?(?:true|my\s+own|your\s+own)|\bconsent|\bagree\b|privacy\s+(?:policy|notice)"
    r"|terms\s+(?:and|&)\s+conditions|export[\s-]*control|\bitar\b|\bear\b|deemed\s+export|u\.?s\.?\s+person"
    r"|citizen|nationalit|passport|\bvisa\b|sponsor|authori[sz]ed\s+to\s+work|work\s+authori[sz]ation|right\s+to\s+work"
    r"|eligib|legally|over\s+(?:the\s+age\s+of\s+)?(?:18|eighteen|21)|\b(?:18|21)\s+years|years\s+of\s+age|age\s+of\s+(?:18|21)"
    r"|minimum\s+age|criminal|convict|felon|background\s+check|drug\s+(?:test|screen)|security\s+clearance"
    r"|non[\s-]*compete|non[\s-]*solicit|employment\s+agreement|restrictive\s+covenant|post[\s-]*employment\s+restriction"
)
# The standard keys that ARE the operator's confirmed answer to a legal-eligibility question, so a
# confirmed value may fill one. Any other standard key reaching a sensitive field is dropped.
_SENSITIVE_OK_KEYS = frozenset({"authorized_us", "requires_sponsorship"})
SENSITIVE_REASON = (
    "a legal, eligibility or attestation question — only the operator's own answer goes here (ask them, "
    'or careercoach_handoff reason="attestation"); never infer it'
)


def is_sensitive(field: dict) -> bool:
    """Whether a field asks a legal / eligibility / attestation question (``_SENSITIVE``), judged on
    its visible label — name/id are usually opaque (``QA_12487827``, a UUID) and say nothing."""
    return isinstance(field, dict) and _SENSITIVE.search(_norm(field.get("label"))) is not None


# A work-authorization question about somewhere other than the US is not answered by the confirmed
# ``authorized_us`` ("Are you authorized to work in the EU?"), so it never classifies to it.
_NON_US = re.compile(
    r"\b(?:eu|eea|european|europe|schengen|uk|united kingdom|britain|england|canada|canadian|australia|germany"
    r"|france|netherlands|ireland|india|greece|spain|portugal|italy|sweden|switzerland|singapore|japan|israel"
    r"|mexico|brazil|poland|denmark|norway|finland|austria|belgium)\b"
)


# ── checkboxes, placeholders, machine fields ──────────────────────────────────────────────
def _is_checkbox(field: dict) -> bool:
    return _norm(field.get("kind")) == "checkbox"


def _is_ticked(field: dict) -> bool:
    """A checkbox's live state. The core reader sends ``value`` as the checked BOOLEAN; a ``checked``
    key, when present, wins."""
    state = field.get("checked", field.get("value"))
    return state is True or _norm(state) in {"true", "checked"}


_PLACEHOLDER = re.compile(
    r"^(?:please\s+)?(?:select|choose|pick)(?:\s+(?:one|an?\s+option|an?\s+answer|\w+))?\s*(?:\.\.\.|…)?$"
    r"|^-+[^-]*-+$|^start\s+typing(?:\.\.\.|…)?$"
)


def _is_placeholder(value: object) -> bool:
    """Whether a read-back value is a widget's placeholder ("Select...", "-- Choose --"), not an answer."""
    v = _norm(value)
    return bool(v) and len(v) <= 32 and _PLACEHOLDER.match(v) is not None


# Fields the page itself fills (bot checks, request tokens): never an answer, never "unplanned".
_MACHINE_FIELD = re.compile(r"recaptcha|hcaptcha|h-captcha|turnstile|captcha|csrf|xsrf|authenticity[_-]?token")


def _is_machine_field(field: dict) -> bool:
    return any(_MACHINE_FIELD.search(_norm(field.get(k))) for k in ("label", "name", "id"))


def classify(field: dict) -> str | None:
    """Map one ``browser_form_read`` field to a ``answers.STANDARD_KEYS`` key, or ``None``.

    Normalized label/name/id matching against a per-key pattern table, plus a context-dependent
    phone rule (country-in-a-phone-widget → ``phone_country``, phone/mobile/tel → ``phone_number``).
    The phone match is folded into the SAME candidate set as the table, so a field that reads as
    both a phone input AND another key is ambiguous and returns ``None``. A field that matches no
    key, or two different keys, returns ``None`` — it never guesses between two keys, and an
    ambiguous field is one for the operator to resolve.

    A personal-key field (email / phone / LinkedIn / URL / location) that asks for ANOTHER person's
    details (``_is_third_party`` — a referrer, a reference, a manager, an emergency contact) returns
    ``None``: the candidate's own answers never describe someone else, so it is left for the
    operator."""
    if not isinstance(field, dict):
        return None
    hay = _hay(field)
    if not hay:
        return None

    if _clean_label(field.get("label")) == "search" and any(m in hay for m in _PHONE_MARKERS):
        return None  # intl-tel-input's country SEARCH box, not the phone number
    matched = {key for key, pat in _KEY_PATTERNS if pat.search(hay)}
    phone_key = _phone_key(hay)
    if phone_key is not None:
        matched.add(phone_key)
    name_key = _name_key(field)
    if name_key is not None:
        matched.add(name_key)

    if len(matched) != 1:
        return None  # no match, or ambiguous between keys → never guess
    key = next(iter(matched))
    if key == "current_location" and re.search(
        r"^(?:current\s+)?(?:state|province|region|country)\b",
        _norm(field.get("label")) or _split_ident(field.get("name") or field.get("id")),
    ):
        return None  # split state/country inputs must not receive the full location
    if key in _PERSONAL_KEYS and _is_third_party(field):
        return None  # someone else's email / phone / profile — the candidate's answer never fits
    if key in _SECTION_KEYS and _in_history_section(field):
        return None  # an employer's / school's location or phone, not the candidate's
    if key in {"authorized_us", "requires_sponsorship"} and _NON_US.search(_norm(field.get("label"))):
        return None  # authorization somewhere else is not the confirmed US answer
    if _is_checkbox(field) and key not in _CHOICE_KEYS:
        return None  # a checkbox takes a ticked state: "LinkedIn" (how did you hear?) is not the URL
    return key


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


def _is_hidden(field: dict) -> bool:
    return (
        field.get("visible") is False
        or field.get("hidden") is True
        or _norm(field.get("type")) == "hidden"
        or _norm(field.get("kind")) == "hidden"
    )


def _readback_value(field: dict, kind: str = "") -> str:
    """The value a read-back proves is set.

    A single radio's ``value`` attribute is not evidence that the option is checked, so it
    needs a live ``checked``/``aria-checked``. A ``radio-group`` is different: the host's
    form reader (``collapse_radio_groups``) collapses a native group into one field whose
    ``value`` IS the checked option (empty when nothing is checked) and sends no ``checked``
    key, so its value counts as proof. A ``checked`` key, when present, always wins."""
    field_kind = _norm(kind or field.get("kind"))
    if field_kind == "checkbox" or (not kind and _is_checkbox(field)):
        return "true" if _is_ticked(field) else ""  # never "False": an unticked box is empty
    if field_kind in {"radio", "radio-group"}:
        has_checked = "checked" in field or "aria-checked" in field
        if has_checked or field_kind == "radio":
            checked = field.get("checked", field.get("aria-checked"))
            if checked is not True and checked != "true":
                return ""
    value = field.get("value")
    if isinstance(value, (list, tuple)):  # a core checkbox-group: the ticked option labels
        return ", ".join(str(v) for v in value if str(v or "").strip())
    return str(value or "")


def _ticked_options(field: dict | None) -> list[str]:
    """The ticked option labels of a core ``checkbox-group`` read-back (``value`` is a list)."""
    value = (field or {}).get("value")
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v or "").strip()]
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _is_group_field(field: dict) -> bool:
    """A whole checkbox question read as ONE field (protoAgent #4951 and later): ``{"kind":
    "checkbox-group", "label": <question>, "options": [...], "value": [<ticked>, ...]}``. Older cores
    read each box on its own (``kind: "checkbox"``), handled by ``_checkbox_groups``."""
    return _norm(field.get("kind")) == "checkbox-group"


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


# ── phone country by position (Greenhouse) ────────────────────────────────────────────────
# Greenhouse renders the phone's country picker as a plain "Country" combobox (id ``country``) right
# before the intl-tel "Search" box and the "Phone" input. Read alone it says nothing about phones, so
# it's recognised by position: a bare "Country" picker whose next field (past the intl-tel search box)
# is the phone number is the phone country.
_BARE_COUNTRY = re.compile(r"^(?:phone\s+)?country(?:\s+code)?$")


def _is_bare_country(field: dict) -> bool:
    texts = [_clean_label(field.get("label"))] + [_split_ident(field.get(k)) for k in ("name", "id")]
    texts = [t for t in texts if t]
    return bool(texts) and all(_BARE_COUNTRY.match(t) for t in texts)


def _is_iti_search(field: dict) -> bool:
    hay = _hay(field)
    return any(m in hay for m in _PHONE_MARKERS) or _clean_label(field.get("label")) == "search"


def _phone_country_positions(fields: list[dict]) -> set[int]:
    """Indexes of bare "Country" pickers that sit immediately before the phone number field."""
    out: set[int] = set()
    for i, f in enumerate(fields):
        if not _is_bare_country(f) or classify(f) is not None:
            continue
        j = i + 1
        while j < len(fields) and j <= i + 2 and _is_iti_search(fields[j]):
            j += 1
        if j < len(fields) and classify(fields[j]) == "phone_number":
            out.add(i)
    return out


# ── checkbox groups ───────────────────────────────────────────────────────────────────────
# browser_form_read lists each checkbox on its own, labelled with its OPTION text ("LinkedIn",
# "They/them"). Members of one question share a ``name`` (Lever: ``pronouns``, ``cards[…][field3]``)
# or an id stem (Ashby: ``<uuid>-labeled-checkbox-0`` … ``-6``). Each group is planned as ONE ``check``
# row: the answer names the option(s) to tick, every other member stays unticked.
_ID_STEM = re.compile(r"^(.*)[-_]\d+$")
_TRUTHY = frozenset({"yes", "y", "true", "checked", "check", "ticked", "tick", "on", "x", "1", "agree", "i agree"})
_FALSY = frozenset({"no", "n", "false", "unchecked", "unticked", "off", "0", "leave unchecked"})


def _checkbox_groups(fields: list[dict]) -> dict[int, tuple[str, list[int]]]:
    """``{index of first member: (group key, member indexes)}`` for checkbox groups of 2+ members;
    every member index maps to its group too (the non-first members are skipped by the caller)."""
    by_name: dict[str, list[int]] = {}
    by_stem: dict[str, list[int]] = {}
    for i, f in enumerate(fields):
        if not _is_checkbox(f):
            continue
        name = str(f.get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(i)
        m = _ID_STEM.match(str(f.get("id") or "").strip())
        if m and m.group(1):
            by_stem.setdefault(m.group(1), []).append(i)
    groups: dict[int, tuple[str, list[int]]] = {}
    for key, members in list(by_name.items()) + list(by_stem.items()):
        if len(members) < 2 or any(i in groups for i in members):
            continue
        for i in members:
            groups[i] = (key, members)
    return groups


def _member_ident(field: dict) -> dict:
    ident = {"label": str(field.get("label") or "").strip()}
    for k in ("id", "name"):
        v = str(field.get(k) or "").strip()
        if v:
            ident[k] = v
    ident["target"] = id_selector(ident["id"]) if ident.get("id") else ident["label"]
    return ident


def _pick_checkboxes(value: str, labels: list[str]) -> list[int] | None:
    """The member indexes an answer ticks: the whole answer as one option first (labels can hold
    commas), else each ``,``/``;``/``|``-separated part as one option. ``None`` when any part matches
    no option — never a guess."""
    whole = _match_option(value, labels)
    if whole is not None:
        return [labels.index(whole)]
    parts = [p.strip() for p in re.split(r"[;|,]", value) if p.strip()]
    picked: list[int] = []
    for part in parts:
        hit = _match_option(part, labels)
        if hit is None:
            return None
        if labels.index(hit) not in picked:
            picked.append(labels.index(hit))
    return picked or None


# ── location autocomplete (G) ─────────────────────────────────────────────────────────────
AUTOCOMPLETE_NOTE = (
    "location autocomplete: browser_select(field, option_text) types it, retries with the city, and clicks "
    'the one matching suggestion — success reads "(autocomplete; the field reads …)". On "matches more than '
    'one suggestion" pass the fuller place; on "matches no suggestion" / "no suggestions appeared", or on an '
    "older core whose browser_select can't search, browser_type the city alone into the field, then "
    "browser_click the suggestion that names this place. Never press Enter to pick one."
)

# ``option_text`` alternatives for browser_select (``A|B|C``, first match wins — protoAgent #4951):
# the other wordings forms use for the same answer, offered where the plan couldn't see the options.
_DECLINE_WORDINGS = (
    "Decline to self-identify",
    "Decline To Self Identify",
    "I don't wish to answer",
    "Prefer not to say",
    "Prefer not to disclose",
    "Decline to answer",
)
_CHOICE_WORDINGS = (
    ("United States", "United States of America"),
    ("United Kingdom", "United Kingdom of Great Britain and Northern Ireland"),
    ("they/them", "they/them/theirs"),
    ("he/him", "he/him/his"),
    ("she/her", "she/her/hers"),
)


def option_alternatives(value: str) -> str:
    """``value`` plus the other wordings of the same answer, as browser_select ``A|B|C`` option text
    (the stored wording first). Just ``value`` when it has none."""
    nv = _norm_option(value)
    alts: list[str] = [value]
    if nv in _DECLINE_FORMS:
        alts += list(_DECLINE_WORDINGS)
    for group in _CHOICE_WORDINGS:
        if nv in {_norm_option(g) for g in group}:
            alts += list(group)
    out: list[str] = []
    for a in alts:
        if "|" not in a and _norm_option(a) not in {_norm_option(o) for o in out}:
            out.append(a)
    return "|".join(out)


# ── the page's own values, before anything was typed ──────────────────────────────────────
def _ident_key(field: dict) -> str:
    """One field's identity across reads: its id, else its name plus label (checkbox members share a
    name), else its label."""
    fid = _norm(field.get("id"))
    if fid:
        return "id:" + fid
    fname, flabel = _norm(field.get("name")), _norm(field.get("label"))
    if fname:
        return f"name:{fname}|{flabel}"
    return "label:" + flabel if flabel else ""


def _present_value(field: dict) -> str:
    """What a read-back field actually holds, as text: "true" for a ticked checkbox, "" for an
    unticked one or a placeholder ("Select..."), else its read-back value."""
    if _is_checkbox(field):
        return "true" if _is_ticked(field) else ""
    value = _readback_value(field)
    return "" if _is_placeholder(value) else value.strip()


def _snapshot(fields: list[dict]) -> dict[str, str]:
    """``{identity: value}`` of every visible field at plan time — what the PAGE put there."""
    out: dict[str, str] = {}
    for f in fields:
        key = _ident_key(f)
        if key and not _is_machine_field(f):
            out.setdefault(key, _present_value(f))
    return out


def build_plan(
    form_fields,
    confirmed,
    extra=None,
    ats=None,
    resume_ready=True,
    company="",
    role="",
    posting="",
    live=True,
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

    fields = [
        f for f in (form_fields if isinstance(form_fields, list) else []) if isinstance(f, dict) and not _is_hidden(f)
    ]
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
    phone_country_at = _phone_country_positions(fields)
    groups = _checkbox_groups(fields)
    companions = _group_companions(fields, groups)
    forced: dict[int, dict] = {}  # index → the row a checkbox group decided for its companion field

    for idx, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        if idx in forced:
            rows.append(forced.pop(idx))
            continue
        if _is_checkbox(field):
            out = _plan_checkbox(field, idx, fields, groups, extra_norm, label_counts, confirmed, drafts)
            if out is None:
                continue  # a non-first member of a group already planned
            kind_out, item = out
            if idx in companions:
                kind_out, item = _settle_companions(kind_out, item, fields, companions[idx], forced)
            if kind_out == "row":
                rows.append(item)
            elif kind_out == "unconfirmed":
                if item["key"] not in seen_unconfirmed:
                    seen_unconfirmed.add(item["key"])
                    unconfirmed.append(item)
            else:
                unmapped.append(item)
            continue
        fid = str(field.get("id") or "").strip()
        fname = str(field.get("name") or "").strip()
        label = str(field.get("label") or field.get("name") or field.get("id") or "").strip()
        kind = str(field.get("kind") or "").strip()
        options = _options(field)
        action = _action_for(field, options)
        required = _is_required(field)
        key = classify(field)
        if key is None and idx in phone_country_at:
            key = "phone_country"
        sensitive = is_sensitive(field)

        # Identity carried on every row / unmapped entry so same-label fields stay distinguishable:
        # id and name when present, and ``target`` — the most specific locator
        # (``#id``, or ``[id="..."]`` for an id CSS would misread; else the label).
        ident = {"label": label, "kind": kind}
        if fid:
            ident["id"] = fid
        if fname:
            ident["name"] = fname
        ident["target"] = id_selector(fid) if fid else label

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
            location_label = _norm(field.get("label")) or _split_ident(fname or fid)
            if (
                key == "current_location"
                and re.search(r"^(?:current\s+)?city\b", location_label)
                and not re.search(r"\b(?:state|region|province|country|location)\b", location_label)
            ):
                # Confirmed locations use city, region, country; never infer missing parts.
                value = str(value or "").split(",", 1)[0].strip() or None
            if sensitive and key not in _SENSITIVE_OK_KEYS:
                # A legal / eligibility / attestation question is answered only by a confirmed answer
                # on exactly that topic, or by the operator in their own words — never by another key.
                value, source = None, None

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
                reason = SENSITIVE_REASON if sensitive else "no confirmed answer"
                unmapped.append({**ident, "key": key, "required": True, "reason": reason})
            else:
                rows.append({**ident, "key": key, "value": "", "action": SKIP})
            continue

        if _is_group_field(field):
            rows_or = _plan_group_field(field, idx, fields, ident, key, value, source, required, sensitive, forced)
            if rows_or[0] == "row":
                rows.append(rows_or[1])
            else:
                unmapped.append(rows_or[1])
            continue

        if action == SELECT:
            # A FREE-TEXT standard answer (an email, a URL, a city, a phone number) is not a choice —
            # it must never be planned onto a select/combobox (the confirmed location landing on a
            # Yes/No "do you live here?" question). Only an operator's EXPLICIT extra, or a
            # CHOICE-typed standard answer, belongs on a choice field; a free-text standard answer is
            # asked about (required) or left blank (optional), never typed.
            if key == "current_location" and not options and _norm(kind) == "combobox":
                # A location AUTOCOMPLETE (a combobox whose options load as you type): pick the
                # suggestion with browser_select; the read-back accepts the place however the widget
                # spells it ("Portland, OR, USA").
                rows.append(
                    {
                        **ident,
                        "key": key,
                        "value": value,
                        "action": SELECT,
                        "source": source,
                        "autocomplete": True,
                        "search": str(value).split(",", 1)[0].strip(),
                    }
                )
                continue
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
            alternatives = option_alternatives(str(value))
            if alternatives != value:
                row["option_text"] = alternatives
        if sensitive:
            row["sensitive"] = True
        rows.append(row)

    _order_phone(rows)
    if is_ashby:
        _order_resume_first(rows)
    session_id = _session_id(rows, company, role, posting)
    plan = {"session_id": session_id, "rows": rows, "unmapped": unmapped, "unconfirmed": unconfirmed}
    if is_ashby:
        plan["note"] = ASHBY_PLAN_NOTE
    snapshot = _snapshot(fields)
    plan["baseline"] = _save_session(
        session_id,
        {**plan, "rows_hash": _rows_hash(rows)},
        snapshot=snapshot,
        app=(company, role, posting) if live else None,
    )
    return plan


def _plan_group_field(field, idx, fields, ident, key, value, source, required, sensitive, forced):
    """Plan a core ``checkbox-group`` field: ``("row", row)`` or ``("unmapped", entry)``.

    The answer names the option(s) to tick (``_pick_checkboxes``); a free-text standard answer never
    lands on one. The row's ``ticks`` are the option labels; each is ticked with
    ``browser_select(field=<question>, option_text=<option>)``. A free-text companion right after the
    question ("Write here…", sharing its name or reading as a custom box) is skipped when an option
    matched, and takes the answer when none did — ticking the group's Custom/Other option if it has one."""
    options = _options(field)
    target = ident.get("label") or ident["target"]
    base = {**ident, "kind": "checkbox-group", "target": target, "group_field": True, "key": key}
    if source == "standard" and key in _FREE_TEXT_KEYS:
        if required:
            return "unmapped", {
                **base,
                "required": True,
                "reason": "standard answer is free text; this is a choice question",
            }
        return "row", {**base, "value": "", "action": SKIP}
    companion = box = None
    for j in range(idx + 1, min(len(fields), idx + 3)):
        f = fields[j]
        hay = " ".join(_norm(f.get(k)) for k in ("label", "id", "name"))
        if _is_checkbox(f) and box is None and _CUSTOM_HINT.search(hay):
            box = j  # a separate Custom/Other toggle between the question and its text box
            continue
        if _is_checkbox(f) or _is_group_field(f) or _norm(f.get("kind")) in _SELECT_KINDS:
            break
        name = str(f.get("name") or "").strip()
        if (name and name == str(field.get("name") or "").strip()) or _CUSTOM_HINT.search(hay):
            companion = j
        break
    if companion is None:
        box = None  # a toggle with no text box after it isn't this question's
    picked = _pick_checkboxes(str(value), options) if options else None
    if picked is not None:
        for j in (companion, box):
            if j is not None:
                forced[j] = {**_member_ident(fields[j]), "kind": str(fields[j].get("kind") or ""), "key": None,
                             "value": "", "action": SKIP}  # fmt: skip
        row = {**base, "value": value, "action": CHECK, "source": source, "ticks": [options[i] for i in picked]}
    elif companion is not None:
        custom = next((o for o in options if _CUSTOM_LABEL.match(_clean_label(o))), None)
        forced[companion] = {**_member_ident(fields[companion]), "kind": str(fields[companion].get("kind") or ""),
                             "key": key, "value": value, "action": FILL, "source": source}  # fmt: skip
        if box is not None:
            member = _member_ident(fields[box])
            forced[box] = {**member, "kind": "checkbox", "key": None, "value": "yes", "action": CHECK,
                           "source": source, "members": [member], "checks": [member]}  # fmt: skip
        row = {**base, "value": custom or "", "action": CHECK, "source": source, "ticks": [custom] if custom else []}
    else:
        return "unmapped", {**base, "required": required, "reason": "answer matches no option", "answer": value,
                            "options": options}  # fmt: skip
    if sensitive:
        row["sensitive"] = True
    return "row", row


# A checkbox group's free-text companion: the "Custom" / "Other" box and its "Write here…" input that
# sit just after the group (Lever's pronouns), or an "Other" member of the group itself. The group
# and its companion answer ONE question, so exactly one of them takes the answer (QA, PR #121).
_CUSTOM_LABEL = re.compile(r"^(?:custom|other|something else|self[\s-]?describe|prefer to self[\s-]?describe)\b")
_CUSTOM_HINT = re.compile(r"custom|\bother\b|write here|type your response|please specify|specify|self[\s-]?describe")


def _group_companions(fields: list[dict], groups: dict) -> dict[int, dict]:
    """``{first member index: {"text": idx|None, "box": idx|None, "member": member index|None}}`` for
    each checkbox group with a free-text companion: a non-checkbox input within three fields after
    the group's last member that shares the group's name or reads as a custom/other answer box, plus
    the single Custom/Other checkbox that toggles it (or the group's own Other member)."""
    out: dict[int, dict] = {}
    for first, (gkey, members) in groups.items():
        if first != members[0]:
            continue
        last = members[-1]
        text = box = None
        for j in range(last + 1, min(len(fields), last + 4)):
            f = fields[j]
            hay = " ".join(_norm(f.get(k)) for k in ("label", "id", "name"))
            if _is_checkbox(f):
                if box is None and j not in groups and _CUSTOM_HINT.search(hay):
                    box = j
                continue
            if text is None and (str(f.get("name") or "").strip() == gkey or _CUSTOM_HINT.search(hay)):
                text = j
            break
        member = next((i for i in members if _CUSTOM_LABEL.match(_clean_label(fields[i].get("label")))), None)
        if text is not None:
            out[first] = {"text": text, "box": box, "member": member}
    return out


def _settle_companions(kind_out, item, fields, comp, forced):
    """Decide a group's companion once the group is planned. A listed option matched → the companion
    text box and its Custom/Other toggle are skipped, so the answer isn't given twice. No listed option
    matched → the answer goes in the text box instead, ticking the Custom/Other toggle (or the group's
    Other member) that opens it; the group itself ticks nothing else."""
    text, box = comp["text"], comp["box"]

    def skip(j):
        return {**_member_ident(fields[j]), "kind": str(fields[j].get("kind") or ""), "key": None, "value": "",
                "action": SKIP}  # fmt: skip

    if kind_out == "row" and item.get("action") == CHECK and item.get("checks"):
        forced[text] = skip(text)
        if box is not None:
            forced[box] = skip(box)
        return kind_out, item
    if kind_out == "unmapped" and item.get("reason") == "answer matches no option" and item.get("answer"):
        answer, source = item["answer"], item.get("source")
        forced[text] = {**_member_ident(fields[text]), "kind": str(fields[text].get("kind") or ""),
                        "key": item.get("key"), "value": answer, "action": FILL, "source": source}  # fmt: skip
        group_row = {k: v for k, v in item.items() if k not in {"reason", "answer", "options", "required"}}
        group_row.update({"value": "", "action": CHECK, "checks": [], "source": source})
        if box is not None:
            forced[box] = {**_member_ident(fields[box]), "kind": "checkbox", "key": None, "value": "yes",
                           "action": CHECK, "source": source, "members": [_member_ident(fields[box])],
                           "checks": [_member_ident(fields[box])]}  # fmt: skip
        elif comp["member"] is not None:
            other = _member_ident(fields[comp["member"]])
            group_row.update({"value": other["label"], "checks": [other]})
        return "row", group_row
    return kind_out, item


def _plan_checkbox(field, idx, fields, groups, extra_norm, label_counts, confirmed, drafts):
    """Plan one checkbox, or one whole checkbox group at its first member: ``("row", row)``,
    ``("unmapped", entry)``, ``("unconfirmed", entry)``, or ``None`` for a later group member.

    A GROUP's answer comes from an extra keyed by the group's shared name or id stem (``group`` on
    the row says which), else from a member keyed by its own id or name with a yes/true value,
    else from a confirmed CHOICE answer the group's name classifies to (``pronouns``). The answer
    names the option(s) to tick. A SINGLE checkbox takes a yes/no answer (or its own label) from an
    extra or a confirmed choice key. Legal / attestation checkboxes ("I consent", "I certify…") are
    never ticked from anything but an extra the operator gave."""
    group = groups.get(idx)
    if group is not None and group[1][0] != idx:
        return None
    members = [fields[i] for i in group[1]] if group else [field]
    labels = [str(m.get("label") or "").strip() for m in members]
    required = any(_is_required(m) for m in members)
    sensitive = any(is_sensitive(m) for m in members)
    if group:
        gkey = group[0]
        ident = {"label": gkey, "kind": "checkbox-group", "target": gkey, "group": gkey}
        key = classify({"label": _split_ident(gkey), "name": gkey})
        key = key if key in _CHOICE_KEYS else None
    else:
        ident = {**_member_ident(field), "kind": "checkbox"}
        key = classify(field)
    ident["members"] = [_member_ident(m) for m in members]

    def unique_label(lab: str) -> bool:
        return bool(lab) and label_counts.get(lab, 0) <= 1

    value, source, picked = None, None, None
    lookups = [_norm(gkey)] if group else [_norm(field.get("id")), _norm(field.get("name"))]
    if not group and unique_label(_norm(field.get("label"))):
        lookups.append(_norm(field.get("label")))
    for k in lookups:
        if k and extra_norm.get(k, "").strip():
            value, source = extra_norm[k].strip(), "extra"
            break
    if value is None and group:
        # A member keyed on its own: {"<member id or name>": "yes"} ticks just that member.
        ticked = []
        for i, m in enumerate(members):
            for k in (_norm(m.get("id")), _norm(m.get("name"))):
                v = extra_norm.get(k, "").strip() if k and k != _norm(gkey) else ""
                if v and (_norm(v) in _TRUTHY or _norm_option(v) == _norm_option(labels[i])):
                    ticked.append(i)
                    break
        if ticked:
            picked, source = ticked, "extra"
            value = ", ".join(labels[i] for i in ticked)
    if value is None and key is not None and key in confirmed and not sensitive:
        value, source = confirmed[key], "standard"

    if value is None:
        if key is not None and key in drafts:
            return "unconfirmed", {"label": ident["label"], "key": key, "draft": drafts[key]}
        if required:
            reason = SENSITIVE_REASON if sensitive else "no confirmed answer"
            if group:
                reason += f" — key the answer by {gkey!r} (the option(s) to tick)"
            return "unmapped", {**ident, "key": key, "required": True, "reason": reason, "options": labels}
        return "row", {**ident, "key": key, "value": "", "action": SKIP}

    if picked is None:
        if not group:
            nv = _norm(value)
            if nv in _TRUTHY or _norm_option(value) == _norm_option(labels[0]):
                picked = [0]
            elif nv in _FALSY:
                picked = []
        else:
            picked = _pick_checkboxes(value, labels)
    if picked is None:
        return "unmapped", {
            **ident,
            "key": key,
            "required": required,
            "reason": "answer matches no option" if group else "a checkbox takes yes or no",
            "answer": value,
            "options": labels,
            "source": source,
        }
    row = {**ident, "key": key, "value": value, "action": CHECK, "source": source}
    row["checks"] = [ident["members"][i] for i in picked]
    if sensitive:
        row["sensitive"] = True
    return "row", row


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


def _values_equal(expected: str, actual: str, row: dict, options: list[str] | None = None) -> bool:
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
    if _norm(expected) == _norm(actual):
        return True
    if row.get("action") == SELECT:
        if _norm_option(expected) == _norm_option(actual):
            return True
        # browser_select commits a UNIQUE-PREFIX match when no option equals the answer. Where the plan
        # couldn't see the options (a combobox, an autocomplete) the committed option is accepted only
        # when that's provably the one the answer names: the read-back lists the options and exactly
        # one starts with the answer, or the two are known spellings of one choice ("United States" /
        # "United States of America", "they/them" / "they/them/theirs"). A bare prefix is not enough:
        # "United States" must never pass for "United States Minor Outlying Islands" (QA, PR #121).
        # A select whose options the plan matched exactly keeps exact matching.
        if (row.get("options_unknown") or row.get("autocomplete")) and _committed_option(expected, actual, options):
            return True
    if _is_location_row(row) and _location_equivalent(expected, actual):
        return True
    return False


# A Yes/No answer committed as a longer option keeps its meaning only when the rest of the option
# doesn't qualify it: "Yes, I am currently legally authorized…" is a Yes; "Yes, but I will need
# sponsorship" is not, and stays a mismatch for the operator.
_YES_QUALIFIER = re.compile(
    r"\b(?:not|no|never|but|however|unless|except|only|require|requires|need|needs|without)\b|n't"
)
_NO_QUALIFIER = re.compile(r"\b(?:but|however|unless|except|only|future|later|yet|currently)\b")


def _committed_prefix(expected: str, actual: str) -> bool:
    """Whether ``actual`` starts with the answer ``expected`` at a word boundary and the rest doesn't
    qualify a Yes/No ("they/them" → "they/them/theirs"). On its own this proves nothing about WHICH
    option was picked — ``_committed_option`` adds that."""
    e, a = _norm_option(expected), _norm_option(actual)
    if not e or not a.startswith(e) or len(a) == len(e) or a[len(e)].isalnum():
        return False
    rest = a[len(e) :]
    if e in {"yes", "y"}:
        return _YES_QUALIFIER.search(rest) is None
    if e in {"no", "n"}:
        return _NO_QUALIFIER.search(rest) is None
    return True


# Spellings of ONE choice that forms list under different wordings. Exact members only, never a
# prefix family: "United States" is "United States of America", not "United States Minor Outlying
# Islands".
_SAME_CHOICE = (
    {"united states", "united states of america", "usa", "us", "u s", "u s a"},
    {"united kingdom", "united kingdom of great britain and northern ireland", "uk", "u k", "great britain"},
    {"they/them", "they/them/theirs"},
    {"he/him", "he/him/his"},
    {"she/her", "she/her/hers"},
)


def _committed_option(expected: str, actual: str, options: list[str] | None) -> bool:
    """Whether ``actual`` is the option a pick of ``expected`` provably committed, on a select whose
    options the plan never saw. With the read-back's options: exactly one option starts with the
    answer, and it's ``actual``. Without them: the two are spellings of one choice (``_SAME_CHOICE``)."""
    e, a = _norm_option(expected), _norm_option(actual)
    if options:
        hits = [o for o in options if _committed_prefix(expected, o)]
        return len(hits) == 1 and _norm_option(hits[0]) == a and _committed_prefix(expected, actual)
    return any(e in group and a in group for group in _SAME_CHOICE)


# ── location equivalence: "Portland, OR, USA" is "Portland, Oregon, United States" ──────────
_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
    "ct": "connecticut", "de": "delaware", "dc": "district of columbia", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland", "ma": "massachusetts",
    "mi": "michigan", "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana",
    "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico",
    "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina", "sd": "south dakota",
    "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
}  # fmt: skip
_COUNTRY_ALIASES = (
    {"us", "usa", "u s", "u s a", "united states", "united states of america", "america"},
    {"uk", "u k", "gb", "great britain", "united kingdom"},
    {"uae", "united arab emirates"},
)
_LOCATION_LABEL = re.compile(r"\blocation\b|\baddress\b|\bcity\b|where are you (?:based|located)")


def _is_location_row(row: dict) -> bool:
    return row.get("key") == "current_location" or bool(row.get("autocomplete"))


def _loc_parts(text: str) -> list[str]:
    parts = []
    for p in str(text or "").split(","):
        p = re.sub(r"\d+", " ", _norm_option(p.replace(".", " ")))  # drop ZIP / postcode digits
        p = " ".join(p.split())
        if p:
            parts.append(p)
    return parts


def _same_place_part(a: str, b: str) -> bool:
    if a == b:
        return True
    if _US_STATES.get(a) == b or _US_STATES.get(b) == a:
        return True
    return any(a in group and b in group for group in _COUNTRY_ALIASES)


def _location_equivalent(expected: str, actual: str) -> bool:
    """Whether a location widget's rewrite names the same place: the same city, plus at least one
    more part, every one of which is one of the answer's own parts (a state code for the state, "USA"
    for "United States"; a ZIP is dropped). So the planned city alone never verifies a read-back that
    adds a region the answer doesn't name: "Portland" is not "Portland, Maine, United States" (QA,
    PR #121), and neither a bare city nor a different state is the answer."""
    e, a = _norm_option(expected), _norm_option(actual)
    if not e or not a:
        return False
    ep, ap = _loc_parts(expected), _loc_parts(actual)
    if len(ap) < 2 or not ep or ep[0] != ap[0]:
        return False
    return all(any(_same_place_part(x, y) for y in ep[1:]) for x in ap[1:])


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


UNPLANNED_EXPECTED = "(not in the plan — leave it blank)"
UNPLANNED_REASON = (
    "nothing in the plan put this value here. If the operator gave you this answer, re-plan with it in "
    "extra_answers keyed by this field's id or name; otherwise clear it."
)
# Address parts a location picker fills from the location you typed (Workable's Address → city,
# postcode, country): derived from a planned answer, not typed by anyone.
_ADDRESS_PART = re.compile(r"^(?:city|town|locality|state|region|province|country|post\s?code|zip(?:\s?code)?)$")


def _is_address_part(field: dict) -> bool:
    texts = [_clean_label(field.get("label"))] + [_split_ident(field.get(k)) for k in ("name", "id")]
    return any(t and _ADDRESS_PART.match(t) for t in texts)


_POSTCODE = re.compile(r"^(?:post\s?code|zip(?:\s?code)?)$")


def _consistent_with_location(value: str, field: dict, locations: list[str]) -> bool:
    """Whether an address part's value is one the planned location itself names: the city, the
    region (or its code), the country (or an alias) — or, for a postcode, digits the planned location
    contains. Anything else ("Canada" in the country box of a Portland, Oregon answer) is not derived
    from the answer, however the field is labelled (QA, PR #121)."""
    texts = [_clean_label(field.get("label"))] + [_split_ident(field.get(k)) for k in ("name", "id")]
    if any(t and _POSTCODE.match(t) for t in texts):
        digits = re.sub(r"\D", "", value)
        return bool(digits) and any(digits in re.sub(r"\D", " ", loc).split() for loc in locations)
    v = " ".join(_norm_option(value.replace(".", " ")).split())
    return bool(v) and any(_same_place_part(v, part) for loc in locations for part in _loc_parts(loc))


def _member_field(member: dict, fields: list[dict]) -> dict | None:
    """The read-back checkbox a plan's group member names: by id, else by name AND label (members of
    one group share a name), else by label."""
    mid, mname, mlabel = _norm(member.get("id")), _norm(member.get("name")), _norm(member.get("label"))
    boxes = [f for f in fields if _is_checkbox(f)]
    if mid:
        for f in boxes:
            if _norm(f.get("id")) == mid:
                return f
    for f in boxes:
        if _norm(f.get("label")) == mlabel and (not mname or _norm(f.get("name")) == mname):
            return f
    return None


def review(plan: dict, form_fields_after) -> tuple[list[dict], list[dict]]:
    """Compare a plan to the form read back after filling: ``(mismatches, readback)``.

    ``mismatches`` is a list of ``{label, target, expected, actual, reason?, unplanned?}``; an empty
    list means VERIFIED. Three passes:

    * **planned rows** — every planned value (except ``skip``) must hold, normalized per field type;
      each row is matched to its OWN field by identity (id, then name, then label). A select whose
      options the plan couldn't see accepts the option browser_select committed for the answer (a
      unique prefix: "they/them" → "they/them/theirs"); a location accepts the widget's spelling of
      the same place ("Portland, OR, USA"); an address part the location picker fills itself (a city
      input left empty while the visible Address holds the place) is satisfied by that companion. A
      ``check`` row needs exactly its listed checkboxes ticked and the rest of its group unticked.
    * **required fields** — a required field still empty is reported.
    * **unplanned values** — any OTHER visible field holding a value is a mismatch, unless the page
      itself showed that value when the application was first planned (``baseline``) or it is an
      address part derived from a planned location. A ``skip`` row that now holds a value counts
      here too. This is what stops a value nobody confirmed — an "over 18" Yes clicked unasked, an
      inferred passport country — from riding through to VERIFIED.

    ``readback`` is what the approval card shows: every visible field that holds a value, with where
    the value came from (``answer`` / ``extra`` / ``page`` / ``derived`` / ``unplanned``) and whether it
    is a legal / eligibility / attestation answer (``sensitive``)."""
    rows = plan.get("rows", []) if isinstance(plan, dict) else []
    baseline = plan.get("baseline") if isinstance(plan, dict) and isinstance(plan.get("baseline"), dict) else {}
    fields = [f for f in (form_fields_after if isinstance(form_fields_after, list) else []) if isinstance(f, dict)]
    visible = [f for f in fields if not _is_hidden(f)]
    by_id, by_name, by_label = _indexes(fields)
    mismatches: list[dict] = []
    covered: set[int] = set()  # id() of each read-back field a row accounted for
    origin: dict[int, dict] = {}  # id() → {"from", "sensitive", "label"} for the read-back
    location_landed: list[str] = []
    planned_radio_names: set[str] = set()
    # Native radios share requiredness by name. Only live, checked radio evidence can satisfy
    # their group; keep planned-row comparisons below specific to the intended option.
    selected_radio_names = {
        field["name"]
        for field in visible
        if _norm(field.get("kind")) == "radio" and field.get("name") and _readback_value(field).strip()
    }

    def src(row: dict) -> str:
        return "extra" if row.get("source") == "extra" else "answer"

    for row in rows:
        if not isinstance(row, dict) or row.get("action") == SKIP:
            continue
        label = row.get("label", "")
        target = row.get("target") or label
        if row.get("action") == CHECK and row.get("group_field"):
            field = _match_field(row, by_id, by_name, by_label)
            if field is not None:
                covered.add(id(field))
            want = sorted(_norm_option(t) for t in row.get("ticks") or [])
            have_labels = _ticked_options(field)
            if field is not None and want == sorted(_norm_option(t) for t in have_labels):
                if have_labels:
                    origin[id(field)] = {"from": src(row), "sensitive": bool(row.get("sensitive")), "label": label}
                continue
            mismatches.append(
                {
                    "label": label,
                    "target": target,
                    "expected": ", ".join(row.get("ticks") or []) or "(nothing ticked)",
                    "actual": ", ".join(have_labels) if field is not None else "(not found)",
                    "reason": "tick each listed option with browser_select(field, option_text); browser_click an "
                    "option's label to untick one that shouldn't be",
                }
            )
            continue
        if row.get("action") == CHECK:
            wanted = {json.dumps(m, sort_keys=True) for m in row.get("checks") or []}
            for member in row.get("members") or row.get("checks") or []:
                box = _member_field(member, fields)
                want = json.dumps(member, sort_keys=True) in wanted
                have = bool(box) and _is_ticked(box)
                if box is not None:
                    covered.add(id(box))
                    if have:
                        origin[id(box)] = {
                            "from": src(row),
                            "sensitive": bool(row.get("sensitive")),
                            "label": f"{label}: {member.get('label', '')}" if row.get("group") else label,
                        }
                if want != have:
                    mismatches.append(
                        {
                            "label": f"{label}: {member.get('label', '')}" if row.get("group") else label,
                            "target": member.get("target") or member.get("label", ""),
                            "expected": "ticked" if want else "unticked",
                            "actual": "ticked" if have else ("unticked" if box is not None else "(not found)"),
                        }
                    )
            continue
        expected = str(row.get("value") or "")
        if not expected.strip():
            # The plan assigned no value to this field — never demand one. (A REQUIRED field left
            # blank is still caught by the required-empty pass below, as it isn't marked covered.)
            continue
        field = _match_field(row, by_id, by_name, by_label)
        if field is not None:
            covered.add(id(field))
        if _norm(row.get("kind")) in {"radio", "radio-group"} and row.get("name"):
            planned_radio_names.add(str(row["name"]))
        actual = _readback_value(field, row.get("kind", "")) if field else ""
        if _values_equal(expected, actual, row, _options(field) if field else None):
            if field is not None:
                origin[id(field)] = {"from": src(row), "sensitive": bool(row.get("sensitive")), "label": label}
            if _is_location_row(row):
                location_landed.append(expected)
            continue
        if (
            field is not None
            and not actual.strip()
            and row.get("key") == "current_location"
            and (_is_hidden(field) or _is_address_part(field))
            and _companion_holds(expected, field, visible)
        ):
            continue  # derived: the visible location field holds the place, the picker fills this
        mismatch = {"label": label, "target": target, "expected": expected, "actual": actual}
        if field and _norm(row.get("kind")) in {"radio", "radio-group"} and not actual:
            mismatch["actual"] = str(field.get("value") or "")
            mismatch["reason"] = "unfilled — no checked radio value read back"
        mismatches.append(mismatch)

    for field in visible:
        if not _is_required(field) or id(field) in covered:
            continue  # already compared as a planned row above
        if _norm(field.get("kind")) == "radio" and field.get("name") in selected_radio_names:
            continue
        if _present_value(field):
            continue
        mismatches.append(
            {
                "label": _field_label(field),
                "target": _field_target(field),
                "expected": "(required — not yet filled)",
                "actual": "",
            }
        )

    for field in visible:
        if id(field) in covered or _is_machine_field(field):
            continue
        if _norm(field.get("kind")) == "radio" and field.get("name") in planned_radio_names:
            continue
        value = _present_value(field)
        if not value:
            continue
        key = _ident_key(field)
        if key and key in baseline and baseline[key] and _norm(baseline[key]) == _norm(value):
            origin[id(field)] = {"from": "page", "sensitive": is_sensitive(field), "label": _field_label(field)}
            continue
        if _is_address_part(field) and location_landed and _consistent_with_location(value, field, location_landed):
            origin[id(field)] = {"from": "derived", "sensitive": False, "label": _field_label(field)}
            continue
        reason = UNPLANNED_REASON
        if is_sensitive(field):
            reason += " " + SENSITIVE_REASON[0].upper() + SENSITIVE_REASON[1:] + "."
        mismatches.append(
            {
                "label": _field_label(field),
                "target": _field_target(field),
                "expected": UNPLANNED_EXPECTED,
                "actual": value,
                "reason": reason,
                "unplanned": True,
            }
        )

    readback = []
    for field in visible:
        if _is_machine_field(field):
            continue
        value = _present_value(field)
        if not value:
            continue
        o = origin.get(id(field), {"from": "unplanned", "sensitive": is_sensitive(field), "label": _field_label(field)})
        shown = "ticked" if _is_checkbox(field) else value
        readback.append(
            {
                "label": o["label"],
                "target": _field_target(field),
                "value": shown,
                "from": o["from"],
                "sensitive": bool(o["sensitive"] or is_sensitive(field)),
            }
        )
    return mismatches, readback


def diff(plan: dict, form_fields_after) -> list[dict]:
    """The mismatches half of :func:`review` — an empty list means VERIFIED."""
    return review(plan, form_fields_after)[0]


def _field_label(field: dict) -> str:
    return str(field.get("label") or field.get("name") or field.get("id") or "").strip()


def _field_target(field: dict) -> str:
    raw_id = str(field.get("id") or "").strip()
    return id_selector(raw_id) if raw_id else _field_label(field)


def _companion_holds(expected: str, field: dict, visible: list[dict]) -> bool:
    """Whether another visible location/address field holds ``expected`` (or the same place) —
    the companion a hidden or derived city/postcode/country input is filled from."""
    for other in visible:
        if other is field or _is_address_part(other):
            continue
        if not _LOCATION_LABEL.search(_hay(other)):
            continue
        value = _present_value(other)
        if not value:
            continue
        parts = _loc_parts(value)
        if _location_equivalent(expected, value) or (parts and parts[0] == _norm_option(expected)):
            return True  # the same place, or (a city-only row) the companion's own city
    return False


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


def _save(sessions: dict, baselines: list | None = None) -> None:
    """Atomic write (callers hold the ``fill_sessions`` lock). ``baselines`` ``None`` keeps the
    stored ones as they are."""
    if baselines is None:
        baselines = _read_baselines()
    payload = {"sessions": sessions, "baselines": baselines, "updated": _today()}
    _store._write_store(_path(), json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


# ── baselines: what the page held before anything was typed ───────────────────────────────
# The read-back blocks VERIFIED on any value the plan didn't put there — except a value the PAGE
# itself shows (a pre-selected country, a default). Telling those apart needs the form as it was
# BEFORE the agent typed anything, and a re-plan can't supply that: a re-plan reads a form the agent
# may already have filled, and taking its values as "the page's" would launder an improvised answer
# into a pre-filled one. So the FIRST live plan of an application records its values, and every later
# plan of the same application reuses that record: a field first seen on a re-plan counts as blank.
# An application is the same when its company + role match, or, failing that, when most of its fields
# do (the agent may spell the role differently between calls).
_MAX_BASELINES = 30


def _read_baselines() -> list[dict]:
    raw, err = _store._read_json(_path())
    if err or not isinstance(raw, dict) or not isinstance(raw.get("baselines"), list):
        return []
    return [b for b in raw["baselines"] if isinstance(b, dict) and isinstance(b.get("values"), dict)]


def _app_key(company: str, role: str, posting: str = "") -> str:
    if not (_norm(company) or _norm(role) or _norm(posting)):
        return ""
    return "|".join((_norm(company), _norm(role), _norm(posting)))


def _find_baseline(baselines: list[dict], app: str) -> dict | None:
    """The stored baseline of exactly this application — never a look-alike (QA, PR #121)."""
    for b in reversed(baselines):
        if app and b.get("app") == app:
            return b
    return None


def _looks_seen(baselines: list[dict], keys: set[str]) -> bool:
    """Whether most of this form's fields already belong to some stored baseline — the same form
    under another company/role spelling, most likely. Its values may be the agent's, so they never
    seed a new baseline."""
    for b in baselines:
        known = set(b.get("values", {}))
        common = known & keys
        if len(common) >= 3 and len(common) / max(1, len(known | keys)) >= 0.6:
            return True
    return False


def _merge_baseline(baselines: list[dict], snapshot: dict, app_parts) -> dict[str, str]:
    """The baseline for this plan, updating ``baselines`` in place (caller holds the lock).

    Only the SAME application (company, role and posting, normalized) reuses a stored baseline. A
    plan with no identity gets none: nothing on its form counts as the page's. A new identity whose
    form mostly matches a stored one gets none either, and records none — that is how a re-spelled
    company or role would otherwise pass values the agent already typed off as pre-filled. Only a
    genuinely new form starts a baseline from what it reads now."""
    app = _app_key(*app_parts)
    if not app:
        return {}
    found = _find_baseline(baselines, app)
    if found is None and _looks_seen(baselines, set(snapshot)):
        return {}
    if found is None:
        found = {"app": app, "values": dict(snapshot), "created": _now_ts()}
        baselines.append(found)
        del baselines[:-_MAX_BASELINES]
        return dict(found["values"])
    for key in snapshot:
        found["values"].setdefault(key, "")  # first seen on a re-plan: the page didn't put it there
    if app and not found.get("app"):
        found["app"] = app
    return dict(found["values"])


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


def _save_session(session_id: str, record: dict, snapshot: dict | None = None, app=None) -> dict:
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
        baselines = _read_baselines()
        prior = sessions.get(session_id, {})
        if app is not None:
            baseline = _merge_baseline(baselines, dict(snapshot or {}), app)
        else:
            baseline = dict(snapshot or {})  # a schema plan (no live page): nothing pre-filled
        merged = {
            **record,
            "baseline": baseline,
            "session_id": session_id,
            "created": prior.get("created") or _now_ts(),
            "verified": False,
            "last_diff": None,
            "last_readback": None,
            "verified_at": "",
            "verification_id": "",  # a plan write retires any prior verification event / grant
        }
        _clear_other_verifications(sessions, session_id)  # at most one verified session, ever
        sessions[session_id] = merged
        _save(sessions, baselines)
        return baseline


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


def record_verification(session_id: str, mismatches: list[dict], readback: list[dict] | None = None) -> dict:
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
        rec["last_readback"] = list(readback) if (verified and readback is not None) else None
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
        rec["last_readback"] = None
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
