"""Career Coach — a protoAgent showcase plugin.

A career coach and job-hunt copilot. It coaches (strategy, mock interviews, honest
CV feedback, offer evaluation, salary negotiation, decisions) and, when you ask, it
executes (evaluate a posting, tailor the CV + cover letter, prep the interview).

This repo is a **deep-dive reference** for the plugin system — it exercises the full
contribution surface in one place:

  • SKILL.md skills (progressive disclosure, with sub-files)  → skills/            (auto-loaded)
  • a static-DAG workflow (the autonomous "apply" pipeline)   → workflows/apply.yaml (auto-loaded)
  • a delegate subagent crew (research → evaluate → write)    → register_subagent
  • agent tools + a tunable Knobs control surface (the rubric)→ register_tools + graph.sdk.Knobs
  • a live job source (JSearch / Remotive)                    → careercoach_search_jobs + jobsource.py
  • an opt-in background job-watch + a goal verifier          → register_surface + supervise · register_goal_verifier
  • a console rail view (the Career Coach dashboard)          → register_router + manifest views:
  • config / secrets / Settings fields                        → manifest + registry.config (ADR 0019)
  • event-bus topics (rail notification dots)                 → registry.emit (ADR 0039)

Adapted, with credit, from Mads Lorentzen's ``ai-job-search`` (MIT) — see CREDITS.md.
The prompt-engineering IP (fit rubric, writing-style discipline, upskill gap analysis)
is ported; the LaTeX toolchain is replaced by a print-correct HTML artifact (the resume skill); and the
whole thing is reframed around coaching.

Host imports (``graph.*``) stay **lazy and guarded** so the plugin loads and its tests
run with no protoAgent present (the host-free testkit stubs the host and the guards let
the core register anyway).
"""

from __future__ import annotations

import hashlib
import json as _json
import logging

from langchain_core.tools import tool

# NOTE: relative imports (`from . import state`) are kept LAZY, inside the functions that
# use them — a standalone plugin's root __init__.py is imported *bare* by pytest during
# collection (not as a package), and a top-level relative import fails there. The host-free
# testkit loads this as a real package, so the lazy imports resolve fine at register() time.

log = logging.getLogger("protoagent.plugins.careercoach")


def register(registry) -> None:
    """Entry point (ADR 0018) — called once at load with a PluginRegistry.

    The DEFAULT tool surface is lean (S4b): ~12 merged tools around the core loop — find roles →
    evaluate → apply kit → track → interview prep. Each pre-consolidation ("legacy") tool name stays
    reachable behind ``extended_tools`` (default off), registered as a thin wrapper over the SAME
    implementation, so existing schedules and prompts that name the old tools keep working. One
    always-on tool is NOT a legacy wrapper: ``careercoach_read_sources`` — a read-only view the
    delegate crew grounds on (see ``_register_delegate_read_tool``). The browser-fill tools (plan_fill,
    verify_fill, handoff, request_submit) register only when ``browser_fill`` (S3a) enables at least one
    ATS; the submit-gate middleware is unchanged."""
    cfg = registry.config  # this plugin's resolved config + secrets (ADR 0019)
    extended = _as_bool(cfg.get("extended_tools", False))
    from .ats import any_fill_enabled

    fill_on = any_fill_enabled(cfg.get("browser_fill", "") or "")

    _register_tracker_tools(registry, extended)
    _register_apply_kit_tool(registry)
    search_jobs = _register_jobsearch_tool(registry, cfg, extended)
    scan_boards = _register_hunt_tools(registry, cfg, extended)
    _register_find_roles_tool(registry, scan_boards, search_jobs)
    get_profile, read_profile = _register_packet_tools(registry, cfg, extended)
    get_answers = _register_answers_tools(registry, extended)
    # The delegate crew reads the verified record through this one read-only tool, so it can ground a
    # score or a draft in the DEFAULT config without holding any write-capable tool (see below).
    _register_delegate_read_tool(registry, get_profile, read_profile, get_answers)
    if fill_on:  # browser fill is opt-in per ATS (S3a); off → the apply kit is the path
        _register_formfill_tools(registry)
    _register_prepare_tool(registry)
    _register_submit_gate(registry, fill_on)
    if fill_on:
        _register_handoff(registry)
    _register_rubric_knobs(registry)
    _register_subagents(registry)
    _register_profile_middleware(registry)
    _register_views(registry, cfg)
    _register_job_watch(registry, cfg)

    # skills/ and workflows/ auto-load from their conventional dirs — no call needed.
    log.info(
        "[careercoach] registered: lean default surface (find_roles + profile + track + answers + "
        "role_packet + apply kit + digest + watchlist + log_contact), browser-fill tools %s, legacy "
        "surface %s, submit gate, rubric knobs, crew, profile injection, dashboard, watch",
        "on" if fill_on else "off",
        "on" if extended else "off",
    )


def _as_bool(value) -> bool:
    """Config booleans can arrive as real bools or strings (env/UI). Normalize."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ── agent tools: the application tracker ──────────────────────────────────────
INBOX_LIST_CAP = 30  # enough to triage in one turn; the view shows the rest


def _promote_inbox_item(item: dict) -> dict:
    """Keep an inbox card: a `considering` pipeline row carrying its reason and posting evidence. An
    existing application is left exactly as it is. The card's score is a keyword title match, not a
    fit evaluation, so `fit_score` stays 0 (unscored) until the role is actually evaluated."""
    from . import state
    from . import watchlist as wl

    row = state.track_application(
        company=item.get("company") or "",
        role=item.get("title") or "",
        status="considering",
        notes=item.get("reason") or "",
        source=item.get("url") or "",
        posting={k: item.get(k) for k in ("title", "location", "snippet", "url", "posted")},
        insert_only=True,
    )
    wl.mark_kept(item["id"])
    return row


def _inbox_card(inbox_id: str, company: str, role: str) -> dict | None:
    """The inbox card named by id, or by company + role. Raises on an unreadable inbox."""
    from . import watchlist as wl
    from .companies import company_key, role_key

    cards = wl.inbox_for_view()
    wanted = (inbox_id or "").strip()
    if wanted:
        return next((j for j in cards if j.get("id") == wanted), None)
    key = (company_key(company or ""), role_key(role or ""))
    return next((j for j in cards if (company_key(j.get("company", "")), role_key(j.get("title", ""))) == key), None)


def _register_tracker_tools(registry, extended: bool) -> None:
    from . import state
    from . import watchlist as wl

    @tool
    def careercoach_track_application(
        company: str,
        role: str,
        fit_score: int = 0,
        status: str = "considering",
        notes: str = "",
        source: str = "",
    ) -> str:
        """Record or update a job in the pipeline: company, role, 0-100 fit score, status
        (considering/preparing/applied/interviewing/offer/rejected/passed), notes, and `source` (posting URL)."""
        try:
            row = state.track_application(
                company=company, role=role, fit_score=fit_score, status=status, notes=notes, source=source
            )
        except state.StoreUnreadable as e:
            return f"Not tracked: {e}"
        warn = ""
        active = state.active_at_company(company)
        if status in state.ACTIVE and len(active) > state.COMPANY_CAP:
            warn = (
                f"\nHeads-up: that's {len(active)} live applications at {company} "
                f"({'; '.join(r['role'] for r in active[:5])}). Recruiters see them all in one ATS, and many "
                "cap repeat applicants — keep it to the best-fit roles there and ask a contact for a referral instead."
            )
        try:
            registry.emit("application_tracked", {"company": company, "role": role, "status": status})
        except Exception:  # noqa: BLE001 — the event bus is best-effort chrome
            pass
        n = len(state.load_applications())
        return (
            f"Tracked {row['role']} @ {row['company']} (fit {row.get('fit_score', 0)}, {status}). "
            f"{n} role(s) in your pipeline.{warn}"
        )

    @tool
    def careercoach_list_applications(status: str = "") -> str:
        """List the jobs in the pipeline, optionally filtered by status."""
        rows, err = state.load_applications_checked(status=status or None)
        if err:
            return f"Your pipeline file {state._path()} is unreadable ({err}), so I can't list it — tell the operator."
        if not rows:
            return (
                'Your pipeline is empty — paste me a posting, or track a role with careercoach_track(action="track").'
            )
        return "\n".join(
            f"- {r['role']} @ {r['company']} — fit {r.get('fit_score', 0)}, {r.get('status', '?')}, {r.get('date', '?')}"
            + (f" · {r['source']}" if r.get("source") else "")
            for r in rows
        )

    @tool
    def careercoach_track(
        action: str = "track",
        company: str = "",
        role: str = "",
        fit_score: int = 0,
        status: str = "",
        notes: str = "",
        source: str = "",
        app_id: str = "",
        inbox_id: str = "",
    ) -> str:
        """Manage your application pipeline and the new-roles inbox. action: "track" (record or update a
        role — company, role, 0-100 fit_score, status considering/preparing/applied/interviewing/offer/
        rejected/passed, notes, source posting URL; tracking a role clears its inbox card); "list" (show
        the pipeline, optional `status` filter); "move" (move an application to `status` — name it by
        `app_id`, or by `company` + `role`); "inbox" (show new roles waiting for a decision); "keep" (add
        an inbox card to the pipeline) and "dismiss" (drop a card for good) — name the card by `inbox_id`,
        or by `company` + `role`. Defaults to "track"."""
        act = (action or "track").strip().lower()
        if act == "inbox":
            try:
                cards = wl.inbox_for_view()
            except wl.StoreUnreadable as e:
                return f"Can't read the inbox: {e}"
            if not cards:
                return "The inbox is empty: no new roles are waiting for a decision."
            lines = [
                f"- {j.get('title', '')} @ {j.get('company', '')} — score {j.get('score', 0)}, "
                f"{j.get('location') or 'location unknown'}, inbox_id: {j.get('id', '')}"
                for j in cards[:INBOX_LIST_CAP]
            ]
            more = len(cards) - len(lines)
            tail = f"\n{more} more in the Career Coach view." if more > 0 else ""
            return f"{len(cards)} new role(s), newest first:\n" + "\n".join(lines) + tail
        if act in ("keep", "dismiss"):
            try:
                card = _inbox_card(inbox_id, company, role)
                if card is None:
                    named = inbox_id or " / ".join(x for x in (company, role) if x) or "nothing"
                    return f'No inbox card matches {named}. Use action="inbox" to see what is waiting.'
                label = f"{card.get('title', '')} @ {card.get('company', '')}"
                if act == "dismiss":
                    wl.dismiss([card["id"]])
                    event = ("inbox_changed", {"action": "dismiss", "id": card["id"]})
                    reply = f"Dismissed {label}. It won't come back."
                else:
                    row = _promote_inbox_item(card)
                    event = (
                        "application_tracked",
                        {
                            "company": row.get("company", ""),
                            "role": row.get("role", ""),
                            "status": row.get("status", ""),
                        },
                    )
                    reply = f"Kept {label}: it's in the pipeline as {row.get('status', 'considering')}."
            except (state.StoreUnreadable, wl.StoreUnreadable) as e:
                return f"Not changed: {e}"
            try:
                registry.emit(*event)
            except Exception:  # noqa: BLE001 — the event bus is best-effort chrome
                pass
            return reply
        if act == "track":
            return careercoach_track_application.invoke(
                {
                    "company": company,
                    "role": role,
                    "fit_score": fit_score,
                    "status": status or "considering",
                    "notes": notes,
                    "source": source,
                }
            )
        if act == "list":
            return careercoach_list_applications.invoke({"status": status})
        if act == "move":
            aid = (app_id or "").strip()
            try:
                if not aid:
                    row0 = state._find(state.load_applications(), (company or "").strip(), (role or "").strip())
                    if row0 is None:
                        return "To move a role, give its app_id (or the company and role) — none matched the pipeline."
                    aid = row0["id"]
                row = state.move(aid, (status or "").strip())
            except state.StoreUnreadable as e:
                return f"Not moved: {e}"
            except ValueError as e:
                return str(e)
            except KeyError as e:
                return str(e.args[0]) if e.args else str(e)
            try:
                registry.emit(
                    "application_tracked",
                    {"company": row.get("company", ""), "role": row.get("role", ""), "status": row.get("status", "")},
                )
            except Exception:  # noqa: BLE001 — the event bus is best-effort chrome
                pass
            return f"Moved {row.get('role', '')} @ {row.get('company', '')} to {row.get('status', '?')}."
        return f'Unknown action {action!r} — use "track", "list", "move", "inbox", "keep" or "dismiss".'

    registry.register_tool(careercoach_track)
    if extended:  # legacy names, reachable behind extended_tools — same implementations
        registry.register_tools([careercoach_track_application, careercoach_list_applications])


# ── agent tool: the apply kit (the default apply path — the coach prepares, the operator files) ─
# The settled default is an APPLY KIT: the coach readies the tailored résumé, a cover letter when
# the posting asks for one, and every form answer; the operator fills the form themselves. Nothing
# auto-submits. The kit lives on the application row (state.set_kit) so what was prepared stays with
# the application. One-off answers are stored as DRAFT and never copied into a confirmed field.
def _parse_kit_answers(src: str | dict | None) -> tuple[list[dict], str | None]:
    """Parse a one-off-answers source into kit DRAFT entries. ``src`` is a JSON object STRING, an
    object/dict passed through directly, or empty/None. Returns ``(entries, error)``: on a non-empty
    result, ``entries`` are ``[{key, label, value, status: "draft"}]``; on bad input, ``error`` is a
    ready-to-return refusal and the caller writes nothing. Accepting a dict (not only a string) is
    why S5c's answers were dropped — a model that passed an object got an empty kit."""
    if src is None:
        return [], None
    if isinstance(src, str):
        text = src.strip()
        if not text:
            return [], None
        try:
            data = _json.loads(text)
        except ValueError as e:
            return (
                [],
                f'Apply kit not saved: answers_json isn\'t valid JSON ({e}). Pass an object like {{"Why us?": "..."}}.',
            )
    elif isinstance(src, dict):
        data = src
    else:
        return [], "Apply kit not saved: answers_json must be a JSON object mapping a form question to an answer."
    if not isinstance(data, dict):
        return [], "Apply kit not saved: answers_json must be a JSON object mapping a form question to an answer."
    entries = [
        {"key": str(k), "label": str(k), "value": "" if v is None else str(v), "status": "draft"}
        for k, v in data.items()
    ]
    return entries, None


def _join_plain(items: list[str]) -> str:
    """Join a short list into plain prose: ``a``; ``a and b``; ``a, b and c``."""
    items = [i for i in items if i]
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _register_apply_kit_tool(registry) -> None:
    from . import state

    @tool
    def careercoach_save_apply_kit(
        company: str,
        role: str,
        resume_path: str | None = None,
        resume_version: str | None = None,
        cover_letter: str | None = None,
        cover_letter_needed: bool | None = None,
        answers_json: str | dict | None = "",
        no_form_questions: bool | None = None,
        answers: str | dict | None = None,
    ) -> str:
        """Save the apply kit for one role onto its pipeline row and move a Saved row to `preparing`. Call this LAST,
        once all three parts exist — in order: (1) render the tailored résumé with careercoach_render_resume and keep
        the PDF path it returns; (2) write the cover letter if the posting asks for one; (3) collect the form answers
        with careercoach_answers — THEN call this with all three. The operator files the form themselves; nothing is
        submitted.

        Arg shapes: `resume_path` is the filesystem path to the rendered résumé PDF and `resume_version` its label;
        `cover_letter` is the letter TEXT (pass `cover_letter_needed=false` when the posting asks for no letter);
        `answers_json` is a JSON object (or a dict) mapping each form question to its answer, e.g. {"Why us?": "..."},
        stored as DRAFT (shown to the operator, never used as a confirmed answer) — `answers` is an accepted alias for
        it; set `no_form_questions=true` when the posting has no form questions. Tracks the role as `preparing` if it
        isn't in the pipeline yet. Omitted or null résumé/cover-letter fields preserve saved values; pass an empty
        string to clear a field or false to clear the cover-letter-needed flag.

        The saved kit is validated: a complete kit (résumé PDF that exists on disk, a cover letter or
        cover_letter_needed=false, and at least one answer or no_form_questions=true) is reported saved; a partial or
        empty kit is saved as a DRAFT that names what is still missing and how to finish it — it is never an error."""
        company_s, role_s = (company or "").strip(), (role or "").strip()
        # `answers_json` is the documented name; `answers` is an accepted alias (a model that names the
        # tool's one-off answers "answers" shouldn't silently drop them). Prefer whichever is non-empty,
        # answers_json first. Both accept a JSON string OR an object/dict passed through directly.
        src = answers_json
        if (src is None or (isinstance(src, str) and not src.strip())) and answers is not None:
            src = answers
        answer_entries, answers_err = _parse_kit_answers(src)
        if answers_err:
            return answers_err
        try:
            row = state._find(state.load_applications(), company_s, role_s)
            if row is None:
                # not in the pipeline yet — track it as `preparing` so the kit has a row to live on
                row = state.track_application(company=company_s, role=role_s, status="preparing")
            app_id = row["id"]
            parts: dict = {}
            if resume_path is not None or resume_version is not None:
                parts["resume"] = {"built": state._today()}
                if resume_path is not None:
                    parts["resume"]["path"] = resume_path.strip()
                if resume_version is not None:
                    parts["resume"]["version"] = resume_version.strip()
            if cover_letter is not None or cover_letter_needed is not None:
                parts["cover_letter"] = {}
                if cover_letter is not None:
                    parts["cover_letter"]["text"] = cover_letter
                if cover_letter_needed is not None:
                    parts["cover_letter"]["needed"] = cover_letter_needed
            if answer_entries:
                parts["answers"] = answer_entries
            if no_form_questions is not None:  # omitted → preserved; set → stored on the kit
                parts["no_form_questions"] = no_form_questions
            saved = state.set_kit(app_id, **parts)
            # a Saved (considering) row is now being readied → move it to preparing with a history entry
            if saved.get("status") == "considering":
                saved = state.move(app_id, "preparing")
        except state.StoreUnreadable as e:
            return f"Apply kit not saved: {e}"
        except Exception as e:  # noqa: BLE001 — a tool never raises
            return f"Apply kit not saved: {e}"
        try:
            registry.emit(
                "application_tracked",
                {"company": saved.get("company", ""), "role": saved.get("role", ""), "status": saved.get("status", "")},
            )
        except Exception:  # noqa: BLE001 — the event bus is best-effort chrome
            pass
        who = f"{saved.get('role', role_s)} @ {saved.get('company', company_s)}"
        kit = saved.get("kit") or {}
        missing = kit.get("missing") or []
        if kit.get("status") == "complete" and not missing:
            pieces: list[str] = []
            if kit.get("resume"):
                v = kit["resume"].get("version", "")
                pieces.append(f"résumé {v}" if v else "résumé")
            cover = kit.get("cover_letter") or {}
            if str(cover.get("text") or "").strip():
                pieces.append("cover letter")
            elif cover.get("needed") is False:
                pieces.append("no cover letter (not requested)")
            if kit.get("answers"):
                pieces.append(f"{len(kit['answers'])} draft answer(s)")
            elif kit.get("no_form_questions"):
                pieces.append("no form questions")
            what = ", ".join(pieces) or "kit"
            return (
                f"Saved the apply kit for {who} ({what}; status {saved.get('status', '?')}). "
                "Draft answers are shown to the operator and never fill a form until confirmed — the operator files "
                "the application themselves."
            )
        steps: list[str] = []
        for part in missing:
            if part == "résumé (no path)":
                steps.append("render the résumé with careercoach_render_resume and pass its PDF path")
            elif part.startswith("résumé (file not found: "):
                steps.append(
                    "no file exists at that résumé path; re-render with careercoach_render_resume "
                    "and pass the real PDF path"
                )
            elif part == "cover letter":
                steps.append("write the cover letter (or set cover_letter_needed=false)")
            elif part == "answers":
                steps.append("collect answers with careercoach_answers (or set no_form_questions=true)")
        return (
            f"Apply kit saved as a DRAFT — not ready: {who} still needs {_join_plain(missing)}. "
            f"Next steps: {'; '.join(steps)}. Then save again."
        )

    registry.register_tool(careercoach_save_apply_kit)


# ── agent tool: live job search (the real job source, Phase 3) ────────────────
# The default surface reaches this through careercoach_find_roles(source="market"); the standalone
# name registers behind extended_tools. Either way the returned tool is handed to find_roles so the
# merged tool reuses this code path as-is.
def _register_jobsearch_tool(registry, cfg, extended: bool):
    @tool
    async def careercoach_search_jobs(
        query: str, location: str = "", remote: bool = False, limit: int = 10, source: str = ""
    ) -> str:
        """Search live job postings for `query` (title, skill or role), optionally by `location` or `remote`-only.
        `source` (default: Settings): "himalayas" (keyless remote board), "hn" (this month's HN 'Who is hiring?'),
        "jsearch" (Google for Jobs; needs an API key). For watched companies, careercoach_scan_boards is fresher."""
        from . import jobsource

        try:
            result = await jobsource.search_jobs(
                query,
                location=location,
                remote=remote,
                limit=limit,
                api_key=cfg.get("jobs_api_key", ""),
                provider=(source or cfg.get("jobs_provider", "auto")),
                country=cfg.get("search_country", "") or "",
            )
        except Exception as e:  # noqa: BLE001 — surface the reason to the user, don't crash the turn
            return f"Job search failed: {e}"
        # Say who answered. A keyless board is one slice of the market, not a search of all of it —
        # the user must be able to tell the two apart.
        src = f"Source: {result.provider_label}"
        if result.keyless_fallback:
            src += (
                " — keyless (no Job-source API key set), remote roles only; add a JSearch key in "
                "Settings → Career Coach for Google-for-Jobs coverage"
            )
        if result.provider in jobsource.KEYLESS:
            src += f". {result.matched} of the {result.fetched} posting(s) it returned mention the query"
            if location:
                src += f"; {result.provider_label.split(' (')[0]} ignores `location`"
        src += "."
        from . import boards, scan, state, watchlist

        # List what can reach the inbox, so the count the agent reports is the count the operator sees.
        jobs = [j for j in result.jobs if boards.passes(j, locations=location)]
        left_out = len(result.jobs) - len(jobs)
        if left_out:
            src += f" Left out {left_out} other(s) outside your locations."
        try:
            index = scan.tracked_index(state.load_applications())
            matches = [
                {
                    **j,
                    "id": f"{j['source']}:{j['url']}",
                    "hash": boards.content_hash(j["title"], j.get("snippet", ""), j.get("location", "")),
                    "score": jobsource.best_prescore(j, query),
                    "reason": watchlist.match_reason(j, query),
                }
                for j in jobs
            ]
            untracked = [j for j in matches if not scan.annotate(j, index)["tracked"]]
            watchlist.add_to_inbox(untracked)
            # Retain displayed results for the view without repeating them in the digest.
            watchlist.drop_from_inbox([j["id"] for j in untracked])
        except state.StoreUnreadable as exc:
            src += f" Results could not reach the inbox: {exc}."
        if not jobs:
            return f"No postings found for {query!r}. Try a broader query or a different location.\n{src}"
        from .boards import age_days

        def _line(i: int, j: dict) -> str:
            age = age_days(j.get("posted", ""))
            extra = [f"posted {int(age)}d ago"] if age is not None else []
            if j.get("comp"):
                extra.append(j["comp"])
            tail = f" · {' · '.join(extra)}" if extra else ""
            return f"{i}. {j['title']} — {j['company']} · {j['location'] or 'n/a'}{tail} ({j['source']})\n   {j['url']}"

        lines = [_line(i, j) for i, j in enumerate(jobs, 1)]
        return (
            f"Found {len(jobs)} role(s):\n"
            + "\n".join(lines)
            + f"\n\n{src}\nWant me to evaluate fit or track any of these?"
        )

    if extended:  # legacy name behind extended_tools; find_roles(source="market") is the default path
        registry.register_tool(careercoach_search_jobs)
    return careercoach_search_jobs


# ── agent tool: find roles (the default entry point — watchlist boards or the live market) ─
# S4b merges careercoach_scan_boards + careercoach_search_jobs behind one lean tool. It REUSES both
# code paths as-is (dispatching to the tool objects the two registrars built), so no behaviour moves
# and the S1a inbox feed the board scan writes is kept.
def _register_find_roles_tool(registry, scan_boards, search_jobs) -> None:
    @tool
    async def careercoach_find_roles(
        source: str = "watched",
        query: str = "",
        roles: str = "",
        locations: str = "",
        max_age_days: int = -1,
        only_new: bool = False,
        limit: int = 0,
        remote: bool = False,
        provider: str = "",
    ) -> str:
        """Find roles to pursue. source="watched" (default) scans the companies on your watchlist's own
        job boards — the freshest source, and it feeds the pipeline inbox; source="market" searches live
        postings across the job source. Both sources retain results in the view inbox, marked as
        already shown so the digest does not repeat them. `roles`/`query` are the titles or keywords to match (both
        accepted); `locations` where (places, or "remote"); `max_age_days` 0 = any age; `only_new`
        (watched) shows only roles no earlier scan returned; `remote` (market) remote-only; `limit` caps
        results. `provider` (market) overrides Settings, e.g. "himalayas" or "hn" for keyless search.
        Choose which companies are watched with careercoach_watchlist."""
        src = (source or "watched").strip().lower()
        if src == "market":
            from .hunt import filters
            from .profile import StoreUnreadable

            try:
                defaults = filters(registry.config)
            except StoreUnreadable as exc:
                return f"Cannot read scan defaults: {exc}"
            q = (query or roles or defaults["roles"]).strip()
            locations = locations or defaults["locations"]
            if not q:
                return "What should I search the market for? Pass a role title or keywords as `query`."
            return await search_jobs.ainvoke(
                {"query": q, "location": locations, "remote": remote, "limit": limit or 10, "source": provider}
            )
        if src in ("watched", ""):
            return await scan_boards.ainvoke(
                {
                    "companies": "",
                    "roles": roles or query,
                    "locations": locations,
                    "max_age_days": max_age_days,
                    "only_new": only_new,
                    "limit": limit or 40,
                }
            )
        return f'Unknown source {source!r} — use "watched" (your watchlist\'s boards) or "market" (a live search).'

    registry.register_tool(careercoach_find_roles)


def _register_hunt_tools(registry, cfg, extended: bool):
    from .hunt import register_hunt_tools

    return register_hunt_tools(registry, cfg, extended)


def _import_detail(row: dict) -> str:
    """One line saying what an import row would do — the numbers come from the same plan that
    writes, so the preview can't promise one thing and write another."""
    if row["outcome"] == "kept":
        return f"file says {row['file']!r}, profile keeps {row['old']!r}"
    if row["error"] is not None:
        return str(row["error"]).split("\n", 1)[0]
    if row["outcome"] == "unchanged":
        return ""
    if row["field"] in ("name", "location", "work_auth", "contact", "headlines"):
        return f"{row['new']!r}" + (f" (was {row['old']!r})" if row["old"] else "")
    return f"{len(row['added'])} new line(s)" + (f", {len(row['dropped'])} removed" if row["dropped"] else "")


