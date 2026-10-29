"""Grounding post-check — pure Python, no host imports, unit-testable.

A drafting pass (a résumé bullet, a STAR interview answer, a cover-letter line) can invent a
hard number that isn't in the operator's record, or quietly flip a real one — on qwen3:14b an
interview-prep run wrote "99.9% uptime" with no uptime figure in the profile at all, and turned
the operator's real "cut failed runs by ~70%" into "had a 70% failure rate". Nothing checked for
that. This module is the check.

Two pure functions, stdlib only:

  * ``extract_figures(text)`` pulls every quantity out of a block of text — percentages, money,
    multipliers (``3x``), durations (``45 minutes``), ranges and plain numbers — each with a short
    context window and a *normalised* value, so ``~70%`` equals ``70%`` and ``1,200`` equals
    ``1200``.
  * ``check_draft(draft, sources)`` compares a draft's figures against the grounding ``sources``
    (the experience profile + story bank + skills) and returns two kinds of flag: **unsupported**
    (the value is nowhere in the sources) and **context-mismatch** (the value IS in the sources,
    the source occurrence has a direction word — the reduce/cut/drop family vs the
    increase/grow/raise family — and the draft reads the direction the other way, or not at all).

It writes nothing and names no record; it only reports. ``render_flags`` turns the flags into the
plain words the agent surface returns.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# ── direction vocabulary ─────────────────────────────────────────────────────────────────────
# Two opposed families. A figure like "70%" means opposite things in "cut … by 70%" and
# "grew … by 70%"; if the source frames it one way and the draft the other (or drops the frame),
# that's the inversion we flag. Kept as explicit inflected words rather than stems so the match is
# predictable and never catches an unrelated token (e.g. "lower" must not fire on "lowercase").
_REDUCE = frozenset(
    {
        "reduce", "reduced", "reduces", "reducing", "reduction", "reductions",
        "cut", "cuts", "cutting", "cutback",
        "drop", "dropped", "drops", "dropping",
        "decrease", "decreased", "decreases", "decreasing",
        "lower", "lowered", "lowers", "lowering",
        "slash", "slashed", "slashes", "slashing",
        "shrink", "shrinks", "shrinking", "shrank", "shrunk",
        "decline", "declined", "declines", "declining",
        "fell", "fewer", "less", "down",
    }
)  # fmt: skip
_INCREASE = frozenset(
    {
        "increase", "increased", "increases", "increasing",
        "grow", "grows", "growing", "grew", "grown", "growth",
        "raise", "raised", "raises", "raising",
        "improve", "improved", "improves", "improving", "improvement",
        "boost", "boosted", "boosts", "boosting",
        "rise", "rises", "rising", "rose", "risen",
        "gain", "gained", "gains", "higher", "more", "up", "doubled", "tripled",
    }
)  # fmt: skip
_OPPOSITE = {"reduce": "increase", "increase": "reduce"}

# Time-unit spellings → one canonical token, so "45 minutes" and "45 min" compare equal.
_UNITS = {
    "ms": "ms", "millisecond": "ms", "milliseconds": "ms",
    "sec": "sec", "secs": "sec", "second": "sec", "seconds": "sec",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "hr": "hr", "hrs": "hr", "hour": "hr", "hours": "hr",
    "day": "day", "days": "day",
    "week": "week", "weeks": "week",
    "month": "month", "months": "month",
    "year": "year", "years": "year",
}  # fmt: skip
_UNIT_ALT = "|".join(sorted(_UNITS, key=len, reverse=True))

# One ordered alternation: the most specific shape wins at each position (money/percent before a
# bare number), so the digits inside "99.9%" are never also read as the plain number "99.9".
_FIGURE_RE = re.compile(
    r"""
      (?P<money>\$\s*\d[\d,]*(?:\.\d+)?\s*(?:million|billion|thousand|[kmb])?)
    | (?P<percent>(?:~\s*)?\d[\d,]*(?:\.\d+)?\s*(?:%|percent\b))
    | (?P<multiplier>\d+(?:\.\d+)?\s*[x]\b)
    | (?P<duration>\d[\d,]*(?:\.\d+)?\s*(?:""" + _UNIT_ALT + r""")\b)
    | (?P<range>\d[\d,]*(?:\.\d+)?\s*[-–—]\s*\d[\d,]*(?:\.\d+)?)
    | (?P<number>\d[\d,]*(?:\.\d+)?)
    """,
    re.VERBOSE | re.IGNORECASE,
)

_WORD_RE = re.compile(r"\S+")
_PLACEHOLDER_RE = re.compile(r"\[\s*fill[ _]in\s*:[^\]]*\]", re.IGNORECASE)
_CONTEXT_WORDS = 6  # how many words each side of a figure make up its context window


class Figure(NamedTuple):
    """One quantity found in text: the ``raw`` slice as written, its ``normalised`` matching key,
    its ``kind``, a short ``context`` window, and the char span it occupied."""

    raw: str
    normalized: str
    kind: str
    context: str
    start: int
    end: int


class Flag(NamedTuple):
    """A grounding problem with one draft figure. ``kind`` is ``"unsupported"`` or
    ``"context-mismatch"``; ``value`` is the normalised figure; the contexts are the short windows
    around the figure in the draft and (for a mismatch) in the source."""

    kind: str
    value: str
    draft_context: str
    source_context: str


def _normalize(kind: str, raw: str) -> str:
    """Canonical matching key for a figure: approximation marks and thousands separators dropped,
    whitespace and letter-casing flattened, and scale/unit words folded to one spelling."""
    s = raw.strip().lstrip("~").strip()
    s = s.replace(",", "")
    s = re.sub(r"\s+", "", s).lower()
    if kind == "percent":
        return s.replace("percent", "%")
    if kind == "money":
        return s.replace("million", "m").replace("billion", "b").replace("thousand", "k")
    if kind == "duration":
        m = re.match(r"(\d[\d.]*)([a-z]+)", s)
        if m:
            return m.group(1) + _UNITS.get(m.group(2), m.group(2))
        return s
    if kind == "range":
        return re.sub(r"[–—]", "-", s)
    return s  # multiplier / number are already canonical once stripped


def extract_figures(text: str) -> list[Figure]:
    """Every quantity in ``text`` — percentages, money, multipliers, durations, ranges and plain
    numbers — each with its normalised value and a ~6-word context window."""
    text = text or ""
    words = [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    figures: list[Figure] = []
    for m in _FIGURE_RE.finditer(text):
        kind = m.lastgroup or "number"
        raw = m.group()
        figures.append(
            Figure(
                raw=raw,
                normalized=_normalize(kind, raw),
                kind=kind,
                context=_context(words, m.start(), m.end()),
                start=m.start(),
                end=m.end(),
            )
        )
    return figures


def _context(words: list[tuple[str, int, int]], start: int, end: int) -> str:
    """The ~6 words either side of the char span ``[start, end)``, joined with single spaces."""
    hit = [i for i, (_, ws, we) in enumerate(words) if we > start and ws < end]
    if not hit:
        return ""
    lo = max(0, hit[0] - _CONTEXT_WORDS)
    hi = min(len(words), hit[-1] + 1 + _CONTEXT_WORDS)
    return " ".join(w for w, _, _ in words[lo:hi])


def _families(context: str) -> set[str]:
    """Which direction families (``reduce`` / ``increase``) the context's words belong to."""
    tokens = {re.sub(r"[^a-z]", "", w.lower()) for w in context.split()}
    fams: set[str] = set()
    if tokens & _REDUCE:
        fams.add("reduce")
    if tokens & _INCREASE:
        fams.add("increase")
    return fams


