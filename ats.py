"""ATS adapters — read a job's PUBLIC application schema before the browser is opened (#4032 phase 4a).

Some applicant-tracking systems publish each job's application questions on a public API. Greenhouse
does, via its Job Board API:

    GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true

Fetching that up front lets the coach PREPARE every answer it already holds and surface ONLY the
genuinely new required questions to the operator — before a browser is even opened. This module does
three host-free things and one network thing:

* ``detect(url)`` — recognise a Greenhouse job URL and pull out the board token + job id. Covers the
  current ``job-boards.greenhouse.io/<board>/jobs/<id>`` form, the older ``boards.greenhouse.io/...``
  form, and a company careers page that embeds the job with a ``?gh_jid=<id>`` query param (there the
  board token isn't in the URL, so ``board`` comes back ``None``).
* ``greenhouse_fields(api_json)`` — convert the API's questions into the SAME field shape
  ``browser_form_read`` emits (``label, kind, name, id, required, value, options``) so
  ``formfill.build_plan`` consumes it unchanged. Greenhouse field types map to kinds; ``input_hidden``
  fields are dropped; the compliance (EEOC) and demographic self-ID questions are included when present.
* ``fetch_schema(board, job_id)`` — the one outbound call (httpx, 15s timeout). A network error, a
  non-200, or malformed JSON comes back as a readable ``(None, error)`` — it NEVER raises, because its
  caller is a tool. ``boards-api.greenhouse.io`` is declared in the manifest's ``capabilities.network``.

Lever / Workday are deliberately deferred until Greenhouse (and Ashby) work end to end. No ``graph.*``
imports, so the pure functions are unit-tested with nothing but a saved fixture, and the fetch is
exercised by monkeypatching it (no live network in the suite).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# The public Job Board API host — the ONLY network host this module (and the manifest) adds.
API_HOST = "boards-api.greenhouse.io"
# Greenhouse's own job-board hosts that carry the board token in the path.
_BOARD_HOSTS = ("job-boards.greenhouse.io", "boards.greenhouse.io")
_FETCH_TIMEOUT = 15.0


# ── detect: a job URL → {ats, board, job_id} | None ───────────────────────────────────────
def detect(url: str) -> dict | None:
    """Recognise a Greenhouse job URL, returning ``{"ats": "greenhouse", "board", "job_id"}`` or
    ``None``.

    * ``job-boards.greenhouse.io/<board>/jobs/<id>`` and the older ``boards.greenhouse.io/<board>/
      jobs/<id>`` → board token + job id from the path.
    * any URL carrying a ``gh_jid=<id>`` query param (a company careers page embedding a Greenhouse
      job) → the job id, with ``board`` = ``None`` because the board token isn't in the URL.
    * anything else → ``None``.
    """
    if not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    parsed = urlparse(raw if "//" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query or "")

    if host in _BOARD_HOSTS:
        m = re.search(r"/([^/]+)/jobs/(\d+)", path)
        if m:
            return {"ats": "greenhouse", "board": m.group(1), "job_id": m.group(2)}
    if host == API_HOST:
        m = re.search(r"/boards/([^/]+)/jobs/(\d+)", path)
        if m:
            return {"ats": "greenhouse", "board": m.group(1), "job_id": m.group(2)}

    jid = query.get("gh_jid")
    if jid and str(jid[0]).strip().isdigit():
        return {"ats": "greenhouse", "board": None, "job_id": str(jid[0]).strip()}

    return None


# ── greenhouse_fields: the API's questions → browser_form_read field dicts ──────────────────
# Greenhouse field type → the browser_form_read `kind` (formfill then picks fill/select/upload by
# kind). ``input_hidden`` is deliberately absent so it's dropped; any other unknown type is dropped
# too — this module never guesses a kind.
_TYPE_TO_KIND: dict[str, str] = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
}
# Field types explicitly dropped (not surfaced to the operator). Hidden fields carry no question.
_SKIP_TYPES = frozenset({"input_hidden"})
# Kinds whose options (select values) are carried through.
_OPTION_KINDS = frozenset({"select", "multiselect"})


def _option_labels(values: object) -> list[str]:
    """The visible labels of a Greenhouse select's ``values`` (``[{label, value}, …]``) as strings,
    dropping blanks. Falls back to the raw ``value`` when a label is missing, matching the human text
    a form picker shows — which is what ``formfill._match_option`` compares a confirmed answer to."""
    out: list[str] = []
    if not isinstance(values, list):
        return out
    for v in values:
        if isinstance(v, dict):
            label = v.get("label")
            if label is None or str(label) == "":
                label = v.get("value")
            if label is not None and str(label) != "":
                out.append(str(label))
        elif v is not None and str(v) != "":
            out.append(str(v))
    return out


def _fields_from_question(question: object) -> list[dict]:
    """Convert one Greenhouse question into browser_form_read-shaped field dicts.

    The question's label and required flag apply to the whole question. When a question has more than
    one input — a file-or-text pair like Resume/CV (``input_file`` + ``textarea``) — only the FIRST
    emitted field carries ``required``: satisfying either input satisfies the question, so the
    companion isn't independently required (otherwise the paste-text box would show up as an
    unanswerable required question the operator is asked about)."""
    if not isinstance(question, dict):
        return []
    label = str(question.get("label") or "").strip()
    required = bool(question.get("required"))
    raw_fields = question.get("fields")
    if not isinstance(raw_fields, list):
        return []
    out: list[dict] = []
    for f in raw_fields:
        if not isinstance(f, dict):
            continue
        ftype = str(f.get("type") or "").strip()
        if ftype in _SKIP_TYPES or ftype not in _TYPE_TO_KIND:
            continue
        kind = _TYPE_TO_KIND[ftype]
        name = str(f.get("name") or "").strip()
        out.append(
            {
                "label": label,
                "kind": kind,
                "name": name,
                "id": name,
                "required": required and not out,  # only the first emitted field of a question
                "value": "",
                "options": _option_labels(f.get("values")) if kind in _OPTION_KINDS else [],
            }
        )
    return out


def greenhouse_fields(api_json: object) -> list[dict]:
    """Flatten a Greenhouse Job Board API job (fetched with ``?questions=true``) into
    ``browser_form_read``-shaped field dicts (``label, kind, name, id, required, value, options``) so
    ``formfill.build_plan`` consumes it unchanged.

    Gathers the main application ``questions`` plus the ``compliance`` (EEOC) and
    ``demographic_questions`` self-ID questions when present; ``input_hidden`` fields are dropped and
    select options are preserved. A non-dict payload yields an empty list."""
    if not isinstance(api_json, dict):
        return []

    questions: list[object] = []
    main = api_json.get("questions")
    if isinstance(main, list):
        questions.extend(main)

    compliance = api_json.get("compliance")
    if isinstance(compliance, list):
        for section in compliance:
            if isinstance(section, dict) and isinstance(section.get("questions"), list):
                questions.extend(section["questions"])

    demographic = api_json.get("demographic_questions")
    if isinstance(demographic, dict) and isinstance(demographic.get("questions"), list):
        questions.extend(demographic["questions"])

    fields: list[dict] = []
    for q in questions:
        fields.extend(_fields_from_question(q))
    return fields


# ── fetch_schema: the one outbound call (httpx — a host dependency, kept lazy) ──────────────
async def fetch_schema(board: str, job_id: str) -> tuple[dict | None, str]:
    """Fetch a Greenhouse job's public application schema. Returns ``(api_json, "")`` on success, or
    ``(None, readable_error)`` on any failure — a missing board/id, an unreachable host, a non-200,
    or a response that isn't JSON. NEVER raises: the caller is a tool that must return a string."""
    board = (board or "").strip()
    job_id = str(job_id or "").strip()
    if not board or not job_id:
        return None, "Can't fetch a Greenhouse schema without both a board token and a job id."

    import httpx

    url = f"https://{API_HOST}/v1/boards/{board}/jobs/{job_id}"
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
            resp = await client.get(url, params={"questions": "true"})
    except Exception as e:  # noqa: BLE001 — a network error becomes a readable message, never a raise
        return None, f"Could not reach the Greenhouse job board API ({e})."
    if resp.status_code != 200:
        return None, (
            f"The Greenhouse job board API returned HTTP {resp.status_code} for board {board!r} "
            f"job {job_id!r} — the job may be closed or the board token may be wrong."
        )
    try:
        return resp.json(), ""
    except Exception as e:  # noqa: BLE001 — malformed JSON is a readable message, not a traceback
        return None, f"The Greenhouse job board API returned a response that wasn't valid JSON ({e})."
