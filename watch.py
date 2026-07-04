"""Pure helpers for the background job-watch. The SDK wiring (supervise + register_surface +
the goal verifier) lives in ``__init__.py``; the matching logic is here so it's unit-testable
with no host and no network."""

from __future__ import annotations

from .jobsource import prescore


def _key(company: str, role: str) -> tuple[str, str]:
    return (company.strip().lower(), role.strip().lower())


def find_new_matches(jobs: list[dict], target_roles: str, seen_keys: set, min_score: int) -> list[dict]:
    """From freshly-searched ``jobs``, return those that (1) prescore >= ``min_score`` against the
    target roles and (2) aren't already tracked (their ``(company, title)`` isn't in ``seen_keys``).
    Each result carries an added ``score``; the list is sorted highest-first. Pure — the watch
    engine feeds it search results + the tracker's existing keys."""
    out: list[dict] = []
    for j in jobs:
        key = _key(j.get("company", ""), j.get("title", ""))
        if key in seen_keys:
            continue
        score = prescore(j, target_roles)
        if score >= min_score:
            out.append({**j, "score": score})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out
