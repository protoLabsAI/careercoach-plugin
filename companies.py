"""Company + role identity — one normalizer for every place that asks "is this the same job?".

The tracker, the first-seen log, the applied-history import and the referral matcher all compare
companies. Two strengths: ``company_key`` (exact — legal forms and punctuation only) decides that two
rows ARE one, and ``company_hint_key`` (also drops "AI", "Labs", "Systems"…) only powers hints like
"you've applied here", because "Mercury" and "Mercury Systems" are different companies. Pure, host-free.
"""

from __future__ import annotations

import re

# Trailing legal forms — never part of a company's identity, safe to drop for an exact match.
_LEGAL = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "plc",
          "sa", "ag", "bv", "pbc"}  # fmt: skip
# Generic descriptors ("Arize AI", "Grafana Labs") — dropping them is only a HINT that two names are
# the same company: "Mercury" and "Mercury Systems" are different companies. Never key a write on it.
_GENERIC = {"technologies", "technology", "labs", "lab", "ai", "hq", "group", "studio", "studios", "software",
            "systems"}  # fmt: skip


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and "))


def _key_words(name: str, drop: set[str]) -> list[str]:
    words = _words(name.replace(".", "") if name else "")
    if words and words[0] == "the" and len(words) > 1:
        words = words[1:]
    while len(words) > 1 and words[-1] in drop:
        words.pop()
    return words


def _key(name: str, drop: set[str]) -> str:
    return "".join(_key_words(name, drop))


def company_key(name: str) -> str:
    """The EXACT comparison key for a company: lower-case, punctuation-free, legal forms dropped from
    the end ("DPR Construction, Inc." = "DPR COnstruction", "Change.org" → "changeorg"). This is what
    tracker writes key on — it must never merge two different companies."""
    return _key(name, _LEGAL)


def company_hint_key(name: str) -> str:
    """A LOOSE key that also drops generic descriptors ("Arize AI" ~ "Arize"). Good for "you've
    applied here" / "you know someone there" hints; never for deciding two rows are one. A plural
    word folds to the singular ("Bright Visions" ~ "Bright Vision")."""
    stripped = _key_words(name, _LEGAL | _GENERIC)  # suffixes first, so "Systems" still drops
    return "".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in stripped)


def role_key(title: str) -> str:
    """A comparison key for a role title: lower-case words, punctuation dropped (a Senior and a
    Staff opening at one company are different roles)."""
    return " ".join(_words(title))


def same_company(a: str, b: str) -> bool:
    ka, kb = company_key(a), company_key(b)
    return bool(ka) and ka == kb
