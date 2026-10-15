"""One scan of the watchlist — shared by the scan tool, the daily digest and the background watch.

Fetch every watched board concurrently, keep the postings that pass the title / location / age
filters, log them in the first-seen log, and annotate each with what the coach needs to judge it:
how fresh it is, whether it's new since the last scan, ghost warnings, and whether the operator
already tracked or applied there (so a sweep never re-surfaces a role they've done).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from . import boards
from . import watchlist as wl
from .companies import company_key, role_key

ACTIVE = ("applied", "interviewing", "offer")


@dataclass
class ScanResult:
    matches: list[dict] = field(default_factory=list)  # filtered postings, freshest first
    new: list[dict] = field(default_factory=list)  # the subset never seen by an earlier scan
    scanned: int = 0  # boards that answered
    total: int = 0  # postings on those boards before filtering
    errors: list[str] = field(default_factory=list)  # "Company (ats/slug): reason"


def tracked_index(rows: list[dict]) -> tuple[dict, dict, dict]:
    """``(by_role, by_url, by_company)`` lookups over the tracker rows."""
    by_role, by_url, by_company = {}, {}, {}
    for r in rows:
        ck = company_key(r.get("company", ""))
        by_role[(ck, role_key(r.get("role", "")))] = r
        if r.get("source"):
            by_url[r["source"].rstrip("/")] = r
        by_company.setdefault(ck, []).append(r)
    return by_role, by_url, by_company


def annotate(job: dict, index: tuple[dict, dict, dict]) -> dict:
    """Stamp ``tracked`` (the tracker row for this exact role, if any), ``applied_here`` (how many
    roles at this company are applied / interviewing / offer) and ``ghost`` flags onto a job."""
    by_role, by_url, by_company = index
    ck = company_key(job.get("company", ""))
    row = by_url.get((job.get("url") or "").rstrip("/")) or by_role.get((ck, role_key(job.get("title", ""))))
    job["tracked"] = row.get("status", "tracked") if row else ""
    job["applied_here"] = sum(1 for r in by_company.get(ck, []) if r.get("status") in ACTIVE)
    job["ghost"] = wl.ghost_flags(job)
    return job


async def scan_watchlist(
    companies: list[dict],
    *,
    roles: str,
    exclude: str = "",
    locations: str = "",
    max_age_days: float = 0,
    tracker_rows: list[dict] | None = None,
    concurrency: int = 8,
) -> ScanResult:
    import httpx

    res = ScanResult()
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:

        async def _one(c: dict):
            async with sem:
                try:
                    return c, await boards.fetch_board(c["ats"], c["slug"], c["company"], client=client), ""
                except Exception as e:  # noqa: BLE001 — one dead board must not sink the scan
                    return c, [], f"{c['company']} ({c['ats']}/{c['slug']}): {e}"

        results = await asyncio.gather(*(_one(c) for c in companies))
    kept: list[dict] = []
    for c, jobs, err in results:
        if err:
            res.errors.append(err)
            continue
        res.scanned += 1
        res.total += len(jobs)
        for j in jobs:
            j["company"] = c["company"]  # the watchlist's spelling, not the board's
            if boards.passes(j, roles=roles, exclude=exclude, locations=locations, max_age_days=max_age_days):
                kept.append(j)
    new, _ = wl.record_sightings(kept)
    new_ids = {j["id"] for j in new}
    index = tracked_index(tracker_rows or [])
    for j in kept:
        j["new"] = j["id"] in new_ids
        annotate(j, index)
    kept.sort(key=lambda j: j.get("posted") or j.get("first_seen") or "", reverse=True)
    res.matches = kept
    res.new = [j for j in kept if j["new"]]
    return res


def format_job(j: dict, i: int | None = None) -> str:
    """One posting as two lines: title / company / location / posted age / comp / markers, then URL."""
    age = boards.age_days(j.get("posted", ""))
    bits = [f"{j.get('title', '?')} — {j.get('company', '?')}", j.get("location") or "location n/a"]
    bits.append(f"posted {int(age)}d ago" if age is not None else "posted ?")
    if j.get("comp"):
        bits.append(j["comp"])
    marks = []
    if j.get("new"):
        marks.append("NEW")
    if j.get("tracked"):
        marks.append(f"already tracked: {j['tracked']}")
    elif j.get("applied_here"):
        marks.append(f"you've applied to {j['applied_here']} role(s) here")
    if j.get("ghost"):
        marks.append("⚠ " + "; ".join(j["ghost"]))
    head = f"{i}. " if i is not None else "- "
    line = head + " · ".join(bits) + (f"  [{' | '.join(marks)}]" if marks else "")
    return f"{line}\n   {j.get('url', '')}"
