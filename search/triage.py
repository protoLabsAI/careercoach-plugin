"""The triage log: what the operator kept, what they dismissed, and why.

Every inbox decision is one entry: when, keep or dismiss, a snapshot of the card as it was shown
(company, title, source, score and the score's own reasoning, place, pay) and, for a dismissal, the
operator's reason when they gave one. The inbox forgets a card once it is handled; this doesn't.

It exists to tune the search. A source whose roles are nearly all dismissed, a target role that
never produces a keep, a reason that keeps recurring ("location", "recruiter"): each points at a
setting or a filter to change. ``report`` adds the log up; ``format_report`` says it in plain
words with the adjustments the numbers suggest. Nothing here changes a setting by itself.

``report`` and ``format_report`` are pure; ``record``, ``set_reason`` and ``entries`` touch the store.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

LOG = "triage-log.json"
LOG_CAP = 5000  # entries kept, oldest dropped first

#: Why a role was dismissed. Codes are stored; labels are shown.
REASONS = {
    "wrong_role": "Not the kind of role I want",
    "level": "Too junior or too senior",
    "stack": "Wrong tech stack",
    "location": "Location doesn't work",
    "pay": "Pay too low",
    "company": "Not this company",
    "recruiter": "Recruiter or reposted listing",
    "stale": "Old, reposted, or a recruiter's listing",  # the view's key 7 covers all three
    "duplicate": "Already seen or applied",
    "other": "Something else",
}
_SNAPSHOT = ("id", "company", "title", "source", "sources", "score", "reason", "location", "comp", "posted", "ghost")


def normalize_reason(reason: str) -> str:
    """A reason code from what was given: the code itself, or its label, in any case. "" stays "".
    Raises ``ValueError`` naming the codes for anything else."""
    text = (reason or "").strip().lower()
    if not text:
        return ""
    key = re.sub(r"[\s-]+", "_", text)
    if key in REASONS:
        return key
    for code, label in REASONS.items():
        if text == label.lower():
            return code
    raise ValueError(f"unknown reason {reason!r}; use one of: {', '.join(REASONS)}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _entry(action: str, card: dict, reason: str, note: str, via: str, when: str) -> dict:
    snap = {k: card.get(k) for k in _SNAPSHOT if card.get(k) not in (None, "", [])}
    # the card's "reason" is the fit score's reasoning; the operator's reason gets its own key
    if "reason" in snap:
        snap["why_scored"] = snap.pop("reason")
    out = {"at": when, "action": action, **snap}
    if reason:
        out["reason"] = reason
    if note:
        out["note"] = note.strip()[:300]
    if via:
        out["via"] = via
    return out


def record(
    action: str, cards: list[dict], reason: str = "", note: str = "", via: str = "", when: str | None = None
) -> int:
    """Append one entry per card. ``action`` is "keep" or "dismiss". Never raises: the log is a
    record of a decision already made, and losing an entry must not undo or block the decision."""
    from . import watchlist as wl

    if action not in ("keep", "dismiss") or not cards:
        return 0
    try:
        code = normalize_reason(reason) if action == "dismiss" else ""
    except ValueError:
        code, note = "other", (note or reason)
    when = when or _now()
    try:
        with wl._store._locked("triage"):
            log = wl._load(LOG, [])
            log += [_entry(action, c, code, note, via, when) for c in cards if c.get("id")]
            wl._save(LOG, log[-LOG_CAP:])
    except (wl.StoreUnreadable, OSError):
        return 0
    return len(cards)


def set_reason(ids: list[str], reason: str, note: str = "") -> int:
    """Attach a reason to the latest dismissal of each id (the view asks after the dismissal, so
    the entry exists first). Returns how many entries changed. Raises ``ValueError`` for an unknown
    reason; an unreadable log raises ``StoreUnreadable``."""
    from . import watchlist as wl

    code = normalize_reason(reason)
    if not code:
        raise ValueError(f"a reason is needed; use one of: {', '.join(REASONS)}")
    want = set(ids or [])
    changed = 0
    with wl._store._locked("triage"):
        log = wl._load(LOG, [])
        for entry in reversed(log):
            if entry.get("action") == "dismiss" and entry.get("id") in want:
                entry["reason"] = code
                if note:
                    entry["note"] = note.strip()[:300]
                want.discard(entry["id"])
                changed += 1
                if not want:
                    break
        if changed:
            wl._save(LOG, log)
    return changed


def entries(days: int = 0, now: datetime | None = None) -> list[dict]:
    """The log, oldest first; with ``days``, only that many days back. An unreadable log reads as empty."""
    from . import watchlist as wl

    try:
        log = wl._load(LOG, [])
    except wl.StoreUnreadable:
        return []
    if not days:
        return log
    cut = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat(timespec="seconds")
    return [e for e in log if str(e.get("at") or "") >= cut]


# ── adding it up (pure) ───────────────────────────────────────────────────────
def _role_of(entry: dict) -> str:
    """The target role the card was matched to, read from the score's reasoning
    (``title ~ "Design Engineer"``); "" when the card didn't say."""
    m = re.search(r'title ~ "([^"]+)"', str(entry.get("why_scored") or ""))
    return m.group(1) if m else ""


