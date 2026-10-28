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
import logging

from langchain_core.tools import tool

# NOTE: relative imports (`from . import state`) are kept LAZY, inside the functions that
# use them — a standalone plugin's root __init__.py is imported *bare* by pytest during
# collection (not as a package), and a top-level relative import fails there. The host-free
# testkit loads this as a real package, so the lazy imports resolve fine at register() time.

log = logging.getLogger("protoagent.plugins.careercoach")


def register(registry) -> None:
    """Entry point (ADR 0018) — called once at load with a PluginRegistry."""
    cfg = registry.config  # this plugin's resolved config + secrets (ADR 0019)

    _register_tracker_tools(registry)
    _register_jobsearch_tool(registry, cfg)
    _register_hunt_tools(registry, cfg)
    _register_packet_tools(registry, cfg)
    _register_answers_tools(registry)
    _register_formfill_tools(registry)
    _register_prepare_tool(registry)
    _register_submit_gate(registry)
    _register_handoff(registry)
    _register_rubric_knobs(registry)
    _register_subagents(registry)
    _register_profile_middleware(registry)
    _register_views(registry, cfg)
    _register_job_watch(registry, cfg)

    # skills/ and workflows/ auto-load from their conventional dirs — no call needed.
    log.info(
        "[careercoach] registered: tracker + job-search + packet/profile + standard-answers + "
        "form-fill tools, job boards + watchlist + contacts + digest, submit gate, rubric knobs, crew, "
        "profile injection, dashboard, watch"
    )