# ── the role-packet workspace: scaffold + write + assemble (the gated resume flow) ─
# Deterministic file mechanics for the `role-packet` skill — create the folder-per-role tree,
# write named artifacts into it, assemble the deliverable, and seed the workspace from the
# fill-in templates/. Pure logic lives in packet.py (host-free, tested); these tools are the
# thin agent surface. The *content* of each artifact is the agent's job (driven by the skill).
def _register_packet_tools(registry, cfg, extended: bool):
    from functools import wraps
    from pathlib import Path

    from . import packet, profile, resume

    templates_dir = Path(__file__).resolve().parent / "templates"

    def _root() -> str:
        return cfg.get("packet_root", "") or ""

    def _with_workspace_notice(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            result = fn(*args, **kwargs)
            notice = packet.legacy_workspace_notice(_root())
            return f"{result}\n\nWorkspace upgrade notice: {notice}" if notice else result

        return wrapped

    @tool
    @_with_workspace_notice
    def careercoach_init_workspace() -> str:
        """Seed the job-search workspace with the fill-in templates (Experience.md, story bank, reviewer rules, humanize,
        improvements log). Only adds missing files, never overwrites. Run once before the first role packet."""
        root = packet.resolve_root(_root())
        res = packet.init_workspace(root, templates_dir)
        if not res["created"]:
            return f"Workspace at {res['root']} already seeded ({len(res['skipped'])} file(s) left untouched)."
        made = "\n".join(f"  - {r}" for r in res["created"])
        return (
            f"Seeded your workspace at {res['root']}:\n{made}\n\n"
            "Next: build the operator profile with /setup-coach — it's the record everything the "
            "coach writes is anchored to. If they'd rather write their history into "
            "Resume/Experience.md, bring it in afterwards with careercoach_import_experience."
        )

    @tool
    def careercoach_get_profile(field: str = "") -> str:
        """Read the operator's verified profile. No `field`: the completeness index (same as the <operator_profile>
        block). With `field` (roles, education, skills, do_not_claim, stories, notes, name, location, work_auth,
        contact, headlines): that section in full — read it before drafting from it."""
        prof, err = profile.load_profile_checked()
        if err:
            return profile.unreadable_block(err)
        field = (field or "").strip()
        if not field:
            cov = profile.completeness(prof)
            if cov["empty"]:
                return (
                    "No operator profile recorded yet. Run /setup-coach to build one — and "
                    "harvest what already exists (memory, artifacts, any CV in the workspace) "
                    "before asking the operator anything."
                )
            return (
                f"Profile: {cov['filled']} of {cov['total']} fields"
                + (f" (updated {cov['updated']})" if cov["updated"] else "")
                + f"\n  known:   {', '.join(cov['known'])}"
                + f"\n  missing: {', '.join(cov['missing']) or 'nothing'}"
            )
        if field in profile.IDENTITY_FIELDS:
            return prof["identity"].get(field) or f"{field} is not recorded yet."
        if field in profile.SECTIONS:
            return prof["sections"].get(field) or f"{field} is not recorded yet."
        known = ", ".join(list(profile.IDENTITY_FIELDS) + list(profile.SECTIONS))
        return f"unknown profile field {field!r}; known: {known}"

    @tool
    def careercoach_update_profile(
        field: str, content: str, mode: str = "append", confirm_removal: bool = False
    ) -> str:
        """Record one profile field the operator told you or confirmed — never anything else; this profile is the
        anti-fabrication anchor for every CV, letter and answer. `field`: identity (name, location, work_auth,
        contact, headlines — pass the whole value) or section (roles, education, skills, do_not_claim, stories, notes
        — appends). To correct, read it with careercoach_get_profile and write the whole section back with
        mode="replace". Dropping or rewording a do_not_claim line needs confirm_removal=true, only when the operator
        explicitly asked."""
        try:
            saved = profile.update_field(field, content, mode=mode, confirm_removal=confirm_removal)
        except (KeyError, ValueError, PermissionError, profile.StoreUnreadable) as e:
            return str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)
        cov = profile.completeness(saved)
        coverage = f"Profile now {cov['filled']}/{cov['total']} fields" + (
            f"; still missing: {', '.join(cov['missing'])}" if cov["missing"] else " — complete."
        )
        change = saved.get("change")
        if change == "unchanged":
            if not (content or "").strip() and mode == "append":
                return f"Nothing recorded: content was empty (sections append; to clear one, use mode='replace'). {coverage}"
            return f"{field} already holds that — nothing changed. {coverage}"
        verb = {"appended": "Added to", "replaced": "Replaced"}.get(change, "Recorded")
        return f"{verb} {field}. {coverage}"

    @tool
    def careercoach_export_experience() -> str:
        """Write a markdown snapshot of the profile to `Resume/Experience (profile export).md`, regenerated each run.
        Never touches the operator's own Resume/Experience.md."""
        prof, err = profile.load_profile_checked()
        if err:
            return f"Not exported: {profile.unreadable_block(err)}"
        if profile.completeness(prof)["empty"]:
            return "Nothing to export yet — no operator profile has been recorded."
        root = packet.resolve_root(_root())
        res = packet.write_export(root, profile.to_markdown(prof))
        return f"Exported the profile → {res['path']} (their own Resume/Experience.md is untouched)."

    @tool
    @_with_workspace_notice
    def careercoach_read_profile(doc: str = "experience") -> str:
        """Read the candidate's record or a workspace reference file — every CV bullet, evidence row and letter claim
        must trace to it. `doc`: experience (the live operator profile as markdown; Resume/Experience.md is never read
        here — see careercoach_import_experience), story-bank (STAR proof + do-NOT-claim guardrails), reviewer,
        humanize, improvements. Read `experience` before drafting in the candidate's name; if empty, build the profile
        — never invent."""
        root = packet.resolve_root(_root())
        if doc == "experience":
            prof, err = profile.load_profile_checked()
            if err:
                return profile.unreadable_block(err)
            if not profile.completeness(prof)["empty"]:
                return (
                    "The operator's career record — their operator profile, the single source of truth "
                    "(what careercoach_get_profile reads), rendered as markdown. Draft only from this; "
                    "record additions or corrections with careercoach_update_profile once they confirm.\n\n"
                    + profile.render_markdown(prof)
                )
            hint = ""
            res = packet.read_source(root, "experience", templates_dir)
            if res["exists"]:
                tpl = templates_dir / packet.SOURCES["experience"]
                fields, _, _ = profile.parse_experience(
                    res["text"], tpl.read_text(encoding="utf-8") if tpl.is_file() else ""
                )
                if fields:
                    hint = (
                        f" Their own {res['path']} has history in it that isn't in the profile: offer to "
                        "bring it in with careercoach_import_experience (preview first, apply once they confirm)."
                    )
            return (
                "No operator profile recorded yet, so there is no career record to draft from — do NOT "
                "invent one. Run /setup-coach (or interview them) and record it with "
                "careercoach_update_profile." + hint
            )
        try:
            res = packet.read_source(root, doc, templates_dir)
        except KeyError as e:
            return f"{e}"
        if not res["exists"]:
            return (
                f"{doc} not found at {res['path']} — the workspace isn't seeded yet. "
                "Run careercoach_init_workspace first."
            )
        if doc in packet.WRITABLE_SOURCES and not res["edited"]:
            return (
                f"{doc} at {res['path']} is still the untouched template — the candidate hasn't "
                "filled it in. Do NOT draft from its placeholder text; ask them (or run /setup-coach) "
                "and save what they confirm with careercoach_write_profile.\n\n"
                f"{res['text']}"
            )
        return res["text"]

    @tool
    @_with_workspace_notice
    def careercoach_import_experience(
        apply: bool = False, preview_id: str = "", mode: str = "append", confirm_removal: bool = False
    ) -> str:
        """Import the operator's own Resume/Experience.md into the profile (the only way its content reaches you). Call
        without `apply` first: writes nothing, returns a per-field preview and a `preview_id` — show it to the
        operator. Only after they confirm, call with apply=true and that preview_id (refused if the file, profile,
        mode or confirm_removal changed). mode="append" merges missing lines; "replace" makes sections match the file.
        Dropping a do_not_claim line needs confirm_removal=true on their explicit say-so."""
        root = packet.resolve_root(_root())
        res = packet.read_source(root, "experience", templates_dir)
        if not res["exists"]:
            return f"No Resume/Experience.md at {res['path']} — nothing to import."
        tpl = templates_dir / packet.SOURCES["experience"]
        fields, unmapped, notes = profile.parse_experience(
            res["text"], tpl.read_text(encoding="utf-8") if tpl.is_file() else ""
        )
        if not fields and not unmapped:
            return f"{res['path']} is still the untouched template — nothing to import."
        source = hashlib.sha256(res["text"].encode("utf-8")).hexdigest()
        pid = ""
        try:
            if apply:
                if not preview_id:
                    return (
                        "Not imported: an apply needs the preview_id from a preview the operator has seen. "
                        "Call this without apply, show them what it would change, and pass the preview_id it gives."
                    )
                rows = profile.apply_import(
                    fields, preview_id=preview_id, mode=mode, confirm_removal=confirm_removal, source=source
                )
            else:
                rows, pid = profile.plan_import(fields, mode=mode, confirm_removal=confirm_removal, source=source)
        except (ValueError, PermissionError, profile.StoreUnreadable) as e:
            return f"Not imported: {e}"

        head = "Imported" if apply else "Import preview — nothing written yet"
        lines = [f"{head} (mode={mode}) from {res['path']}:"]
        for r in rows:
            lines.append(f"  {r['field']}: {r['outcome']}" + (f" — {_import_detail(r)}" if _import_detail(r) else ""))
            lines += [f"      + {ln.strip()[:160]}" for ln in r["added"][:8]]
            lines += [f"      - {ln[:160]}" for ln in r["dropped"][:8]] if r["outcome"] == "replaced" else []
            if len(r["added"]) > 8 or (r["outcome"] == "replaced" and len(r["dropped"]) > 8):
                lines.append("      …")
        if not rows:
            lines.append("  (nothing in it maps to a profile field)")
        lines += [f"  note: {n}" for n in dict.fromkeys(notes)]
        outcomes = {r["outcome"] for r in rows}
        if "kept" in outcomes:
            lines.append("'kept' = the file differs from the profile; mode='replace' takes the file's version.")
        if unmapped:
            lines.append(
                f"Not imported — {len(unmapped)} line(s) match no profile field; raise them with the operator "
                "and record what they confirm with careercoach_update_profile:"
            )
            lines += [f"  {u[:160]}" for u in unmapped[:12]] + (["  …"] if len(unmapped) > 12 else [])
        if outcomes == {"unchanged"}:
            lines.append("Nothing new to import: the profile already has everything the file says.")
        elif not apply and outcomes & {"set", "appended", "replaced"}:
            lines.append(
                f"Show the operator this preview; if they confirm, call again with apply=true, preview_id={pid!r}."
            )
        if apply:
            cov = profile.completeness()
            lines.append(f"Profile now {cov['filled']}/{cov['total']} fields.")
        return "\n".join(lines)

    @tool
    def careercoach_write_profile(doc: str, content: str) -> str:
        """Save the story bank (doc="story-bank") after the candidate confirmed it — never write a claim they didn't give
        you. Overwrites the whole file: read it first (careercoach_read_profile) and pass the full merged markdown.
        Career history goes to careercoach_update_profile, not here."""
        if doc == "experience":
            return (
                "Not written: Resume/Experience.md is the operator's own file, and their history lives in "
                "the operator profile, the single source of truth. Record what they confirmed with "
                "careercoach_update_profile (mode='replace' to correct a section you've read in full). "
                "If THEY edited Experience.md, bring it in with careercoach_import_experience."
            )
        root = packet.resolve_root(_root())
        try:
            res = packet.write_source(root, doc, content)
        except (KeyError, PermissionError) as e:
            return f"{e}"
        return f"{'Updated' if res['replaced'] else 'Wrote'} {doc} → {res['path']}"

    @tool
    def careercoach_scaffold_role(company: str, role: str, req: str = "", raw_jd: str = "") -> str:
        """Start a role packet (Phase 1, Intake): create Companies/<company>/Roles/<role - req>/ with the posting
        (`raw_jd`) and a process log. `req` is an optional requisition id. Idempotent. Then write each gated artifact
        with careercoach_write_artifact (role-packet skill)."""
        root = packet.resolve_root(_root())
        out = packet.scaffold_role(root, company, role, req, raw_jd=raw_jd)
        checklist = "\n".join(f"  - {packet.ARTIFACTS[k]}" for k in packet.AUTHORED)
        verb = "Reusing" if out["existed"] else "Created"
        return (
            f"{verb} role folder:\n  {out['path']}\n\n"
            "Artifacts to produce (one human-approved gate each — write with "
            "careercoach_write_artifact):\n"
            f"{checklist}\n\n"
            "Then run careercoach_assemble_packet to build 'role packet.md'."
        )

    @tool
    def careercoach_write_artifact(company: str, role: str, artifact: str, content: str, req: str = "") -> str:
        """Write one packet artifact (markdown `content`) into a role folder. `artifact`: recruiter-brief,
        hiring-manager-profile, evidence-map, tailored-resume, skills-entry, cover-letter, prompt-transcript. Only
        after the user approved that step."""
        root = packet.resolve_root(_root())
        try:
            res = packet.write_artifact(root, company, role, req, artifact, content)
        except KeyError as e:
            return f"{e}"
        return f"{'Updated' if res['replaced'] else 'Wrote'} {packet.ARTIFACTS[artifact]} → {res['path']}"

    @tool
    def careercoach_assemble_packet(company: str, role: str, req: str = "") -> str:
        """Phase 6 (QA): build 'role packet.md' from the produced artifacts, refresh the orchestration log, and report
        which sections are present or missing."""
        root = packet.resolve_root(_root())
        try:
            res = packet.assemble_packet(root, company, role, req)
        except FileNotFoundError as e:
            return f"{e}"
        present = ", ".join(res["present"]) or "none yet"
        missing = ", ".join(res["missing"]) or "none"
        try:
            registry.emit("documents_generated", {"company": company, "role": role, "sections": len(res["present"])})
        except Exception:  # noqa: BLE001 — the event bus is best-effort chrome
            pass
        return f"Assembled {res['path']}\n  present: {present}\n  missing: {missing}"

    @tool
    @_with_workspace_notice
    def careercoach_list_roles() -> str:
        """List role packets with artifact counts and each role folder's absolute path."""
        rows = packet.list_roles(packet.resolve_root(_root()))
        if not rows:
            return "No role packets yet. Start one with careercoach_scaffold_role(company, role)."
        return "\n".join(
            f"- {r['role']} @ {r['company']} — {r['artifacts']}/{r['total']} artifacts — {r['path']}" for r in rows
        )

    @tool
    def careercoach_render_resume(company: str = "") -> str:
        """Render the operator's résumé to Resume/resume.html for an ATS upload. Prints ONLY the operator-approved
        `resume` profile section plus `name` and adds no claims; never reads the coaching record (roles, skills,
        notes, …), whose annotations must not reach an employer. Writes nothing and says why if the body is empty, has
        no `Contact:` line, or carries coach annotations — then draft a clean body, show the operator, and save it
        with careercoach_update_profile(field="resume", mode="replace") only after they approve. Returns the file://
        URL and the steps (browser_open → browser_pdf → browser_upload; the PDF must come from browser_pdf). Pass
        `company` to name the PDF. Body format: apply-form skill."""
        prof, err = profile.load_profile_checked()
        if err:
            return f"Not rendered: {profile.unreadable_block(err)}"
        missing = resume.missing_for_resume(prof)
        if missing:
            needs = []
            if "name" in missing:
                needs.append("a name")
            if "resume" in missing:
                needs.append("the operator-approved résumé body (the `resume` section)")
            if "contact" in missing:
                needs.append("a `Contact:` line in the résumé body (résumé Contact line)")
            return (
                f"Not rendered — a résumé needs {', and '.join(needs)}. Nothing was written. Draft a "
                "clean, employer-facing résumé body (a `Contact:` header line, optional `Headline:` / "
                "`Location:` lines, then `## ` sections), SHOW it to the operator, and save it with "
                'careercoach_update_profile(field="resume", content=…, mode="replace") only AFTER they '
                "approve it. A résumé is never built from an unapproved body, and never from the "
                "coaching record — its notes are not for an employer."
            )
        annotations = resume.annotation_lines(prof)
        if annotations:
            quoted = "\n".join(f"  > {ln}" for ln in annotations)
            return (
                "Not rendered — the `resume` body carries coaching annotations that must never reach an "
                "employer. Nothing was written. The résumé body must hold only employer-facing text; "
                "these line(s) look like internal coaching notes:\n"
                f"{quoted}\n"
                "Rewrite the résumé body with the operator so it contains only what belongs on their "
                'résumé, then save it with careercoach_update_profile(field="resume", …, mode="replace"). '
                "It is never silently stripped — a stripped résumé could still misstate facts, so the "
                "operator-approved body is fixed instead."
            )
        root = packet.resolve_root(_root())
        path = resume.write_resume_html(root, prof)
        url = path.resolve().as_uri()
        pdf = resume.pdf_name(company)
        return (
            f"Wrote the résumé HTML (built only from the operator-approved résumé body — no coaching "
            f"notes, no added claims) to:\n"
            f"  {path}\n\n"
            "Show the operator the rendered résumé and get their explicit approval before "
            "browser_upload — it has never been sent to an employer unreviewed.\n\n"
            "To turn it into a PDF an ATS form will accept, in this exact order:\n"
            f"  1. browser_open {url}\n"
            f'  2. browser_pdf("{pdf}")  — prints the open page into the browser plugin\'s capture '
            "folder; take the path from its 'Saved to' line\n"
            "  3. pass that path to browser_upload\n\n"
            "The PDF must come from browser_pdf: browser_upload only accepts files already in the "
            "browser's capture folder, and printing the opened file:// page is the only way one "
            "gets there."
        )

    @tool
    def careercoach_profile(
        action: str = "get",
        field: str = "",
        content: str = "",
        mode: str = "append",
        confirm_removal: bool = False,
        doc: str = "experience",
        apply: bool = False,
        preview_id: str = "",
    ) -> str:
        """Read and maintain the operator's verified profile — the anti-fabrication anchor every CV,
        letter and answer traces to. action: "get" (the completeness index, or one `field`: roles,
        education, skills, do_not_claim, stories, notes, name, location, work_auth, contact, headlines);
        "read" (a `doc`: experience, story-bank, reviewer, humanize, improvements); "update" (record one
        `field` the operator confirmed — `content`, mode append/replace, confirm_removal to drop a
        do_not_claim line); "write" (save the story-bank `doc` with the full merged `content`);
        "import_experience" (import the operator's Resume/Experience.md — call without `apply` for a
        preview + preview_id, then apply=true with that id); "export_experience" (a markdown snapshot);
        "init_workspace" (seed the fill-in templates). Read before drafting; never invent. Defaults to
        "get"."""
        act = (action or "get").strip().lower()
        if act == "get":
            return careercoach_get_profile.invoke({"field": field})
        if act == "read":
            return careercoach_read_profile.invoke({"doc": doc})
        if act == "update":
            return careercoach_update_profile.invoke(
                {"field": field, "content": content, "mode": mode, "confirm_removal": confirm_removal}
            )
        if act == "write":
            return careercoach_write_profile.invoke({"doc": doc, "content": content})
        if act == "import_experience":
            return careercoach_import_experience.invoke(
                {"apply": apply, "preview_id": preview_id, "mode": mode, "confirm_removal": confirm_removal}
            )
        if act == "export_experience":
            return careercoach_export_experience.invoke({})
        if act == "init_workspace":
            return careercoach_init_workspace.invoke({})
        return (
            f"Unknown action {action!r} — use get, read, update, write, import_experience, "
            "export_experience or init_workspace."
        )

    @tool
    def careercoach_role_packet(
        action: str = "list",
        company: str = "",
        role: str = "",
        req: str = "",
        raw_jd: str = "",
        artifact: str = "",
        content: str = "",
    ) -> str:
        """The gated role-packet workspace (a folder per role). action: "scaffold" (start a packet —
        Companies/<company>/Roles/<role - req>/ with the posting `raw_jd` and a process log); "write"
        (write one `artifact`: recruiter-brief, hiring-manager-profile, evidence-map, tailored-resume,
        skills-entry, cover-letter, prompt-transcript — `content` markdown, only after the operator
        approved that step); "assemble" (build 'role packet.md' from the artifacts and report what's
        present/missing); "list" (role packets with artifact counts and paths). Defaults to "list"."""
        act = (action or "list").strip().lower()
        if act == "scaffold":
            return careercoach_scaffold_role.invoke({"company": company, "role": role, "req": req, "raw_jd": raw_jd})
        if act == "write":
            return careercoach_write_artifact.invoke(
                {"company": company, "role": role, "artifact": artifact, "content": content, "req": req}
            )
        if act == "assemble":
            return careercoach_assemble_packet.invoke({"company": company, "role": role, "req": req})
        if act == "list":
            return careercoach_list_roles.invoke({})
        return f"Unknown action {action!r} — use scaffold, write, assemble or list."

    # Default surface: the two merged façades + the unchanged résumé renderer.
    registry.register_tools([careercoach_profile, careercoach_role_packet, careercoach_render_resume])
    if extended:  # legacy names behind extended_tools — same implementations
        registry.register_tools(
            [
                careercoach_init_workspace,
                careercoach_get_profile,
                careercoach_update_profile,
                careercoach_export_experience,
                careercoach_import_experience,
                careercoach_read_profile,
                careercoach_write_profile,
                careercoach_scaffold_role,
                careercoach_write_artifact,
                careercoach_assemble_packet,
                careercoach_list_roles,
            ]
        )
    # Hand the two read-only profile readers to register() so the always-on delegate-read tool can
    # dispatch to them (the crew grounds on these even when extended is off — see below).
    return careercoach_get_profile, careercoach_read_profile


# ── agent tools: saved standard application answers (confirm once, reuse confirmed) ─
# The confirm-once ATS fields. Pure logic lives in answers.py (host-free, tested); these tools are
# the thin agent surface. Form filling draws ONLY from the confirmed set — an improvised answer is
# exactly the failure this replaces, so a draft is shown to the operator and confirmed before use.
def _unknown_answer_key(key: str) -> str:
    from . import answers

    return f"Unknown answer key {key!r}. Valid keys: {', '.join(answers.STANDARD_KEYS)}."


def _register_answers_tools(registry, extended: bool):
    from . import answers, profile

    @tool
    def careercoach_get_answers() -> str:
        """List the operator's standard application answers (email, phone, work authorization, sponsorship, LinkedIn,
        self-ID, pronouns): value, CONFIRMED or DRAFT, and missing keys. Only confirmed answers may fill a form. First
        use seeds drafts from the profile (self-ID: "Decline to self-identify"); it never confirms anything on the
        operator's behalf."""
        try:
            stored, err = answers.load_checked()
            if err:
                # An unreadable file is reported, never seeded or written over — a seed now would
                # replace everything in it (see answers._load_strict).
                path = answers._path()
                out = [
                    f"Your saved standard answers file is unreadable ({err}): {path}",
                    "Nothing was changed, and nothing it holds can fill a form until it's fixed.",
                ]
                backup = profile.backup_path(path)
                if backup.is_file():
                    out.append(f"The version before the coach's last change is {backup}, if fixing the file is harder.")
                return "\n".join(out)
            if not stored:
                answers.seed_from_profile(profile.load_profile())
                stored, _ = answers.load_checked()
            lines = [
                "Standard application answers — ONLY confirmed values may fill a form; a draft must "
                "be shown to the operator and confirmed first.",
                "",
            ]
            missing: list[str] = []
            for key, desc in answers.STANDARD_KEYS.items():
                entry = stored.get(key)
                if entry and entry.get("value"):
                    tag = (
                        "confirmed" if entry.get("status") == answers.CONFIRMED else "DRAFT — not used until confirmed"
                    )
                    lines.append(f"  {key}: {entry['value']}  [{tag}] — {desc}")
                else:
                    missing.append(key)
            n = len(answers.confirmed())
            total = len(answers.STANDARD_KEYS)
            lines += ["", f"{n}/{total} confirmed."]
            if missing:
                lines.append("Missing (never recorded): " + ", ".join(missing))
            lines.append(
                "Propose a value with careercoach_propose_answer; after showing the operator the exact "
                "values and getting their explicit OK, confirm them with careercoach_confirm_answers."
            )
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001 — a tool never raises
            return f"Could not read your standard answers: {e}"

    @tool
    def careercoach_propose_answer(key: str, value: str) -> str:
        """Save a DRAFT value for one standard answer key (e.g. phone_number, authorized_us). Drafts never fill a form
        until careercoach_confirm_answers; changing a confirmed value sends it back to draft."""
        key = (key or "").strip()
        try:
            answers.propose(key, value)
        except KeyError:
            return _unknown_answer_key(key)
        except Exception as e:  # noqa: BLE001 — a tool never raises
            return f"Not saved: {e}"
        return (
            f"Saved {key} as a draft: {(value or '').strip()!r}. It will NOT be used to fill a form "
            "until the operator confirms it with careercoach_confirm_answers."
        )

    @tool
    def careercoach_confirm_answers(keys: str) -> str:
        """Promote drafts to CONFIRMED (`keys` comma-separated). Call ONLY after showing the operator the exact values
        and getting their explicit confirmation in this conversation — never on their behalf."""
        requested = [k.strip() for k in (keys or "").split(",") if k.strip()]
        if not requested:
            return "No keys given. Pass a comma-separated list of the keys the operator confirmed."
        unknown = [k for k in requested if k not in answers.STANDARD_KEYS]
        if unknown:
            return _unknown_answer_key(", ".join(unknown))
        try:
            promoted = answers.confirm(requested)
        except Exception as e:  # noqa: BLE001 — a tool never raises
            return f"Not confirmed: {e}"
        skipped = [k for k in requested if k not in promoted]
        out: list[str] = []
        if promoted:
            out.append("Confirmed: " + ", ".join(promoted) + ". These may now be used to fill a form.")
        if skipped:
            out.append(
                "Nothing to confirm for: "
                + ", ".join(skipped)
                + " — no value is stored yet; propose one first with careercoach_propose_answer."
            )
        return "\n".join(out) or "Nothing confirmed."

    @tool
    def careercoach_answers(action: str = "get", key: str = "", value: str = "", keys: str = "") -> str:
        """The operator's standard application answers (email, phone, work authorization, sponsorship,
        LinkedIn, self-ID, pronouns). action: "get" (list each value with CONFIRMED/DRAFT status plus
        the missing keys; first use seeds drafts from the profile); "propose" (save a DRAFT `value` for
        one `key`); "confirm" (promote drafts to CONFIRMED — `keys` comma-separated — only after showing
        the operator the exact values and getting their explicit OK). Only confirmed answers may fill a
        form. Defaults to "get"."""
        act = (action or "get").strip().lower()
        if act == "get":
            return careercoach_get_answers.invoke({})
        if act == "propose":
            return careercoach_propose_answer.invoke({"key": key, "value": value})
        if act == "confirm":
            return careercoach_confirm_answers.invoke({"keys": keys})
        return f"Unknown action {action!r} — use get, propose or confirm."

    registry.register_tool(careercoach_answers)
    if extended:  # legacy names behind extended_tools — same implementations
        registry.register_tools([careercoach_get_answers, careercoach_propose_answer, careercoach_confirm_answers])
    # The delegate-read tool reads the confirmed answers through this reader; hand it to register().
    return careercoach_get_answers


# ── a read-only grounding view for the delegate crew (ADR 0002) ───────────────────────────────
# The host builds subagents WITHOUT the plugin's profile middleware, so the always-on
# <operator_profile> block never reaches job_evaluator / application_writer. They must READ the
# verified profile (and the confirmed standard answers) to ground a score or a draft — but a
# hands-off workflow run has no operator to gate a write, so the crew must own NO tool that changes
# the record or confirms an answer in the operator's name (the anti-fabrication invariant). The
# merged careercoach_profile / careercoach_answers façades both carry write actions, so the crew is
# granted this instead: ONE always-on, read-only tool that dispatches to the very same read
# implementations and can never write. It lives on the default surface so the grant resolves with
# extended_tools off — the shipped config the crew actually runs in.
def _register_delegate_read_tool(registry, get_profile, read_profile, get_answers) -> None:
    @tool
    def careercoach_read_sources(
        source: str = "profile", doc: str = "experience", field: str = "", text: str = ""
    ) -> str:
        """Read-only view of the verified record a draft must trace to — for grounding before you
        score or write, never to change it. source="profile" (default): one `field` (roles, education,
        skills, do_not_claim, stories, notes, name, location, work_auth, contact, headlines) returns
        that section, or with no field the whole `doc` (experience, story-bank, reviewer, humanize,
        improvements) as markdown. source="answers": the operator's standard application answers with
        CONFIRMED/DRAFT status. source="check": pass a draft as `text` and get back every hard figure
        (percentages, money, multipliers, durations, numbers) in it that the operator's profile and
        story bank do NOT support — "Not found in your profile: …" for an invented value, "Check the
        direction: …" for one that's real but read the wrong way (a "cut by 70%" that became a "70%
        rate"), or "No unsupported figures." when the draft is clean. Guardrail sections are
        excluded as evidence; blank text requests a draft. Writes nothing — the operator
        records and confirms through the Career Coach view (careercoach_profile / careercoach_answers).
        Defaults to the experience profile."""
        src = (source or "profile").strip().lower()
        if src == "profile":
            if (field or "").strip():
                return get_profile.invoke({"field": field})
            return read_profile.invoke({"doc": doc})
        if src == "answers":
            return get_answers.invoke({})
        if src == "check":
            from . import grounding

            if not (text or "").strip():
                return "No draft given. Pass the draft to check as text."

            # The same profile + story-bank the grounding readers serve — the figures a draft must
            # trace to. Read-only: this only reports, it never records anything.
            sources = "\n\n".join(
                grounding.supporting_text(read_profile.invoke({"doc": d})) for d in ("experience", "story-bank")
            )
            return grounding.render_flags(grounding.check_draft(text or "", sources))
        return f'Unknown source {source!r} — use "profile", "answers" or "check".'

    registry.register_tool(careercoach_read_sources)