def _tally(log: list[dict], key) -> dict:
    out: dict[str, dict] = {}
    for e in log:
        for name in key(e):
            if not name:
                continue
            row = out.setdefault(name, {"kept": 0, "dismissed": 0})
            row["kept" if e.get("action") == "keep" else "dismissed"] += 1
    for row in out.values():
        total = row["kept"] + row["dismissed"]
        row["total"] = total
        row["keep_rate"] = round(row["kept"] / total, 2) if total else 0.0
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["total"]))


def _band(score) -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return ""
    return "90-100" if s >= 90 else "80-89" if s >= 80 else "65-79" if s >= 65 else "under 65"


def report(log: list[dict]) -> dict:
    """Add a triage log up: totals, the reasons given, and keep rates by source, by matched target
    role and by score band; plus, per reason, which sources and companies it fell on."""
    dismissed = [e for e in log if e.get("action") == "dismiss"]
    kept = [e for e in log if e.get("action") == "keep"]
    reasons: dict[str, dict] = {}
    for e in dismissed:
        code = e.get("reason") or ""
        if not code:
            continue
        row = reasons.setdefault(code, {"count": 0, "sources": {}, "companies": {}, "notes": []})
        row["count"] += 1
        src = e.get("source") or "unknown"
        row["sources"][src] = row["sources"].get(src, 0) + 1
        co = e.get("company") or ""
        if co:
            row["companies"][co] = row["companies"].get(co, 0) + 1
        if e.get("note") and len(row["notes"]) < 5:
            row["notes"].append(e["note"])
    total = len(dismissed) + len(kept)
    return {
        "decisions": total,
        "kept": len(kept),
        "dismissed": len(dismissed),
        "keep_rate": round(len(kept) / total, 2) if total else 0.0,
        "with_reason": sum(1 for e in dismissed if e.get("reason")),
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]["count"])),
        "by_source": _tally(log, lambda e: e.get("sources") or [e.get("source") or "unknown"]),
        "by_role": _tally(log, lambda e: [_role_of(e)]),
        "by_score": _tally(log, lambda e: [_band(e.get("score"))]),
        "first": str(log[0].get("at") or "")[:10] if log else "",
        "last": str(log[-1].get("at") or "")[:10] if log else "",
    }


MIN_DECISIONS = 10  # fewer than this on a source or role says nothing yet
#: Sources that are the watched companies' own boards: tuned through the watchlist, not Job source.
BOARD_SOURCES = ("ashby", "greenhouse", "lever", "smartrecruiters", "dover")


def _low(row: dict, overall: float) -> bool:
    """Is this keep rate low FOR THIS OPERATOR? Someone who keeps one role in ten overall is not
    told that every source at 10% is failing: low means under half their own overall rate (and
    under 5% whatever that is)."""
    return row["total"] >= MIN_DECISIONS and row["keep_rate"] < max(0.05, overall / 2)