def _as_bool(value) -> bool:
    """Config booleans can arrive as real bools or strings (env/UI). Normalize."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ── agent tools: the application tracker ──────────────────────────────────────
def _register_tracker_tools(registry) -> None:
    from . import state

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
            return "Your pipeline is empty — paste me a posting, or track a role with careercoach_track_application."
        return "\n".join(
            f"- {r['role']} @ {r['company']} — fit {r.get('fit_score', 0)}, {r.get('status', '?')}, {r.get('date', '?')}"
            + (f" · {r['source']}" if r.get("source") else "")
            for r in rows
        )

    registry.register_tools([careercoach_track_application, careercoach_list_applications])


# ── agent tool: live job search (the real job source, Phase 3) ────────────────
def _register_jobsearch_tool(registry, cfg) -> None:
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
        jobs = result.jobs
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

    registry.register_tool(careercoach_search_jobs)


def _register_hunt_tools(registry, cfg) -> None:
    from .hunt import register_hunt_tools

    register_hunt_tools(registry, cfg)


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
def _register_packet_tools(registry, cfg) -> None:
    from pathlib import Path

    from . import packet, profile, resume

    templates_dir = Path(__file__).resolve().parent / "templates"

    def _root() -> str:
        return cfg.get("packet_root", "") or ""

    @tool
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
            careercoach_render_resume,
        ]
    )


# ── agent tools: saved standard application answers (confirm once, reuse confirmed) ─
# The confirm-once ATS fields. Pure logic lives in answers.py (host-free, tested); these tools are
# the thin agent surface. Form filling draws ONLY from the confirmed set — an improvised answer is
# exactly the failure this replaces, so a draft is shown to the operator and confirmed before use.
def _unknown_answer_key(key: str) -> str:
    from . import answers

    return f"Unknown answer key {key!r}. Valid keys: {', '.join(answers.STANDARD_KEYS)}."


def _register_answers_tools(registry) -> None:
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

    registry.register_tools([careercoach_get_answers, careercoach_propose_answer, careercoach_confirm_answers])


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


def _register_formfill_tools(registry) -> None:
    from . import answers, formfill

    @tool
    def careercoach_plan_fill(form_json: str, company: str, role: str, extra_answers: str = "", ats: str = "") -> str:
        """Plan a job-form fill from ONLY the operator's confirmed standard answers plus `extra_answers` (JSON object:
        field label → operator-supplied value). `form_json`: the exact array browser_form_read returned. `ats="ashby"`
        for jobs.ashbyhq.com (adds a required résumé upload first). Returns a session_id, ordered fill rows, and an
        "Ask the operator about ONLY these" list. A draft never fills; a select answer is never fuzzy-matched. The
        full loop: apply-form skill."""
        try:
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
        operator about ONLY these" list. Ashby, Workable, gh_jid careers pages and other URLs: no schema; it returns
        the live-form path. Preparation only — the live read-back (browser_form_read → careercoach_plan_fill → fill →
        careercoach_verify_fill) is still required before any submit."""
        from . import answers, ats, formfill

        live_path = (
            "Open the application in the browser and use the live-form path: "
            "browser_form_read → careercoach_plan_fill → fill → careercoach_verify_fill."
        )
        try:
            detected = ats.detect(url)
            if not detected:
                return f"{url!r} isn't a recognised Greenhouse job URL, so there's no public schema to prepare from. {live_path}"
            if detected.get("ats") == "workable":
                return (
                    f"This is a Workable job ({detected['company']}/{detected['job_id']}). "
                    f"Open apply_url directly: {detected['apply_url']} instead of clicking Apply on the overview. "
                    "Use browser_form_read → careercoach_plan_fill → fill → browser_form_read again → "
                    "careercoach_verify_fill. Read back the checked state of every radio/radio-group; "
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
                "the operator now, but the live read-back is still required before any submit.\n"
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


def _register_submit_gate(registry) -> None:
    """Register ``careercoach_request_submit`` (the operator-approval tool) and the middleware that
    blocks submit-like browser calls without a live grant. The middleware is the enforcement; the
    tool is the only path that ever creates a grant."""
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

    registry.register_tool(careercoach_request_submit)
    _register_submit_middleware(registry)


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
# job-application-assistant skill (the source of truth) rather than duplicating it here.
#
# The host builds subagents WITHOUT plugin middleware (graph/agent.py's subagent stack), so the
# always-on <operator_profile> block never reaches them. The two that judge or write about the
# candidate get the profile READ tools instead, and their prompts say to read before working.
PROFILE_READ_TOOLS = ["careercoach_get_profile", "careercoach_read_profile"]


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
            "careercoach_read_profile('experience') and their skills with "
            "careercoach_get_profile('skills'); if both come back empty, say the score can't be "
            "grounded rather than scoring an imagined background. "
            "For the full rubric, bands, and exact output format, call load_skill('job-application-"
            "assistant') and read its job-evaluation.md. Use web_search/fetch_url for missing context. "
            "Output the evaluation table, weighted overall, verdict, strengths, gaps, and a clear "
            "apply/apply-with-caveats/skip recommendation, then record it with "
            "careercoach_track_application. Be honest: a weak fit is a weak fit — never inflate a score."
        ),
        tools=[
            "load_skill",
            "list_skills",
            "web_search",
            "fetch_url",
            "current_time",
            "careercoach_track_application",
            *PROFILE_READ_TOOLS,
        ],
    )

    application_writer = SubagentConfig(
        name="application_writer",
        description=(
            "Given an evaluation + company research, drafts a tailored CV and cover letter following "
            "the writing-style discipline and the honesty test — reframes emphasis, never fabricates."
        ),
        system_prompt=(
            "You are application_writer, the Career Coach's drafter. Given a fit evaluation and a "
            "company-research brief, produce a tailored CV and a cover letter. FIRST read what is true "
            "about the candidate: careercoach_read_profile('experience') for their verified career "
            "history and careercoach_get_profile('do_not_claim') for the lines you must never claim "
            "(plus careercoach_get_profile('skills') and ('stories') as needed). Draft only from what "
            "those return, and treat every do_not_claim line as a hard stop. If the history comes back "
            "empty, say so and stop: never invent a career. Follow the writing-style "
            "discipline: no em-dashes, no clichés, and the interview-backtrack honesty test (reframe "
            "emphasis, NEVER claim experience the candidate lacks). For the full guidance call "
            "load_skill('job-application-assistant') and read writing-style.md, cv-guide.md, and "
            "cover-letter-guide.md. Verify every company-specific claim against the research brief's "
            "sources before including it. If the verdict was Weak or Poor Fit, say so and stop rather "
            "than forcing a draft. End with a verification checklist (factual accuracy against the "
            "profile, do_not_claim respected, targeting, company claims verified, style)."
        ),
        tools=["load_skill", "list_skills", *PROFILE_READ_TOOLS, "careercoach_track_application", "current_time"],
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

    from . import profile, state
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
        when an applied role is overdue a nudge), the column mapping, and the follow-up window."""
        days = _followup_days()
        today = state._today()
        try:
            inbox = wl.inbox_for_view()
        except wl.StoreUnreadable:
            inbox = []  # an unreadable inbox reads as empty — the board still renders the pipeline
        apps = [{**r, "followup": state.needs_followup(r, days, today)} for r in state.load_applications()]
        return JSONResponse({"inbox": inbox, "applications": apps, "columns": state.COLUMNS, "followup_days": days})

    @data.post("/inbox/{item_id:path}/keep")
    async def _keep(item_id: str):
        """Promote a watch-inbox item into the pipeline as a `considering` row (keeping its score,
        reason and posting evidence) and hide it from the inbox. Existing applications are
        preserved unchanged."""
        try:
            item = next((j for j in wl.inbox_for_view() if j.get("id") == item_id), None)
        except wl.StoreUnreadable as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        if item is None:
            return JSONResponse({"error": f"no inbox item {item_id!r}"}, status_code=404)
        company = item.get("company") or ""
        role = item.get("title") or ""
        url = item.get("url") or ""
        posting = {k: item.get(k) for k in ("title", "location", "snippet", "url", "posted")}
        try:
            row = state.track_application(
                company=company,
                role=role,
                fit_score=int(item.get("score") or 0),
                status="considering",
                notes=item.get("reason") or "",
                source=url,
                posting=posting,
                insert_only=True,
            )
            wl.mark_kept(item_id)
        except (state.StoreUnreadable, wl.StoreUnreadable) as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        _emit_tracked(row.get("company", ""), row.get("role", ""), row.get("status", ""))
        return JSONResponse({"ok": True, "application": row})

    @data.post("/inbox/{item_id:path}/dismiss")
    async def _dismiss(item_id: str):
        """Drop a watch-inbox item and remember the decision so it doesn't come back."""
        try:
            wl.dismiss([item_id])
        except wl.StoreUnreadable as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"ok": True})

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
        for j in got.jobs:
            if boards.passes(
                j, roles=roles, exclude=f["exclude"], locations=f["locations"], max_age_days=f["max_age_days"]
            ):
                j["id"] = f"{j['source']}:{j['url']}"
                j["hash"] = boards.content_hash(j["title"], j.get("snippet", ""), j.get("location", ""))
                market[j["id"]] = j
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
<title>Career Coach</title>
<script>
  // Fleet-proxy-safe base: "" on the host, "/agents/<slug>" behind the proxy (ADR 0042).
  var BASE = location.pathname.split('/plugins/')[0];
  var link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = BASE + '/_ds/plugin-kit.css';
  document.head.appendChild(link);