# ── form filling: a PLAN before, a DIFF after (#4032 phase 3b) ────────────────
# The browser plugin reads a form (browser_form_read) and fills it (browser_fill/select/upload) —
# those tools are the AGENT's. Career Coach owns the deterministic halves: build a plan from the
# confirmed standard answers before filling, and diff the read-back against it after. Pure logic
# lives in formfill.py (host-free, tested); these are the thin agent surface. A plan draws ONLY
# from confirmed answers plus the session's operator-supplied extras — an improvised answer is the
# failure this replaces, so an unmapped field is handed back for the operator, never guessed.
def _parse_form_json(form_json: str):
    """``(fields, error)``: the browser_form_read array, or a readable error. Accepts the array
    itself, or an object wrapping it under ``fields`` / ``form``."""
    import json as _json

    text = (form_json or "").strip()
    if not text:
        return None, "No form JSON given. Pass the exact array that browser_form_read returned."
    try:
        data = _json.loads(text)
    except ValueError as e:
        return None, f"That form JSON could not be parsed ({e}). Pass the exact array browser_form_read returned."
    if isinstance(data, dict):
        for key in ("fields", "form"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return None, "That form JSON isn't a list of fields. browser_form_read returns one object per field."
    return data, ""


def _parse_extra_answers(extra_answers: str):
    """``(extra, error)``: the operator's one-off answers as a ``{label: value}`` dict, or a
    readable error. An empty string is an empty dict (no extras)."""
    import json as _json

    text = (extra_answers or "").strip()
    if not text:
        return {}, ""
    try:
        data = _json.loads(text)
    except ValueError as e:
        return (
            None,
            f'extra_answers could not be parsed ({e}). Pass a JSON object, e.g. {{"Start date": "Immediately"}}.',
        )
    if not isinstance(data, dict):
        return (
            None,
            'extra_answers must be a JSON object mapping a field label to a value, e.g. {"Start date": "Immediately"}.',
        )
    return {str(k): ("" if v is None else str(v)) for k, v in data.items()}, ""


def _resume_renderable() -> bool:
    """Whether a résumé can be rendered right now — the profile is readable and has everything
    ``careercoach_render_resume`` needs (name, contact, roles). Used to decide whether an Ashby
    required-résumé field becomes an upload row or is handed back to ask about."""
    from . import profile, resume

    prof, err = profile.load_profile_checked()
    if err:
        return False
    return not resume.missing_for_resume(prof)


def _render_plan(plan: dict, company: str, role: str) -> str:
    from . import formfill

    rows = plan.get("rows", [])
    unmapped = plan.get("unmapped", [])
    unconfirmed = plan.get("unconfirmed", [])
    sid = plan.get("session_id", "")
    note = plan.get("note", "")

    who = " — ".join(x for x in (role.strip(), company.strip()) if x)
    out = [
        f"Fill plan ({who})" if who else "Fill plan",
        f"session_id: {sid}",
    ]
    if note:
        out += ["", f"⚠ {note}"]
    out += [
        "",
        "Fill IN THIS ORDER (values are from confirmed answers or the extra answers you supplied):",
    ]
    if rows:
        for i, r in enumerate(rows, 1):
            action = r.get("action", "")
            target = r.get("target") or r.get("label", "")
            if action == formfill.SKIP:
                out.append(f"  {i}. [skip]   {target} — optional, no saved answer; leave blank")
                continue
            value = r.get("value", "")
            if r.get("kind", "").lower() in {"radio", "radio-group"}:
                out.append("     Read back checked=true (or aria-checked=true) and the selected value after filling.")
            shown = value if action != formfill.UPLOAD else f"{value}  (browser_upload this file)"
            # A self-ID select whose options the reader couldn't list (options_unknown) is planned with
            # the stored decline wording verbatim — which is usually NOT the form's exact option text. Tell
            # the filler to pick the form's OWN decline-to-answer option, and that verify_fill accepts any
            # decline wording for this field, so a wording mismatch here isn't treated as a real mismatch.
            if (
                action == formfill.SELECT
                and r.get("options_unknown")
                and formfill._norm_option(value) in formfill._DECLINE_FORMS
            ):
                out.append(
                    f"  {i}. [{action}] {target} -> {shown}  "
                    "(options weren't readable; pick the form's OWN decline-to-answer option — "
                    'e.g. "Decline To Self Identify" or "I don\'t wish to answer" — '
                    "careercoach_verify_fill accepts any decline wording for this field)"
                )
            else:
                out.append(f"  {i}. [{action}] {target} -> {shown}")
    else:
        out.append("  (nothing to fill from saved answers)")

    out += [
        "",
        "Then re-read the form with browser_form_read and call "
        f'careercoach_verify_fill("{sid}", <that JSON>). Repeat until VERIFIED.',
        "",
        "Ask the operator about ONLY these:",
    ]
    if not unmapped and not unconfirmed:
        out.append("  (nothing — every required field has a confirmed answer; fill without asking)")
    for u in unmapped:
        req = "required" if u.get("required") else "optional"
        label = u.get("label", "")
        loc = u.get("target")
        head = label if (not loc or loc == label) else f"{label} [{loc}]"  # name the field when its label is shared
        line = f"  - {head} ({req}): {u.get('reason', 'no confirmed answer')}."
        if u.get("answer"):
            line += f" Your answer {u['answer']!r} matched no option."
        if u.get("options"):
            opts = ", ".join(str(o) for o in u["options"][:12])
            more = "" if len(u["options"]) <= 12 else f", … (+{len(u['options']) - 12} more)"
            line += f" Options: {opts}{more}"
        out.append(line)
    for u in unconfirmed:
        out.append(
            f"  - {u.get('label', '')}: you have a DRAFT answer ({u.get('key', '')}={u.get('draft', '')!r}); "
            "confirm it with careercoach_confirm_answers before it can fill."
        )
    return "\n".join(out)


def _kit_handoff(ats: str) -> str:
    return (
        f"KIT HANDOFF — browser fill is off for {ats or 'unknown ATS'}. "
        "careercoach_plan_fill is disabled for this ATS. Build the apply kit: "
        "careercoach_render_resume → browser_open (local file:// URL) → browser_pdf; keep the PDF path. "
        "Then write a cover letter only if the posting asks, then collect confirmed answers from "
        'careercoach_answers(action="get"). Only THEN call careercoach_save_apply_kit with all three together: '
        "resume_path=PDF path, cover_letter=letter text, answers_json=question-to-answer object/dict or JSON string. "
        'If no letter is needed, pass cover_letter="" and cover_letter_needed=false; '
        "set no_form_questions=true only if there genuinely are no form questions. "
        "If the reply says DRAFT, fill in the listed missing parts and save again with all three; report blockers. "
        "The operator fills the form from the Career Coach view using copy buttons. "
        "To opt into browser fill (beta), add the ATS type to the browser_fill setting "
        "(greenhouse, lever, ashby, workable). Unknown ATS types cannot be enabled. Never auto-submit."
    )


def _register_formfill_tools(registry) -> None:
    from . import answers, formfill

    @tool
    def careercoach_plan_fill(form_json: str, company: str, role: str, extra_answers: str = "", ats: str = "") -> str:
        """Plan a job-form fill from ONLY the operator's confirmed standard answers plus `extra_answers` (JSON object:
        field label → operator-supplied value). `form_json`: the exact array browser_form_read returned. `ats` is the
        ATS type you're applying through (greenhouse/lever/ashby/workable — the one careercoach_prepare_application or
        ats.detect reported); `ats="ashby"` also adds a required résumé upload first. Returns a session_id, ordered
        fill rows, and an "Ask the operator about ONLY these" list. A draft never fills; a select answer is never
        fuzzy-matched. Requires this ATS in the browser_fill beta setting; otherwise returns the apply-kit handoff
        without creating a session (an empty or unlisted ATS is off). The full loop: apply-form skill."""
        try:
            from .ats import fill_enabled

            if not fill_enabled(ats, registry.config.get("browser_fill", "")):
                return _kit_handoff((ats or "").strip().lower())
            fields, err = _parse_form_json(form_json)
            if err:
                return err
            extra, err = _parse_extra_answers(extra_answers)
            if err:
                return err
            ats_norm = (ats or "").strip().lower()
            resume_ready = _resume_renderable() if ats_norm == "ashby" else True
            company_s, role_s = (company or "").strip(), (role or "").strip()
            plan = formfill.build_plan(
                fields,
                answers.confirmed(),
                extra,
                ats=ats_norm,
                resume_ready=resume_ready,
                company=company_s,
                role=role_s,
            )
            formfill.update_session(plan["session_id"], company=company_s, role=role_s)
            return _render_plan(plan, company or "", role or "")
        except Exception as e:  # noqa: BLE001 — a tool never raises
            return f"Could not build a fill plan: {e}"

    @tool
    def careercoach_verify_fill(session_id: str, form_json: str) -> str:
        """Diff a filled form, re-read with browser_form_read (include radio checked/aria-checked — unchecked is
        unfilled), against the careercoach_plan_fill `session_id`. Returns VERIFIED or the mismatches and blank
        required fields; whitespace, case, phone formatting and upload paths are ignored. Only the latest check counts
        — fix and re-run until VERIFIED."""
        try:
            sid = (session_id or "").strip()
            session = formfill.load_session(sid)
            if session is None:
                return (
                    f"No fill plan found for session {sid!r}. Run careercoach_plan_fill first and use the "
                    "session_id it returns."
                )
            fields, err = _parse_form_json(form_json)
            if err:
                return err
            mismatches = formfill.diff(session, fields)
            formfill.record_verification(sid, mismatches)
            if not mismatches:
                return "VERIFIED — every field matches the plan and no required field is empty."
            lines = [f"NOT verified — {len(mismatches)} field(s) need fixing:"]
            for i, m in enumerate(mismatches, 1):
                label = m.get("label", "")
                loc = m.get("target")
                # name the field by its locator when its label is shared by another field
                where = label if (not loc or loc == label) else f"{label} [{loc}]"
                lines.append(f"  {i}. {where}: expected {m.get('expected', '')!r}, found {m.get('actual', '')!r}")
                if m.get("reason"):
                    lines.append(f"     {m['reason']}")
            lines.append(
                "Fix these (browser_select / browser_fill / browser_upload), then re-read the form and verify again."
            )
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001 — a tool never raises
            return f"Could not verify the fill: {e}"

    registry.register_tools([careercoach_plan_fill, careercoach_verify_fill])


# ── ATS schema prep: fetch the PUBLIC question schema before opening the page (#4032 phase 4a) ─
# Greenhouse publishes each job's application questions on its public Job Board API. Fetching them up
# front lets the coach prepare every answer it already holds and surface ONLY the genuinely new
# required questions to the operator — before a browser is even opened. URL detection + schema→field
# conversion live in ats.py (host-free, tested); the one network call (httpx) is there too and never
# raises. This tool is the thin async surface. The schema plan is PREPARATION: the live read-back diff
# (browser_form_read → careercoach_plan_fill → careercoach_verify_fill) stays mandatory before submit.
def _register_prepare_tool(registry) -> None:
    @tool
    async def careercoach_prepare_application(url: str, company: str = "", role: str = "") -> str:
        """Before opening a posting `url`, prepare answers from the ATS's public question schema. Greenhouse
        (job-boards./boards.greenhouse.io/<board>/jobs/<id>): a plan from CONFIRMED answers only plus an "Ask the
        operator about ONLY these" list. Browser fill defaults off: return an apply-kit handoff, retaining
        Greenhouse questions and confirmed answers for the kit. Enable an ATS via the browser_fill beta setting
        to use the live-form path. Preparation only — the live read-back (browser_form_read → careercoach_plan_fill → fill →
        careercoach_verify_fill) is still required before any submit."""
        from . import answers, ats, formfill

        try:
            detected = ats.detect(url)
            ats_name = detected["ats"] if detected else ""
            enabled = ats.fill_enabled(ats_name, registry.config.get("browser_fill", ""))
            if enabled:
                # Opted in: the agent MUST pass this ats to careercoach_plan_fill, which refuses any
                # ATS not in the browser_fill setting (an empty ats counts as off). Name it in the
                # live-form path so the fill session can actually start.
                live_path = (
                    "Open the application in the browser and use the live-form path: browser_form_read → "
                    f'careercoach_plan_fill(form_json, company, role, ats="{ats_name}") → fill → '
                    "careercoach_verify_fill."
                )
            else:
                live_path = _kit_handoff(ats_name)
            if not detected:
                return f"{url!r} isn't a recognised Greenhouse job URL, so there's no public schema to prepare from. {live_path}"
            if not enabled and ats_name != "greenhouse":
                return live_path
            if ats_name == "lever":
                return f"This is a Lever job. Open apply_url directly: {detected['apply_url']}. {live_path}"
            if detected.get("ats") == "workable":
                return (
                    f"This is a Workable job ({detected['company']}/{detected['job_id']}). "
                    f"Open apply_url directly: {detected['apply_url']} instead of clicking Apply on the overview. "
                    'Use browser_form_read → careercoach_plan_fill(form_json, company, role, ats="workable") → '
                    "fill → browser_form_read again → careercoach_verify_fill. "
                    "Read back the checked state of every radio/radio-group; "
                    "a successful click is not proof of selection."
                )
            if detected.get("ats") == "ashby":
                org, ashby_id = detected.get("board") or "?", detected.get("job_id") or "?"
                return (
                    f"This is an Ashby job ({org}/{ashby_id}). Ashby publishes no public per-job "
                    "application-form schema — its posting API returns only job-listing metadata — so "
                    "there is nothing to prepare up front and the coach does NOT scrape the page. Use the "
                    "live-form path: open the application, then browser_form_read → "
                    'careercoach_plan_fill(form_json, company, role, ats="ashby") → fill with the résumé '
                    "UPLOAD FIRST → browser_form_read again → careercoach_verify_fill, until the read-back "
                    "diff is empty. Ashby always requires a résumé FILE, and its 'autofill from résumé' "
                    "can overwrite fields, so every field must be re-read AFTER the upload."
                )
            board, job_id = detected.get("board"), detected.get("job_id")
            if not board:
                return (
                    f"Found a Greenhouse job id ({job_id}) but not the board token — this looks like a "
                    f"company careers page using gh_jid, so the public schema can't be fetched. {live_path}"
                )
            api_json, err = await ats.fetch_schema(board, job_id)
            if err:
                return f"Couldn't prepare from the Greenhouse schema: {err} {live_path}"
            fields = ats.greenhouse_fields(api_json)
            if not fields:
                return f"The Greenhouse schema had no fillable questions to prepare from. {live_path}"

            if not enabled:
                confirmed = answers.confirmed()
                lines = [live_path, "", "Greenhouse public-schema questions and confirmed answers for the kit:"]
                for field in fields:
                    key = formfill.classify(field)
                    value = confirmed.get(key) if key else None
                    label = field.get("label") or field.get("name") or "Question"
                    requirement = "required" if field.get("required") else "optional"
                    lines.append(
                        f"  - {label} ({requirement}): {value or 'no confirmed answer; ask the operator if needed'}"
                    )
                return "\n".join(lines)

            company_s, role_s = (company or "").strip(), (role or "").strip()
            # Fold the posting URL into the session id: prepare lets company/role default to "", so
            # without the URL two different postings with identical questions would share one id and
            # one verification slot — a grant for A could then be spent on B (bd-ywmm.4).
            plan = formfill.build_plan(
                fields, answers.confirmed(), company=company_s, role=role_s, posting=(url or "").strip()
            )
            formfill.update_session(plan["session_id"], company=company_s, role=role_s)
            title = (api_json.get("title") if isinstance(api_json, dict) else "") or ""
            preamble = (
                f"Prepared from the Greenhouse public schema for {title or 'this role'} "
                f"(board {board!r}, job {job_id}). This is PREPARATION — resolve the questions below with "
                "the operator now, but the live read-back is still required before any submit: browser_form_read → "
                'careercoach_plan_fill(form_json, company, role, ats="greenhouse") → fill → careercoach_verify_fill.\n'
            )
            return preamble + "\n" + _render_plan(plan, company or "", role or title)
        except Exception as e:  # noqa: BLE001 — a tool never raises
            return f"Could not prepare the application: {e}"

    registry.register_tool(careercoach_prepare_application)


# ── the HARD submit gate: operator approval card (interrupt) + submit-click middleware (#4032 3c) ─
# Phase 3's non-goal is "submitting without explicit operator consent". A skill line can't enforce
# that and neither can a tool the agent calls on its own, so consent comes from the operator through
# an interrupt the model can't forge, and submit clicks are BLOCKED unless that consent exists.
# Pure logic (grant state + submit detection) lives in submitgate.py (host-free, tested); these are
# the thin agent/host surface. This is defense in depth OVER the browser tools — it does not sandbox
# the browser, and the operator can still submit by hand in the visible browser.
def _turn_is_headless() -> bool:
    """Whether this turn has no operator who could answer an approval card.

    An ``interrupt`` only resolves inside a resumable graph run — one carrying a checkpointer and a
    thread to resume (an interactive chat turn). Called outside one — a headless/autonomous turn
    (the ``apply`` workflow, a background surface, a cron'd fleet member), or host-free — there's no
    operator to pause for, so ``request_submit`` refuses rather than hang a turn no one will answer.
    An explicit host headless/autonomous flag on the run config wins if present. Guarded: fails
    CLOSED (treats the turn as headless) when the signal can't be read."""
    try:
        from langgraph.config import get_config
    except Exception:  # noqa: BLE001 — no langgraph: an interrupt can't be answered at all
        return True
    try:
        cfg = get_config()
    except Exception:  # noqa: BLE001 — outside a runnable context → no operator to ask
        return True
    configurable = (cfg.get("configurable") or {}) if isinstance(cfg, dict) else {}
    if any(bool(configurable.get(k)) for k in ("headless", "is_headless", "autonomous", "is_autonomous")):
        return True
    return not configurable.get("thread_id")


def _submitgate_interrupt(payload: dict):
    """Pause the turn with ``langgraph.types.interrupt`` and return the operator's answer. Imported
    lazily and guarded like every other host seam. The ``GraphInterrupt`` it raises to pause is a
    bubble-up signal the graph runtime must receive, so it is NEVER caught here (see ``_is_graph_bubble``)."""
    from langgraph.types import interrupt

    return interrupt(payload)


def _is_graph_bubble(exc: BaseException) -> bool:
    """Whether ``exc`` is langgraph's pause/resume bubble-up (``GraphBubbleUp`` — base of
    ``GraphInterrupt``). A tool catching one would break the human-in-the-loop pause, so the submit
    tool re-raises it instead of turning it into a string. Guarded: no langgraph → nothing bubbles."""
    try:
        from langgraph.errors import GraphBubbleUp
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, GraphBubbleUp)


# Explicit approve words only — default-deny, modelled on the delegates consent path. Anything
# that isn't an unambiguous yes is a decline, so a vague or empty answer never authorizes a submit.
_SUBMIT_APPROVE_WORDS = frozenset(
    {"approve", "approved", "yes", "y", "ok", "okay", "confirm", "confirmed", "allow", "accept", "submit"}
)


def _is_submit_approval(answer) -> bool:
    """Parse an interrupt answer into approve (``True``) / decline (``False``). Accepts a bare
    string, a bool, or a choice object (``{"choice": "approve"}`` / ``value`` / ``decision`` / …).
    Default-deny: only an explicit approve returns ``True``."""
    value = answer
    if isinstance(answer, dict):
        for key in ("choice", "value", "decision", "action", "response", "answer", "approved"):
            if answer.get(key) not in (None, ""):
                value = answer.get(key)
                break
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _SUBMIT_APPROVE_WORDS


def _submit_ready(session, session_id: str) -> tuple[bool, str]:
    """Whether a fill session is safe to authorize for submit: it must be VERIFIED — its latest
    ``careercoach_verify_fill`` read-back diff was empty, AND that verification describes the plan as
    it stands now. "Changed since it was verified" is enforced upstream: ``formfill`` resets
    verification on every ``build_plan`` write, so a re-plan of this session comes back unverified
    until read back again. Verification is also EXCLUSIVE — planning or verifying any other form
    clears this one — so a grant for form A reads as not-verified here once form B is planned. A plan
    that was re-planned or superseded since its last verification is refused WITHOUT asking the
    operator. This is the pre-interrupt gate; the grant itself binds to the specific verification
    EVENT (``formfill.current_verification``), so submit safety does not depend on session ids being
    unique per application. Returns ``(ok, reason)`` — ``reason`` is empty when ``ok``."""
    if not isinstance(session, dict):
        return False, "no fill plan was found for this session."
    if not session.get("verified"):
        return False, (
            "this form isn't VERIFIED — its latest read-back diff wasn't empty, or the plan was "
            "re-planned or changed since it was last verified."
        )
    return True, ""


def _submit_approval_card(session, session_id: str) -> dict:
    """The approval card an operator sees in the interrupt: company, role, and the planned
    field→value table (skip rows excluded, upload rows flagged), plus the explicit choices."""
    from . import formfill

    rows = session.get("rows", []) if isinstance(session, dict) else []
    fields: list[dict] = []
    table_lines: list[str] = []
    table_rows: list[list[str]] = []
    for r in rows:
        if not isinstance(r, dict) or r.get("action") == formfill.SKIP:
            continue
        label = str(r.get("label", ""))
        value = str(r.get("value", ""))
        action = str(r.get("action", ""))
        fields.append({"label": label, "value": value, "action": action})
        shown = f"{value}  (upload)" if action == formfill.UPLOAD else value
        table_lines.append(f"  {label} -> {shown}")
        table_rows.append([label, shown])

    company = str((session or {}).get("company") or "").strip()
    role = str((session or {}).get("role") or "").strip()
    who = " — ".join(x for x in (role, company) if x) or "this application"
    prompt = (
        f"Submit this job application? ({who})\n\n"
        + ("Planned answers:\n" + "\n".join(table_lines) if table_lines else "(no planned fields)")
        + "\n\nApprove to authorize ONE submit click (valid 120s); decline to stop."
    )
    return {
        # Core's approval shape (see `_handoff_card`): the summary as prose, the planned answers as
        # the detail block, and Approve / Decline buttons whose values `_is_submit_approval` reads.
        "kind": "approval",
        "type": "careercoach_submit_approval",
        "title": "Approve submitting this job application?",
        "description": (
            f"Submit this job application? ({who})\n\n"
            "Approve to authorize ONE submit click (valid 120s); decline to stop."
        ),
        "detail": "\n".join(table_lines) if table_lines else "(no planned fields)",
        "options": [
            {"label": "Approve submit", "value": "approve", "primary": True},
            {"label": "Decline", "value": "decline"},
        ],
        # A structured rendering of the SAME non-SKIP, upload-marked rows the `detail` block carries,
        # for the protoAgent display renderer (protoAgent#4405). Older consoles/clients ignore this
        # key and fall back to `detail`/`prompt` unchanged; the shape follows the core table bounds.
        "display": {
            "component": "table",
            "props": {"columns": ["Field", "Value"], "rows": table_rows},
        },
        "company": company,
        "role": role,
        "session_id": session_id,
        "fields": fields,
        "prompt": prompt,
    }


def _register_submit_gate(registry, fill_on: bool) -> None:
    """Register ``careercoach_request_submit`` (the operator-approval tool) and the middleware that
    blocks submit-like browser calls without a live grant. The middleware is the enforcement and is
    ALWAYS registered (unchanged) — with browser fill off and no request_submit tool there is no way
    to mint a grant, so it default-denies every submit-like browser call. The approval tool is one of
    the browser-fill tools, so it registers only when ``browser_fill`` enables at least one ATS."""
    from . import formfill, submitgate

    @tool
    def careercoach_request_submit(session_id: str) -> str:
        """Ask the operator to approve submitting a filled application; only on explicit approval, unlock exactly one
        submit click for 120s. A hard gate: submit-like browser calls are blocked without it (an "Apply" button that
        only opens the form isn't). Refuses without asking unless the careercoach_plan_fill `session_id` is VERIFIED
        and unchanged since; refuses on headless/autonomous turns."""
        try:
            sid = (session_id or "").strip()
            session = formfill.load_session(sid)
            if session is None:
                return (
                    f"No fill plan found for session {sid!r}. Run careercoach_plan_fill, fill the form, "
                    "and careercoach_verify_fill until VERIFIED before requesting submit."
                )
            ready, reason = _submit_ready(session, sid)
            if not ready:
                return (
                    f"Submit not authorized — {reason} Fix the form and run careercoach_verify_fill until "
                    "it reports VERIFIED, then request submit again."
                )
            if _turn_is_headless():
                return (
                    "Submit not authorized: this is a headless or autonomous turn with no operator to "
                    "approve it. I will not submit an application without an explicit operator approval — "
                    "run this again in an interactive session, or submit by hand in the browser."
                )
            # Capture the verification event the operator is about to approve. The grant binds to this
            # exact (session_id, verification_id) pair, not to the session id alone — so it authorizes
            # ONLY this read-back, never a later one or a colliding form's.
            vid = formfill.current_verification(sid)
            if not vid:
                return (
                    "Submit not authorized — this form isn't VERIFIED right now. Run "
                    "careercoach_verify_fill until it reports VERIFIED, then request submit again."
                )
            answer = _submitgate_interrupt(_submit_approval_card(session, sid))
            if _is_submit_approval(answer):
                # If the verification changed while the approval card was open — a re-plan, a
                # re-verification, or another form read back — the approval no longer matches the
                # current form, so refuse rather than grant against a stale event.
                if formfill.current_verification(sid) != vid:
                    return (
                        "Submit not authorized — the form was re-planned or re-verified while the "
                        "approval card was open, so the approval no longer matches the current form. "
                        "Run careercoach_verify_fill until VERIFIED, then request submit again."
                    )
                submitgate.grant(sid, vid)
                return (
                    "Submit authorized for one click within 120s: click the form's submit button now, "
                    "then browser_form_read/snapshot to confirm the confirmation page."
                )
            return "Operator declined; do not submit."
        except Exception as e:  # noqa: BLE001 — a tool returns a readable string, never raises…
            if _is_graph_bubble(e):  # …except langgraph's pause signal, which must reach the runtime
                raise
            return f"Could not request submit approval: {e}"

    if fill_on:  # a browser-fill tool — registers only when browser_fill enables an ATS
        registry.register_tool(careercoach_request_submit)
    _register_submit_middleware(registry)  # enforcement is always on (the submit gate is unchanged)


def _register_submit_middleware(registry) -> None:
    """The enforcement: an ``AgentMiddleware`` that intercepts every tool call and short-circuits a
    submit-like one unless a live operator grant exists AND the session it was approved for is still
    VERIFIED (the grant is then consumed). Guarded like the profile middleware — if
    ``AgentMiddleware`` isn't importable (older host) the plugin still registers; it just can't block
    (the browser tools' own confirmations remain the only guard)."""
    try:
        from langchain.agents.middleware import AgentMiddleware
    except ImportError:  # pragma: no cover — no langchain agents
        log.debug("[careercoach] AgentMiddleware unavailable; submit gate middleware skipped")
        return

    from langchain_core.messages import ToolMessage

    from . import formfill, submitgate

    BLOCKED = (
        "Blocked by careercoach submit gate: no operator authorization. Run careercoach_verify_fill "
        "until VERIFIED, then careercoach_request_submit."
    )

    class _SubmitGateMiddleware(AgentMiddleware):
        """Block submit-like browser calls unless an operator grant is live AND still tied to the
        exact verification event it was approved for.

        A grant is one-shot and created only by ``careercoach_request_submit`` after the operator
        approves the interrupt — the model can't forge it. A ``browser_click`` carries no session
        id, so the gate can't read a session off the click itself; instead it checks the live grant's
        ``(session_id, verification_id)`` pair AGAINST ``formfill.current_verification`` — spending it
        only while that session's CURRENT verification is still the exact id granted. A grant whose
        plan was re-planned (id cleared), re-verified (new id), or displaced by a colliding form B's
        read-back (also a new id) is therefore inert, and can't authorize a submit against anything
        but the one form+read-back the operator saw. Binding to the verification_id — not the session
        id, which two forms can share — is what makes this hold even on an id collision. On a
        submit-like call with a matching grant the grant is consumed and the call runs; otherwise the
        call is short-circuited with a ``ToolMessage`` carrying the SAME ``tool_call_id`` (so the
        transcript stays valid) and the browser tool never runs. Every non-submit call passes through
        untouched."""

        def _blocked(self, request):
            """A ``ToolMessage`` to return instead of running the tool, or ``None`` to pass through."""
            call = getattr(request, "tool_call", None) or {}
            name = call.get("name", "")
            args = call.get("args", {})
            if not submitgate.is_submit_like(name, args):
                return None  # not submit-like — never touch it
            # A live grant alone isn't enough. The click names no session, so THIS is where the gate
            # checks the grant against the exact verification event it was approved for: the grant's
            # (session_id, verification_id) pair must still be the session's CURRENT verification in
            # formfill. A re-plan clears that id, and a re-verification — of this form OR of a form B
            # that collides onto its session id — mints a NEW id, so a stale grant no longer matches
            # and can't authorize a submit. Only on an exact match is the one-shot grant spent.
            grant = submitgate.active_grant()
            if grant is not None:
                sid, vid = grant
                if vid and formfill.current_verification(sid) == vid and submitgate.consume(sid, vid):
                    return None  # live grant for the still-current verification; let this call through
            return ToolMessage(content=BLOCKED, tool_call_id=call.get("id", ""), name=name)

        def wrap_tool_call(self, request, handler):
            blocked = self._blocked(request)
            return blocked if blocked is not None else handler(request)

        async def awrap_tool_call(self, request, handler):
            blocked = self._blocked(request)
            if blocked is not None:
                return blocked
            return await handler(request)

    registry.register_middleware(lambda config: _SubmitGateMiddleware())


# ── the visible-browser handoff: the one step only a human can do (#4032 phase 4c) ─
# Where a human is genuinely required — a captcha, a login/sign-in, a legal attestation — the agent
# fills everything else and hands over a VISIBLE browser for that one step: the interactive Browser
# panel (agent_browser's live CDP screencast viewport in the console) or the real window the browser
# plugin's ``headed`` setting opens. Solving or bypassing a captcha is an explicit NON-GOAL, so this
# tool never touches one — it only pauses for the operator, with the same ``interrupt`` + headless
# refusal as the submit gate (and the same guarded, lazy import). Because a captcha or login can
# re-render the form and clear fields, a Done comes back with a MANDATORY read-back before any submit
# can be requested. It creates no grant and clicks nothing.
_HANDOFF_REASONS = {
    "captcha": "a captcha / 'confirm you're human' challenge",
    "login": "a login / sign-in step",
    "attestation": "a legal attestation or declaration you must personally agree to",
    "other": "a step only a human can complete",
}

# Explicit done words only — default-deny, like the submit gate's approve words. Anything that is not
# an unambiguous "done" is a cancel, so a vague or empty answer stops rather than marching on to submit.
_HANDOFF_DONE_WORDS = frozenset(
    {"done", "complete", "completed", "finish", "finished", "ready", "continue", "proceed", "ok", "okay", "yes"}
)


def _handoff_interrupt(payload: dict):
    """Pause the turn with ``langgraph.types.interrupt`` for the visible-browser handoff and return
    the operator's answer. Imported lazily and guarded like every other host seam. The
    ``GraphInterrupt`` it raises to pause is a bubble-up signal the runtime must receive, so it is
    NEVER caught here (see ``_is_graph_bubble``)."""
    from langgraph.types import interrupt

    return interrupt(payload)


def _is_handoff_done(answer) -> bool:
    """Parse a handoff answer into Done (``True``) / Cancel (``False``). Accepts a bare string, a
    bool, or a choice object (``{"choice": "done"}`` / ``value`` / ``decision`` / …). Default-deny:
    only an explicit Done returns ``True``, so an ambiguous answer never reads as "step completed"."""
    value = answer
    if isinstance(answer, dict):
        for key in ("choice", "value", "decision", "action", "response", "answer", "done"):
            if answer.get(key) not in (None, ""):
                value = answer.get(key)
                break
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _HANDOFF_DONE_WORDS


def _handoff_card(session_id: str, reason: str) -> dict:
    """The card an operator sees in the handoff interrupt: what step to complete, in which visible
    browser, the reminder not to submit, and the explicit Done / Cancel choice."""
    what = _HANDOFF_REASONS[reason]
    prompt = (
        f"A step on this page needs you: {what}.\n\n"
        "Open the live page in a VISIBLE browser — the Browser panel (the live viewport in the "
        "console), or the real window if you've turned on the browser's `headed` setting — and "
        f"complete ONLY this one step ({reason}). The agent has already filled everything it can.\n\n"
        "Do NOT submit the application unless you mean to — submitting is approved separately through "
        "its own card. Choose Done once the step is complete, or Cancel to stop."
    )
    return {
        # Core's approval shape, so the console renders the prompt as prose with real Done / Cancel
        # buttons; any other dict reaches the operator as its raw repr. Each button resumes with its
        # `value`, which `_is_handoff_done` reads.
        "kind": "approval",
        "type": "careercoach_handoff",
        "title": "Finish one step in the visible browser",
        "description": prompt,
        "options": [{"label": "Done", "value": "done", "primary": True}, {"label": "Cancel", "value": "cancel"}],
        "reason": reason,
        "session_id": session_id,
        "prompt": prompt,
    }


