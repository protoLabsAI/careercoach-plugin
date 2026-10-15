"""Company + role identity — one normalizer for every place that asks "is this the same job?".

The tracker, the first-seen log, the applied-history import and the referral matcher all compare
companies. Exact lower-cased equality said "Arize AI" ≠ "Arize" and "DPR COnstruction" ≠ "DPR
Construction, Inc." and let the same company through twice, so everything keys on
``company_key`` instead. Pure, host-free.
"""

from __future__ import annotations

import re

# Trailing words that name a legal form or a generic descriptor, not the company. Stripped only
# from the END, and never down to nothing ("AI Inc" stays "ai").
_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh",
    "plc", "sa", "ag", "bv", "pbc", "technologies", "technology", "labs", "lab", "ai", "hq", "group",
    "studio", "studios", "software", "systems", "the",
}  # fmt: skip


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and "))


def company_key(name: str) -> str:
    """A comparison key for a company name: lower-case, punctuation-free, legal-form and generic
    suffixes dropped from the end ("Arize AI" → "arize", "Scale AI, Inc." → "scale",
    "Change.org" → "changeorg"). A leading "the" is dropped too."""
    words = _words(name.replace(".", "") if name else "")
    if words and words[0] == "the" and len(words) > 1:
        words = words[1:]
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return "".join(words)


def role_key(title: str) -> str:
    """A comparison key for a role title: lower-case words, punctuation and seniority-neutral
    noise kept (a Senior and a Staff opening at one company are different roles)."""
    return " ".join(_words(title))


def same_company(a: str, b: str) -> bool:
    ka, kb = company_key(a), company_key(b)
    return bool(ka) and ka == kb
