"""The job-fit rubric — pure Python, no host imports, unit-testable.

Ported and adapted from Mads Lorentzen's ``ai-job-search`` (MIT), the
``04-job-evaluation.md`` scoring framework. The four scored dimensions are
weighted; **Location is a separate pass/fail gate**, not weighted here.

The weights live here as data so they can be tested for real, and are wired into
a live, agent-tunable ``Knobs`` surface in ``__init__.py`` — so "score these more
on growth than raw skills" is a one-line ``careercoach_tune`` / ``careercoach_preset``,
not a prompt edit. The ``job-application-assistant`` skill reads them at eval time.
"""

from __future__ import annotations

# The four scored dimensions and their default weights. Must sum to 100.
DEFAULT_WEIGHTS: dict[str, int] = {
    "technical": 30,  # required/preferred skills vs. the candidate's capabilities
    "experience": 25,  # does the work history line up with what they're hiring for
    "behavioral": 15,  # role + company culture vs. the behavioral profile
    "career": 30,  # does this advance the candidate's direction and energize them
}

DIMENSIONS: tuple[str, ...] = tuple(DEFAULT_WEIGHTS)

# Named presets the agent (or operator) can snap the rubric to.
PRESETS: dict[str, dict[str, int]] = {
    "balanced": dict(DEFAULT_WEIGHTS),
    "skills-first": {"technical": 45, "experience": 30, "behavioral": 10, "career": 15},
    "growth-first": {"technical": 20, "experience": 15, "behavioral": 20, "career": 45},
    "culture-first": {"technical": 20, "experience": 20, "behavioral": 40, "career": 20},
}

# Verdict bands on the 0-100 overall score: (floor, label, recommendation).
THRESHOLDS: tuple[tuple[int, str, str], ...] = (
    (75, "Strong Fit", "Definitely apply — tailor everything."),
    (60, "Good Fit", "Apply; address the gaps in the cover letter."),
    (45, "Moderate Fit", "Consider carefully — talk it through with your coach first."),
    (30, "Weak Fit", "Probably skip unless there's a strategic reason."),
    (0, "Poor Fit", "Skip."),
)


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Scale a weight map so the four dimensions sum to 100. A missing dimension
    counts as 0; an all-zero map falls back to the defaults (never divide by zero)."""
    picked = {d: max(0.0, float(weights.get(d, 0))) for d in DIMENSIONS}
    total = sum(picked.values())
    if total <= 0:
        return {d: float(DEFAULT_WEIGHTS[d]) for d in DIMENSIONS}
    return {d: v * 100.0 / total for d, v in picked.items()}


def weighted_overall(scores: dict[str, float], weights: dict[str, float] | None = None) -> float:
    """Weighted average of the four 0-100 dimension scores, rounded to one decimal.
    Only dimensions that were actually scored contribute (so a partial evaluation
    still yields a sensible number). Location is handled separately as pass/fail."""
    w = normalize_weights(weights or DEFAULT_WEIGHTS)
    num = 0.0
    denom = 0.0
    for dim in DIMENSIONS:
        if dim in scores and scores[dim] is not None:
            s = min(100.0, max(0.0, float(scores[dim])))
            num += s * w[dim]
            denom += w[dim]
    if denom <= 0:
        return 0.0
    return round(num / denom, 1)


def verdict(overall: float) -> tuple[str, str]:
    """Map a 0-100 overall score to its (label, recommendation)."""
    for floor, label, rec in THRESHOLDS:
        if overall >= floor:
            return label, rec
    return THRESHOLDS[-1][1], THRESHOLDS[-1][2]