def _register_handoff(registry) -> None:
    """Register ``careercoach_handoff`` — the visible-browser handoff. It shares the submit gate's
    ``interrupt`` + headless-refusal pattern but creates no grant and clicks nothing: it hands a
    human the page for the one step only they can do, then requires a fresh read-back before submit."""

    from . import formfill, submitgate

    @tool
    def careercoach_handoff(session_id: str, reason: str) -> str:
        """Hand the operator a visible browser for a step only a human can do; `reason`: captcha, login, attestation,
        other. It NEVER solves or bypasses a captcha and clicks nothing — never use it to get past a bot check. Pauses
        with Done/Cancel; refuses on headless turns. After Done, re-read with browser_form_read and
        careercoach_verify_fill(session_id) until VERIFIED before careercoach_request_submit."""
        try:
            sid = (session_id or "").strip()
            key = (reason or "").strip().lower()
            if key not in _HANDOFF_REASONS:
                return (
                    f"Unknown handoff reason {reason!r}. Use one of: {', '.join(_HANDOFF_REASONS)}. This hands "
                    "a human the step only they can do (a captcha, a login, a legal attestation); it never "
                    "solves or bypasses a captcha."
                )
            if _turn_is_headless():
                return (
                    f"Handoff not possible: this is a headless or autonomous turn with no operator to complete "
                    f"the {key} step in a visible browser. I can't and won't solve or bypass it myself — stop "
                    "here and report that a human needs to finish this step in an interactive session."
                )
            answer = _handoff_interrupt(_handoff_card(sid, key))
            # A human just had the live page in a visible browser; whatever they did — and the
            # captcha / login / attestation itself — can re-render the form and clear filled fields,
            # so the verification made BEFORE the handoff no longer describes it. Drop that
            # verification and revoke any live submit grant, so the mandatory browser_form_read +
            # careercoach_verify_fill (and a fresh careercoach_request_submit approval) are required
            # before any submit can go through — the stale pre-handoff state can no longer authorize
            # one. Done or Cancel alike: control left the agent either way.
            if sid:
                formfill.invalidate_verification(sid)
            submitgate.clear()
            if _is_handoff_done(answer):
                verify = f'careercoach_verify_fill("{sid}", <that JSON>)' if sid else "careercoach_verify_fill"
                return (
                    f"Operator completed the {key} step. Before any submit, re-read the form with "
                    f"browser_form_read and run {verify} — a {key} can re-render the form and clear filled "
                    "fields, so this read-back is mandatory and must report VERIFIED before "
                    "careercoach_request_submit. Never request submit on a form you haven't re-read since the handoff."
                )
            return "Operator cancelled the handoff; stop."
        except Exception as e:  # noqa: BLE001 — a tool returns a readable string, never raises…
            if _is_graph_bubble(e):  # …except langgraph's pause signal, which must reach the runtime
                raise
            return f"Could not hand off to the operator: {e}"

    registry.register_tool(careercoach_handoff)


# ── a tunable control surface: the fit rubric as live Knobs (graph.sdk) ───────
def _register_rubric_knobs(registry) -> None:
    """Expose the four rubric weights as agent-tunable knobs + named presets, so the fit
    evaluation can be retuned live (``careercoach_tune weight_career 45``, ``careercoach_preset
    growth-first``). Guarded: if the host SDK isn't present (host-free tests), the core still
    registers — only the knob tools are skipped."""
    from .rubric import DEFAULT_WEIGHTS, DIMENSIONS, PRESETS

    try:
        from graph.sdk import Knobs, make_knob_tools
    except Exception as e:  # noqa: BLE001
        log.info("[careercoach] knob tools unavailable (host-free?): %s", e)
        return
    try:
        knobs = Knobs()
        for dim in DIMENSIONS:
            knobs.define(f"weight_{dim}", DEFAULT_WEIGHTS[dim], lo=0, hi=100)
        for name, preset in PRESETS.items():
            knobs.preset(name, {f"weight_{k}": v for k, v in preset.items()})
        registry.register_tools(make_knob_tools(knobs, prefix="careercoach"))
    except Exception as e:  # noqa: BLE001
        log.warning("[careercoach] failed to wire rubric knobs: %s", e)


# ── a delegate crew: research → evaluate → write (register_subagent) ──────────
# Three purpose-built subagents that the `apply` workflow chains (ADR 0002). Each carries a
# COMPACT prompt and is granted `load_skill` so it can pull the full discipline from the
# `apply` skill (the source of truth) rather than duplicating it here.
#
# The host builds subagents WITHOUT plugin middleware (graph/agent.py's subagent stack), so the
# always-on <operator_profile> block never reaches them. The two that judge or write about the
# candidate get the read-only grounding tool instead (careercoach_read_sources), and their prompts
# say to read before working. It is on the DEFAULT surface, so the grant resolves in the shipped
# config (extended_tools off) — and because it can only read, the autonomous crew holds no tool that
# writes the record or confirms an answer in the operator's name.
DELEGATE_READ_TOOL = "careercoach_read_sources"


def _register_subagents(registry) -> None:
    try:
        from graph.subagents.config import SubagentConfig
    except Exception as e:  # noqa: BLE001
        # An older host (or a test harness without the stand-in) — keep register() green.
        log.info("[careercoach] subagent registration skipped (host-free?): %s", e)
        return

    company_researcher = SubagentConfig(
        name="company_researcher",
        description=(
            "Researches a company before you apply or interview: mission, products, recent news, "
            "funding/layoffs, Glassdoor sentiment, team, and likely interviewers. Returns a SOURCED "
            "brief — every company-specific claim carries a link, so nothing unverified reaches a cover letter."
        ),
        system_prompt=(
            "You are company_researcher, the Career Coach's research delegate. Given a company (and "
            "optionally a role), gather a concise, SOURCED brief: what they do; mission and values; "
            "recent news (funding, launches, layoffs, restructuring); Glassdoor/employee sentiment; "
            "team size and notable people; and any red or green flags for a candidate. Use web_search "
            "and fetch_url. Cite a URL for every factual claim; if you cannot verify something, say so "
            "rather than guessing. Finish with two short lists: 'Questions worth asking in an interview' "
            "and 'Things to verify before applying'."
        ),
        tools=["web_search", "fetch_url", "current_time"],
    )

    job_evaluator = SubagentConfig(
        name="job_evaluator",
        description=(
            "Scores a job posting against the Career Coach's weighted fit rubric and returns the "
            "evaluation table, overall score, verdict, strengths, gaps, and a straight recommendation."
        ),
        system_prompt=(
            "You are job_evaluator, the Career Coach's fit assessor. Score a posting on four "
            "dimensions (defaults: technical 30, experience 25, behavioral 15, career 30) plus a "
            "location pass/fail gate, then take the weighted overall and map it to a verdict band. "
            "Score the REAL candidate: before scoring, read their verified history with "
            "careercoach_read_sources(doc='experience') and their skills with "
            "careercoach_read_sources(field='skills'); if both come back empty, say the score can't be "
            "grounded rather than scoring an imagined background. "
            "For the full rubric, bands, and exact output format, call load_skill('apply') and read "
            "its job-evaluation.md. Use web_search/fetch_url for missing context. "
            "Output the evaluation table, weighted overall, verdict, strengths, gaps, and a clear "
            "apply/apply-with-caveats/skip recommendation, then record it with "
            "careercoach_track(action='track'). Be honest: a weak fit is a weak fit — never inflate a score."
        ),
        tools=[
            "load_skill",
            "list_skills",
            "web_search",
            "fetch_url",
            "current_time",
            "careercoach_track",
            DELEGATE_READ_TOOL,
        ],
    )

    application_writer = SubagentConfig(
        name="application_writer",
        description=(
            "Given an evaluation + company research, builds and saves an apply kit — a tailored résumé "
            "PDF, a cover letter only when the posting asks, and confirmed answers — then hands off to "
            "the operator in the Career Coach view. Reframes emphasis, never fabricates, never submits."
        ),
        system_prompt=(
            "You are application_writer, the Career Coach's apply-kit builder. Given a fit evaluation "
            "and a company-research brief, BUILD AND SAVE an apply kit that the operator completes "
            "themselves — you never open or drive the employer's form, and nothing auto-submits. "
            "FIRST read what is true about the candidate: careercoach_read_sources(doc='experience') "
            "for their verified career history and careercoach_read_sources(field='do_not_claim') for "
            "the lines you must never claim (plus careercoach_read_sources(field='skills') and "
            "(field='stories') as needed). "
            "Draft only from what those return, and treat every do_not_claim line as a hard stop. If "
            "the history comes back empty, say so and stop: never invent a career. If the verdict was "
            "Weak or Poor Fit, say so and stop rather than forcing a kit. Follow the writing-style "
            "discipline: no em-dashes, no clichés, and the interview-backtrack honesty test (reframe "
            "emphasis, NEVER claim experience the candidate lacks). For the full guidance call "
            "load_skill('apply'): its SKILL.md carries the apply-kit flow, and read its writing-style.md, "
            "cv-guide.md, and cover-letter-guide.md for the writing discipline. "
            "Every candidate metric must come from the profile or story bank; if missing, write "
            "[fill in: <what>] and never estimate; before showing a draft, run "
            "careercoach_read_sources(source='check', text=<draft>) and put each flagged figure to the "
            "operator as a question or replace it with a placeholder, never silently include it. "
            "Build the tailored résumé PDF from the operator-approved résumé with "
            "careercoach_render_resume(company) → browser_open (the returned local file:// URL only) → "
            "browser_pdf; keep the returned PDF path, and never silently change the saved profile. If "
            "rendering refuses or a PDF tool is unavailable, report the blocker instead of claiming a "
            "PDF was built. Write a cover letter ONLY if the posting asks for one, verifying every "
            "company-specific claim against the research brief's sources before including it. Read "
            "every form answer with careercoach_read_sources(source='answers'): keep CONFIRMED and "
            "DRAFT answers distinct, "
            "leave drafts as drafts, and report new or missing questions for the operator rather than "
            "inventing answers. A workflow run has NO operator in the loop, so you never confirm "
            "answers or write the operator's profile — you hold no tool for either; save one-off "
            "answers only as DRAFTS and leave every confirmation and profile change to the operator in "
            "the Career Coach view. When the posting is a URL, fetch its public form questions with "
            "careercoach_prepare_application (it returns a kit handoff when browser fill is off). Only THEN call "
            "careercoach_save_apply_kit with all three together: resume_path=the returned PDF path, "
            "cover_letter=letter text, answers_json=a question-to-answer object/dict or JSON string "
            "(not the answer-bank report). Include resume_version if known; kit answers are stored as drafts. "
            'When no letter is requested pass cover_letter="" and cover_letter_needed=false. '
            "Set no_form_questions=true only when there genuinely are no form questions. "
            "If the reply says DRAFT, fill in the listed missing parts and save again with all three; "
            "report blockers requiring operator input without claiming completion. Then hand "
            "off: tell the operator to open the role in the Career Coach view, review the drafts, "
            "attach the PDF, fill the form with the copy buttons, and press Mark applied after "
            "submitting it themselves — do not mark the role applied yourself. End with a verification "
            "checklist (factual accuracy against the profile, do_not_claim respected, company claims "
            "verified, style, and whether the kit saved)."
        ),
        # No operator gates a workflow run, so this autonomous drafter is granted only read-only
        # (careercoach_read_sources), draft-only, résumé-render, kit-save and tracking tools: never a
        # profile write or answer confirmation (careercoach_profile's update/write/import or
        # careercoach_answers' confirm — gate-less writes in the operator's name) nor any
        # form-driving/submit tool. The operator confirms and files later in the Career Coach view.
        tools=[
            "load_skill",
            "list_skills",
            DELEGATE_READ_TOOL,
            "careercoach_prepare_application",
            "careercoach_render_resume",
            "careercoach_save_apply_kit",
            "careercoach_track",
            "browser_open",
            "browser_pdf",
            "current_time",
        ],
    )

    for cfg in (company_researcher, job_evaluator, application_writer):
        registry.register_subagent(cfg)


# ── always-on context: the operator profile, in front of every model call (ADR 0032) ───
def _context_frame(text: str):
    """``text`` as the host's injected-context frame (``graph.context_frame``: a tagged
    ``HumanMessage`` in an ``<injected_context>`` envelope), or the same shape built here on a host
    that predates the module — tagged identically, so the host still recognises it as a frame."""
    try:
        from graph.context_frame import context_frame_message

        return context_frame_message(text)
    except Exception:  # noqa: BLE001 — older host / host-free
        from langchain_core.messages import HumanMessage

        return HumanMessage(
            content=f"<injected_context>\n{text}\n</injected_context>",
            additional_kwargs={"protoagent_injected_context": True},
        )


def _stash_for_prompt_capture(text: str) -> None:
    """Hand the block to the host's prompt capture (ADR 0108 D5) so the prompt viewer shows what
    this call really carried. A no-op on a host without the seam or with capture off."""
    try:
        from graph.context_frame import stash_projected_context
    except Exception:  # noqa: BLE001
        return
    try:
        stash_projected_context(text)
    except Exception:  # noqa: BLE001 — observability must never break a turn
        log.debug("[careercoach] prompt-capture stash failed", exc_info=True)


def _standard_answers_note() -> str:
    """One always-on line for the profile block: how many standard application answers are confirmed,
    and the rule that unconfirmed ones never fill a form. Kept to a single line so always-on context
    stays small; "" on any failure, so it can never break a turn."""
    try:
        from . import answers

        total = len(answers.STANDARD_KEYS)
        n = len(answers.confirmed())
        return f"{n}/{total} standard answers confirmed; unconfirmed answers are never used to fill a form."
    except Exception:  # noqa: BLE001 — context injection must never break a turn
        return ""


def _with_standard_answers(block: str) -> str:
    """Splice the standard-answers note into the ``<operator_profile>`` block, just before its
    closing tag, so it rides along with the always-on profile context (recomputed per call, so a
    freshly confirmed answer shows without re-reading the whole profile)."""
    note = _standard_answers_note()
    if not block or not note:
        return block
    closing = "</operator_profile>"
    stripped = block.rstrip()
    if stripped.endswith(closing):
        head = stripped[: -len(closing)].rstrip("\n")
        return f"{head}\n\n{note}\n{closing}"
    return f"{block}\n{note}"


def _register_profile_middleware(registry) -> None:
    """Put the operator's profile in front of the model on every call.

    Without this the agent has no cheap way to know what it already knows, so it re-interviews
    for facts it holds — the failure that made a real first run collapse into "stop the 21
    questions". A recall tool can't fix that: the agent has to already suspect there's something
    to recall. Always-on completeness removes the guess.

    Guarded like every other host seam: if ``AgentMiddleware`` isn't importable (older host),
    the plugin still registers and simply doesn't inject."""
    try:
        from langchain.agents.middleware import AgentMiddleware
    except ImportError:  # pragma: no cover — no langchain agents
        log.debug("[careercoach] AgentMiddleware unavailable; profile injection skipped")
        return

    from . import profile

    class _ProfileMiddleware(AgentMiddleware):
        """Deliver ``<operator_profile>`` as an ephemeral frame on every model call — the host's
        own contract for derived context (ADR 0108 D2; ``graph/middleware/tool_delta.py``).

        **Why not the ``context`` state channel.** v0.6 returned ``{"context": …}`` from
        ``before_model``. protoAgent #3234 (v0.155.0) removed that channel and its last reader, and
        LangGraph drops an update for a key the state doesn't declare — so the block silently never
        reached the model. It also re-read its own previous block from that channel and appended
        another each call. Now the block is composed from the profile file and appended to the
        request's messages via ``request.override``: never returned as state, never checkpointed,
        exactly one copy per call.

        **Cache-stable.** The system prompt is untouched, and the frame goes at the tail, where the
        host's knowledge frame goes. ``PromptCacheMiddleware`` sits outside every plugin middleware,
        so its breakpoints (system prefix + newest history messages) are placed before this frame
        is added: the block never displaces them.

        **Fresh without re-reading.** Composed at turn entry (``before_agent``) and recomposed only
        when the profile file actually changes, so a field recorded mid-turn shows on the next
        call while an unchanged profile costs one ``stat`` per call."""

        def __init__(self):
            super().__init__()
            self._block: str = ""
            self._sig: object = False  # sentinel: nothing composed yet

        def _current(self, *, force: bool = False) -> str:
            try:
                sig = profile.store_signature()
                if force or sig != self._sig:
                    self._block = profile.context_block()
                    if not self._block:
                        self._block = (
                            "<operator_profile>\n"
                            "First run: profile empty — follow skills/job-hunt/SKILL.md's first-run flow; "
                            "ask at most one confirmation question before the first scan.\n"
                            "</operator_profile>"
                        )
                    self._sig = sig
            except Exception as exc:  # noqa: BLE001 — context injection must never break a turn
                log.debug("[careercoach] profile injection failed: %s", exc)
            return self._block

        def before_agent(self, state, runtime) -> dict | None:  # type: ignore[override]
            self._current(force=True)
            return None

        async def abefore_agent(self, state, runtime) -> dict | None:  # type: ignore[override]
            return self.before_agent(state, runtime)

        def _with_profile(self, request):
            block = self._current()
            if not block:
                return request  # nothing known yet; /setup-coach owns the cold start
            block = _with_standard_answers(block)
            msgs = list(getattr(request, "messages", None) or [])
            msgs.append(_context_frame(block))
            _stash_for_prompt_capture(block)
            return request.override(messages=msgs)

        def wrap_model_call(self, request, handler):
            return handler(self._with_profile(request))

        async def awrap_model_call(self, request, handler):
            return await handler(self._with_profile(request))

    registry.register_middleware(lambda config: _ProfileMiddleware())