def suggestions(rep: dict) -> list[str]:
    """What the numbers suggest changing, in plain sentences, each with what it would cost.
    Suggestions only: nothing is changed."""
    out: list[str] = []
    overall = rep["keep_rate"]
    pct = f"{round(overall * 100)}%"
    for name, row in rep["by_source"].items():
        if not _low(row, overall):
            continue
        if name in BOARD_SOURCES:
            out.append(
                f"Watched companies on {name}: you kept {row['kept']} of {row['total']} (you keep {pct} overall). "
                "Worth a look at which of those companies are still on your watchlist."
            )
        else:
            out.append(
                f"{name}: you kept {row['kept']} of {row['total']} (you keep {pct} overall). Dropping it from "
                f"Job source would lose those {row['kept']}."
            )
    for name, row in rep["by_role"].items():
        if _low(row, overall):
            out.append(
                f'Target role "{name}": you kept {row["kept"]} of {row["total"]} (you keep {pct} overall). '
                "Consider removing it from your target roles, or adding the titles you keep dismissing to the "
                "excluded titles."
            )
    low = rep["by_score"].get("65-79")
    if low and _low(low, overall):
        out.append(
            f"Roles scoring 65-79: you kept {low['kept']} of {low['total']}. Raising the watch match threshold to "
            f"80 would take {low['total']} such roles out of the inbox, including the {low['kept']} you kept."
        )
    reasoned = rep["with_reason"]
    hints = {
        "location": "Check the Locations setting: roles outside it are still getting through.",
        "pay": "Check the pay floor (Minimum salary): roles under it are still getting through.",
        "level": 'Add the levels you don\'t want ("staff", "principal", "junior") to the excluded titles.',
        "recruiter": "These companies can be added to the recruiter list so their listings stay under the bar",
        "stack": "Add the stacks you don't work in to your profile's do-not-claim list, so they count against a role.",
        "stale": "Lower the maximum posting age, and see which sources these came from",
        "duplicate": "The same role is arriving more than once: worth a look at how sources are deduplicated.",
    }
    for code, row in rep["reasons"].items():
        if reasoned and row["count"] >= 5 and row["count"] / reasoned >= 0.2 and code in hints:
            line = f"{REASONS[code]} was the reason {row['count']} times. {hints[code]}"
            if code == "stale" and row["sources"]:
                top = sorted(row["sources"].items(), key=lambda kv: -kv[1])[:3]
                line += ": " + ", ".join(f"{name} {n}" for name, n in top) + "."
            if code == "recruiter" and row["companies"]:
                top = sorted(row["companies"].items(), key=lambda kv: -kv[1])[:5]
                line += ": " + ", ".join(name for name, _ in top) + "."
            out.append(line)
    return out


def format_report(rep: dict, days: int = 0) -> str:
    """The report as the coach says it: totals, reasons, the weakest sources and roles, suggestions."""
    if not rep["decisions"]:
        return "No triage decisions are logged yet. Each keep or dismiss in the inbox is recorded from now on."
    span = f"the last {days} days" if days else f"{rep['first']} to {rep['last']}"
    lines = [
        f"Triage over {span}: {rep['decisions']} decisions, {rep['kept']} kept, {rep['dismissed']} dismissed "
        f"({round(rep['keep_rate'] * 100)}% kept). {rep['with_reason']} dismissals have a reason."
    ]
    if rep["reasons"]:
        lines.append("\nWhy roles were dismissed:")
        for code, row in rep["reasons"].items():
            srcs = ", ".join(f"{s} {n}" for s, n in sorted(row["sources"].items(), key=lambda kv: -kv[1])[:3])
            lines.append(f"- {REASONS.get(code, code)}: {row['count']} ({srcs})")
            lines += [f'  "{n}"' for n in row["notes"][:2]]

    def _table(title: str, rows: dict) -> None:
        rows = {k: v for k, v in rows.items() if k}
        if rows:
            lines.append(f"\n{title} (kept of total):")
            lines.extend(
                f"- {name}: {r['kept']} of {r['total']} ({round(r['keep_rate'] * 100)}%)"
                for name, r in list(rows.items())[:12]
            )

    _table("By source", rep["by_source"])
    _table("By target role", rep["by_role"])
    _table("By fit score", rep["by_score"])
    tips = suggestions(rep)
    if tips:
        lines.append("\nWhat this suggests (nothing has been changed):")
        lines.extend(f"- {t}" for t in tips)
    return "\n".join(lines)