def _is_year(fig: Figure) -> bool:
    """A standalone four-digit year (1900-2099) — never flagged."""
    return fig.kind == "number" and bool(re.fullmatch(r"\d{4}", fig.normalized)) and 1900 <= int(fig.normalized) <= 2099


def _is_list_marker(fig: Figure, text: str) -> bool:
    """A plain integer used as list numbering — at the line start and trailed by ``.`` or ``)``."""
    if fig.kind != "number" or not fig.normalized.isdigit():
        return False
    line_start = text.rfind("\n", 0, fig.start) + 1
    prefix = text[line_start : fig.start]
    trailer = text[fig.end : fig.end + 1]
    return prefix.strip(" \t-*") == "" and trailer in (".", ")")


def check_draft(draft: str, sources: str) -> list[Flag]:
    """Flag figures in ``draft`` that the grounding ``sources`` don't support.

    ``unsupported`` — the value appears nowhere in the sources. ``context-mismatch`` — the value
    IS in the sources, the source occurrence carries a direction word, and the draft reads that
    figure in the opposite direction (or with no direction at all). ``[fill in: …]`` placeholders,
    standalone years and list numbering are ignored.
    """
    masked = _PLACEHOLDER_RE.sub(lambda m: " " * (m.end() - m.start()), draft or "")
    source_figs = extract_figures(sources or "")
    by_value: dict[str, list[Figure]] = {}
    for sf in source_figs:
        by_value.setdefault(sf.normalized, []).append(sf)

    flags: list[Flag] = []
    for fig in extract_figures(masked):
        if _is_year(fig) or _is_list_marker(fig, masked):
            continue
        matches = by_value.get(fig.normalized)
        if not matches:
            flags.append(Flag("unsupported", fig.normalized, fig.context, ""))
            continue
        mismatch = _direction_mismatch(fig, matches)
        if mismatch is not None:
            flags.append(Flag("context-mismatch", fig.normalized, fig.context, mismatch))
    return flags


def _direction_mismatch(fig: Figure, matches: list[Figure]) -> str | None:
    """If the draft figure contradicts (or drops) the direction every matching source occurrence
    frames it with, return that source's context window; otherwise ``None`` (consistent, or the
    sources give no single clear direction)."""
    draft_fams = _families(fig.context)
    directional = [m for m in matches if len(_families(m.context)) == 1]
    if not directional:
        return None
    for m in directional:
        (fam,) = _families(m.context)
        if fam in draft_fams and _OPPOSITE[fam] not in draft_fams:
            return None  # the draft agrees with at least one framed source occurrence
    return directional[0].context


def render_flags(flags: list[Flag]) -> str:
    """The flags in plain words for the agent surface — or the all-clear line when there are none."""
    if not flags:
        return "No unsupported figures."
    lines = ["Checked the draft's figures against your profile and story bank:"]
    for f in flags:
        if f.kind == "unsupported":
            lines.append(f"Not found in your profile: {f.value} (…{f.draft_context}…)")
        else:
            lines.append(f"Check the direction: {f.value} — your profile says '…{f.source_context}…'")
    return "\n".join(lines)
