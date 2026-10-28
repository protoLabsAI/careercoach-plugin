"""The job-hunt tools — finding fresh roles, knowing who you know there, and the daily digest.

Registered from ``register()`` like the rest; kept out of ``__init__.py`` so that file stays the
wiring of the original surfaces. Every tool reads its defaults from the plugin config so the agent
can call them bare:

- ``target_roles``  which titles count (each comma-separated role is an all-words title match)
- ``title_exclude`` words that drop a title (intern, manager, …)
- ``locations``     where the role may be: places, plus ``remote`` (= remote and open to the US)
- ``max_age_days``  how old a posting may be
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import zipfile
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.careercoach")


DEFAULT_EXCLUDE = "intern, internship, new grad"  # the manifest default, for a host that doesn't merge it
MAX_EXPORT_BYTES = 50 * 1024 * 1024  # a Connections.csv is KBs; refuse anything absurd inside the zip


def _int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def filters(cfg) -> dict:
    """The scan filters as configured (the tools' defaults)."""
    exclude = cfg.get("title_exclude")
    return {
        "roles": cfg.get("target_roles", "") or "",
        "exclude": DEFAULT_EXCLUDE if exclude is None else exclude,
        "locations": cfg.get("locations", "") or "",
        "max_age_days": _int(cfg.get("max_age_days", 30), 30),
    }


def _guarded(fn):
    """A store file that can't be read (corrupt JSON) answers as a message, like the tracker tools,
    instead of raising through the turn — and nothing gets written over it."""
    from .profile import StoreUnreadable

    msg = "A Career Coach store file is unreadable, so I stopped: {}. Tell the operator; don't overwrite it."
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _a(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except StoreUnreadable as e:
                return msg.format(e)

        return _a

    @functools.wraps(fn)
    def _s(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StoreUnreadable as e:
            return msg.format(e)

    return _s


def register_hunt_tools(registry, cfg) -> None:
    from . import boards, contacts, scan, state
    from . import watchlist as wl

    def _companies(names: str) -> list[str]:
        return [n.strip() for n in (names or "").split(",") if n.strip()]

    @tool
    @_guarded
    async def careercoach_watchlist(action: str = "list", company: str = "", slug: str = "", ats: str = "",
                                    notes: str = "") -> str:  # fmt: skip
        """Manage the companies whose job boards you scan for fresh roles.
        action="list" shows them. action="add" adds one company or a comma-separated list: each is looked up on
        Ashby, Greenhouse and Lever automatically (pass `ats` + `slug` to pin a board you already know, e.g. the
        `anthropic` in boards.greenhouse.io/anthropic). action="remove" drops a company. Companies on Workday or
        their own careers site can't be scanned — research those with web search instead."""
        action = (action or "list").strip().lower()
        if action == "list":
            rows = wl.load_watchlist()
            if not rows:
                return "Your watchlist is empty. Add companies with careercoach_watchlist(action='add', company='A, B, C')."
            return f"{len(rows)} watched compan{'y' if len(rows) == 1 else 'ies'}:\n" + "\n".join(
                f"- {r['company']} — {r['ats']}/{r['slug']}" + (f" ({r['notes']})" if r.get("notes") else "")
                for r in rows
            )
        if action == "remove":
            names = _companies(company)
            if not names:
                return "Name the company to remove."
            gone = [n for n in names if wl.remove_company(n)]
            missing = [n for n in names if n not in gone]
            out = f"Removed {', '.join(gone)}." if gone else ""
            return (out + (f" Not on the watchlist: {', '.join(missing)}." if missing else "")).strip()
        if action != "add":
            return f"Unknown action {action!r} — use list, add or remove."
        names = _companies(company)
        if not names:
            return "Name the company (or a comma-separated list) to add."
        if ats or slug:
            if len(names) != 1 or not (ats and slug):
                return "To pin a board, pass exactly one company with both `ats` and `slug`."
            try:
                n = len(await boards.fetch_board(ats.strip().lower(), slug.strip(), names[0]))
            except Exception as e:  # noqa: BLE001
                return f"Couldn't read the {ats}/{slug} board: {e}. Check the slug (Ashby slugs are case-sensitive)."
            wl.add_company(names[0], ats.strip().lower(), slug.strip(), notes)
            return f"Watching {names[0]} — {ats}/{slug}, {n} open role(s) right now."
        import asyncio

        import httpx

        # one client and one concurrency cap for the whole batch — 60 companies × 3 ATSes × a few
        # slug spellings must not open hundreds of connections at once
        sem = asyncio.Semaphore(10)
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            found = await asyncio.gather(*(boards.probe(n, client=client, sem=sem) for n in names))
        added, missed, notes_out = [], [], []
        for name, (hits, problems) in zip(names, found, strict=True):
            notes_out += [f"{name}: {p}" for p in problems if "board, not" in p or "confirm it's" in p]
            if not hits:
                errs = [p for p in problems if "board, not" not in p and "confirm it's" not in p]
                missed.append(f"{name} (lookup errors: {errs[0]})" if errs else name)
                continue
            a, s, n = hits[0]
            wl.add_company(name, a, s, notes)
            extra = f"; also on {', '.join(f'{h[0]}/{h[1]}' for h in hits[1:])}" if len(hits) > 1 else ""
            added.append(f"- {name} — {a}/{s}, {n} open role(s){extra}")
        out = []
        if added:
            out.append(f"Watching {len(added)}:\n" + "\n".join(added))
        if notes_out:
            out.append("Check these:\n" + "\n".join(f"- {n}" for n in notes_out[:10]))
        if missed:
            out.append(
                f"Not found on Ashby, Greenhouse or Lever: {', '.join(missed)}. They may use Workday or their own "
                "site, or a different board name — find their careers page and pass `ats` + `slug` if it's one of the three."
            )
        return "\n\n".join(out)

    @tool
    @_guarded
    async def careercoach_scan_boards(companies: str = "", roles: str = "", locations: str = "",
                                      max_age_days: int = -1, only_new: bool = False, limit: int = 40) -> str:  # fmt: skip
        """Scan the watched companies' job boards for open roles that fit — the freshest source there is (the
        employer's own board, with the date each role first went up). Filters default to Settings → Career Coach
        (target roles, title exclusions, locations, max age); override with `roles` (comma-separated titles),
        `locations` ("remote" = remote and open to the US, or place names) or `max_age_days` (0 = any age).
        `companies` limits the scan to some watched companies. `only_new` shows just roles no earlier scan
        returned. Each result says how fresh it is, whether you already tracked or applied there, and any
        ghost-job warning. Track the ones worth pursuing with careercoach_track_application."""
        f = filters(cfg)
        roles = roles or f["roles"]
        if not roles.strip():
            return "No target roles to match — set Target roles in Settings → Career Coach, or pass `roles`."
        rows = wl.load_watchlist()
        if companies.strip():
            from .companies import company_key

            want = {company_key(c) for c in _companies(companies)}
            rows = [r for r in rows if company_key(r["company"]) in want]
        if not rows:
            return "Nothing to scan — add companies with careercoach_watchlist(action='add', company='…')."
        age = f["max_age_days"] if max_age_days < 0 else max_age_days
        res = await scan.scan_watchlist(
            rows, roles=roles, exclude=f["exclude"], locations=locations or f["locations"], max_age_days=age,
            tracker_rows=state.load_applications(),
        )  # fmt: skip
        from . import jobsource

        wl.add_to_inbox(
            [
                {**j, "score": jobsource.best_prescore(j, roles), "reason": wl.match_reason(j, roles)}
                for j in res.matches
                if not j.get("tracked")
            ]
        )
        jobs = res.new if only_new else res.matches
        head = (
            f"Scanned {res.scanned} board(s), {res.total} open roles → {len(res.matches)} match "
            f"(roles: {roles}; locations: {locations or f['locations'] or 'any'}; "
            f"{'any age' if not age else f'≤ {age} days old'}); {len(res.new)} new since the last scan."
        )
        if res.errors:
            head += "\nCouldn't read: " + "; ".join(res.errors[:5])
        if not jobs:
            return head + "\nNo matching roles" + (" that are new." if only_new else ".")
        lines = [scan.format_job(j, i) for i, j in enumerate(jobs[: max(1, min(limit, 100))], 1)]
        more = f"\n…and {len(jobs) - len(lines)} more (raise `limit`)." if len(jobs) > len(lines) else ""
        return head + "\n\n" + "\n".join(lines) + more

    @tool
    @_guarded
    def careercoach_import_history(entries: str) -> str:
        """Bring past applications the tracker never saw (from a notes file, a spreadsheet, an email search)
        into the pipeline, so sweeps stop re-suggesting roles already applied to. `entries` is a JSON list of
        objects: {"company", "role", "status" (default "applied"), "date" (YYYY-MM-DD, optional), "notes",
        "source" (posting URL, optional)}. Extract them from the source yourself first — one object per role,
        company and role as separate fields, skipping lines that aren't applications (interviews, to-dos).
        A role already tracked keeps its status; only its blank fields are filled."""
        try:
            data = json.loads(entries)
        except json.JSONDecodeError as e:
            return f"`entries` isn't valid JSON ({e}). Pass a JSON list of {{company, role, status, date}} objects."
        if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
            return "`entries` must be a JSON list of objects."
        try:
            res = state.import_history(data)
        except state.StoreUnreadable as e:
            return f"Not imported: {e}"
        try:
            registry.emit("application_tracked", {"imported": res["added"]})
        except Exception:  # noqa: BLE001
            pass
        out = f"Imported {res['added']} new role(s), filled in {res['updated']}; {res['total']} in your pipeline now."
        if res["skipped"]:
            out += f"\nSkipped {len(res['skipped'])}: " + "; ".join(res["skipped"][:10])
        return out

    def _resolve(path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            root = Path(cfg.get("packet_root", "") or "~/CareerCoach").expanduser()
            p = root / p
        return p

    @tool
    @_guarded
    def careercoach_import_connections(path: str) -> str:
        """Import the operator's LinkedIn connections so the coach can find referrals. The operator downloads them
        from LinkedIn (Settings → Data privacy → Get a copy of your data → Connections; it arrives by email as a
        .zip) — never scrape LinkedIn. `path` is the Connections.csv or the export .zip (absolute, ~/…, or relative
        to the role-packet workspace). Re-importing a newer export updates people who changed jobs."""
        p = _resolve(path)
        if not p.exists():
            return f"No file at {p}. Download the export from LinkedIn first, then pass its path."
        try:
            if p.suffix.lower() == ".zip":
                with zipfile.ZipFile(p) as z:
                    info = next((i for i in z.infolist() if i.filename.lower().endswith("connections.csv")), None)
                    if info is None:
                        return f"{p.name} has no Connections.csv — request the Connections data in the export."
                    if info.file_size > MAX_EXPORT_BYTES:
                        return (
                            f"Connections.csv in {p.name} is {info.file_size:,} bytes — that isn't a LinkedIn export."
                        )
                    text = z.read(info).decode("utf-8-sig", errors="replace")
            else:
                text = p.read_text(encoding="utf-8-sig", errors="replace")
            people = contacts.parse_linkedin_connections(text)
            added, updated = contacts.merge_contacts(people)
        except (ValueError, contacts.StoreUnreadable) as e:
            return f"Not imported: {e}"
        except (OSError, zipfile.BadZipFile) as e:
            return f"Couldn't read {p.name}: {e}. Pass LinkedIn's Connections.csv or the export .zip."
        companies = len({c["company"] for c in people if c.get("company")})
        return (
            f"Imported {len(people)} connection(s) across {companies} companies ({added} new, {updated} updated). "
            "Ask careercoach_find_referrals who you know at a company."
        )

    @tool
    @_guarded
    def careercoach_find_referrals(company: str = "") -> str:
        """Who the operator knows at a company — the referral path, which converts far better than a cold
        application. With no `company`, checks every watched company and every role in the pipeline that's still
        being considered. Draft the ask yourself: short, specific about the role, leading with proof of work."""
        try:
            people = contacts.load_contacts()
        except contacts.StoreUnreadable as e:
            return f"Contacts unreadable: {e}"
        if not people:
            return (
                "No contacts yet. Ask the operator for their LinkedIn Connections export and run "
                "careercoach_import_connections, or log people with careercoach_log_contact."
            )
        targets = _companies(company) or sorted(
            {r["company"] for r in wl.load_watchlist()}
            | {r["company"] for r in state.load_applications() if r.get("status") == "considering"}
        )
        out = []
        for t in targets:
            hits = contacts.at_company(t, people)
            if hits:
                out.append(
                    f"{t}:\n"
                    + "\n".join(
                        f"  - {c['name']} — {c.get('title') or 'title n/a'}"
                        + (f" · {c['url']}" if c.get("url") else "")
                        for c in hits
                    )
                )
        if not out:
            return f"No contacts at {', '.join(targets[:10]) or 'those companies'}."
        return "\n".join(out)

    @tool
    @_guarded
    def careercoach_log_contact(name: str, company: str = "", note: str = "", next_action: str = "", due: str = "",
                                title: str = "", url: str = "") -> str:  # fmt: skip
        """Log a networking touch (message sent, intro asked, call held) and the next step, so follow-ups don't
        slip. `due` is a YYYY-MM-DD date for `next_action`; due follow-ups appear in the daily digest. Creates
        the contact if they're new."""
        try:
            c = contacts.log_touch(name, company, note=note, next_action=next_action, due=due, title=title, url=url)
        except ValueError:
            return f"`due` must be a date like 2026-10-22, not {due!r}."
        except contacts.StoreUnreadable as e:
            return f"Not logged: {e}"
        nxt = f" Next: {c['next_action']} (due {c['due']})." if c.get("due") else ""
        return f"Logged {c['name']}" + (f" @ {c['company']}" if c.get("company") else "") + "." + nxt

    @tool
    @_guarded
    async def careercoach_digest(days: int = 3) -> str:
        """The daily job-hunt digest: roles first posted in the last `days` days on watched boards that fit and
        aren't tracked yet (freshest first — early applications get read), follow-ups due with contacts,
        applications gone quiet for two weeks, and the pipeline at a glance. Run it each morning (schedule it)."""
        from datetime import UTC, datetime

        sections = []
        shown_new: list[dict] = []
        rows = state.load_applications()
        watched = wl.load_watchlist()
        f = filters(cfg)
        if watched and f["roles"].strip():
            res = await scan.scan_watchlist(
                watched, roles=f["roles"], exclude=f["exclude"], locations=f["locations"],
                max_age_days=max(1, days), tracker_rows=rows,
            )  # fmt: skip
            fresh = [j for j in res.matches if not j["tracked"]]
            shown_new = fresh[:25]
            title = f"## New roles (posted ≤ {days}d, {len(fresh)} of {len(res.matches)} untracked)"
            body = "\n".join(scan.format_job(j, i) for i, j in enumerate(fresh[:25], 1)) or "Nothing new today."
            if res.errors:
                body += "\nCouldn't read: " + "; ".join(res.errors[:5])
            sections.append(f"{title}\n{body}")
        else:
            sections.append(
                "## New roles\nNo watchlist or target roles yet — add companies with careercoach_watchlist and set "
                "Target roles in Settings."
            )
        # Peek now, drop what was SHOWN only once the digest has rendered — a digest that fails
        # half-way, or one that shows 25 of 60, must not lose the rest.
        index = scan.tracked_index(rows)
        listed = {j["id"] for j in shown_new}
        queued = wl.peek_inbox()
        stale = [j["id"] for j in queued if j.get("id") in listed or scan.annotate(dict(j), index)["tracked"]]
        inbox = [j for j in queued if j.get("id") not in set(stale)]
        shown_inbox = inbox[:25]
        if inbox:
            sections.append(
                f"## From the background watch ({len(inbox)} queued)\n"
                + "\n".join(scan.format_job(j, i) for i, j in enumerate(shown_inbox, 1))
                + (f"\n…{len(inbox) - 25} more stay queued for the next digest." if len(inbox) > 25 else "")
            )
        try:
            due = contacts.due_followups()
        except contacts.StoreUnreadable:
            due = []
        if due:
            sections.append(
                "## Follow-ups due\n"
                + "\n".join(
                    f"- {c['name']} ({c.get('company') or '?'}): {c.get('next_action') or 'follow up'} — due {c['due']}"
                    for c in due
                )
            )
        today = datetime.now(UTC).date()
        quiet = []
        for r in rows:
            if r.get("status") != "applied":
                continue
            try:
                idle = (today - datetime.fromisoformat(r.get("updated") or r.get("date")).date()).days
            except (TypeError, ValueError):
                continue
            if idle >= 14:
                quiet.append((idle, r))
        if quiet:
            quiet.sort(key=lambda x: -x[0])
            sections.append(
                f"## Gone quiet ({len(quiet)} applied ≥ 14 days, no update)\n"
                + "\n".join(f"- {r['role']} @ {r['company']} — {d} days" for d, r in quiet[:10])
                + "\nFollow up with a contact there, or mark them rejected/passed to keep the pipeline honest."
            )
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        sections.append("## Pipeline\n" + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"))
        out = f"# Job-hunt digest — {today.isoformat()}\n\n" + "\n\n".join(sections)
        wl.drop_from_inbox([j["id"] for j in shown_inbox] + stale)
        return out

    registry.register_tools(
        [
            careercoach_watchlist,
            careercoach_scan_boards,
            careercoach_import_history,
            careercoach_import_connections,
            careercoach_find_referrals,
            careercoach_log_contact,
            careercoach_digest,
        ]
    )
