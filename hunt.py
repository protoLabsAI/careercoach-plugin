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
    """Settings take precedence; blank search fields use confirmed search preferences.

    Identity location is a home location, never an implicit restriction on job searches.
    """
    from . import profile

    roles = cfg.get("target_roles", "") or ""
    locations = cfg.get("locations", "") or ""
    if not roles.strip() or not locations.strip():
        saved, error = profile.load_profile_checked()
        if error:
            raise profile.StoreUnreadable(profile._path(), error)
        identity = saved.get("identity", {})
        roles = roles if roles.strip() else identity.get("target_roles", "")
        locations = locations if locations.strip() else identity.get("target_locations", "")
    exclude = cfg.get("title_exclude")
    return {
        "roles": roles,
        "exclude": DEFAULT_EXCLUDE if exclude is None else exclude,
        "locations": locations,
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


def register_hunt_tools(registry, cfg, extended: bool):
    """Register the job-hunt tools and return ``careercoach_scan_boards`` (the watchlist-board scan)
    so ``careercoach_find_roles`` can reuse it. The default surface keeps watchlist, log_contact and
    digest; scan_boards is reached through careercoach_find_roles(source="watched"); the past-apps,
    LinkedIn-connections and referral importers register only behind ``extended_tools``."""
    from . import boards, contacts, scan, state
    from . import watchlist as wl

    def _companies(names: str) -> list[str]:
        return [n.strip() for n in (names or "").split(",") if n.strip()]

    @tool
    @_guarded
    async def careercoach_watchlist(action: str = "list", company: str = "", slug: str = "", ats: str = "",
                                    notes: str = "") -> str:  # fmt: skip
        """Manage the companies whose job boards are scanned. action: "list"; "add" (one or a comma-separated list,
        auto-found on Ashby/Greenhouse/Lever, or pin one with `ats` + `slug`); "remove". Workday and custom careers
        sites can't be scanned — use web search."""
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
        """Scan the watched companies' own job boards (the freshest source) for fitting roles. Filters default to
        Settings; override with `roles` (comma-separated titles), `locations` ("remote" = remote and US-open, or
        places), `max_age_days` (0 = any age). `companies` limits to some watched ones; `only_new` shows only roles no
        earlier scan returned. Results flag freshness, already tracked/applied, and ghost-job warnings."""
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

        jobs = res.new if only_new else res.matches
        shown = jobs[: max(1, min(limit, 100))]
        # Feed the inbox so the standalone view (S1c) reflects what manual scans surfaced too — but
        # don't let the daily digest re-raise a role the operator just saw here. Queue only the
        # genuinely new roles (res.new, logged in seen.json, so an evicted row is never re-queued on
        # the next scan), and mark only the displayed slice as already surfaced so
        # peek_inbox / the digest skip them. Best-effort: a corrupt inbox must not lose scan results.
        try:
            wl.add_to_inbox(
                [
                    {**j, "score": jobsource.best_prescore(j, roles), "reason": wl.match_reason(j, roles)}
                    for j in res.new
                    if not j.get("tracked")
                ]
            )
            wl.drop_from_inbox([j["id"] for j in shown if not j.get("tracked")])
        except wl.StoreUnreadable as e:
            log.warning("[careercoach] scan results did not reach the inbox: %s", e)
        head = (
            f"Scanned {res.scanned} board(s), {res.total} open roles → {len(res.matches)} match "
            f"(roles: {roles}; locations: {locations or f['locations'] or 'any'}; "
            f"{'any age' if not age else f'≤ {age} days old'}); {len(res.new)} new since the last scan."
        )
        if res.errors:
            head += "\nCouldn't read: " + "; ".join(res.errors[:5])
        if not jobs:
            return head + "\nNo matching roles" + (" that are new." if only_new else ".")
        lines = [scan.format_job(j, i) for i, j in enumerate(shown, 1)]
        more = f"\n…and {len(jobs) - len(lines)} more (raise `limit`)." if len(jobs) > len(lines) else ""
        return head + "\n\n" + "\n".join(lines) + more

    @tool
    @_guarded
    def careercoach_import_history(entries: str) -> str:
        """Import past applications the tracker never saw, so sweeps stop re-suggesting them. `entries`: JSON list, one
        object per role: {company, role, status (default "applied"), date (YYYY-MM-DD), notes, source}; extract them
        yourself and skip non-applications. A tracked role keeps its status; only blank fields fill."""
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
        """Import the operator's LinkedIn connections for referrals from their own export (Settings → Data privacy → Get
        a copy of your data → Connections) — never scrape LinkedIn. `path`: Connections.csv or the export .zip
        (absolute, ~/…, or workspace-relative)."""
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
        """Who the operator knows at `company` (default: every watched company and considering role) — a referral beats a
        cold application."""
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
        """Log a networking touch and the next step; `due` (YYYY-MM-DD) puts the follow-up in the digest. Creates the
        contact if new."""
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
        """Daily digest: untracked fitting roles posted in the last `days` on watched boards, contact follow-ups due,
        applications quiet for two weeks, and the pipeline at a glance."""
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

    # Default surface: the watchlist, the daily digest and contact logging stay first-class;
    # careercoach_find_roles(source="watched") is the default way to the board scan.
    registry.register_tools([careercoach_watchlist, careercoach_log_contact, careercoach_digest])
    if extended:  # legacy names behind extended_tools — same implementations
        registry.register_tools(
            [
                careercoach_scan_boards,
                careercoach_import_history,
                careercoach_import_connections,
                careercoach_find_referrals,
            ]
        )
    return careercoach_scan_boards