# ── console rail view: the Career Coach dashboard (ADR 0026) ──────────────────
def _register_views(registry, cfg) -> None:
    """Serve the dashboard PAGE (public, un-gated — an iframe load can't carry a bearer)
    and its DATA route (gated under /api/plugins/careercoach). Models the view-vs-data
    gating rule and the fleet-proxy-safe fetch (ADR 0026/0042), like the notes plugin."""
    from fastapi import APIRouter, Body
    from fastapi.responses import HTMLResponse, JSONResponse

    from . import answers as ans_mod
    from . import contacts, profile, state
    from . import watchlist as wl
    from .rubric import DEFAULT_WEIGHTS

    data = APIRouter()

    def _followup_days() -> int:
        try:
            return int(cfg.get("followup_days", 10) or 10)
        except (TypeError, ValueError):
            return 10

    @data.get("/state")
    async def _state():
        prof, profile_error = profile.load_profile_checked()
        return JSONResponse(
            {
                "settings": {
                    "name": cfg.get("full_name", ""),
                    "location": cfg.get("location", ""),
                    "target_roles": cfg.get("target_roles", ""),
                },
                # The operator profile, verbatim + its completeness, so the panel shows exactly
                # what the agent is told each turn. Transparency is the point: the person being
                # described should never have to open a file to see their own record.
                "profile": prof,
                "profile_error": profile_error,  # why profile.json can't be read ("" when fine)
                "completeness": profile.completeness(prof),
                "field_labels": {**profile.IDENTITY_FIELDS, **profile.SECTIONS},
                "context_block": profile.context_block(),
                "weights": DEFAULT_WEIGHTS,  # the rubric defaults; the agent tunes live via knobs
                "applications": state.load_applications(),
            }
        )

    def _emit_tracked(company: str, role: str, status: str) -> None:
        try:
            registry.emit("application_tracked", {"company": company, "role": role, "status": status})
        except Exception:  # noqa: BLE001 — the event bus is best-effort chrome
            pass

    @data.get("/jobs")
    async def _jobs():
        """The pipeline board: the watch inbox, the tracked applications (each flagged `followup`
        when an applied role is overdue a nudge), the column mapping, the follow-up window, and
        when the watch last ran ("" if never)."""
        days = _followup_days()
        today = state._today()
        try:
            inbox = wl.inbox_for_view()
        except wl.StoreUnreadable:
            inbox = []  # an unreadable inbox reads as empty — the board still renders the pipeline
        apps = [{**r, "followup": state.needs_followup(r, days, today)} for r in state.load_applications()]
        return JSONResponse(
            {
                "inbox": inbox,
                "applications": apps,
                "columns": state.COLUMNS,
                "followup_days": days,
                "watch_last_run": wl.watch_last_run(),
            }
        )

    def _keep_inbox_item(item_id: str):
        """Promote a watch-inbox item into the pipeline as a `considering` row (keeping its reason
        and posting evidence) and hide it from the inbox. Existing applications are
        preserved unchanged."""
        try:
            item = wl.inbox_item(item_id)
        except wl.StoreUnreadable as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        if item is None:
            return JSONResponse({"error": f"no inbox item {item_id!r}"}, status_code=404)
        try:
            row = _promote_inbox_item(item)
        except (state.StoreUnreadable, wl.StoreUnreadable) as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        _emit_tracked(row.get("company", ""), row.get("role", ""), row.get("status", ""))
        return JSONResponse({"ok": True, "application": row})

    def _dismiss_inbox_item(item_id: str):
        """Drop a watch-inbox item and remember the decision so it doesn't come back."""
        try:
            wl.dismiss([item_id])
        except wl.StoreUnreadable as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"ok": True})

    @data.post("/inbox/keep")
    async def _keep_body(payload: dict = Body(default={})):  # noqa: B008 — FastAPI body declaration
        item_id = payload.get("id")
        if not isinstance(item_id, str) or not item_id:
            return JSONResponse({"error": "id must be a non-empty string"}, status_code=400)
        return _keep_inbox_item(item_id)

    @data.post("/inbox/dismiss")
    async def _dismiss_body(payload: dict = Body(default={})):  # noqa: B008 — FastAPI body declaration
        item_id = payload.get("id")
        if not isinstance(item_id, str) or not item_id:
            return JSONResponse({"error": "id must be a non-empty string"}, status_code=400)
        return _dismiss_inbox_item(item_id)

    @data.post("/inbox/{item_id:path}/keep")
    async def _keep(item_id: str):
        return _keep_inbox_item(item_id)

    @data.post("/inbox/{item_id:path}/dismiss")
    async def _dismiss(item_id: str):
        return _dismiss_inbox_item(item_id)

    @data.post("/applications/{app_id}/move")
    async def _move(app_id: str, payload: dict = Body(default={})):
        """Move a tracked application to a new status (body: ``{"status": ...}``). A bad id or status
        is a 4xx and writes nothing."""
        status = str((payload or {}).get("status") or "").strip()
        try:
            row = state.move(app_id, status)
        except state.StoreUnreadable as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e.args[0]) if e.args else str(e)}, status_code=400)
        _emit_tracked(row.get("company", ""), row.get("role", ""), row.get("status", ""))
        return JSONResponse({"ok": True, "application": row})

    @data.post("/applications/{app_id}/undo-move")
    async def _undo_move(app_id: str):
        """Take back the last move, exactly: prior status, `updated` and history. The server holds
        what to restore, so the client can't name a status. Nothing to undo is a 409; an unknown id
        is a 400, as for a move."""
        try:
            row = state.undo_move(app_id)
        except (state.StoreUnreadable, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except KeyError as e:
            return JSONResponse({"error": str(e.args[0]) if e.args else str(e)}, status_code=400)
        _emit_tracked(row.get("company", ""), row.get("role", ""), row.get("status", ""))
        return JSONResponse({"ok": True, "application": row})

    def _find_app(app_id: str) -> dict | None:
        return next((r for r in state.load_applications() if r.get("id") == app_id), None)

    def _answers_view() -> list[dict]:
        """The saved standard answers rendered as ``[{key, value, status}]`` — draft/confirmed
        preserved so the detail view can show what still needs confirming. A load never raises."""
        return [
            {"key": k, "value": e.get("value", ""), "status": e.get("status", "draft")}
            for k, e in ans_mod.load().items()
        ]

    def _timeline(app: dict) -> list[dict]:
        """The application's history and its kit events merged into one date-sorted trail (ISO dates
        sort chronologically as strings). Each entry carries a ``kind`` so the view can tell a status
        change from a prepared-the-kit event."""
        out: list[dict] = []
        for h in app.get("history") or []:
            out.append({"date": str(h.get("date") or ""), "kind": "status", "status": h.get("status", "")})
        for ev in (app.get("kit") or {}).get("events") or []:
            out.append({"date": str(ev.get("date") or ""), "kind": "kit", "what": ev.get("what", "")})
        out.sort(key=lambda e: e.get("date") or "")
        return out

    @data.get("/applications/{app_id}")
    async def _application(app_id: str):
        """One application in full: the row, its apply kit, the saved standard answers (each with its
        draft/confirmed status), the contacts at that company (``[]`` when the contacts store is
        absent or unreadable — a referral hint never blocks the view), and a date-sorted timeline of
        history + kit events. An unknown id is a 404."""
        app = _find_app(app_id)
        if app is None:
            return JSONResponse({"error": f"no application with id {app_id!r}"}, status_code=404)
        try:
            company_contacts = contacts.at_company(app.get("company", ""))
        except contacts.StoreUnreadable:
            company_contacts = []
        return JSONResponse(
            {
                "application": app,
                "kit": app.get("kit") or state._empty_kit(),
                "answers": _answers_view(),
                "contacts": company_contacts,
                "timeline": _timeline(app),
            }
        )

    @data.post("/applications/{app_id}/applied")
    async def _applied(app_id: str, payload: dict = Body(default={})):
        """Mark an application applied: move it to ``applied`` with a history entry and snapshot what
        was sent into ``applied_with``. An optional body ``{"answers": [...]}`` overrides the snapshot
        source; otherwise it comes from the kit, else the saved answers. A bad id is a 404."""
        answers_used = (payload or {}).get("answers")
        answers_used = answers_used if isinstance(answers_used, list) else None
        try:
            row = state.mark_applied(app_id, answers_used=answers_used)
        except state.StoreUnreadable as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except KeyError as e:
            return JSONResponse({"error": str(e.args[0]) if e.args else str(e)}, status_code=404)
        _emit_tracked(row.get("company", ""), row.get("role", ""), row.get("status", ""))
        return JSONResponse({"ok": True, "application": row})

    @data.patch("/applications/{app_id}/notes")
    async def _notes(app_id: str, payload: dict = Body(default={})):
        """Persist the operator's notes on an application (body: ``{"notes": ...}``). A bad id is a 404."""
        notes = str((payload or {}).get("notes") or "")
        try:
            row = state.set_notes(app_id, notes)
        except state.StoreUnreadable as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except KeyError as e:
            return JSONResponse({"error": str(e.args[0]) if e.args else str(e)}, status_code=404)
        _emit_tracked(row.get("company", ""), row.get("role", ""), row.get("status", ""))
        return JSONResponse({"ok": True, "application": row})

    registry.register_router(data, prefix="/api/plugins/careercoach")  # GATED

    page = APIRouter()

    @page.get("/view")
    async def _view():
        return HTMLResponse(_DASHBOARD_HTML)

    registry.register_router(page, prefix="/plugins/careercoach")  # PUBLIC page


# ── a background job-watch: periodic scan → nudge via the event bus (Phase 3) ─
def _register_job_watch(registry, cfg) -> None:
    """Off unless `watch_enabled` (ADR 0071 posture). When on, a supervised background engine
    (graph.sdk.supervise) every `watch_interval_min` scans the watched companies' boards and sweeps
    the keyless job source for each target role, and puts postings it has never seen before (its
    own first-seen log) that pass the filters into the watch inbox — never the tracker — which the
    daily digest shows, and emits `careercoach.new_matches` so the
    console rail icon lights up. The goal verifier is registered either way, so you can arm a WATCH on
    the pipeline yourself (create_watch, ADR 0067) even with the auto-scan off."""
    _register_watch_verifier(registry)

    if not _as_bool(cfg.get("watch_enabled", False)):
        return

    interval_s = max(300, int(cfg.get("watch_interval_min", 180) or 180) * 60)
    min_score = int(cfg.get("watch_min_score", 75) or 75)
    holder: dict = {"engine": None}

    async def _scan() -> None:
        # Never raise out of a tick: a crash skips `breath`, and the watchdog would re-kick a full
        # board scan every `interval` instead of every `watch_interval_min`.
        try:
            await _watch_tick(registry, cfg, min_score)
        except Exception as e:  # noqa: BLE001
            log.warning("[careercoach] watch tick failed: %s", e)

    async def _start() -> None:
        try:
            from graph.sdk import supervise
        except Exception as e:  # noqa: BLE001
            log.info("[careercoach] job-watch unavailable (host-free?): %s", e)
            return
        # `breath` is the pause between work units; `interval` is only the watchdog's check period.
        # Passing the scan period as `interval` ran the scan back-to-back every 3 s (breath's
        # default) — 41 boards hammered continuously.
        holder["engine"] = supervise(_scan, name="careercoach-watch", breath=interval_s)
        holder["engine"].start()
        log.info("[careercoach] job-watch started (every %d min, >= %d)", interval_s // 60, min_score)

    async def _stop() -> None:
        engine = holder.get("engine")
        if engine is not None:
            try:
                engine.stop()
            except Exception:  # noqa: BLE001
                pass

    registry.register_surface(_start, stop=_stop, name="careercoach-watch")


async def _watch_tick(registry, cfg, min_score: int) -> list[dict]:
    """One background-watch pass (see ``_register_job_watch``). Returns what it tracked."""
    from . import boards, jobsource, scan, state
    from . import watchlist as wl
    from .hunt import filters

    f = filters(cfg)
    roles = f["roles"].strip()
    if not roles:
        return []
    rows = state.load_applications()
    found: list[dict] = []
    answered = False  # did any source answer? A pass where all failed didn't run, for the view
    # The watch keeps its OWN first-seen log (watch-seen.json) and records only what it handled:
    # a morning digest showing a role first must not make the watch skip it, and a role cut by
    # the per-scan cap must come back next scan.
    # 1) the watched companies' own boards — exact titles, real first-published dates
    try:
        watched = wl.load_watchlist()
        if watched:
            res = await scan.scan_watchlist(
                watched, roles=roles, exclude=f["exclude"], locations=f["locations"],
                max_age_days=f["max_age_days"], tracker_rows=rows, log_name=wl.WATCH_SEEN, record=False,
            )  # fmt: skip
            answered = True
            found += [j for j in res.new if not j["tracked"]]
    except Exception as e:  # noqa: BLE001
        log.info("[careercoach] watch board scan failed: %s", e)
    # 2) a keyless market sweep per target role, for companies not on the watchlist yet
    market: dict[str, dict] = {}
    for role in [r.strip() for r in roles.split(",") if r.strip()][:5]:
        try:
            got = await jobsource.search_jobs(
                role, remote=True, limit=25, api_key=cfg.get("jobs_api_key", ""),
                provider=cfg.get("jobs_provider", "auto"), country=cfg.get("search_country", "") or "",
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001
            log.info("[careercoach] watch market search failed for %r: %s", role, e)
            continue
        answered = True
        for j in got.jobs:
            if boards.passes(
                j, roles=roles, exclude=f["exclude"], locations=f["locations"], max_age_days=f["max_age_days"]
            ):
                j["id"] = f"{j['source']}:{j['url']}"
                j["hash"] = boards.content_hash(j["title"], j.get("snippet", ""), j.get("location", ""))
                market[j["id"]] = j
    if answered:
        try:
            wl.record_watch_run()
        except OSError as e:  # the note is chrome: it must not cost this pass its results
            log.info("[careercoach] watch run not recorded: %s", e)
    try:
        if market:
            index = scan.tracked_index(rows)
            fresh = wl.unseen(list(market.values()), wl.WATCH_SEEN)
            found += [j for j in (scan.annotate(j, index) for j in fresh) if not j["tracked"]]
        if not found:
            return []
        found.sort(key=lambda j: j.get("posted") or "", reverse=True)
        batch = found[:15]  # a first scan of a big watchlist must not flood the pipeline
        # The watch never writes the TRACKER — that's the operator's record of roles they chose.
        # New matches wait in the watch inbox; the digest shows them, and the coach tracks the
        # ones the operator wants.
        matches = []
        for j in batch:
            score = jobsource.best_prescore(j, roles)
            if score >= min_score:
                matches.append({**j, "score": score, "reason": wl.match_reason(j, roles)})
        wl.add_to_inbox(matches)
        wl.record_sightings(batch, log_name=wl.WATCH_SEEN)  # handled: inboxed, or below the bar
    except state.StoreUnreadable as e:
        log.warning("[careercoach] watch scan recorded nothing: %s", e)
        return []
    if matches:
        try:
            registry.emit(
                "new_matches",
                {
                    "count": len(matches),
                    "min_score": min_score,
                    "top": [{"title": m["title"], "company": m["company"], "score": m["score"]} for m in matches[:3]],
                },
            )
        except Exception:  # noqa: BLE001
            pass
        log.info("[careercoach] watch surfaced %d new match(es) >= %d", len(matches), min_score)
    return matches


def _register_watch_verifier(registry) -> None:
    """Register a plugin goal verifier so a WATCH (create_watch, ADR 0067) or a monitor goal can be
    armed on the pipeline: `careercoach:new_matches` trips when there's an un-actioned tracked role
    scoring >= `min_score`, or the background watch has queued new matches in its inbox. Guarded — needs the host goal types."""
    if not hasattr(registry, "register_goal_verifier"):
        return
    try:
        from graph.goals.types import VerifyResult
    except Exception as e:  # noqa: BLE001
        log.info("[careercoach] watch verifier skipped (host-free?): %s", e)
        return

    def _verify(min_score: int = 75, **_ignored):
        from . import state
        from . import watchlist as wl

        hi = [
            r
            for r in state.load_applications()
            if int(r.get("fit_score", 0)) >= int(min_score) and r.get("status") == "considering"
        ]
        try:
            queued = len(wl.peek_inbox())  # only PENDING matches — digested/kept/dismissed don't trip it
        except wl.StoreUnreadable:
            queued = 0
        n = len(hi) + queued
        return VerifyResult(
            bool(n), f"{len(hi)} un-actioned role(s) >= {min_score}, {queued} new in the watch inbox", str(n)
        )

    registry.register_goal_verifier("careercoach:new_matches", _verify)


# The jobs view (S1c). Self-contained: loads the DS plugin-kit (CSS + JS) and lets it own the
# `protoagent:init` handshake — it applies the console's LIVE theme (stamps data-theme on :root
# so the --pl-* tokens follow light/dark) and hands back a slug-aware authed `apiFetch`, which we
# use for the gated /jobs · keep · dismiss · move routes. Base is derived fleet-proxy-safe from the
# iframe's own path (ADR 0042). Live refresh subscribes to `careercoach.#` and reloads on an event.
_DASHBOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Career Coach — Jobs</title>
<script>
  // Fleet-proxy-safe base: "" on the host, "/agents/<slug>" behind the proxy (ADR 0042).
  var BASE = location.pathname.split('/plugins/')[0];
  var link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = BASE + '/_ds/plugin-kit.css';
  document.head.appendChild(link);
</script>
<style>
  /* House style (spec §1.7 / §6): /_ds/plugin-kit.css classes and --pl-* tokens only. No custom
     accent, no hex fallbacks, no hand-rolled .btn/.pill/.card; overlay colour from --pl-color-overlay. */
  html, body { height: 100%; }
  body { margin: 0; background: var(--pl-color-bg); color: var(--pl-color-fg);
         font-family: var(--pl-font-sans, ui-sans-serif, system-ui, -apple-system, sans-serif); overflow: hidden; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .grow { flex: 1; min-width: 0; }
  .muted { color: var(--pl-color-fg-muted); }
  .sm { font-size: 12px; }
  .empty { color: var(--pl-color-fg-muted); font-size: 14px; padding: 12px 0; }
  code { background: var(--pl-color-bg-subtle); padding: 1px 6px; border-radius: 5px; }

  /* One-viewport shell: the page never scrolls; the active tab body scrolls inside itself (§2). */
  .cc { container: cc / inline-size; position: relative; overflow: hidden; height: 100dvh;
        display: flex; flex-direction: column; background: var(--pl-color-bg); }
  .hd { display: flex; align-items: flex-start; gap: 12px; padding: 20px 24px 8px; flex: none; }
  .hd .t { font-size: 18px; font-weight: 600; margin: 2px 0 0; color: var(--pl-color-fg); }
  .hd .s { font-size: 13px; color: var(--pl-color-fg-muted); margin: 3px 0 0; }
  .hd .sp { flex: 1; }

  .banner { margin: 8px 24px 0; flex: none; }
  .pl-banner { display: flex; align-items: center; gap: 12px; padding: 10px 14px; border-radius: var(--pl-radius, 10px);
               border: 1px solid var(--pl-color-border); background: var(--pl-color-bg-raised); font-size: 13px; }
  .pl-banner--error { border-color: var(--pl-color-status-error); }

  .today { margin: 8px 24px 0; padding: 10px 14px; border: 1px solid var(--pl-color-border);
           border-radius: var(--pl-radius, 10px); background: var(--pl-color-bg-raised); flex: none; }
  .today > h3 { margin: 0 0 4px; font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
                color: var(--pl-color-fg-muted); font-weight: 600; }
  .trow { display: flex; align-items: center; gap: 8px; padding: 5px 0; font-size: 13px;
          border-top: 1px solid var(--pl-color-border); }
  .trow:first-of-type { border-top: 0; }
  .tmark { flex: none; font-size: 12px; }
  .pl-chip--warning { color: var(--pl-color-status-warning); }

  .tabs { display: flex; gap: 2px; padding: 8px 24px 0; border-bottom: 1px solid var(--pl-color-border);
          flex: none; position: relative; }
  .tab { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--pl-color-fg-muted);
         font: inherit; font-size: 13px; padding: 8px 12px; cursor: pointer; display: flex; gap: 6px; align-items: center; white-space: nowrap; }
  .tab[aria-selected="true"] { color: var(--pl-color-fg); border-bottom-color: var(--pl-color-accent); }
  .tab:focus-visible, .body:focus-visible, .tab--menu:focus-visible { outline: 2px solid var(--pl-color-focus); outline-offset: -2px; }
  /* Responsive tabs (§8): when the three tabs + counts don't fit the container, the strip collapses to
     a single menu trigger (fitTabs measures overflow) that opens this menu of the same tabs. */
  .tab--menu { background: none; border: 0; color: var(--pl-color-fg); font: inherit; font-size: 13px;
               padding: 8px 12px; cursor: pointer; display: flex; gap: 6px; align-items: center; }
  .tabs-menu { position: absolute; top: 100%; left: 24px; z-index: 30; min-width: 180px; padding: 4px;
               background: var(--pl-color-bg-raised); border: 1px solid var(--pl-color-border-strong);
               border-radius: var(--pl-radius, 10px); }
  .tabs-menu[hidden] { display: none; }
  .tabs-menu .tab { width: 100%; border-bottom: 0; justify-content: space-between; }
  .pl-count { font-size: 11px; padding: 0 6px; border-radius: 999px; background: var(--pl-color-bg-subtle);
              color: var(--pl-color-fg-muted); font-variant-numeric: tabular-nums; }

  .body { flex: 1; min-height: 0; overflow: auto; padding: 16px 24px 24px; }
  .skel { display: grid; gap: 10px; }
  .pl-skel { height: 16px; border-radius: 6px; background: var(--pl-color-bg-subtle); }

  .emptyc { display: grid; place-items: center; text-align: center; gap: 10px; padding: 48px 24px; }
  .emptyc .empty-title { font-size: 15px; font-weight: 600; }
  .emptyc-desc { max-width: 380px; margin: 0; }
  .sugs { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }

  /* Inbox tab (§3): a toolbar, freshness accordions (reusing .grp) and listbox rows whose Keep /
     Dismiss / View posting + checkbox reveal only on hover, focus or selection — never at rest. */
  .ibx-toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 12px; font-size: 12px; }
  .ibx-toolbar label { color: var(--pl-color-fg-muted); }
  .ibx-kbd { margin-left: auto; color: var(--pl-color-fg-muted); }
  .ibx-kbd .pl-kbd { margin: 0 1px; }
  .grp > summary .pl-count { margin-left: 4px; }
  .ibx-row { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; cursor: pointer;
             border-top: 1px solid var(--pl-color-border); }
  .ibx-row:first-child { border-top: 0; }
  .ibx-row:hover, .ibx-row.sel { background: var(--pl-color-bg-hover); }
  .ibx-row:focus-visible { outline: 2px solid var(--pl-color-focus); outline-offset: -2px; }
  .ibx-main { flex: 1; min-width: 0; }
  .ibx-ti { font-size: 13px; font-weight: 600; }
  .ibx-meta { font-size: 12px; color: var(--pl-color-fg-muted); margin-top: 2px; }
  .ibx-meta em { font-style: normal; color: var(--pl-color-fg); }
  .ibx-ghost { margin-top: 5px; display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; }
  /* The "New" marker carries text, not just the dot (§10): never colour alone. */
  .ibx-new { display: inline-flex; gap: 4px; align-items: center; font-size: 11px; font-weight: 600;
             color: var(--pl-color-status-info); margin-right: 2px; }
  /* Hover/focus/selection reveal (§3/§6); focus-within so a focused action stays visible (§10). */
  .ibx-acts { display: flex; gap: 6px; flex: none; visibility: hidden; }
  .ibx-row:hover .ibx-acts, .ibx-row:focus .ibx-acts, .ibx-row:focus-within .ibx-acts, .ibx-row.sel .ibx-acts { visibility: visible; }
  .ibx-ck { flex: none; margin-top: 2px; visibility: hidden; }
  .ibx-row:hover .ibx-ck, .ibx-row:focus .ibx-ck, .ibx-row:focus-within .ibx-ck, .ibx-row.sel .ibx-ck, .ibx-multi .ibx-ck { visibility: visible; }
  .plink { color: var(--pl-color-accent); font-size: 12px; text-decoration: none; }
  .plink:hover { text-decoration: underline; }

  /* Pipeline board (§3): five open columns + an "Other" column for legacy statuses + the Closed stub.
     Layout bands switch on a CONTAINER query on the view root (§8), never the viewport, so the rail
     panel and split-screen both work. ≥900px: the five columns share the width, Closed stub 120px. */
  .board { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)) 120px; gap: 12px; align-items: start; }
  /* 600–899px (split-screen, §8): columns fix at 220px and the board scrolls sideways with scroll-snap. */
  @container cc (max-width: 899px) {
    .board { grid-template-columns: repeat(5, 220px) 110px; overflow-x: auto; scroll-snap-type: x mandatory; }
    .col { scroll-snap-align: start; }
  }
  .col { background: var(--pl-color-bg-subtle); border: 1px solid var(--pl-color-border); border-radius: 10px;
         padding: 8px; min-height: 60px; }
  .col.over { border-color: var(--pl-color-accent); border-style: dashed; }
  .col h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; margin: 2px 4px 8px;
            color: var(--pl-color-fg-muted); font-weight: 600; display: flex; justify-content: space-between; }
  /* Applied (and any tall column) scrolls inside the column, so the board stays one viewport — no paging (§3). */
  .col-list { display: grid; gap: 8px; max-height: 62vh; overflow: auto; }
  .col.stub { display: flex; flex-direction: column; gap: 6px; align-items: center; justify-content: center;
              text-align: center; border-style: dashed; color: var(--pl-color-fg-muted); font-size: 12px; }
  .col.stub .stub-n { color: var(--pl-color-fg); font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .pcard { background: var(--pl-color-bg-raised); border: 1px solid var(--pl-color-border); border-radius: 8px;
           padding: 9px 10px; cursor: grab; }
  .pcard:focus-visible { outline: 2px solid var(--pl-color-focus); outline-offset: -2px; }
  .pcard.lift { border-color: var(--pl-color-accent); }  /* picked up by keyboard (§3, WCAG 2.5.7) */
  .pcard .prole { font-weight: 600; font-size: 13px; }
  .pcard .pco { color: var(--pl-color-fg-muted); font-size: 12px; }
  .pcard .pmeta { display: flex; gap: 8px; align-items: baseline; font-size: 12px; color: var(--pl-color-fg-muted); margin-top: 5px; }
  .pcard .fit { font-variant-numeric: tabular-nums; }
  /* The single follow-up line — a pl-chip--warning, carrying text, never colour alone (§3/§10). */
  .pcard .nudge { margin-top: 6px; font-size: 12px; border: 1px solid var(--pl-color-border);
                  border-radius: 6px; padding: 4px 7px; }
  .pcard .movesel { margin-top: 7px; width: 100%; font: inherit; font-size: 12px; }
  .closed-list { display: grid; gap: 8px; max-width: 520px; }
  .closed-list .pcard { margin-bottom: 0; }
  .pipeline-hint { margin: 0 0 10px; }

  /* Rail (<600px, §8): the board is swapped for a stacked list of stage accordions (Saved · Preparing
     · Applied · Interviewing · Offer). Hidden at wider widths; the @container 599 rule toggles it on
     and the board off. Drag is off here — a row opens the drawer, and the keyboard pick-up (↑/↓
     between stages) highlights the lifted row (.lift) and the target stage (details[data-stage].over). */
  .stages { display: none; }
  .srow { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; cursor: pointer;
          border-top: 1px solid var(--pl-color-border); }
  .srow:first-child { border-top: 0; }
  .srow:hover { background: var(--pl-color-bg-hover); }
  .srow:focus-visible { outline: 2px solid var(--pl-color-focus); outline-offset: -2px; }
  .srow.lift { background: var(--pl-color-bg-hover); box-shadow: inset 2px 0 0 var(--pl-color-accent); }
  .srow .srole { font-size: 13px; font-weight: 600; }
  .srow .smeta { font-size: 12px; color: var(--pl-color-fg-muted); margin-top: 2px; }
  /* The follow-up marker carries text ("No reply"), never colour alone (§10). */
  .srow .snudge { color: var(--pl-color-status-warning); }
  details[data-stage].over > summary { outline: 2px dashed var(--pl-color-accent); outline-offset: -2px; }
  .stage-closed .srole { font-weight: 600; }

  /* About-you drawer: three read-only accordions + the rubric bars (§5). */
  .arow { display: flex; gap: 12px; align-items: baseline; padding: 4px 0; font-size: 13px; }
  .arow .aname { width: 150px; flex: none; color: var(--pl-color-fg-muted); }
  .track { flex: 1; height: 6px; border-radius: 4px; background: var(--pl-color-border); overflow: hidden; }
  .bar { height: 6px; border-radius: 4px; background: var(--pl-color-accent); }
  .fit { font-variant-numeric: tabular-nums; font-weight: 600; }
  .grp { border: 1px solid var(--pl-color-border); border-radius: var(--pl-radius, 10px); margin-bottom: 10px;
         background: var(--pl-color-bg-raised); }
  .grp > summary { list-style: none; cursor: pointer; display: flex; gap: 8px; align-items: center;
                   padding: 10px 12px; font-size: 13px; font-weight: 600; }
  .grp > summary::-webkit-details-marker { display: none; }
  .grp > summary .chev { color: var(--pl-color-fg-muted); transition: transform .15s; }
  .grp[open] > summary .chev { transform: rotate(90deg); }
  .grp-body { padding: 0 12px 12px; }
  .ctx { margin: 0; padding: 10px; border-radius: 8px; background: var(--pl-color-bg-subtle);
         color: var(--pl-color-fg-muted); font-size: 12px; line-height: 1.5; white-space: pre-wrap; overflow-x: auto; }

  /* Drawers / dialogs / toast — overlay colour from the token, never a hardcoded rgba (§6). */
  .scrim { position: fixed; inset: 0; z-index: 41; background: var(--pl-color-overlay); }
  #about { position: fixed; top: 0; right: 0; bottom: 0; width: min(560px, 100%); z-index: 42; display: flex;
           flex-direction: column; background: var(--pl-color-bg-raised); border-left: 1px solid var(--pl-color-border); }
  #about[aria-hidden="true"] { display: none; }
  #about-scrim { display: none; }
  #cc.about-open ~ #about-scrim { display: block; }
  .dtop { display: flex; gap: 8px; align-items: flex-start; padding: 16px 20px; border-bottom: 1px solid var(--pl-color-border); }
  .dtitle { font-size: 16px; font-weight: 600; }
  .dbody { overflow: auto; padding: 16px 20px 28px; }

  #toast { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); z-index: 60;
           display: inline-flex; align-items: center; gap: 12px;
           background: var(--pl-color-bg-raised); border: 1px solid var(--pl-color-border-strong);
           color: var(--pl-color-fg); padding: 10px 16px; border-radius: 10px; font-size: 13px;
           opacity: 0; pointer-events: none; transition: opacity .15s; max-width: 80%; }
  #toast.show { opacity: 1; pointer-events: auto; }
  #toast.pl-toast--error { border-color: var(--pl-color-status-error); }  /* §7 move/undo failure */
  #toast #toast-text { min-width: 0; }

  #chooser { position: fixed; inset: 0; z-index: 50; display: none; align-items: center; justify-content: center;
             background: var(--pl-color-overlay); }
  #confirm { position: fixed; inset: 0; z-index: 50; display: none; align-items: center; justify-content: center;
             background: var(--pl-color-overlay); }
  #chooser .box, #confirm .box { max-width: 320px; }
  #chooser p, #confirm p { margin: 0 0 14px; font-size: 14px; }
  #chooser .choices, #confirm .choices { display: flex; gap: 8px; flex-wrap: wrap; }

  /* Job detail drawer (§4). */
  #detail { position: fixed; inset: 0; z-index: 40; display: none; }
  #detail.open { display: block; }
  #detail .detail-backdrop { position: absolute; inset: 0; background: var(--pl-color-overlay); }
  #detail .detail-panel { position: absolute; top: 0; right: 0; height: 100%; width: min(560px, 100%);
    box-sizing: border-box; overflow-y: auto; padding: 0 20px 28px;
    background: var(--pl-color-bg-raised); border-left: 1px solid var(--pl-color-border); }
  #detail .detail-top { position: sticky; top: 0; z-index: 1; display: flex; align-items: flex-start;
    justify-content: space-between; gap: 12px; padding: 18px 0 12px;
    background: var(--pl-color-bg-raised); border-bottom: 1px solid var(--pl-color-border); }
  #detail .detail-role { font-size: 17px; font-weight: 700; }
  #detail .detail-co { color: var(--pl-color-fg-muted); font-size: 13px; margin-top: 2px; }
  #detail .detail-close { font-size: 16px; line-height: 1; }
  #detail .dsec { padding: 14px 0; border-top: 1px solid var(--pl-color-border); }
  #detail .dsec:first-child { border-top: 0; }
  #detail .dsec > h4 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
    color: var(--pl-color-fg-muted); margin: 0 0 10px; font-weight: 600; }
  #detail .kit-block { margin-bottom: 14px; }
  #detail .kit-block:last-child { margin-bottom: 0; }
  #detail .kit-block h5 { font-size: 12px; margin: 0 0 6px; font-weight: 600; }
  #detail .kv { display: flex; gap: 8px; align-items: baseline; font-size: 13px; margin: 4px 0; flex-wrap: wrap; }
  #detail .kv .k { width: 72px; flex: none; color: var(--pl-color-fg-muted); }
  #detail .kv a.plink { word-break: break-all; }
  #detail .snippet, #detail .cover-text, #detail .answer-val { white-space: pre-wrap; line-height: 1.5; font-size: 13px; }
  #detail .snippet { margin-top: 10px; color: var(--pl-color-fg-muted); }
  #detail .cover-text { margin-top: 8px; padding: 10px; border-radius: 8px; background: var(--pl-color-bg-subtle); }
  #detail .fitline { display: flex; align-items: baseline; gap: 10px; }
  #detail .fit.big { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--pl-color-accent); }
  #detail .verdict { color: var(--pl-color-fg-muted); font-size: 13px; }
  #detail .notes-ta { width: 100%; min-height: 84px; box-sizing: border-box; resize: vertical; font: inherit;
    font-size: 13px; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--pl-color-border);
    background: var(--pl-color-bg-subtle); color: var(--pl-color-fg); }
  #detail .dsec-actions { margin-top: 8px; }
  #detail .answer { padding: 8px 10px; border-radius: 8px; margin: 8px 0; border: 1px solid var(--pl-color-border); }
  #detail .answer.draft { border-color: var(--pl-color-status-warning); border-style: dashed; background: var(--pl-color-bg-subtle); }
  #detail .answer-head { display: flex; align-items: center; gap: 8px; }
  #detail .answer-key { font-weight: 600; font-size: 13px; }
  #detail .answer-head .copy { margin-left: auto; }
  #detail .answer-val { margin-top: 5px; }
  #detail .timeline, #detail .contacts { list-style: none; margin: 0; padding: 0; font-size: 13px; }
  #detail .timeline li { display: flex; gap: 10px; padding: 3px 0; }
  #detail .timeline .tl-date { flex: none; width: 92px; font-variant-numeric: tabular-nums; color: var(--pl-color-fg-muted); }
  #detail .contacts li { padding: 3px 0; }
  #detail .action-btns { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
  #detail .hint { color: var(--pl-color-fg-muted); font-size: 12px; margin: 6px 0 0; }

  .closed-sub { display: none; }
  /* 600–899px (§8): the drawers go full width. */
  @container cc (max-width: 899px) { #detail .detail-panel, #about { width: 100%; } }
  @container cc (max-width: 599px) {
    /* Rail (§8): Pipeline becomes the stage-list (board off); the Closed table drops Company and
       Closed-on, folding both under the role as one muted line; the drawer goes bottom-side at
       85dvh; the header's target-roles subline is hidden. */
    .board { display: none; }
    .stages { display: block; }
    .closed-company, .closed-date { display: none; }
    .closed-sub { display: block; }
    #detail .detail-panel { top: auto; bottom: 0; height: 85dvh; }
    .hd .s { display: none; }
    #about, #detail .detail-panel { width: 100%; }
  }
  /* §10: honour reduced-motion — kill the chevron spin and the toast fade. */
  @media (prefers-reduced-motion: reduce) { .grp > summary .chev { transition: none; } #toast { transition: none; } }
</style></head>
<body>
  <div class="cc" id="cc">
    <div class="hd">
      <div>
        <div class="pl-eyebrow">Career Coach</div>
        <h1 class="t" id="title">Jobs</h1>
        <p class="s" id="sub" hidden></p>
      </div>
      <span class="sp"></span>
      <button class="pl-btn pl-btn--ghost pl-btn--sm" id="about-btn">About you</button>
    </div>
    <div class="banner" id="banner" hidden></div>
    <section class="today pl-callout pl-callout--info" id="today" aria-label="Needs you today" hidden></section>
    <div class="tabs" role="tablist" aria-label="Jobs" id="tabs"></div>
    <div class="body" id="body" role="tabpanel" tabindex="0"></div>
  </div>

  <!-- About-you drawer (§5): opened by the header button; three read-only accordions. -->
  <div class="scrim" id="about-scrim"></div>
  <aside id="about" class="pl-drawer pl-drawer--right" role="dialog" aria-modal="true"
         aria-labelledby="about-title" aria-hidden="true">
    <div class="dtop">
      <div class="grow"><div class="dtitle" id="about-title">About you</div></div>
      <button class="pl-btn pl-btn--ghost pl-btn--sm" id="about-close" aria-label="Close">✕</button>
    </div>
    <div class="dbody" id="about-body"></div>
  </aside>

  <div id="toast" class="pl-toast" role="status" aria-live="polite">
    <span id="toast-text"></span>
    <button class="pl-btn pl-btn--ghost pl-btn--sm" id="toast-undo" type="button" hidden>Undo</button>
  </div>
  <div id="live" class="sr-only" role="status" aria-live="polite"></div>

  <div id="chooser" role="dialog" aria-modal="true">
    <div class="pl-card pl-dialog box">
      <p id="chooser-q">Close this role as rejected, or passed on?</p>
      <div class="choices">
        <button class="pl-btn pl-btn--primary" id="choose-passed">Mark passed (default)</button>
        <button class="pl-btn" id="choose-rejected">Mark rejected</button>
        <button class="pl-btn" id="choose-cancel">Cancel</button>
      </div>
    </div>
  </div>
  <div id="confirm" role="dialog" aria-modal="true">
    <div class="pl-card pl-dialog box">
      <p id="confirm-q"></p>
      <div class="choices">
        <button class="pl-btn pl-btn--primary" id="confirm-ok">Yes (default)</button>
        <button class="pl-btn" id="confirm-cancel">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Job drawer: Inbox rows, board cards and Closed history share this surface. -->
  <div id="detail" aria-hidden="true">
    <div class="detail-backdrop"></div>
    <aside class="detail-panel pl-drawer pl-drawer--right" role="dialog" aria-modal="true" aria-label="Job detail">
      <div class="detail-top">
        <div id="detail-title"></div>
        <button class="pl-btn pl-btn--ghost detail-close" id="detail-close" aria-label="Close detail">✕</button>
      </div>
      <div id="detail-body"></div>
    </aside>
  </div>

<script type="module">
  // The DS plugin-kit owns the protoagent:init handshake — bearer + LIVE theme (data-theme on
  // :root so the --pl-* tokens follow light/dark). Import it; fall back to a plain fetch so the
  // page still renders without theme sync on an older/offline host.
  var kit;
  try { kit = await import(BASE + '/_ds/plugin-kit.js'); }
  catch (e) { kit = { initPluginView: function (cb) { cb(); },
                      apiFetch: function (p, i) { return fetch(BASE + p, i); } }; }

  function api(path) {
    return kit.apiFetch('/api/plugins/careercoach' + path).then(function (r) { return r.json(); });
  }
  function write(path, body) {
    return kit.apiFetch('/api/plugins/careercoach' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body == null ? undefined : JSON.stringify(body)
    });
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }


  // ── shell state ─────────────────────────────────────────────────────────────────────
  var JOBS = { inbox: [], applications: [], columns: {}, followup_days: 0 };
  var STATE = null, STATE_FAILED = false, JOBS_FAILED = false, LOADING = true;
  var TAB = 'pipeline', TODAY_ALL = false, LAST_VISIT = 0;
  var DRAG_ID = null, DETAIL = null, DETAIL_ID = null, DETAIL_RETURN = null, DETAIL_REQUEST = 0;
  var CLOSED_Q = '', CLOSED_OUT = 'all', CLOSED_PAGE = 0;
  var NOTE_DRAFTS = Object.create(null), NOTE_WRITES = Object.create(null), NOTE_PENDING = Object.create(null);
  // Keyboard board move (WCAG 2.5.7): LIFT_ID is the card currently picked up, LIFT_COL the column
  // the arrow keys are hovering. Both null at rest. A pointer drag never touches these. The same two
  // drive the rail stage-list pick-up (§8), where ↑/↓ walk LIFT_COL instead of ←/→.
  var LIFT_ID = null, LIFT_COL = null;
  // Rail stage-list (§8): which stage accordions the operator has toggled (col -> bool). A stage
  // defaults open when it has cards and collapses to one line when empty; an explicit toggle wins.
  var STAGE_OPEN = {};
  // Inbox triage state (§3): sort order, the ghost filter toggle, the current multi-selection
  // (id -> 1), which freshness groups are open, rows removed optimistically (kept or dismissing,
  // id -> 1), the HELD dismiss timers (id -> handle; the write waits out the 6s Undo window), and
  // the row to focus after the next inbox re-render. All reset to their defaults here.
  var INBOX_SORT = 'newest', HIDE_GHOST = false, INBOX_SEL = {},
      INBOX_OPEN = { today: true, week: true, older: false, miss: false },
      INBOX_HIDDEN = {}, DISMISS_TIMERS = {}, INBOX_FOCUS = null;

  function closedColName() {
    var cols = JOBS.columns || {}, names = Object.keys(cols);
    for (var i = 0; i < names.length; i++) { if (isClosedColumn(cols[names[i]] || [])) return names[i]; }
    return null;
  }
  function closedStatuses() { var c = closedColName(); return c ? (JOBS.columns[c] || []) : []; }
  function isClosedApp(a) { return closedStatuses().indexOf(a.status) !== -1; }
  function openApps() { return (JOBS.applications || []).filter(function (a) { return !isClosedApp(a); }); }
  function closedApps() { return (JOBS.applications || []).filter(isClosedApp); }
  // Counts: Pipeline is the live roles (closed never counts), Closed the dead ones (§2, principle 3).
  function counts() { return { pipeline: openApps().length, inbox: (JOBS.inbox || []).length, closed: closedApps().length }; }
  // Brand-new agent (§7): no profile, and nothing anywhere. Needs /state, so a partial failure is never "new".
  function isBrandNew() {
    if (JOBS_FAILED || LOADING || STATE_FAILED || !STATE) return false;
    var cov = STATE.completeness || {};
    var noProfile = cov.empty || cov.filled === 0;
    return noProfile && (JOBS.applications || []).length === 0 && (JOBS.inbox || []).length === 0;
  }
  function itemTime(it) { var s = it.first_seen || it.posted || ''; if (!s) return 0; var t = new Date(s).getTime(); return isNaN(t) ? 0 : t; }
  function compose(text) { try { parent.postMessage({ type: 'protoagent:chat:compose', text: String(text).slice(0, 4000) }, '*'); } catch (e) {} }
  function skel(n) { var out = '<div class="skel" aria-hidden="true">'; for (var i = 0; i < n; i++) { out += '<div class="pl-skel"></div>'; } return out + '</div>'; }
  function setTab(t) {
    TAB = t; renderTabs(); renderBody();
    // When the strip has collapsed to the responsive menu trigger (§8) there is no tab-<t> button to
    // return focus to, so land on the trigger instead.
    var el = document.getElementById('tabs');
    var b = (el && el.querySelector && el.querySelector('#tabs-trigger')) || document.getElementById('tab-' + t);
    if (b && b.focus) b.focus();
  }
  // Announce a board move / pick-up in the single role="status" live region (§10). Cleared then set
  // on a tick so the same message announced twice (e.g. two moves to the same column) still fires.
  function announce(msg) {
    var live = document.getElementById('live');
    if (!live) return;
    live.textContent = '';
    setTimeout(function () { live.textContent = msg; }, 30);
  }
  // Toast with an optional 6s Undo action and an error variant (§7). opts: {undo: fn, error: bool}.
  // A plain toast(msg) keeps its old 4s, no-button behaviour, so existing callers are unchanged.
  function toast(msg, opts) {
    opts = opts || {};
    var t = document.getElementById('toast');
    var txt = document.getElementById('toast-text');
    var undo = document.getElementById('toast-undo');
    if (txt) txt.textContent = msg; else t.textContent = msg;
    t.classList.toggle('pl-toast--error', !!opts.error);
    if (undo) {
      if (opts.undo) {
        undo.hidden = false;
        undo.onclick = function () { t.classList.remove('show'); clearTimeout(toast._t); opts.undo(); };
      } else {
        undo.hidden = true;
        undo.onclick = null;
      }
    }
    t.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.classList.remove('show'); }, opts.undo ? 6000 : 4000);
  }

  function findApp(id) {
    var list = JOBS.applications || [];
    for (var i = 0; i < list.length; i++) { if (list[i].id === id) return list[i]; }
    return null;
  }
  function statusToColumn() {
    var m = Object.create(null), cols = JOBS.columns || {};
    Object.keys(cols).forEach(function (col) {
      (cols[col] || []).forEach(function (s) { m[s] = col; });
    });
    return m;
  }
  function isClosedColumn(statuses) {
    return statuses.indexOf('rejected') !== -1 && statuses.indexOf('passed') !== -1;
  }

  // Days since the row last moved — its most recent history entry (else updated/date). Computed in
  // the browser so the card can show "Nd" without the server stamping a clock into every row.
  function lastMoveDate(a) {
    var h = a.history || [];
    if (h.length) return h[h.length - 1].date || '';
    return a.updated || a.date || '';
  }
  function daysSince(dateStr) {
    if (!dateStr) return null;
    var then = new Date(String(dateStr).slice(0, 10) + 'T00:00:00Z');
    if (isNaN(then.getTime())) return null;
    var ms = Date.now() - then.getTime();
    return ms < 0 ? 0 : Math.floor(ms / 86400000);
  }

  // Dropping on Closed (rejected OR passed) asks which, with passed as the default. An inline
  // chooser, not window.confirm — a sandboxed iframe may block native modals.
  function askClosedStatus() {
    return new Promise(function (resolve) {
      var ov = document.getElementById('chooser');
      var passed = document.getElementById('choose-passed');
      var rejected = document.getElementById('choose-rejected');
      var cancel = document.getElementById('choose-cancel');
      var previous = document.activeElement;
      function done(v) {
        ov.style.display = 'none';
        passed.onclick = rejected.onclick = cancel.onclick = null;
        ov.onkeydown = null;
        if (previous && previous.isConnected && previous.focus) previous.focus();
        resolve(v);
      }
      passed.onclick = function () { done('passed'); };
      rejected.onclick = function () { done('rejected'); };
      cancel.onclick = function () { done(null); };
      ov.onkeydown = function (e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); return; }
        trap(e, ov);
      };
      ov.style.display = 'flex';
      passed.focus();
    });
  }

  // A yes/no confirmation as an inline overlay above the drawer — never window.confirm, which a
  // sandboxed iframe (no allow-modals) silently suppresses, returning false. Mirrors
  // askClosedStatus. Resolves true only on an explicit Yes; Cancel (or re-ask) resolves false.
  function askConfirm(message) {
    return new Promise(function (resolve) {
      var ov = document.getElementById('confirm');
      var q = document.getElementById('confirm-q');
      var ok = document.getElementById('confirm-ok');
      var cancel = document.getElementById('confirm-cancel');
      var previous = document.activeElement;
      q.textContent = message;
      function done(v) {
        ov.style.display = 'none';
        ok.onclick = cancel.onclick = null; ov.onkeydown = null;
        if (previous && previous.isConnected) previous.focus();
        resolve(v);
      }
      ov.onkeydown = function (e) { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(false); } else trap(e, ov); };
      ok.onclick = function () { done(true); };
      cancel.onclick = function () { done(false); };
      ov.style.display = 'flex';
      if (ok.focus) ok.focus();
    });
  }

  function syncDetailStatus() {
    var sel = document.getElementById('detail-status'), app = findApp(DETAIL_ID);
    if (sel && app) sel.value = app.status;
    if (DETAIL && app && DETAIL.application.status !== app.status) {
      DETAIL.application.status = app.status;
      refreshDetailMarkup(DETAIL);
    }
  }
  function detailStatusHtml(app) {
    var statuses = [], labels = {};
    Object.keys(JOBS.columns || {}).forEach(function (col) {
      JOBS.columns[col].forEach(function (status) {
        statuses.push(status);
        labels[status] = status === 'passed' ? 'Passed' : status === 'rejected' ? 'Rejected' : col;
      });
    });
    if (app.status && statuses.indexOf(app.status) === -1) statuses.unshift(app.status);
    return '<label for="detail-status">Status</label> <select id="detail-status" class="pl-select pl-dropselect" aria-label="Status"' + (isClosedApp(app) ? ' disabled' : '') + '>' +
      statuses.map(function (status) {
        return '<option value="' + esc(status) + '"' + (status === app.status ? ' selected' : '') + '>' +
          esc(labels[status] || status) + '</option>';
      }).join('') + '</select>';
  }
  function columnOfStatus(status) { return statusToColumn()[status] || status; }
  // Optimistic move: flip the card's status and re-render now. On success, announce it in the live
  // region, offer a 6s Undo in the toast (§7 — Undo POSTs /undo-move, never a second move), and
  // reload for the authoritative row (new date, history, recomputed follow-up). On a non-2xx (or
  // network error) roll the status back, re-render, and show the §7 error toast. `label` is the
  // destination column name for the announce + toast copy.
  function moveApp(id, status, label) {
    var app = findApp(id);
    if (!app || app.status === status) return;
    var role = app.role || 'This role', dest = label || columnOfStatus(status);
    var prev = app.status;
    app.status = status;
    syncDetailStatus();
    renderBody();
    write('/applications/' + encodeURIComponent(id) + '/move', { status: status })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        announce(role + ' moved to ' + dest + '.');
        toast(role + ' moved to ' + dest + '.', { undo: function () { undoMove(id); } });
        load();
      })
      .catch(function () {
        app.status = prev;
        syncDetailStatus();
        renderBody();
        toast('Couldn’t move that role. It’s back where it was.', { error: true });
      });
  }
  // Undo the last move exactly (§7): POST /undo-move restores the prior status (a legacy one
  // included), `updated` and history, so the follow-up clock and the "oldest move" sort are
  // untouched — never a second move. A failure reloads the board to the true state and shows the
  // §7 error toast.
  function undoMove(id) {
    write('/applications/' + encodeURIComponent(id) + '/undo-move')
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        announce('Move undone.');
        load();
      })
      .catch(function () {
        load();
        toast('Couldn’t undo that. The role stayed where you moved it.', { error: true });
      });
  }
  function moveToColumn(id, col) {
    var app = findApp(id);
    if (!app) return;
    var statuses = (JOBS.columns || {})[col] || [];
    if (!statuses.length || statuses.indexOf(app.status) !== -1) return;  // already there / unknown
    if (isClosedColumn(statuses)) {
      // Dropping on Closed asks rejected / passed, with passed as the default (§3).
      askClosedStatus().then(function (s) { if (s) moveApp(id, s, col); });
    } else {
      moveApp(id, statuses[0], col);
    }
  }

  // >>> view renderers
  function renderBanner() {
    var b = document.getElementById('banner');
    if (JOBS_FAILED) {
      b.hidden = false;
      b.innerHTML = '<div class="pl-banner pl-banner--error" role="alert"><span class="grow">Couldn’t load your jobs.</span>' +
        '<button class="pl-btn pl-btn--sm" id="retry">Retry</button></div>';
      var r = document.getElementById('retry'); if (r) r.onclick = function () { load(); };
    } else { b.hidden = true; b.innerHTML = ''; }
  }
  function renderHeader() {
    var st = (STATE && STATE.settings) || {};
    var roles = st.target_roles || '';
    var sub = document.getElementById('sub');
    if (roles) { sub.textContent = 'Targeting: ' + roles; sub.hidden = false; }
    else { sub.textContent = ''; sub.hidden = true; }
  }
  // The Today band's rows, in priority order (§2): overdue follow-ups, interviews to prep, offers
  // awaiting a decision, new inbox roles since the last visit, then Saved/Preparing with no kit.
  function todayItems() {
    var a = JOBS.applications || [], days = JOBS.followup_days || 0, items = [];
    var fu = a.filter(function (x) { return x.followup && x.status === 'applied'; });
    if (fu.length) {
      var minD = Math.min.apply(null, fu.map(function (x) { var n = daysSince(lastMoveDate(x)); return n == null ? days : n; }));
      items.push({ ic: '⚠', tone: 'warning',
        t: fu.length + (fu.length === 1 ? ' application has' : ' applications have') + ' had no reply in ' + minD + '+ days',
        b: 'Review', go: function () { setTab('pipeline'); } });
    }
    a.filter(function (x) { return x.status === 'interviewing'; }).forEach(function (x) {
      items.push({ ic: '●', tone: 'info', t: 'Interviewing: ' + (x.role || 'a role') + ' @ ' + (x.company || '') + ' · prep not started',
        b: 'Prep', go: function () { openDetail(x.id); } });
    });
    a.filter(function (x) { return x.status === 'offer'; }).forEach(function (x) {
      items.push({ ic: '★', tone: 'info', t: 'Offer: ' + (x.role || 'a role') + ' @ ' + (x.company || '') + ' — awaiting your decision',
        b: 'Review', go: function () { openDetail(x.id); } });
    });
    var nw = (JOBS.inbox || []).filter(function (it) { var t = itemTime(it); return t && t > LAST_VISIT; }).length;
    if (nw) { items.push({ ic: '✦', tone: 'info', t: nw + ' new ' + (nw === 1 ? 'role' : 'roles') + ' since your last visit',
      b: 'Triage', go: function () { setTab('inbox'); } }); }
    var nk = a.filter(function (x) { return (x.status === 'considering' || x.status === 'preparing') && !(x.kit && x.kit.status === 'complete'); });
    if (nk.length) { items.push({ ic: '○', tone: 'info',
      t: nk.length + (nk.length === 1 ? ' saved role has' : ' saved or preparing roles have') + ' no apply kit yet',
      b: 'Build', go: function () { openDetail(nk[0].id); } }); }
    return items;
  }
  function renderToday() {
    var el = document.getElementById('today');
    if (LOADING) { el.hidden = false; el.innerHTML = '<h3>Needs you today</h3>' + skel(2); return; }
    if (JOBS_FAILED || isBrandNew()) { el.hidden = true; el.innerHTML = ''; return; }
    var items = todayItems();
    if (!items.length) { el.hidden = true; el.innerHTML = ''; return; }  // "Nothing needs you today" (§7)
    el.hidden = false;
    var cc = document.getElementById('cc');
    var cap = (cc && cc.clientWidth && cc.clientWidth < 600) ? 2 : 3;  // 2 rows on the rail, 3 otherwise (§8)
    var shown = TODAY_ALL ? items : items.slice(0, cap), more = items.length - shown.length;
    el.innerHTML = '<h3>Needs you today</h3>' + shown.map(function (it, i) {
      var mark = it.tone === 'warning'
        ? '<span class="tmark pl-chip pl-chip--warning" aria-hidden="true">' + it.ic + '</span>'
        : '<span class="tmark" aria-hidden="true">' + it.ic + '</span>';
      return '<div class="trow pl-row">' + mark + '<span class="grow">' + esc(it.t) + '</span>' +
        '<button class="pl-btn pl-btn--sm" data-t="' + i + '">' + esc(it.b) + '</button></div>';
    }).join('') + (more > 0 ? '<div class="trow pl-row"><span class="grow"></span>' +
      '<button class="pl-btn pl-btn--ghost pl-btn--sm" id="today-more">+' + more + ' more</button></div>' : '');
    el.querySelectorAll('[data-t]').forEach(function (btn) { btn.onclick = function () { shown[+btn.getAttribute('data-t')].go(); }; });
    var m = document.getElementById('today-more'); if (m) m.onclick = function () { TODAY_ALL = true; renderToday(); };
  }
  // WAI-ARIA tabs (§10): roving tabindex, arrow/Home/End keys, counts in the accessible name,
  // the count hidden at 0. During loading the strip shows with no counts.
  function renderTabs() {
    var el = document.getElementById('tabs');
    if (isBrandNew()) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    var showCounts = !LOADING && !JOBS_FAILED;
    var c = showCounts ? counts() : null;
    var T = [['pipeline', 'Pipeline'], ['inbox', 'Inbox'], ['closed', 'Closed']];
    el.innerHTML = T.map(function (t) {
      var n = c ? c[t[0]] : 0, on = TAB === t[0];
      var name = t[1] + (n ? ', ' + n : '');
      return '<button class="tab" role="tab" id="tab-' + t[0] + '" aria-controls="body" aria-selected="' + on + '"' +
        ' tabindex="' + (on ? 0 : -1) + '" data-tab="' + t[0] + '" aria-label="' + name + '">' +
        t[1] + (n ? ' <span class="pl-count" aria-hidden="true">' + n + '</span>' : '') + '</button>';
    }).join('');
    el.querySelectorAll('.tab').forEach(function (btn, i) {
      btn.onclick = function () { setTab(btn.getAttribute('data-tab')); };
      btn.onkeydown = function (e) {
        var ni = -1;
        if (e.key === 'ArrowRight') ni = (i + 1) % T.length;
        else if (e.key === 'ArrowLeft') ni = (i - 1 + T.length) % T.length;
        else if (e.key === 'Home') ni = 0;
        else if (e.key === 'End') ni = T.length - 1;
        else return;
        e.preventDefault(); setTab(T[ni][0]);
      };
    });
    var body = document.getElementById('body'); if (body) body.setAttribute('aria-labelledby', 'tab-' + TAB);
    fitTabs();
  }
  // Responsive tabs (§8): the WAI-ARIA tablist above is the default. When the three tabs + counts
  // don't fit the container (a very narrow rail or split-screen), the strip collapses to a single
  // menu trigger (aria-haspopup) that opens a pl-menu of the same tabs, so the counts never overflow
  // or clip. Reads layout only (scrollWidth vs clientWidth) and swaps #tabs markup — never the state.
  // Re-run after every renderTabs and on resize; inert where the container reports no width.
  function fitTabs() {
    var el = document.getElementById('tabs');
    if (!el || el.hidden) return;
    if (!el.scrollWidth || el.scrollWidth <= el.clientWidth + 1) return;  // they fit → keep the tablist
    var showCounts = !LOADING && !JOBS_FAILED, c = showCounts ? counts() : null;
    var T = [['pipeline', 'Pipeline'], ['inbox', 'Inbox'], ['closed', 'Closed']];
    function nameOf(t) { var n = c ? c[t[0]] : 0; return t[1] + (n ? ', ' + n : ''); }
    function badge(t) { var n = c ? c[t[0]] : 0; return n ? ' <span class="pl-count" aria-hidden="true">' + n + '</span>' : ''; }
    var active = T.filter(function (t) { return TAB === t[0]; })[0] || T[0];
    el.innerHTML = '<button class="tab--menu" id="tabs-trigger" aria-haspopup="menu" aria-expanded="false"' +
      ' aria-label="' + nameOf(active) + ', choose a view">' + active[1] + badge(active) +
      ' <span aria-hidden="true">▾</span></button>' +
      '<div class="tabs-menu" id="tabs-menu" role="menu" aria-label="Jobs" hidden>' + T.map(function (t) {
        return '<button class="tab" role="menuitemradio" data-tab="' + t[0] + '" aria-checked="' + (TAB === t[0]) +
          '" aria-label="' + nameOf(t) + '">' + t[1] + badge(t) + '</button>';
      }).join('') + '</div>';
    var trigger = document.getElementById('tabs-trigger'), menu = document.getElementById('tabs-menu');
    function closeMenu(toTrigger) { menu.hidden = true; trigger.setAttribute('aria-expanded', 'false'); if (toTrigger && trigger.focus) trigger.focus(); }
    function openMenu() { menu.hidden = false; trigger.setAttribute('aria-expanded', 'true'); var f = menu.querySelector('[aria-checked="true"]') || menu.querySelector('button'); if (f && f.focus) f.focus(); }
    trigger.onclick = function () { if (menu.hidden) openMenu(); else closeMenu(false); };
    trigger.onkeydown = function (e) { if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); if (menu.hidden) openMenu(); } };
    var items = Array.prototype.slice.call(menu.querySelectorAll('[data-tab]'));
    items.forEach(function (it, i) {
      it.onclick = function () { closeMenu(false); setTab(it.getAttribute('data-tab')); };
      it.onkeydown = function (e) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus(); }
        else if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); it.onclick(); }
        else if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); }
      };
    });
    var body = document.getElementById('body'); if (body) body.setAttribute('aria-labelledby', 'tabs-trigger');
  }
  function renderBody() {
    var b = document.getElementById('body');
    if (LOADING || JOBS_FAILED) { b.innerHTML = skel(5); return; }  // API unreachable: bodies stay skeletons (§7)
    if (isBrandNew()) return renderNew(b);
    if (TAB === 'inbox') return renderInbox(b);
    if (TAB === 'closed') return renderClosedTab(b);
    return renderPipeline(b);
  }
  // Brand-new empty state (§7): the coaching-tips paragraph now lives here as three suggestion chips.
  function renderNew(b) {
    b.innerHTML = '<div class="emptyc pl-empty">' +
      '<div class="empty-title">Your coach hasn’t started a search yet</div>' +
      '<p class="muted sm emptyc-desc">Run /setup-coach in chat. It builds your profile, then a watch starts surfacing roles here.</p>' +
      '<button class="pl-btn pl-btn--primary" id="setup">Start setup</button>' +
      '<div class="sugs">' + ['Paste a posting and say /apply', 'Critique my CV', 'Run a mock interview'].map(function (s) {
        return '<button class="pl-suggestion pl-btn pl-btn--sm" data-sug="' + esc(s) + '">' + esc(s) + '</button>'; }).join('') +
      '</div></div>';
    var setup = document.getElementById('setup');
    if (setup) setup.onclick = function () { compose('/setup-coach'); toast('Drafted “/setup-coach” into the chat composer. Nothing was sent.'); };
    b.querySelectorAll('[data-sug]').forEach(function (x) { x.onclick = function () { var s = x.getAttribute('data-sug'); compose(s); toast('Drafted “' + s + '” into the chat composer.'); }; });
  }
  // ── Inbox tab (§3): freshness groups, text match reasons, hover/focus/selection actions,
  // keyboard triage, multi-select and a 6s-held dismiss. The numeric score never shows (§1.5). ──

  // The freshness bucket (§3/§11.2): score-0 rows are always "Title didn't match"; everything else
  // is cut by posted (falling back to first_seen) into Today / This week / Older.
  function inboxWhenMs(it) {
    var s = it.posted || it.first_seen || '';
    if (!s) return 0;
    var t = new Date(s).getTime();
    return isNaN(t) ? 0 : t;
  }
  function bucket(it) {
    if (Number(it.score || 0) <= 0) return 'miss';
    var t = inboxWhenMs(it);
    if (!t) return 'older';
    var h = (Date.now() - t) / 3600000;
    return h < 24 ? 'today' : (h < 168 ? 'week' : 'older');
  }
  var GRP = [['today', 'Today'], ['week', 'This week'], ['older', 'Older'], ['miss', 'Title didn’t match']];
  // A row is "new" when it was surfaced since the last visit — the same first_seen||posted clock the
  // Today band counts (§3). LAST_VISIT comes from localStorage, falling back to the last 24h (§11.1).
  function isNew(it) { var t = itemTime(it); return !!t && t > LAST_VISIT; }
  // "2h ago" / "3d ago" from the posted (or first_seen) date — plain text, never a numeric score.
  function relAge(dateStr) {
    if (!dateStr) return '';
    var t = new Date(dateStr).getTime();
    if (isNaN(t)) return '';
    var h = Math.floor((Date.now() - t) / 3600000);
    if (h < 1) return 'just now';
    if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }
  // Ghost evidence is a list of strings from the server (empty when none). Join it for the tooltip.
  function ghostText(it) {
    var g = it.ghost;
    if (!g || !g.length) return '';
    return (typeof g === 'string') ? g : g.join('; ');
  }
  function hasGhost(it) { return !!(it.ghost && it.ghost.length); }
  // The matched target role, pulled from the server's match reason ('title matches "X" (…)'), so the
  // row can emphasise it. Empty when nothing was quoted.
  function matchedRole(it) {
    var m = /"([^"]+)"/.exec(it.reason || '');
    return m ? m[1] : '';
  }
  // Match reason AS TEXT (§3): the score itself never shows. 100 → "Title matches X", a partial
  // score → "Title partly matches X", 0 (the miss group) → "Title doesn't match your targets".
  function reasonText(it) {
    var score = Number(it.score || 0);
    if (score <= 0) return 'Title doesn’t match your targets';
    var role = matchedRole(it), verb = score >= 100 ? 'Title matches' : 'Title partly matches';
    return role ? verb + ' <em>' + esc(role) + '</em>' : verb + ' your targets';
  }
  // The longer form shown in the inbox drawer's "Why it surfaced" section.
  function reasonSentence(it) {
    var score = Number(it.score || 0);
    if (score <= 0) return 'The title doesn’t match your target roles. The watch kept it because the company is on your list.';
    var role = matchedRole(it), verb = score >= 100 ? 'Title matches' : 'Title partly matches';
    return role ? verb + ' <em>' + esc(role) + '</em>, one of your target roles.' : verb + ' your target roles.';
  }
  function findInbox(id) {
    var l = JOBS.inbox || [];
    for (var i = 0; i < l.length; i++) { if (l[i].id === id) return l[i]; }
    return null;
  }
  function inboxVisible() {
    return (JOBS.inbox || []).filter(function (it) { return !INBOX_HIDDEN[it.id]; });
  }
  function selIds() { var out = []; for (var k in INBOX_SEL) { if (INBOX_SEL[k]) out.push(k); } return out; }

  // One triage row: a listbox option carrying the "New" marker, title, company · posted · reason,
  // the ghost warning, and the hover/focus/selection-only Keep / Dismiss / View posting (§3/§6).
  function inboxRowHtml(it) {
    var sel = !!INBOX_SEL[it.id], ghost = ghostText(it), when = it.posted || it.first_seen || '';
    var parts = [];
    if (it.company) parts.push(esc(it.company));
    var rel = relAge(when); if (rel) parts.push(esc(rel));
    parts.push(reasonText(it));
    var newMark = isNew(it) ? '<span class="ibx-new"><span class="pl-dot pl-dot--info" aria-hidden="true"></span>New</span> ' : '';
    return '<div class="ibx-row' + (sel ? ' sel' : '') + '" role="option" aria-selected="' + sel + '" tabindex="0" data-id="' + esc(it.id) + '">' +
      '<input type="checkbox" class="pl-checkbox ibx-ck" tabindex="-1" aria-label="Select ' + esc(it.title) + '"' + (sel ? ' checked' : '') + '>' +
      '<div class="ibx-main">' +
        '<div class="ibx-ti">' + newMark + esc(it.title) + '</div>' +
        '<div class="ibx-meta">' + parts.join(' · ') + '</div>' +
        (ghost ? '<div class="ibx-ghost"><span class="pl-badge pl-badge--warning" title="' + esc(ghost) + '">Possible ghost job</span> ' +
          '<span class="muted sm">' + esc(ghost) + '</span></div>' : '') +
      '</div>' +
      '<div class="ibx-acts">' +
        (it.url ? '<a class="pl-btn pl-btn--ghost pl-btn--xs" href="' + esc(it.url) + '" target="_blank" rel="noopener" data-a="open" tabindex="-1">View posting</a>' : '') +
        '<button class="pl-btn pl-btn--xs" type="button" data-a="dismiss" tabindex="-1">Dismiss</button>' +
        '<button class="pl-btn pl-btn--primary pl-btn--xs" type="button" data-a="keep" tabindex="-1">Keep</button>' +
      '</div></div>';
  }
  // One freshness accordion: chev + label + per-group Select all + the Count, then the listbox rows.
  function inboxGroupHtml(g, rows) {
    var key = g[0];
    return '<details class="grp pl-accordion" data-g="' + key + '"' + (INBOX_OPEN[key] ? ' open' : '') + '>' +
      '<summary><span class="chev" aria-hidden="true">›</span><span class="grow">' + esc(g[1]) + '</span>' +
      '<button class="pl-btn pl-btn--ghost pl-btn--xs" type="button" data-all="' + key + '">Select all</button>' +
      '<span class="pl-count">' + rows.length + '</span></summary>' +
      '<div role="listbox" aria-multiselectable="true" aria-label="' + esc(g[1]) + '">' +
      rows.map(inboxRowHtml).join('') + '</div></details>';
  }
  function inboxSortToolbar(ghosts) {
    return '<div class="ibx-toolbar">' +
      '<label class="muted" for="ibx-sort">Sort</label>' +
      '<select id="ibx-sort" class="pl-select">' +
        '<option value="newest"' + (INBOX_SORT === 'newest' ? ' selected' : '') + '>Newest</option>' +
        '<option value="oldest"' + (INBOX_SORT === 'oldest' ? ' selected' : '') + '>Oldest</option>' +
      '</select>' +
      (ghosts ? '<button class="pl-chip" type="button" id="ibx-ghost" aria-pressed="' + (HIDE_GHOST ? 'true' : 'false') + '">' +
        (HIDE_GHOST ? '✓ ' : '') + 'Hide likely ghost jobs · ' + ghosts + '</button>' : '') +
      '<span class="ibx-kbd sm"><span class="pl-kbd">↑↓</span> move <span class="pl-kbd">K</span> keep ' +
      '<span class="pl-kbd">D</span> dismiss <span class="pl-kbd">O</span> posting <span class="pl-kbd">Enter</span> open</span>' +
      '</div>';
  }
  function inboxSelToolbar(n) {
    return '<div class="ibx-toolbar"><strong>' + n + ' selected</strong>' +
      '<button class="pl-btn pl-btn--sm" type="button" id="ibx-bkeep">Keep</button>' +
      '<button class="pl-btn pl-btn--sm" type="button" id="ibx-bdismiss">Dismiss</button>' +
      '<button class="pl-btn pl-btn--ghost pl-btn--sm" type="button" id="ibx-bclear">Clear</button></div>';
  }
  // §7 Inbox empty states: an empty watch_last_run means the watch has never run ("looking");
  // otherwise the inbox is simply cleared ("All caught up", with the date the watch last ran).
  function renderInboxEmpty(b) {
    var ran = JOBS.watch_last_run || '', title, desc;
    if (!ran) { title = 'Your watch is looking'; desc = 'New roles land here.'; }
    else { title = 'All caught up'; desc = 'Your watch last ran on ' + esc(String(ran).slice(0, 10)) + '. New roles land here.'; }
    b.innerHTML = '<div class="emptyc pl-empty"><div class="empty-title">' + title + '</div>' +
      '<p class="muted sm emptyc-desc">' + desc + '</p></div>';
  }
  function renderInbox(b) {
    var all = inboxVisible();
    if (!all.length) return renderInboxEmpty(b);
    var ghosts = all.filter(hasGhost).length;
    var rows = all.filter(function (it) { return !(HIDE_GHOST && hasGhost(it)); }).sort(function (x, y) {
      var dx = inboxWhenMs(x), dy = inboxWhenMs(y);
      return INBOX_SORT === 'newest' ? dy - dx : dx - dy;  // newest first by default; freshness is the only signal
    });
    var n = selIds().length;
    var groups = GRP.map(function (g) {
      var gr = rows.filter(function (it) { return bucket(it) === g[0]; });
      return gr.length ? inboxGroupHtml(g, gr) : '';
    }).join('');
    b.innerHTML = (n ? inboxSelToolbar(n) : inboxSortToolbar(ghosts)) +
      '<div class="' + (n ? 'ibx-multi' : '') + '" id="ibx-groups">' + groups + '</div>';
    wireInbox(b);
  }
  function wireInbox(b) {
    var sort = document.getElementById('ibx-sort');
    if (sort) sort.onchange = function () { INBOX_SORT = sort.value; render(); };
    var ghost = document.getElementById('ibx-ghost');
    if (ghost) ghost.onclick = function () { HIDE_GHOST = !HIDE_GHOST; render(); };
    var bk = document.getElementById('ibx-bkeep'); if (bk) bk.onclick = function () { inboxKeep(selIds()); };
    var bd = document.getElementById('ibx-bdismiss'); if (bd) bd.onclick = function () { inboxDismiss(selIds()); };
    var bc = document.getElementById('ibx-bclear'); if (bc) bc.onclick = function () { INBOX_SEL = {}; render(); };
    b.querySelectorAll('details[data-g]').forEach(function (d) {
      d.ontoggle = function () { INBOX_OPEN[d.getAttribute('data-g')] = d.open; };
    });
    b.querySelectorAll('[data-all]').forEach(function (x) {
      x.onclick = function (e) { e.preventDefault(); e.stopPropagation(); inboxSelectAll(x.getAttribute('data-all')); };
    });
    wireInboxRows(b);
    if (INBOX_FOCUS) {
      var fe = b.querySelector ? b.querySelector('[data-id="' + INBOX_FOCUS + '"]') : null;
      INBOX_FOCUS = null;
      if (fe && fe.focus) fe.focus();
    }
  }
  // Keyboard triage (§3/§10): shortcuts fire ONLY on the focused row (never a nested field, and only
  // inside the listbox). ↑/↓ roves, K keeps, D dismisses, O opens the posting, Enter opens the
  // drawer, Space toggles the selection.
  function wireInboxRows(b) {
    var rows = [];
    b.querySelectorAll('.ibx-row[data-id]').forEach(function (r) { rows.push(r); });
    rows.forEach(function (row, i) {
      var id = row.getAttribute('data-id');
      function nextId() { var nb = rows[i + 1] || rows[i - 1]; return nb ? nb.getAttribute('data-id') : null; }
      row.onclick = function (e) {
        var t = e.target;
        if (t.closest && t.closest('.ibx-ck')) { inboxToggle(id); return; }
        var act = t.closest ? t.closest('[data-a]') : null;
        if (act) {
          var a = act.getAttribute('data-a');
          e.stopPropagation();
          if (a === 'open') return;  // the <a> opens the posting itself
          if (a === 'keep') { INBOX_FOCUS = nextId(); inboxKeep([id]); }
          else if (a === 'dismiss') { INBOX_FOCUS = nextId(); inboxDismiss([id]); }
          return;
        }
        openInboxDetail(id);
      };
      row.onkeydown = function (e) {
        if (e.target !== row) return;  // never from a nested field (§10)
        var k = (e.key || '').toLowerCase();
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          var nb = rows[i + (e.key === 'ArrowDown' ? 1 : -1)];
          if (nb && nb.focus) nb.focus();
        } else if (k === 'k') { e.preventDefault(); INBOX_FOCUS = nextId(); inboxKeep([id]); }
        else if (k === 'd') { e.preventDefault(); INBOX_FOCUS = nextId(); inboxDismiss([id]); }
        else if (k === 'o') { e.preventDefault(); openPosting(id); }
        else if (e.key === 'Enter') { e.preventDefault(); openInboxDetail(id); }
        else if (e.key === ' ' || e.key === 'Spacebar' || k === 'x') { e.preventDefault(); inboxToggle(id); }
      };
    });
  }
  function inboxToggle(id) {
    if (INBOX_SEL[id]) delete INBOX_SEL[id]; else INBOX_SEL[id] = 1;
    INBOX_FOCUS = id;
    render();
  }
  // Select all of one group (its currently-visible rows) — how the "Title didn't match" or "Older"
  // group is cleared in one move (§3).
  function inboxSelectAll(key) {
    inboxVisible().forEach(function (it) {
      if (HIDE_GHOST && hasGhost(it)) return;
      if (bucket(it) === key) INBOX_SEL[it.id] = 1;
    });
    render();
  }
  function openPosting(id) {
    var it = findInbox(id);
    if (!it) return;
    if (it.url) { try { window.open(it.url, '_blank', 'noopener'); return; } catch (e) {} }
    toast('No posting link for this role.');
  }
  // Keep (§3): optimistic remove + an immediate write (the route promotes the role to Saved and
  // emits a live-refresh event). No Undo — the spec gives Keep none. A failed write rolls the row
  // back and shows the §7 error toast.
  function inboxKeep(ids) {
    if (!ids || !ids.length) return;
    ids.forEach(function (id) { INBOX_HIDDEN[id] = 1; });
    INBOX_SEL = {};
    render();
    toast(ids.length === 1 ? 'Kept — in Saved.' : 'Kept ' + ids.length + ' roles — in Saved.');
    announce('Kept, moved to Saved.');
    ids.forEach(function (id) {
      write('/inbox/keep', { id: id })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); load(); })
        .catch(function () {
          delete INBOX_HIDDEN[id];
          render();
          toast('Couldn’t move that role. It’s back where it was.', { error: true });
        });
    });
  }
  // Dismiss (§3): optimistic remove + a 6s Undo. There is NO undo route, so the dismiss write is
  // HELD until the 6s window closes — Undo simply cancels the pending timer and un-hides the row.
  // A write that fails after the window rolls the row back and shows the §7 error toast.
  function inboxDismiss(ids) {
    if (!ids || !ids.length) return;
    var copy = ids.slice();
    copy.forEach(function (id) { INBOX_HIDDEN[id] = 1; });
    INBOX_SEL = {};
    render();
    var one = copy.length === 1 ? findInbox(copy[0]) : null;
    toast(copy.length === 1 ? 'Dismissed ' + ((one && one.title) || 'role') + '.' : 'Dismissed ' + copy.length + ' roles.', {
      undo: function () {
        copy.forEach(function (id) { clearTimeout(DISMISS_TIMERS[id]); delete DISMISS_TIMERS[id]; delete INBOX_HIDDEN[id]; });
        render();
        announce('Dismiss undone.');
      }
    });
    announce('Dismissed.');
    copy.forEach(function (id) {
      DISMISS_TIMERS[id] = setTimeout(function () {
        delete DISMISS_TIMERS[id];
        write('/inbox/dismiss', { id: id })
          .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); load(); })
          .catch(function () {
            delete INBOX_HIDDEN[id];
            render();
            toast('Couldn’t move that role. It’s back where it was.', { error: true });
          });
      }, 6000);
    });
  }
  // The inbox drawer (§3): opened by a row click or Enter so a posting can be read before Keep.
  // Rendered straight from the inbox item already in hand (no fetch — it is not an application yet).
  function openInboxDetail(id) {
    var it = findInbox(id);
    if (!it) return;
    rememberDetailOpener(); DETAIL_ID = null; DETAIL_REQUEST++;
    DETAIL = { application: { role: it.title, company: it.company, source: it.url } };
    var when = it.posted || it.first_seen || '', ghost = ghostText(it);
    document.getElementById('detail-title').innerHTML =
      '<div class="detail-role">' + esc(it.title || 'This role') + '</div>' +
      '<div class="detail-co">' + esc(it.company || '') + (when ? ' · posted ' + esc(relAge(when)) : '') + '</div>' +
      '<label>Status <select class="pl-select pl-dropselect" disabled><option>Inbox</option></select></label>' +
      (it.url ? '<a class="pl-btn pl-btn--ghost" href="' + esc(it.url) + '" target="_blank" rel="noopener">View posting</a>' : '');
    var html = '<section class="dsec"><h4>Next step</h4><div class="action-btns">' +
      '<button class="pl-btn pl-btn--primary" type="button" id="ibx-d-keep">Keep (move to Saved)</button>' +
      '<button class="pl-btn" type="button" id="ibx-d-dismiss">Dismiss</button>' +
      coachMenu() + '</div></section>' +
      '<section class="dsec"><h4>Why it surfaced</h4><p class="sm">' + reasonSentence(it) + '</p>' +
      (ghost ? '<p style="margin-top:8px"><span class="pl-badge pl-badge--warning">Possible ghost job</span> ' +
        '<span class="muted sm">' + esc(ghost) + '</span></p>' : '') + '</section>';
    if (it.snippet) html += '<section class="dsec"><h4>Posting</h4><div class="snippet">' + esc(it.snippet) + '</div></section>';
    document.getElementById('detail-body').innerHTML = html;
    wireDetail();
    var keep = document.getElementById('ibx-d-keep'); if (keep) keep.onclick = function () { closeDetail(); inboxKeep([id]); };
    var dis = document.getElementById('ibx-d-dismiss'); if (dis) dis.onclick = function () { closeDetail(); inboxDismiss([id]); };
    showDetail();
  }
  // A board card (§3): role, company, age. Fit shows only when fit_score > 0 (never a 0). No status
  // pill — the column already says it. A server-flagged follow-up adds ONE pl-chip--warning line that
  // carries text ("No reply in N days", §10). The card is both drag-movable (draggable) and
  // keyboard-movable (role=button, tabindex 0); the per-card "Move to…" select is gone (§6).
  function cardHtml(a) {
    var n = daysSince(lastMoveDate(a));
    var age = n == null ? '' : '<span>' + n + 'd</span>';
    var fitN = Number(a.fit_score || 0);
    var fit = fitN > 0 ? '<span class="fit">fit ' + fitN + '</span>' : '';
    var nd = n == null ? (JOBS.followup_days || 0) : n;
    var nudge = a.followup ? '<div class="nudge pl-chip pl-chip--warning">No reply in ' + nd + ' days</div>' : '';
    var picked = LIFT_ID === a.id;
    var aria = (a.role || 'Role') + ' at ' + (a.company || '') + ', ' + (a.status || '') +
      (n == null ? '' : ', ' + n + ' days') + (a.followup ? ', no reply, follow up' : '') + (picked ? ', picked up' : '');
    return '<div class="pcard pl-board-card' + (picked ? ' lift' : '') + '" draggable="true" tabindex="0" role="button"' +
      ' data-id="' + esc(a.id) + '" aria-pressed="' + (picked ? 'true' : 'false') + '" aria-label="' + esc(aria) + '">' +
      '<div class="prole">' + esc(a.role) + '</div>' +
      '<div class="pco">' + esc(a.company) + '</div>' +
      '<div class="pmeta">' + age + fit + '</div>' +
      nudge +
    '</div>';
  }
  // A rail stage-list row (§8): the same live role as a focusable list item under its stage accordion.
  // Drag is off at this width, so the row opens the drawer on click/Enter and moves via the keyboard
  // pick-up (↑/↓ between stages, stageKey) or the drawer Status select. The follow-up marker carries
  // text ("No reply"), never colour alone (§10). Picked-up state mirrors the board card's lift.
  function stageRowHtml(a) {
    var n = daysSince(lastMoveDate(a));
    var picked = LIFT_ID === a.id;
    var meta = [];
    if (a.company) meta.push(esc(a.company));
    if (n != null) meta.push(n + 'd');
    var nudge = a.followup ? ' · <span class="snudge">No reply</span>' : '';
    var aria = (a.role || 'Role') + ' at ' + (a.company || '') + ', ' + (a.status || '') +
      (n == null ? '' : ', ' + n + ' days') + (a.followup ? ', no reply, follow up' : '') + (picked ? ', picked up' : '');
    return '<div class="srow' + (picked ? ' lift' : '') + '" tabindex="0" role="button" data-id="' + esc(a.id) + '"' +
      ' aria-pressed="' + (picked ? 'true' : 'false') + '" aria-label="' + esc(aria) + '">' +
      '<div class="grow"><div class="srole">' + esc(a.role) + '</div>' +
      '<div class="smeta">' + meta.join(' · ') + nudge + '</div></div></div>';
  }
  // The carried Closed-tab card: its §3 table and drawer Reopen are R4, so until then this keeps the
  // status pill and the Move-to select — the only reopen path. Board cards never use it.
  function closedCardHtml(a, selOpts) {
    var n = daysSince(lastMoveDate(a));
    var age = n == null ? '' : '<span>' + n + 'd</span>';
    var nudge = a.followup ? '<div class="nudge">No reply in ' + (n == null ? (JOBS.followup_days || 0) : n) + ' days — follow up?</div>' : '';
    return '<div class="pcard pl-board-card" draggable="true" data-id="' + esc(a.id) + '">' +
      '<div class="prole">' + esc(a.role) + '</div>' +
      '<div class="pco">' + esc(a.company) + '</div>' +
      '<span class="pl-badge">' + esc(a.status) + '</span>' +
      '<div class="pmeta"><span class="fit">' + Number(a.fit_score || 0) + '</span>' + age + '</div>' +
      nudge +
      '<select class="movesel pl-select" aria-label="Move ' + esc(a.role) + ' to another column">' + selOpts + '</select>' +
    '</div>';
  }
  // Column order (§3): flagged follow-ups first, then oldest move first (earliest lastMoveDate).
  function sortCards(rows) {
    return rows.slice().sort(function (x, y) {
      var fx = x.followup ? 1 : 0, fy = y.followup ? 1 : 0;
      if (fx !== fy) return fy - fx;
      var dx = lastMoveDate(x), dy = lastMoveDate(y);
      return dx === dy ? 0 : (dx < dy ? -1 : 1);
    });
  }
  // The visible element carrying a data-id inside #body. In rail mode the board is display:none and
  // the stage-list is shown (and vice versa), so the same role renders twice (§8); focus restoration
  // and the keyboard move must land on whichever copy is actually on screen, not the hidden one.
  function visibleInBody(id) {
    var body = document.getElementById('body');
    if (!body || !body.querySelectorAll) return null;
    var els = body.querySelectorAll('[data-id]'), first = null;
    for (var i = 0; i < els.length; i++) {
      if (els[i].getAttribute && els[i].getAttribute('data-id') !== id) continue;
      if (!first) first = els[i];
      if (els[i].offsetParent !== null) return els[i];
    }
    return first;
  }
  // The board's navigable columns, in order: the five open columns + the Closed stub (each a
  // data-col drop target). The legacy "Other" column has no data-col and is never a move target.
  function boardCols(b) {
    return Array.prototype.slice.call(b.querySelectorAll('.col[data-col]')).map(function (c) { return c.getAttribute('data-col'); });
  }
  function overCol(b, col) {
    b.querySelectorAll('.col[data-col]').forEach(function (c) { c.classList.toggle('over', c.getAttribute('data-col') === col); });
  }
  function clearLift(b, card) {
    LIFT_ID = null; LIFT_COL = null;
    if (card) { card.classList.remove('lift'); card.setAttribute('aria-pressed', 'false'); }
    b.querySelectorAll('.col[data-col]').forEach(function (c) { c.classList.remove('over'); });
  }
  // Keyboard pick-up (§3, WCAG 2.5.7): Space picks a focused card up, ←/→ choose a column, Space or
  // Enter drops it where the arrows left off, Escape cancels. Each step is announced in the single
  // live region. Enter on a card at rest opens its drawer; a drop goes through moveToColumn (so the
  // Closed column still asks rejected/passed, and the move still offers Undo).
  function boardKey(ev, b, card) {
    var id = card.getAttribute('data-id'), app = findApp(id);
    if (!app) return;
    var k = ev.key;
    if (k === ' ' || k === 'Spacebar' || k === 'Enter') {
      if (LIFT_ID === id) {               // drop
        ev.preventDefault();
        var to = LIFT_COL;
        clearLift(b, card);
        if (to) moveToColumn(id, to);
        // The chooser owns focus once opened; never focus a card behind its modal.
        if (document.getElementById('chooser').style.display !== 'flex') {
          var moved = visibleInBody(id);
          if (moved && moved.focus) moved.focus();
        }
        return;
      }
      if (k === 'Enter') { ev.preventDefault(); openDetail(id); return; }
      ev.preventDefault();                // Space → pick up
      LIFT_ID = id; LIFT_COL = statusToColumn()[app.status] || boardCols(b)[0] || null;
      card.classList.add('lift'); card.setAttribute('aria-pressed', 'true');
      overCol(b, LIFT_COL);
      announce('Picked up ' + (app.role || 'this role') +
        '. Use left and right arrows to choose a column, then Space or Enter to drop, Escape to cancel.');
      return;
    }
    if (LIFT_ID === id && (k === 'ArrowRight' || k === 'ArrowLeft')) {
      ev.preventDefault();
      var cols = boardCols(b); if (!cols.length) return;
      var i = cols.indexOf(LIFT_COL); if (i === -1) i = 0;
      i = Math.max(0, Math.min(cols.length - 1, i + (k === 'ArrowRight' ? 1 : -1)));
      LIFT_COL = cols[i];
      overCol(b, LIFT_COL);
      announce('Over ' + LIFT_COL);
      return;
    }
    if (k === 'Escape' && LIFT_ID === id) {
      ev.preventDefault();
      clearLift(b, card);
      announce('Move cancelled.');
    }
  }
  function overStage(b, col) {
    b.querySelectorAll('details[data-stage]').forEach(function (d) { d.classList.toggle('over', d.getAttribute('data-stage') === col); });
  }
  function clearStageLift(b, row) {
    LIFT_ID = null; LIFT_COL = null;
    if (row) { row.classList.remove('lift'); row.setAttribute('aria-pressed', 'false'); }
    b.querySelectorAll('details[data-stage]').forEach(function (d) { d.classList.remove('over'); });
  }
  // Rail stage-list keyboard move (§8, WCAG 2.5.7): drag is off at this width, so a focused row is
  // picked up with Space, the target stage is chosen with ↑/↓ (the vertical analogue of the board's
  // ←/→), and Space or Enter drops it — Escape cancels. Each step is announced in the single live
  // region (§10). Enter on a row at rest opens its drawer; a drop routes through moveToColumn, so the
  // Closed stage still asks rejected/passed and the move still offers Undo. Columns come from the
  // (hidden) board, so pick-up targets stay identical across widths.
  function stageKey(ev, b, row) {
    var id = row.getAttribute('data-id'), app = findApp(id);
    if (!app) return;
    var k = ev.key;
    if (k === ' ' || k === 'Spacebar' || k === 'Enter') {
      if (LIFT_ID === id) {               // drop
        ev.preventDefault();
        var to = LIFT_COL;
        clearStageLift(b, row);
        if (to) moveToColumn(id, to);
        if (document.getElementById('chooser').style.display !== 'flex') {
          var moved = visibleInBody(id);
          if (moved && moved.focus) moved.focus();
        }
        return;
      }
      if (k === 'Enter') { ev.preventDefault(); openDetail(id); return; }
      ev.preventDefault();                // Space → pick up
      LIFT_ID = id; LIFT_COL = statusToColumn()[app.status] || boardCols(b)[0] || null;
      row.classList.add('lift'); row.setAttribute('aria-pressed', 'true');
      overStage(b, LIFT_COL);
      announce('Picked up ' + (app.role || 'this role') +
        '. Use up and down arrows to choose a stage, then Space or Enter to drop, Escape to cancel.');
      return;
    }
    if (LIFT_ID === id && (k === 'ArrowDown' || k === 'ArrowUp')) {
      ev.preventDefault();
      var cols = boardCols(b); if (!cols.length) return;
      var i = cols.indexOf(LIFT_COL); if (i === -1) i = 0;
      i = Math.max(0, Math.min(cols.length - 1, i + (k === 'ArrowDown' ? 1 : -1)));
      LIFT_COL = cols[i];
      overStage(b, LIFT_COL);
      announce('Over ' + LIFT_COL);
      return;
    }
    if (k === 'Escape' && LIFT_ID === id) {
      ev.preventDefault();
      clearStageLift(b, row);
      announce('Move cancelled.');
    }
  }
  // Rail stage-list wiring (§8): a click/Enter opens the drawer; keydown drives the pick-up move.
  function wireStages(b) {
    b.querySelectorAll('.srow[data-id]').forEach(function (row) {
      row.addEventListener('click', function () { if (!LIFT_ID) openDetail(row.getAttribute('data-id')); });
      row.addEventListener('keydown', function (ev) { stageKey(ev, b, row); });
    });
  }
  // Board wiring: pointer drag (dragstart/over/drop) + keyboard pick-up + click-to-open. No select.
  function wireBoard(b) {
    b.querySelectorAll('.pcard').forEach(function (card) {
      card.addEventListener('dragstart', function (ev) { DRAG_ID = card.getAttribute('data-id'); try { ev.dataTransfer.setData('text/plain', DRAG_ID); ev.dataTransfer.effectAllowed = 'move'; } catch (e) {} });
      card.addEventListener('dragend', function () { DRAG_ID = null; });
      card.addEventListener('click', function () { if (!LIFT_ID) openDetail(card.getAttribute('data-id')); });
      card.addEventListener('keydown', function (ev) { boardKey(ev, b, card); });
    });
    b.querySelectorAll('.col[data-col]').forEach(function (colEl) {
      colEl.addEventListener('dragover', function (ev) { ev.preventDefault(); if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move'; colEl.classList.add('over'); });
      colEl.addEventListener('dragleave', function () { colEl.classList.remove('over'); });
      colEl.addEventListener('drop', function (ev) { ev.preventDefault(); colEl.classList.remove('over'); var id = DRAG_ID || (ev.dataTransfer && ev.dataTransfer.getData('text/plain')); if (id) moveToColumn(id, colEl.getAttribute('data-col')); });
    });
  }
  // Carried wiring for the Closed-tab cards (drag + click + the Move-to select); the board uses wireBoard.
  function wireCards(b) {
    b.querySelectorAll('.pcard').forEach(function (card) {
      card.addEventListener('dragstart', function (ev) { DRAG_ID = card.getAttribute('data-id'); try { ev.dataTransfer.setData('text/plain', DRAG_ID); ev.dataTransfer.effectAllowed = 'move'; } catch (e) {} });
      card.addEventListener('dragend', function () { DRAG_ID = null; });
      card.addEventListener('click', function (ev) { if (ev.target && ev.target.closest && ev.target.closest('.movesel')) return; openDetail(card.getAttribute('data-id')); });
    });
    b.querySelectorAll('.col[data-col]').forEach(function (colEl) {
      colEl.addEventListener('dragover', function (ev) { ev.preventDefault(); if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move'; colEl.classList.add('over'); });
      colEl.addEventListener('dragleave', function () { colEl.classList.remove('over'); });
      colEl.addEventListener('drop', function (ev) { ev.preventDefault(); colEl.classList.remove('over'); var id = DRAG_ID || (ev.dataTransfer && ev.dataTransfer.getData('text/plain')); if (id) moveToColumn(id, colEl.getAttribute('data-col')); });
    });
    b.querySelectorAll('.movesel').forEach(function (sel) {
      var card = sel.closest('.pcard');
      sel.addEventListener('change', function () { var col = sel.value; sel.value = ''; if (col) moveToColumn(card.getAttribute('data-id'), col); });
    });
  }
  function colSelOpts() {
    var cols = JOBS.columns || {};
    return '<option value="">Move to…</option>' + Object.keys(cols).map(function (c) { return '<option value="' + esc(c) + '">' + esc(c) + '</option>'; }).join('');
  }
  // Pipeline tab (§3): the live board — five open columns (Saved · Preparing · Applied ·
  // Interviewing · Offer), an "Other" column only when legacy statuses exist (§7), and a narrow
  // Closed stub that links to the Closed tab and still takes a drag-to-close / keyboard drop. Closed
  // cards never render on the board. Each column sorts follow-ups first, then oldest move.
  function renderPipeline(b) {
    var cols = JOBS.columns || {}, colNames = Object.keys(cols);
    if (!colNames.length) { b.innerHTML = '<p class="empty">No pipeline columns.</p>'; return; }
    var reverse = statusToColumn(), apps = JOBS.applications || [], closedName = closedColName();
    var buckets = Object.create(null), other = [];
    colNames.forEach(function (c) { buckets[c] = []; });
    apps.forEach(function (a) { var col = reverse[a.status]; if (col && buckets[col]) buckets[col].push(a); else other.push(a); });
    var openNames = colNames.filter(function (c) { return c !== closedName; });
    // Pipeline Empty (§7): no live roles (closed never counts). The coaching tips moved to §7's
    // brand-new empty state and to chat, so this stays a single plain line.
    var openCount = other.length;
    openNames.forEach(function (c) { openCount += buckets[c].length; });
    if (!openCount) {
      b.innerHTML = '<div class="emptyc pl-empty"><div class="empty-title">No live applications</div>' +
        '<p class="muted sm emptyc-desc">Keep a role from Inbox, or paste a posting in chat.</p></div>';
      return;
    }
    var displayCols = openNames.concat(other.length ? [null] : []);
    var cards = displayCols.map(function (col) {
      var rows = sortCards(col === null ? other : buckets[col]);
      var inner = rows.map(cardHtml).join('') || '<p class="empty" style="padding:6px 4px;font-size:12px">—</p>';
      var lbl = col === null ? 'Other statuses' : col;
      return '<div class="col pl-board-col"' + (col === null ? '' : ' data-col="' + esc(col) + '"') + ' role="listitem" aria-label="' + esc(lbl) + ', ' + rows.length + '">' +
        '<h3>' + esc(lbl) + '<span class="pl-count">' + rows.length + '</span></h3>' +
        '<div class="col-list">' + inner + '</div></div>';
    }).join('');
    var stub = '';
    if (closedName) {
      var cn = buckets[closedName].length;
      stub = '<div class="col stub" data-col="' + esc(closedName) + '" role="listitem" aria-label="' + esc(closedName) + ', ' + cn + '">' +
        '<span class="muted sm">' + esc(closedName) + '</span><b class="stub-n">' + cn + '</b>' +
        '<span class="muted sm">Drop here to close</span>' +
        '<button class="pl-btn pl-btn--ghost pl-btn--sm" id="to-closed">View all</button></div>';
    }
    // Rail stage-list (§8): the same live roles as a stacked set of stage accordions, one per open
    // column. Rendered always; CSS hides it until <600px, where the board is hidden instead. A stage
    // opens when it has cards and collapses to one line when empty, unless the operator toggled it.
    // Legacy "Other" rows aren't a named stage, so (as on the board, where they get the "Other"
    // column) they aren't mirrored here — the five spec stages plus a Closed link row is the list.
    var stageItems = openNames.map(function (col) {
      var rows = sortCards(buckets[col]);
      var open = (STAGE_OPEN[col] != null) ? STAGE_OPEN[col] : rows.length > 0;
      var inner = rows.map(stageRowHtml).join('') ||
        '<p class="empty" style="padding:8px 12px;font-size:12px">Nothing here yet.</p>';
      return '<details class="grp pl-accordion" data-stage="' + esc(col) + '"' + (open ? ' open' : '') + '>' +
        '<summary><span class="chev" aria-hidden="true">›</span><span class="grow">' + esc(col) + '</span>' +
        '<span class="pl-count">' + rows.length + '</span></summary>' +
        '<div role="list">' + inner + '</div></details>';
    }).join('');
    var stageClosed = closedName ? '<div class="srow stage-closed" tabindex="0" role="button" id="to-closed2" ' +
      'aria-label="Closed, ' + buckets[closedName].length + ', open the Closed tab"><div class="grow">' +
      '<div class="srole">Closed · ' + buckets[closedName].length + '</div>' +
      '<div class="smeta">Open the Closed tab</div></div></div>' : '';
    var stages = '<div class="stages" aria-label="Pipeline by stage">' + stageItems + stageClosed + '</div>';
    b.innerHTML = '<p class="muted sm pipeline-hint">Drag a card, or focus it and press <span class="pl-kbd">Space</span> to pick up, ' +
      '<span class="pl-kbd">←</span><span class="pl-kbd">→</span> to choose a column, <span class="pl-kbd">Space</span> to drop. ' +
      'On a narrow panel the board becomes a stage list — pick up and use <span class="pl-kbd">↑</span><span class="pl-kbd">↓</span>. ' +
      'Closed roles live on the Closed tab.</p>' +
      '<div class="board pl-board" role="list">' + cards + stub + '</div>' + stages;
    var tc = document.getElementById('to-closed'); if (tc) tc.onclick = function () { setTab('closed'); };
    var tc2 = document.getElementById('to-closed2');
    if (tc2) {
      tc2.onclick = function () { setTab('closed'); };
      tc2.onkeydown = function (e) { if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); setTab('closed'); } };
    }
    b.querySelectorAll('details[data-stage]').forEach(function (d) {
      d.ontoggle = function () { STAGE_OPEN[d.getAttribute('data-stage')] = d.open; };
    });
    wireStages(b);
    wireBoard(b);
  }
  // Closed history: filters never change the overall summary counts.
  function renderClosedTab(b) {
    var all = closedApps().slice().sort(function (x, y) { return lastMoveDate(y).localeCompare(lastMoveDate(x)); });
    var rejected = all.filter(function (a) { return a.status === 'rejected'; }).length;
    var passed = all.filter(function (a) { return a.status === 'passed'; }).length;
    var q = CLOSED_Q.toLowerCase();
    var rows = all.filter(function (a) {
      return (CLOSED_OUT === 'all' || a.status === CLOSED_OUT) &&
        (String(a.role || '') + ' ' + String(a.company || '')).toLowerCase().indexOf(q) !== -1;
    });
    var pages = Math.max(1, Math.ceil(rows.length / 25));
    CLOSED_PAGE = Math.max(0, Math.min(CLOSED_PAGE, pages - 1));
    var page = rows.slice(CLOSED_PAGE * 25, CLOSED_PAGE * 25 + 25);
    b.innerHTML = '<p class="muted sm">' + all.length + ' closed · ' + rejected + ' rejected · ' + passed + ' passed</p>' +
      '<div class="toolbar"><label for="closed-search">Search closed roles</label>' +
      '<input id="closed-search" class="pl-input" placeholder="Search role or company" value="' + esc(CLOSED_Q) + '">' +
      '<div class="pl-tabs pl-tabs--segmented" role="tablist" aria-label="Outcome">' +
      [['all', 'All'], ['rejected', 'Rejected'], ['passed', 'Passed']].map(function (o) {
        return '<button class="pl-tab" role="tab" aria-controls="closed-results" aria-selected="' + (CLOSED_OUT === o[0]) +
          '" tabindex="' + (CLOSED_OUT === o[0] ? '0' : '-1') + '" data-out="' + o[0] + '">' + o[1] + '</button>';
      }).join('') + '</div></div><div id="closed-results" role="tabpanel" aria-label="Closed roles">' +
      '<table class="pl-table"><thead><tr><th>Role</th><th class="closed-company">Company</th><th>Outcome</th><th class="closed-date">Closed on</th></tr></thead><tbody>' +
      page.map(function (a) {
        var date = lastMoveDate(a);
        return '<tr class="pl-tr--interactive" tabindex="0" data-id="' + esc(a.id) + '"><td>' + esc(a.role) +
          '<div class="closed-sub muted sm">' + esc(a.company) + ' · ' + esc(date) + '</div></td>' +
          '<td class="closed-company">' + esc(a.company) + '</td><td><span class="pl-badge">' +
          (a.status === 'rejected' ? 'Rejected' : 'Passed') + '</span></td><td class="closed-date">' + esc(date) + '</td></tr>';
      }).join('') + '</tbody></table>' + (page.length ? '' : '<p class="pl-empty">No closed roles match</p>') +
      '</div><div class="toolbar"><span class="muted">' + (rows.length ? (CLOSED_PAGE * 25 + 1) + '–' + (CLOSED_PAGE * 25 + page.length) + ' of ' + rows.length : '0') + '</span>' +
      '<button class="pl-btn pl-btn--sm" id="closed-prev"' + (CLOSED_PAGE ? '' : ' disabled') + '>Previous</button>' +
      '<button class="pl-btn pl-btn--sm" id="closed-next"' + (CLOSED_PAGE < pages - 1 ? '' : ' disabled') + '>Next</button></div>';
    document.getElementById('closed-search').oninput = function () {
      CLOSED_Q = this.value; CLOSED_PAGE = 0; var caret = this.selectionStart;
      renderClosedTab(b);
      var input = document.getElementById('closed-search'); input.focus(); input.setSelectionRange(caret, caret);
    };
    var tabs = b.querySelectorAll('[data-out]');
    tabs.forEach(function (tab, i) {
      tab.onclick = function () { CLOSED_OUT = tab.getAttribute('data-out'); CLOSED_PAGE = 0; renderClosedTab(b); b.querySelectorAll('[data-out]')[i].focus(); };
      tab.onkeydown = function (e) {
        var n = e.key === 'ArrowRight' ? (i + 1) % 3 : e.key === 'ArrowLeft' ? (i + 2) % 3 : e.key === 'Home' ? 0 : e.key === 'End' ? 2 : -1;
        if (n !== -1) { e.preventDefault(); tabs[n].onclick(); }
      };
    });
    document.getElementById('closed-prev').onclick = function () { CLOSED_PAGE--; renderClosedTab(b); document.getElementById('closed-next').focus(); };
    document.getElementById('closed-next').onclick = function () { CLOSED_PAGE++; renderClosedTab(b); document.getElementById('closed-prev').focus(); };
    b.querySelectorAll('tr[data-id]').forEach(function (row) {
      row.onclick = function () { row.focus(); openDetail(row.getAttribute('data-id')); };
      row.onkeydown = function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); row.onclick(); } };
    });
  }
  // About-you drawer content (§5): three read-only accordions. /state failure shows a warning callout.
  function accordion(title, open, inner) {
    return '<details class="grp pl-accordion"' + (open ? ' open' : '') + '><summary><span class="chev" aria-hidden="true">›</span><span class="grow">' + esc(title) + '</span></summary><div class="grp-body">' + inner + '</div></details>';
  }
  function aboutProfile() {
    var inner;
    if (STATE_FAILED || !STATE) {
      inner = '<div class="pl-callout pl-callout--warning" role="status">Couldn’t load your profile. Nothing new will be saved until it is fixed — ask your coach where it is.</div>';
    } else if (STATE.profile_error) {
      inner = '<div class="pl-callout pl-callout--warning" role="status">Your profile file is unreadable (' + esc(STATE.profile_error) + '). Nothing new will be saved until it is fixed.</div>';
    } else {
      var prof = STATE.profile || { identity: {}, sections: {} };
      var cov = STATE.completeness || { filled: 0, total: 0, updated: '' };
      var labels = STATE.field_labels || {};
      var covLine = cov.filled ? (cov.filled + ' of ' + cov.total + ' recorded' + (cov.updated ? ' · updated ' + esc(cov.updated) : '')) : 'Nothing recorded yet — run /setup-coach in chat and it will build this with you.';
      var rows = Object.keys(labels).map(function (k) {
        var v = (prof.identity && prof.identity[k]) || (prof.sections && prof.sections[k]) || '';
        var val = v ? esc(v.length > 220 ? v.slice(0, 220) + '…' : v) : '<span class="muted"><em>not recorded</em></span>';
        return '<div class="arow"><span class="aname">' + esc(labels[k]) + '</span><span>' + val + '</span></div>';
      }).join('');
      inner = '<p class="muted sm">' + covLine + '</p>' + rows;
    }
    return accordion('What your coach knows about you', true, inner);
  }
  function aboutRubric() {
    var w = (STATE && STATE.weights) || {}, keys = Object.keys(w);
    var inner = keys.length ? keys.map(function (k) {
      return '<div class="arow"><span class="aname">' + esc(k) + '</span><span class="track"><span class="bar" style="width:' + Number(w[k] || 0) + '%"></span></span><span class="fit">' + Number(w[k] || 0) + '</span></div>';
    }).join('') : '<p class="muted sm">Not available.</p>';
    return accordion('Fit rubric — how a role is scored', false, inner);
  }
  function aboutContext() {
    var ctx = (STATE && STATE.context_block) || '(nothing injected — no profile recorded yet)';
    return accordion('Exactly what it’s told each turn', false, '<pre class="pl-code-block ctx">' + esc(ctx) + '</pre>');
  }
  function renderAboutBody() {
    var body = document.getElementById('about-body');
    var openIdx = [];
    body.querySelectorAll('details.grp').forEach(function (d, i) { openIdx[i] = d.open; });
    var head = '<p class="muted sm">A read-only view. To change anything, tell your coach in chat. Settings live in Settings › Career Coach.</p>';
    body.innerHTML = head + aboutProfile() + aboutRubric() + aboutContext();
    body.querySelectorAll('details.grp').forEach(function (d, i) { if (typeof openIdx[i] === 'boolean') d.open = openIdx[i]; });
  }
  function aboutIsOpen() { return document.getElementById('about').getAttribute('aria-hidden') === 'false'; }
  function openAbout() {
    renderAboutBody();
    document.getElementById('cc').classList.add('about-open');
    document.getElementById('about').setAttribute('aria-hidden', 'false');
    var c = document.getElementById('about-close'); if (c && c.focus) c.focus();
  }
  function closeAbout() {
    document.getElementById('cc').classList.remove('about-open');
    document.getElementById('about').setAttribute('aria-hidden', 'true');
    var ab = document.getElementById('about-btn'); if (ab && ab.focus) ab.focus();
  }
  function trap(e, root) {
    if (e.key !== 'Tab') return;
    var f = Array.prototype.slice.call(root.querySelectorAll('button:not([disabled]),summary,select:not([disabled]),textarea,input,a[href],[tabindex="0"]')).filter(function (el) { return el.getClientRects().length > 0 && !el.closest('[hidden]'); }); if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
  }
  // Live refresh (§7): re-render in place, keep the active tab, the body scroll and the focused row.
  function render() {
    var body = document.getElementById('body');
    var scroll = body ? body.scrollTop : 0;
    var ae = document.activeElement;
    var fid = ae && ae.getAttribute ? ae.getAttribute('data-id') : null;
    var fieldId = ae && ae.id && body.contains(ae) ? ae.id : null, caret = ae && ae.selectionStart;
    renderBanner(); renderHeader(); renderToday(); renderTabs(); renderBody();
    syncDetailStatus();
    if (aboutIsOpen()) renderAboutBody();
    body = document.getElementById('body'); if (body) body.scrollTop = scroll;
    if (fid) { try { var el = visibleInBody(fid); if (el && el.focus) el.focus(); } catch (e) {} }
    else if (fieldId) { var field = document.getElementById(fieldId); if (field) { field.focus(); if (caret != null && field.setSelectionRange) field.setSelectionRange(caret, caret); } }
  }
  // <<< view renderers
  // >>> s2b pure renderers — self-contained markup builders (depend only on esc), unit-tested.
  function fitVerdict(score) {
    var s = Number(score || 0);
    if (s >= 75) return 'Strong fit';
    if (s >= 50) return 'Moderate fit';
    if (s > 0) return 'Poor fit';
    return 'Not scored yet';
  }

  // One saved answer -> a row with its OWN copy control. A draft carries a visible badge and
  // distinct (dashed) styling, and is never presented as confirmed. The value is rendered as text
  // in .answer-val; the copy handler lifts it from there (so it survives a round-trip through esc).
  function answerRow(a) {
    a = a || {};
    var isDraft = a.status === 'draft';
    var badge = isDraft ? '<span class="pl-badge pl-badge--warning">Draft: confirm before using</span>' : '';
    return '<div class="answer' + (isDraft ? ' draft' : '') + '">' +
      '<div class="answer-head">' +
        '<span class="answer-key">' + esc(a.label || a.key || '') + '</span>' + badge +
        '<button class="pl-btn pl-btn--sm copy" type="button">Copy</button>' +
      '</div>' +
      '<div class="answer-val">' + esc(a.value == null ? '' : a.value) + '</div>' +
    '</div>';
  }
  function renderAnswers(list) {
    var items = list || [];
    if (!items.length) return '<p class="empty">No saved answers yet.</p>';
    return items.map(answerRow).join('');
  }
  // The kit's per-application one-off answers plus the saved standard answers, in one list — both
  // are things the operator copies into a form, and both carry a draft/confirmed status.
  function kitAnswers(kit, answers) {
    return ((kit && kit.answers) || []).concat(answers || []);
  }

  function renderKit(kit, answers) {
    kit = kit || {};
    var out = '<div class="pl-steps">';
    var r = kit.resume || {};
    out += '<div class="kit-block pl-step"><h5>Résumé</h5>';
    if (r.path || r.version || r.built) {
      if (r.version) out += '<div class="kv"><span class="k">Version</span><span>' + esc(r.version) + '</span></div>';
      if (r.built) out += '<div class="kv"><span class="k">Built</span><span>' + esc(r.built) + '</span></div>';
      if (r.path) {
        out += '<div class="kv"><span class="k">File</span><code>' + esc(r.path) + '</code>' +
          '<button class="pl-btn pl-btn--sm copy" type="button" data-copy-path>Copy path</button></div>';
      }
    } else {
      out += '<p class="muted">Not started</p><p class="muted sm">Tailors your résumé to this posting’s title and requirements.</p>';
    }
    out += '</div>';

    var cl = kit.cover_letter || {};
    out += '<div class="kit-block pl-step"><h5>Cover letter</h5>';
    if (cl.text) {
      out += '<div class="dsec-actions"><button class="pl-btn pl-btn--sm copy" type="button" data-copy-cover>' +
        'Copy cover letter</button></div><div class="cover-text">' + esc(cl.text) + '</div>';
    } else if (cl.needed === false) {
      // Only an explicit "not needed" clears the letter; unset (null) still needs one, matching
      // the kit's own _kit_missing (needed is not False ⇒ 'cover letter' is missing). A fresh kit
      // has needed:null, so JS must not treat that as "not needed".
      out += '<p class="empty">Not needed for this posting.</p>';
    } else {
      out += '<p class="muted">Not started</p><p class="muted sm">Writes one if the posting asks for it, or marks it not needed.</p>';
    }
    out += '</div>';

    out += '<div class="kit-block pl-step"><h5>Form answers</h5>';
    if ((kit.answers || []).length || kit.no_form_questions) {
      out += kit.no_form_questions ? '<p class="muted sm">No form questions for this posting.</p>' : '';
      out += renderAnswers(kitAnswers(kit, answers));
    } else {
      out += '<p class="muted">Not started</p><p class="muted sm">Drafts any new questions for you to confirm.</p>';
      if ((answers || []).length) out += '<p class="muted sm">' + answers.length + ' saved answers will be reused</p>' + renderAnswers(answers);
    }
    out += '</div></div>';
    return out;
  }

  function renderTimeline(timeline) {
    var items = timeline || [];
    if (!items.length) return '<p class="empty">No activity yet.</p>';
    return '<ol class="timeline">' + items.map(function (e) {
      e = e || {};
      var what = e.kind === 'kit' ? esc(e.what || 'updated kit') : ('Status → ' + esc(e.status || ''));
      return '<li><span class="tl-date">' + esc(e.date || '') + '</span><span class="tl-what">' + what + '</span></li>';
    }).join('') + '</ol>';
  }

  function renderContacts(contacts) {
    return '<ul class="contacts">' + (contacts || []).map(function (c) {
      c = c || {};
      var title = c.title ? ' · ' + esc(c.title) : '';
      var link = c.url
        ? ' <a class="plink" href="' + esc(c.url) + '" target="_blank" rel="noopener">profile</a>' : '';
      return '<li>' + esc(c.name || '') + title + link + '</li>';
    }).join('') + '</ul>';
  }

  // The chat-compose text for an action button. Names the action and attaches the role, company,
  // posting URL and application id (acceptance r3). Kept well under 4 KB — it only DRAFTS a message
  // into the composer (protoagent:chat:compose); the operator reviews and sends it.
  function composeText(kind, app) {
    app = app || {};
    var role = app.role || 'this role';
    var company = app.company || 'the company';
    var url = app.source || (app.posting && app.posting.url) || '';
    var id = app.id || '';
    var ref = '(' + (url ? url + ', ' : '') + 'application ' + id + ')';
    var text;
    if (kind === 'kit') {
      text = 'Build the apply kit for ' + role + ' at ' + company + ' ' + ref +
        '. Render the résumé with careercoach_render_resume → browser_open (local file:// URL) → browser_pdf; ' +
        'keep the PDF path. Then write a cover letter if the posting asks for one, then collect answers with ' +
        'careercoach_answers(action="get"). Only THEN call careercoach_save_apply_kit with all three together: ' +
        'resume_path=PDF path, cover_letter=letter text, answers_json=question-to-answer object/dict or JSON string. ' +
        'If no letter is needed, pass cover_letter="" and cover_letter_needed=false; ' +
        'set no_form_questions=true only if there genuinely are no form questions. ' +
        'If the reply says DRAFT, fill in the listed missing parts and save again with all three; report blockers.';
    } else if (kind === 'follow') {
      text = 'Draft a follow-up for ' + role + ' at ' + company + ' ' + ref + '.';
    } else if (kind === 'offer') {
      text = 'Help me weigh the offer for ' + role + ' at ' + company + ' ' + ref + '.';
    } else if (kind === 'discuss') {
      text = 'Help me with ' + role + ' at ' + company + ' ' + ref + '.';
    } else if (kind === 'tailor') {
      text = 'Tailor my resume for ' + role + ' at ' + company + ' ' + ref + '.';
    } else {
      text = 'Prep me for the interview for ' + role + ' at ' + company + ' ' + ref + '.';
    }
    return text.slice(0, 4000);  // stay under the 4 KB compose cap
  }
  // <<< s2b pure renderers

  // Copy to the clipboard: navigator.clipboard.writeText, with a textarea + execCommand fallback
  // for an older/insecure context. Flashes the button to confirm.
  function copyFallback(text) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text == null ? '' : String(text);
      ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (e) {}
  }
  function copyText(text, btn) {
    function flash() {
      if (!btn) return;
      var old = btn.textContent;
      btn.textContent = 'Copied';
      setTimeout(function () { btn.textContent = old; }, 1200);
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text == null ? '' : String(text))
          .then(flash, function () { copyFallback(text); flash(); });
        return;
      }
    } catch (e) {}
    copyFallback(text); flash();
  }

  // Draft a message for the coach; Next step and the menu never auto-send.
  function composeAction(kind) {
    if (!DETAIL) return;
    if (isClosedApp(DETAIL.application || {})) return;
    var text = composeText(kind, DETAIL.application || {});
    try { parent.postMessage({ type: 'protoagent:chat:compose', text: text }, '*'); } catch (e) {}
    toast('Drafted a message for your coach — review and send it in chat.');
  }

  // Mark applied: an INLINE confirm (never window.confirm — a sandboxed iframe without allow-modals
  // silently suppresses the native modal, returning false, so the POST could never fire), then
  // POST /applied and refresh the drawer and the board (the card moves to Applied). A declined
  // confirm, a non-2xx or a network error leaves everything untouched.
  function markApplied() {
    if (!DETAIL) return;
    var app = DETAIL.application || {};
    if (isClosedApp(app)) return;
    var id = app.id;
    if (!id) return;
    askConfirm('Mark ' + (app.role || 'this role') + ' @ ' + (app.company || 'this company') + ' as applied?')
      .then(function (ok) {
        if (!ok) return;
        write('/applications/' + encodeURIComponent(id) + '/applied', {})
          .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            if (DETAIL_ID !== id) return;
            return openDetail(id);
          })
          .then(function () { load(); })
          .catch(function () { toast('Could not mark that applied — try again.'); });
      });
  }

  // Persist the editable notes (PATCH /notes). POST-only `write` can't do PATCH, so call apiFetch.
  function saveNotes() {
    if (!DETAIL) return;
    var app = DETAIL.application || {};
    var ta = document.getElementById('detail-notes-text');
    if (!app.id || !ta || isClosedApp(app)) return;
    var notes = ta.value;
    if (notes === (app.notes || '') && !NOTE_PENDING[app.id]) return;
    var status = document.getElementById('detail-notes-status');
    status.textContent = 'Saving…';
    NOTE_DRAFTS[app.id] = notes;
    NOTE_PENDING[app.id] = (NOTE_PENDING[app.id] || 0) + 1;
    NOTE_WRITES[app.id] = (NOTE_WRITES[app.id] || Promise.resolve()).then(function () {
      return kit.apiFetch('/api/plugins/careercoach/applications/' + encodeURIComponent(app.id) + '/notes', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ notes: notes })
      });
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      app.notes = notes;
      if (NOTE_DRAFTS[app.id] === notes) delete NOTE_DRAFTS[app.id];
      if (DETAIL && DETAIL.application.id === app.id) {
        DETAIL.application.notes = notes;
        var current = document.getElementById('detail-notes-text');
        if (current && current.value === notes) document.getElementById('detail-notes-status').textContent = 'Saved';
      }
    }).catch(function () {
      if (DETAIL_ID === app.id) document.getElementById('detail-notes-status').textContent = 'Could not save — blur to retry';
      toast('Could not save your notes — try again.');
    }).then(function () { NOTE_PENDING[app.id]--; });
  }

  function wireDetail() {
    var body = document.getElementById('detail-body');
    var cp = body.querySelector('[data-copy-path]');
    if (cp) cp.addEventListener('click', function () {
      copyText(((DETAIL.kit && DETAIL.kit.resume) || {}).path || '', cp);
    });
    var cc = body.querySelector('[data-copy-cover]');
    if (cc) cc.addEventListener('click', function () {
      copyText(((DETAIL.kit && DETAIL.kit.cover_letter) || {}).text || '', cc);
    });
    body.querySelectorAll('.answer').forEach(function (el) {
      var btn = el.querySelector('.copy');
      var val = el.querySelector('.answer-val');
      if (btn && val) btn.addEventListener('click', function () { copyText(val.textContent, btn); });
    });
    body.querySelectorAll('.act').forEach(function (b) {
      b.addEventListener('click', function () { composeAction(b.getAttribute('data-act')); var menu = document.getElementById('detail-menu'); if (menu) { menu.hidden = true; var more = document.getElementById('detail-more'); more.setAttribute('aria-expanded', 'false'); more.focus(); } });
    });
    var status = document.getElementById('detail-status');
    if (status) status.addEventListener('change', function () {
      if (DETAIL_ID && (!DETAIL || !isClosedApp(DETAIL.application))) { if (status.value === 'applied') { syncDetailStatus(); markApplied(); } else moveApp(DETAIL_ID, status.value); }
    });
    var applied = document.getElementById('detail-applied');
    if (applied) applied.addEventListener('click', markApplied);
    body.querySelectorAll('[data-applied]').forEach(function (b) { b.onclick = markApplied; });
    var notes = document.getElementById('detail-notes-text');
    if (notes && !notes.readOnly) {
      notes.addEventListener('blur', saveNotes);
      notes.oninput = function () { if (DETAIL_ID) NOTE_DRAFTS[DETAIL_ID] = notes.value; document.getElementById('detail-notes-status').textContent = '';  };
    }
    var reopen = document.getElementById('detail-reopen');
    if (reopen) reopen.onclick = function () { if (DETAIL_ID) moveApp(DETAIL_ID, ((JOBS.columns || {}).Saved || ['considering'])[0]); };
    var more = document.getElementById('detail-more'), menu = document.getElementById('detail-menu');
    if (more && menu) {
      more.onclick = function () { menu.hidden = !menu.hidden; more.setAttribute('aria-expanded', String(!menu.hidden)); if (!menu.hidden) menu.querySelector('button').focus(); };
      menu.onkeydown = function (e) {
        var items = Array.prototype.slice.call(menu.querySelectorAll('button'));
        var i = items.indexOf(document.activeElement);
        if (e.key === 'Escape') { e.stopPropagation(); menu.hidden = true; more.setAttribute('aria-expanded', 'false'); more.focus(); }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus(); }
      };
    }
  }

  function coachMenu() {
    return '<button class="pl-btn" id="detail-more" aria-haspopup="menu" aria-expanded="false">Ask your coach</button>' +
      '<div class="pl-menu" id="detail-menu" role="menu" hidden>' +
      '<button class="pl-btn act" role="menuitem" data-act="kit">Build apply kit</button>' +
      '<button class="pl-btn act" role="menuitem" data-act="tailor">Tailor resume</button>' +
      '<button class="pl-btn act" role="menuitem" data-act="prep">Prep me for this interview</button>' +
      '<button class="pl-btn act" role="menuitem" data-act="follow">Draft a follow-up</button></div>';
  }
  function renderDetail(d) {
    var app = (d && d.application) || {}, kitData = (d && d.kit) || {};
    var posting = app.posting || {}, title = posting.title || app.role || '';
    var url = app.source || posting.url || '', closed = isClosedApp(app);
    var early = ['considering', 'saved', 'preparing'].indexOf(app.status) !== -1;
    document.getElementById('detail-title').innerHTML =
      '<div class="detail-role">' + esc(app.role || title || 'This role') + '</div>' +
      '<div class="detail-co">' + esc(app.company || '') + '</div>' + detailStatusHtml(app) +
      (url ? ' <a class="pl-btn pl-btn--ghost" href="' + esc(url) + '" target="_blank" rel="noopener">View posting</a>' : '') +
      '<details class="pl-accordion" id="detail-posting"><summary>Posting</summary>' +
      '<p>' + esc(title) + '</p>' + (posting.location ? '<p>' + esc(posting.location) + '</p>' : '') +
      (posting.snippet ? '<div class="snippet">' + esc(posting.snippet) + '</div>' : '') + '</details>';
    var next;
    if (closed) next = '<button class="pl-btn pl-btn--primary" id="detail-reopen">Reopen</button>';
    else if (app.status === 'interviewing') next = '<button class="pl-btn pl-btn--primary act" data-act="prep">Prep me for this interview</button>';
    else if (early && kitData.status === 'complete') next = '<button class="pl-btn pl-btn--primary" type="button" id="detail-applied">Mark applied</button>';
    else if (early) next = '<button class="pl-btn pl-btn--primary act" data-act="kit">Build apply kit</button>';
    else next = '<button class="pl-btn pl-btn--primary act" data-act="' + (app.status === 'offer' ? 'offer' : app.followup ? 'follow' : 'discuss') + '">' +
      (app.status === 'offer' ? 'Weigh this offer' : app.followup ? 'Draft a follow-up' : 'Ask coach about this role') + '</button>';
    var html = '<section id="detail-actions" class="dsec"><h4>Next step</h4>' + next + (closed ? '' : coachMenu()) + '</section>';
    html += '<details id="detail-kit" class="pl-card pl-accordion"' + (early ? ' open' : '') + '><summary>Apply kit</summary>' +
      (kitData.status === 'draft' && kitData.updated ? '<span class="pl-badge pl-badge--warning">Draft: confirm before using</span>' : '') +
      renderKit(kitData, (d && d.answers) || []) +
      (closed ? '' : '<button class="pl-btn pl-btn--primary act" data-act="kit">Build apply kit</button>' +
        '<p class="muted sm">Your coach builds all three in chat. You review each before anything is sent.</p>' +
        (app.status !== 'applied' ? '<button class="pl-btn" data-applied>I applied already: mark applied</button>' : '')) + '</details>';
    if (Number(app.fit_score) > 0) html += '<section id="detail-fit" class="dsec"><h4>Fit</h4>' +
      '<div class="fitline"><span class="fit big">' + Number(app.fit_score) + '</span><span class="verdict">' +
      esc(fitVerdict(app.fit_score)) + '</span></div><p>' + esc(app.notes || '') + '</p></section>';
    html += '<section id="detail-notes" class="dsec"><h4><label for="detail-notes-text">Notes</label></h4>' +
      '<textarea id="detail-notes-text" class="pl-textarea notes-ta"' + (closed ? ' readonly' : '') +
      ' placeholder="Your notes on this role — the fit reason, next steps, anything.">' + esc(Object.prototype.hasOwnProperty.call(NOTE_DRAFTS, app.id) ? NOTE_DRAFTS[app.id] : app.notes || '') + '</textarea>' +
      '<div id="detail-notes-status" class="muted sm" role="status"></div></section>';
    html += '<details id="detail-timeline" class="pl-accordion"><summary>Timeline</summary>' + renderTimeline(d && d.timeline) + '</details>';
    var contacts = (d && d.contacts) || [];
    if (contacts.length) html += '<details id="detail-contacts" class="pl-accordion"><summary>Contacts at ' + esc(app.company || 'this company') + '</summary>' + renderContacts(contacts) + '</details>';
    document.getElementById('detail-body').innerHTML = html;
    wireDetail();
  }

  function refreshDetailMarkup(d) {
    var root = document.getElementById('detail'), active = document.activeElement;
    var inside = active && root.contains(active), focusId = inside ? active.id : null;
    var controls = Array.prototype.slice.call(root.querySelectorAll('button,select,textarea,input,a,summary'));
    var focusIndex = inside ? controls.indexOf(active) : -1;
    var menu = document.getElementById('detail-menu'), menuOpen = menu && !menu.hidden;
    var ta = document.getElementById('detail-notes-text');
    var caret = ta && active === ta ? ta.selectionStart : null;
    var panel = document.querySelector('#detail .detail-panel'), scroll = panel ? panel.scrollTop : 0;
    var open = {};
    root.querySelectorAll('details[id]').forEach(function (el) { open[el.id] = el.open; });
    renderDetail(d);
    root.querySelectorAll('details[id]').forEach(function (el) { if (el.id in open) el.open = open[el.id]; });
    if (panel) panel.scrollTop = scroll;
    menu = document.getElementById('detail-menu');
    if (menu && menuOpen) { menu.hidden = false; document.getElementById('detail-more').setAttribute('aria-expanded', 'true'); }
    var target = focusId ? document.getElementById(focusId) : (focusIndex >= 0 ? root.querySelectorAll('button,select,textarea,input,a,summary')[focusIndex] : null);
    if (!target && inside) target = document.getElementById('detail-close');
    if (target && target.disabled) target = document.getElementById('detail-close');
    if (target) { target.focus(); if (caret != null && target.setSelectionRange) target.setSelectionRange(caret, caret); }
  }
  function rememberDetailOpener() {
    if (!document.getElementById('detail').classList.contains('open')) DETAIL_RETURN = document.activeElement;
  }
  function sizeDetail() {
    var panel = document.querySelector('#detail .detail-panel');
    if (!panel) return;
    var bottom = document.getElementById('cc').clientWidth < 600;
    panel.classList.toggle('pl-drawer--bottom', bottom);
    panel.classList.toggle('pl-drawer--right', !bottom);
  }
  // A width change (split-screen drag, rail) re-evaluates the width-dependent chrome without new
  // data: the drawer side, the Today-band cap (§8, 2 rows on the rail / 3 otherwise) and the
  // responsive tab collapse. The layout bands themselves are pure CSS container queries (§8); this
  // only re-runs the JS that reads the container's clientWidth.
  function onResize() { sizeDetail(); renderTabs(); renderToday(); }
  function showDetail() {
    sizeDetail();
    var el = document.getElementById('detail');
    var wasOpen = el.classList.contains('open');
    el.classList.add('open');
    el.setAttribute('aria-hidden', 'false');
    var close = document.getElementById('detail-close');
    if (!wasOpen && close && close.focus) close.focus();
  }
  function closeDetail() {
    saveNotes();
    var el = document.getElementById('detail');
    el.classList.remove('open');
    el.setAttribute('aria-hidden', 'true');
    DETAIL = null; DETAIL_ID = null; DETAIL_REQUEST++;
    var opener = DETAIL_RETURN; DETAIL_RETURN = null;
    if (opener && !opener.isConnected && opener.getAttribute) {
      var id = opener.getAttribute('data-id');
      opener = Array.prototype.slice.call(document.querySelectorAll('#body [data-id]')).find(function (el) { return el.getAttribute('data-id') === id; });
    }
    if (!opener || !opener.isConnected) opener = document.getElementById('tab-' + TAB);
    if (opener && opener.focus) opener.focus();
  }
  // Fetch one application through the gated apiFetch and render the drawer. A bad id / error toasts
  // and leaves the drawer closed.
  function openDetail(id) {
    if (!id) return Promise.resolve();
    rememberDetailOpener();
    DETAIL_ID = id;
    var request = ++DETAIL_REQUEST;
    return api('/applications/' + encodeURIComponent(id)).then(function (d) {
      // Drop a response the user has moved on from: a slow fetch for a card they've since closed
      // (Esc/✕ null DETAIL_ID) or clicked past (DETAIL_ID points at a newer id) must not reopen or
      // overwrite the drawer with stale data.
      if (DETAIL_ID !== id) return;
      if (DETAIL_REQUEST !== request) return;
      if (!d || d.error) { toast('Could not open that role — try again.'); return; }
      DETAIL = d;
      if (document.getElementById('detail').classList.contains('open')) refreshDetailMarkup(d); else renderDetail(d);
      showDetail();
    }, function () {
      if (DETAIL_ID !== id) return;
      if (DETAIL_REQUEST !== request) return;
      toast('Could not open that role — try again.');
    });
  }


  // /jobs drives the band, tabs and bodies; /state feeds the header subline and the About-you
  // drawer. A failed /state is a partial failure (§7) — the view still renders. A failed /jobs is
  // "API unreachable": the error banner + Retry, bodies left as skeletons.
  function load() {
    return api('/jobs').then(function (jobs) {
      JOBS = jobs || { inbox: [], applications: [], columns: {}, followup_days: 0 };
      JOBS_FAILED = false;
      return api('/state').then(function (s) { STATE = s; STATE_FAILED = false; },
                               function () { STATE = null; STATE_FAILED = true; });
    }, function () { JOBS_FAILED = true; STATE = null; STATE_FAILED = false; }).then(function () {
      LOADING = false; render();
      if (DETAIL_ID && document.getElementById('detail').classList.contains('open')) return openDetail(DETAIL_ID);
    });
  }

  // Live refresh (ADR 0039): subscribe to `careercoach.#` and reload in place on an event.
  function subscribe() {
    try { parent.postMessage({ type: 'protoagent:subscribe', patterns: ['careercoach.#'] }, '*'); } catch (e) {}
  }
  window.addEventListener('message', function (e) {
    var d = e && e.data;
    if (d && d.type === 'protoagent:event') load();
  });

  // About-you drawer: open from the header; Esc / the ✕ / the scrim dismiss it and return focus.
  document.getElementById('about-btn').addEventListener('click', openAbout);
  document.getElementById('about-close').addEventListener('click', closeAbout);
  document.getElementById('about-scrim').addEventListener('click', closeAbout);
  document.getElementById('about').addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { e.stopPropagation(); closeAbout(); } else { trap(e, document.getElementById('about')); }
  });

  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(onResize).observe(document.getElementById('cc'));

  // The job detail drawer dismisses on the ✕, the backdrop, and Esc.
  (function () {
    var close = document.getElementById('detail-close');
    if (close) close.addEventListener('click', closeDetail);
    var backdrop = document.querySelector('#detail .detail-backdrop');
    if (backdrop) backdrop.addEventListener('click', closeDetail);
    document.getElementById('detail').addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.keyCode === 27) { e.stopPropagation(); closeDetail(); }
      else trap(e, document.querySelector('#detail .detail-panel'));
    });
  })();

  // "New since your last visit" (§11.1): read the prior visit, then stamp now for the next one.
  // Per browser, in a try/catch; if storage is unavailable, fall back to the last 24h.
  try {
    var lv = localStorage.getItem('cc:lastVisit');
    LAST_VISIT = lv ? Number(lv) : (Date.now() - 86400000);
    localStorage.setItem('cc:lastVisit', String(Date.now()));
  } catch (e) { LAST_VISIT = Date.now() - 86400000; }

  // First paint: skeletons + the tab strip (no counts) before any data arrives (§7 Loading).
  LOADING = true; render();
  kit.initPluginView(function () { subscribe(); load(); });
  setTimeout(function () { subscribe(); load(); }, 800);
</script>
</body></html>"""