</script>
<style>
  :root { --gap: 14px; }
  body { margin: 0; padding: 24px; background: var(--pl-color-bg, #0a0f14);
         color: var(--pl-color-fg, #e6e6e6);
         font-family: var(--pl-font-sans, ui-sans-serif, system-ui, -apple-system, sans-serif); }
  h1 { font-size: 20px; margin: 0 0 2px; color: var(--pl-color-accent, #9b87f2); }
  .sub { color: var(--pl-color-fg-muted, #9aa0aa); font-size: 13px; margin: 0 0 20px; }
  .card { background: var(--pl-color-bg-raised, #12181f); border: 1px solid var(--pl-color-border, #232b36);
          border-radius: 12px; padding: 16px 18px; margin-bottom: 16px; }
  .card > h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
               color: var(--pl-color-fg-muted, #9aa0aa); margin: 0 0 12px; font-weight: 600; }
  .empty { color: var(--pl-color-fg-muted, #9aa0aa); font-size: 14px; padding: 16px 0; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px;
          background: var(--pl-color-bg-subtle, #1b2330); color: var(--pl-color-fg-muted, #9aa0aa); }
  .fit { font-variant-numeric: tabular-nums; font-weight: 600; }
  code { background: var(--pl-color-bg-subtle, #1b2330); padding: 1px 6px; border-radius: 5px; color: var(--pl-color-accent, #9b87f2); }

  /* Inbox: new roles the watch surfaced, each a keep/dismiss decision. */
  .inbox-item { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 10px;
                padding: 12px 0; border-top: 1px solid var(--pl-color-border, #232b36); }
  .inbox-item:first-child { border-top: 0; }
  .inbox-item .title { font-weight: 600; font-size: 14px; }
  .inbox-item .co { color: var(--pl-color-fg-muted, #9aa0aa); font-size: 13px; }
  .score { font-variant-numeric: tabular-nums; font-weight: 700; font-size: 12px; padding: 2px 8px;
           border-radius: 999px; background: var(--pl-color-bg-subtle, #1b2330); color: var(--pl-color-accent, #9b87f2); }
  .reason { flex-basis: 100%; color: var(--pl-color-fg-muted, #9aa0aa); font-size: 13px; }
  .inbox-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  .plink { color: var(--pl-color-accent, #9b87f2); font-size: 12px; text-decoration: none; }
  .plink:hover { text-decoration: underline; }
  button.btn { font: inherit; font-size: 13px; cursor: pointer; padding: 5px 12px; border-radius: 8px;
               border: 1px solid var(--pl-color-border, #232b36); background: var(--pl-color-bg-subtle, #1b2330);
               color: var(--pl-color-fg, #e6e6e6); }
  button.btn.keep { border-color: var(--pl-color-accent, #9b87f2); color: var(--pl-color-accent, #9b87f2); }
  button.btn:disabled { opacity: .5; cursor: default; }

  /* Pipeline board: six columns, cards drag between them (or use the per-card Move-to select). */
  .board { display: grid; grid-template-columns: repeat(6, minmax(150px, 1fr)); gap: var(--gap);
           align-items: start; overflow-x: auto; }
  @media (max-width: 860px) { .board { grid-template-columns: repeat(2, minmax(150px, 1fr)); } }
  .col { background: var(--pl-color-bg-subtle, #1b2330); border: 1px solid var(--pl-color-border, #232b36);
         border-radius: 10px; padding: 8px; min-height: 60px; }
  .col.over { border-color: var(--pl-color-accent, #9b87f2); border-style: dashed; }
  .col h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; margin: 2px 4px 8px;
            color: var(--pl-color-fg-muted, #9aa0aa); font-weight: 600; display: flex; justify-content: space-between; }
  .col h3 .count { background: var(--pl-color-bg-raised, #12181f); border-radius: 999px; padding: 0 7px; }
  .pcard { background: var(--pl-color-bg-raised, #12181f); border: 1px solid var(--pl-color-border, #232b36);
           border-radius: 8px; padding: 9px 10px; margin-bottom: 8px; cursor: grab; }
  .pcard:last-child { margin-bottom: 0; }
  .pcard .prole { font-weight: 600; font-size: 13px; }
  .pcard .pco { color: var(--pl-color-fg-muted, #9aa0aa); font-size: 12px; }
  .pcard .pmeta { display: flex; gap: 8px; align-items: baseline; font-size: 12px;
                  color: var(--pl-color-fg-muted, #9aa0aa); margin-top: 5px; }
  .pcard .nudge { margin-top: 6px; font-size: 12px; color: var(--pl-color-accent, #9b87f2);
                  border: 1px solid var(--pl-color-accent, #9b87f2); border-radius: 6px; padding: 4px 7px; }
  .pcard .movesel { margin-top: 7px; width: 100%; font: inherit; font-size: 12px; padding: 4px;
                    border-radius: 6px; border: 1px solid var(--pl-color-border, #232b36);
                    background: var(--pl-color-bg-subtle, #1b2330); color: var(--pl-color-fg, #e6e6e6); }

  /* The secondary disclosures: the profile the coach holds, and the fit rubric. Compact, below the board. */
  details.disc { margin-top: 12px; }
  details.disc > summary { cursor: pointer; color: var(--pl-color-fg-muted, #9aa0aa); font-size: 13px;
                           padding: 6px 0; font-weight: 600; }
  .row { display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 13px; }
  .row .name { width: 92px; color: var(--pl-color-fg-muted, #9aa0aa); text-transform: capitalize; }
  .track { flex: 1; height: 6px; border-radius: 4px; background: var(--pl-color-border, #232b36); overflow: hidden; }
  .bar { height: 6px; border-radius: 4px; background: var(--pl-color-accent, #9b87f2); }
  #profile .row { align-items: baseline; gap: 12px; }
  #profile .row .name { width: 190px; flex: none; text-transform: none; }
  #profile .row .empty { padding: 0; font-size: 13px; font-style: italic; }
  #profile-ctx { margin: 10px 0 0; padding: 12px; border-radius: 8px; overflow-x: auto;
                 background: var(--pl-color-bg-subtle, #1b2330); color: var(--pl-color-fg-muted, #9aa0aa);
                 font-size: 12px; line-height: 1.5; white-space: pre-wrap; }
  .coach { margin-top: 18px; padding: 14px 16px; border-radius: 12px;
           border: 1px dashed var(--pl-color-border, #232b36); color: var(--pl-color-fg-muted, #9aa0aa);
           font-size: 13px; line-height: 1.6; }

  /* The inline move-failure toast and the rejected/passed chooser — no browser modals (sandboxed iframe). */
  #toast { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); z-index: 20;
           background: var(--pl-color-bg-raised, #12181f); border: 1px solid var(--pl-color-accent, #9b87f2);
           color: var(--pl-color-fg, #e6e6e6); padding: 10px 16px; border-radius: 10px; font-size: 13px;
           opacity: 0; pointer-events: none; transition: opacity .15s; max-width: 80%; }
  #toast.show { opacity: 1; }
  #chooser { position: fixed; inset: 0; z-index: 30; display: none; align-items: center; justify-content: center;
             background: rgba(0,0,0,.5); }
  #chooser .box { background: var(--pl-color-bg-raised, #12181f); border: 1px solid var(--pl-color-border, #232b36);
                  border-radius: 12px; padding: 20px; max-width: 320px; }
  #chooser p { margin: 0 0 14px; font-size: 14px; }
  #chooser .choices { display: flex; gap: 8px; flex-wrap: wrap; }
</style></head>
<body>
  <h1 id="title">Career Coach</h1>
  <p class="sub" id="sub">Loading your jobs…</p>

  <div class="card">
    <h2>New roles from your watch</h2>
    <div id="inbox"><p class="empty">Loading…</p></div>
  </div>

  <div class="card">
    <h2>Pipeline</h2>
    <div class="board" id="board"><p class="empty">Loading…</p></div>
  </div>

  <div class="card">
    <details class="disc">
      <summary>What your coach knows about you</summary>
      <p class="sub" id="profile-cov" style="margin-top:10px">Loading…</p>
      <p class="sub">A read-only view. To change anything, tell your coach in chat.</p>
      <div id="profile"></div>
      <details style="margin-top:12px">
        <summary style="cursor:pointer;color:var(--pl-color-fg-muted,#9aa0aa)">
          Exactly what it&rsquo;s told each turn
        </summary>
        <pre id="profile-ctx"></pre>
      </details>
    </details>
    <details class="disc">
      <summary>Fit rubric — how a posting is scored</summary>
      <div id="weights" style="margin-top:10px"></div>
    </details>
  </div>

  <div class="coach">
    This is your coach, not just an auto-applier. Ask it to <b>run a mock interview</b>,
    <b>critique your CV</b>, <b>weigh an offer</b>, or <b>rehearse a salary negotiation</b> —
    or paste a posting and say <code>/apply</code> to have it evaluate fit and draft a tailored
    CV + cover letter. Tune how strictly it scores with <code>careercoach_preset growth-first</code>.
  </div>

  <div id="toast" role="status" aria-live="polite"></div>
  <div id="chooser" role="dialog" aria-modal="true">
    <div class="box">
      <p id="chooser-q">Close this role as rejected, or passed on?</p>
      <div class="choices">
        <button class="btn keep" id="choose-passed">Mark passed (default)</button>
        <button class="btn" id="choose-rejected">Mark rejected</button>
        <button class="btn" id="choose-cancel">Cancel</button>
      </div>
    </div>
  </div>

<script type="module">
  // The DS plugin-kit owns the protoagent:init handshake — bearer + LIVE theme: it stamps
  // data-theme on :root so the --pl-* tokens track the console's light/dark theme (and re-theme
  // live). Import it and let it drive; fall back to a plain fetch if it can't load (older host /
  // offline) so the page still renders — just without theme sync.
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

  var JOBS = { inbox: [], applications: [], columns: {}, followup_days: 0 };
  var DRAG_ID = null;

  function toast(msg) {
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.classList.remove('show'); }, 4000);
  }

  function findApp(id) {
    var list = JOBS.applications || [];
    for (var i = 0; i < list.length; i++) { if (list[i].id === id) return list[i]; }
    return null;
  }
  function statusToColumn() {
    var m = {}, cols = JOBS.columns || {};
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
      function done(v) {
        ov.style.display = 'none';
        passed.onclick = rejected.onclick = cancel.onclick = null;
        resolve(v);
      }
      passed.onclick = function () { done('passed'); };
      rejected.onclick = function () { done('rejected'); };
      cancel.onclick = function () { done(null); };
      ov.style.display = 'flex';
      passed.focus();
    });
  }

  // Optimistic move: flip the card's status and re-render now; on a non-2xx (or network error)
  // roll the status back, re-render, and surface an inline toast. On success, reload for the
  // authoritative row (new date, history, recomputed follow-up).
  function moveApp(id, status) {
    var app = findApp(id);
    if (!app || app.status === status) return;
    var prev = app.status;
    app.status = status;
    renderBoard();
    write('/applications/' + encodeURIComponent(id) + '/move', { status: status })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        load();
      })
      .catch(function () {
        app.status = prev;
        renderBoard();
        toast('Could not move that role — left it where it was.');
      });
  }
  function moveToColumn(id, col) {
    var app = findApp(id);
    if (!app) return;
    var statuses = (JOBS.columns || {})[col] || [];
    if (!statuses.length || statuses.indexOf(app.status) !== -1) return;  // already there / unknown
    if (isClosedColumn(statuses)) {
      askClosedStatus().then(function (s) { if (s) moveApp(id, s); });
    } else {
      moveApp(id, statuses[0]);
    }
  }

  function inboxAction(id, action, btns) {
    btns.forEach(function (b) { b.disabled = true; });
    write('/inbox/' + encodeURIComponent(id) + '/' + action, null)
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        load();
      })
      .catch(function () {
        btns.forEach(function (b) { b.disabled = false; });
        toast('Could not ' + action + ' that role — try again.');
      });
  }

  function renderInbox() {
    var box = document.getElementById('inbox');
    var items = JOBS.inbox || [];
    if (!items.length) {
      box.innerHTML = '<p class="empty">Nothing new — your watch hasn\\'t surfaced any roles to review.</p>';
      return;
    }
    box.innerHTML = items.map(function (it) {
      var score = Number(it.score || 0);
      var link = it.url
        ? '<a class="plink" href="' + esc(it.url) + '" target="_blank" rel="noopener">View posting</a>'
        : '';
      var reason = it.reason ? '<div class="reason">' + esc(it.reason) + '</div>' : '';
      return '<div class="inbox-item" data-id="' + esc(it.id) + '">' +
        '<span class="title">' + esc(it.title) + '</span>' +
        '<span class="co">' + esc(it.company) + '</span>' +
        '<span class="score">' + score + '</span>' +
        '<span class="inbox-actions">' + link +
          '<button class="btn keep" data-keep>Keep</button>' +
          '<button class="btn" data-dismiss>Dismiss</button>' +
        '</span>' + reason +
      '</div>';
    }).join('');
    box.querySelectorAll('.inbox-item').forEach(function (el) {
      var id = el.getAttribute('data-id');
      var keep = el.querySelector('[data-keep]');
      var dismiss = el.querySelector('[data-dismiss]');
      var btns = [keep, dismiss];
      keep.addEventListener('click', function () { inboxAction(id, 'keep', btns); });
      dismiss.addEventListener('click', function () { inboxAction(id, 'dismiss', btns); });
    });
  }

  function renderBoard() {
    var board = document.getElementById('board');
    var cols = JOBS.columns || {};
    var colNames = Object.keys(cols);
    if (!colNames.length) { board.innerHTML = '<p class="empty">No pipeline columns.</p>'; return; }
    var reverse = statusToColumn();
    var apps = JOBS.applications || [];
    var days = JOBS.followup_days || 0;
    var buckets = {};
    colNames.forEach(function (c) { buckets[c] = []; });
    apps.forEach(function (a) {
      var col = reverse[a.status];
      if (col && buckets[col]) buckets[col].push(a);
    });

    var selOpts = '<option value="">Move to…</option>' +
      colNames.map(function (c) { return '<option value="' + esc(c) + '">' + esc(c) + '</option>'; }).join('');

    board.innerHTML = colNames.map(function (col) {
      var cards = buckets[col].map(function (a) {
        var n = daysSince(lastMoveDate(a));
        var age = n == null ? '' : '<span>' + n + 'd</span>';
        // The follow-up nudge renders ONLY for an applied card the server flagged `followup`.
        var nudge = a.followup
          ? '<div class="nudge">No reply in ' + (n == null ? days : n) + ' days — follow up?</div>'
          : '';
        return '<div class="pcard" draggable="true" data-id="' + esc(a.id) + '">' +
          '<div class="prole">' + esc(a.role) + '</div>' +
          '<div class="pco">' + esc(a.company) + '</div>' +
          '<div class="pmeta"><span class="fit">' + Number(a.fit_score || 0) + '</span>' + age + '</div>' +
          nudge +
          '<select class="movesel" aria-label="Move ' + esc(a.role) + ' to another column">' + selOpts + '</select>' +
        '</div>';
      }).join('') || '<p class="empty" style="padding:6px 4px;font-size:12px">—</p>';
      return '<div class="col" data-col="' + esc(col) + '">' +
        '<h3>' + esc(col) + '<span class="count">' + buckets[col].length + '</span></h3>' +
        cards +
      '</div>';
    }).join('');

    board.querySelectorAll('.pcard').forEach(function (card) {
      card.addEventListener('dragstart', function (ev) {
        DRAG_ID = card.getAttribute('data-id');
        try { ev.dataTransfer.setData('text/plain', DRAG_ID); ev.dataTransfer.effectAllowed = 'move'; } catch (e) {}
      });
      card.addEventListener('dragend', function () { DRAG_ID = null; });
    });
    board.querySelectorAll('.col').forEach(function (colEl) {
      colEl.addEventListener('dragover', function (ev) {
        ev.preventDefault();
        if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
        colEl.classList.add('over');
      });
      colEl.addEventListener('dragleave', function () { colEl.classList.remove('over'); });
      colEl.addEventListener('drop', function (ev) {
        ev.preventDefault();
        colEl.classList.remove('over');
        var id = DRAG_ID || (ev.dataTransfer && ev.dataTransfer.getData('text/plain'));
        if (id) moveToColumn(id, colEl.getAttribute('data-col'));
      });
    });
    board.querySelectorAll('.movesel').forEach(function (sel) {
      var card = sel.closest('.pcard');
      sel.addEventListener('change', function () {
        var col = sel.value;
        sel.value = '';
        if (col) moveToColumn(card.getAttribute('data-id'), col);
      });
    });
  }

  // The transparency panel: every field the coach holds, and every one it doesn't. The
  // operator should never have to open a file to see their own record.
  function renderProfile(d) {
    if (!d) {
      document.getElementById('profile-cov').textContent = 'Could not load your profile.';
      return;
    }
    var prof = d.profile || { identity: {}, sections: {} };
    var cov = d.completeness || { known: [], missing: [], filled: 0, total: 0 };
    var labels = d.field_labels || {};

    document.getElementById('profile-cov').textContent = d.profile_error
      ? ('Your profile file is unreadable (' + d.profile_error + '). Nothing new will be saved '
         + 'until it is fixed — ask your coach where it is.')
      : cov.filled
      ? (cov.filled + ' of ' + cov.total + ' recorded'
         + (cov.updated ? ' · updated ' + cov.updated : ''))
      : 'Nothing recorded yet — run /setup-coach in chat and it will build this with you.';

    var rows = Object.keys(labels).map(function (k) {
      var v = (prof.identity && prof.identity[k]) || (prof.sections && prof.sections[k]) || '';
      var body = v
        ? '<span>' + esc(v.length > 220 ? v.slice(0, 220) + '…' : v) + '</span>'
        : '<span class="empty">not recorded</span>';
      return '<div class="row"><span class="name">' + esc(labels[k]) + '</span>' + body + '</div>';
    });
    document.getElementById('profile').innerHTML = rows.join('');
    document.getElementById('profile-ctx').textContent =
      d.context_block || '(nothing injected — no profile recorded yet)';
  }

  function renderWeights(d) {
    var w = (d && d.weights) || {};
    document.getElementById('weights').innerHTML = Object.keys(w).map(function (k) {
      return '<div class="row"><span class="name">' + esc(k) + '</span>' +
        '<span class="track"><span class="bar" style="width:' + Number(w[k] || 0) + '%"></span></span>' +
        '<span class="fit">' + Number(w[k] || 0) + '</span></div>';
    }).join('');
  }

  function renderHeader(state) {
    var st = (state && state.settings) || {};
    var prof = (state && state.profile) || { identity: {} };
    var name = (prof.identity && prof.identity.name) || st.name || '';
    document.getElementById('title').textContent = name ? ('Career Coach — ' + name) : 'Career Coach';
    document.getElementById('sub').textContent = st.target_roles
      ? ('Targeting: ' + st.target_roles) : 'Set your profile in Settings › Career Coach.';
  }

  function load() {
    // /jobs drives the inbox + board; /state is the profile/rubric disclosure and the header. A
    // failed /state still renders the jobs view.
    return api('/jobs').then(function (jobs) {
      JOBS = jobs || { inbox: [], applications: [], columns: {}, followup_days: 0 };
      renderInbox();
      renderBoard();
      return api('/state').then(function (s) { return s; }, function () { return null; });
    }).then(function (state) {
      renderHeader(state);
      renderProfile(state);
      renderWeights(state);
    }).catch(function () {
      document.getElementById('sub').textContent = 'Could not load your jobs.';
    });
  }

  // Live refresh (ADR 0039): ask the console to forward `careercoach.#` events into this iframe,
  // and reload whenever one arrives. We also reload after each successful write (above).
  function subscribe() {
    try { parent.postMessage({ type: 'protoagent:subscribe', patterns: ['careercoach.#'] }, '*'); }
    catch (e) {}
  }
  window.addEventListener('message', function (e) {
    var d = e && e.data;
    if (d && d.type === 'protoagent:event') load();
  });

  // initPluginView runs the console handshake (ADR 0026) — applies the theme and captures the
  // bearer — then fires our callback once ready. The timer is a fallback for a host context that
  // never posts an init, so the page isn't left blank.
  kit.initPluginView(function () { subscribe(); load(); });
  setTimeout(function () { subscribe(); load(); }, 800);
</script>
</body></html>"""
