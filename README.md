# Career Coach — a protoAgent plugin

Your **career coach and job-hunt copilot**, built as a drop-in [protoAgent](https://github.com/protoLabsAI/protoAgent)
plugin. It coaches — strategy and positioning, mock interviews with honest feedback, a straight
critique of your CV, weighing an offer, rehearsing a salary negotiation — and, when you want it
done for you, it executes: evaluate a posting, tailor the CV + cover letter, prep the interview.

> It's a **coach, not an auto-applier.** The autonomous "evaluate → tailor → draft" pipeline is
> one mode. The point is to make *you* clearer and more prepared, not to spray applications.

It's also a **deliberate deep-dive reference for the plugin system** — one repo that exercises the
whole contribution surface, so it doubles as a worked example of how far a plugin can go without
forking core.

---

## What it does

**Setup (once):**
- **`/setup-coach`** — works out what it already knows about you, asks only about the real gaps, and records
  the result as a profile it carries into every future turn. It harvests first (memory, saved artifacts, any
  CV in your workspace, a resume PDF you've shared) so it never asks for what it already has.
- **See what it knows.** The **Career Coach** console panel lists every field the coach holds, every one
  still missing, and — behind a disclosure — the exact block it's told each turn. Your record, visible to you,
  not a file you have to go find. The panel is read-only: to change anything, tell the coach in chat.

**Coaching (the default):**
- **Career strategy** — positioning, what roles to target, whether to take a job, comparing offers, salary negotiation (skill: `career-strategy`).
- **Interview practice** — a STAR answer bank and realistic **mock interviews with per-answer feedback** (skill: `interview-coach`).
- **Honest material feedback** — a real critique of your CV/cover letter against an anti-slop, anti-fabrication standard (skill: `job-application-assistant` + `writing-style`).
- **Upskilling** — turns the jobs you're chasing into a prioritized gap heatmap + a learning plan with real resources (skill: `upskill`).

**Execution (when you ask):**
- **Find live jobs** — `careercoach_search_jobs` queries a real job source: JSearch (Google-for-Jobs) with an API key, or the keyless **Remotive** remote-jobs board out of the box.
- **Evaluate fit** against a weighted, tunable rubric, with sourced company research.
- **Tailor a CV + cover letter** — reframing emphasis, never fabricating (the interview-backtrack test).
- Run the whole thing with the **`apply` workflow**: `run_workflow("apply", {"posting": "<url or text>"})`.
- **Work up a full, filed role packet** — the gated **`role-packet`** flow files a folder per role
  (`Companies/<Co>/Roles/<Role - Req>/`) and produces each artifact — recruiter brief, evidence map,
  tailored resume, ATS skills list, cover letter, assembled packet — **one human-approved step at a
  time**, anchored to your operator profile. Say *"build the role packet."*

**In the background (opt-in):** turn on the **job-watch** and it periodically searches your target
roles, surfaces new matching postings on the dashboard, and lights the rail icon — or arm a **WATCH**
on your pipeline yourself via the `careercoach:new_matches` verifier.

Track it all on the **Career Coach dashboard** (a console rail view): your pipeline, fit scores, the rubric.

---

## The plugin system, exercised (the deep dive)

Every protoAgent extension surface, in one plugin:

| Surface | Where | What it shows |
|---------|-------|---------------|
| **SKILL.md skills** (progressive disclosure) | `skills/` (auto-loaded) | 6 skills; `job-application-assistant` + `role-packet` use **sub-files** (`writing-style.md`, `evidence-map.md`, `ats-skills-entry.md`, …) read on demand |
| **User-facing slash skill** | `skills/setup-coach/` (`user_facing` + `slash`) | `/setup-coach` — the first-run interview that grounds every other skill; files are the truth, memory is a derived recall index |
| **Gated, filed pipeline** (skill-driven) | `skills/role-packet/` + `packet.py` + `templates/` | the resume flow: a **human-approved gate before every phase**, artifacts filed to `Companies/<Co>/Roles/…` via tested scaffolding tools, seeded from fill-in templates |
| **Static-DAG workflow** (ADR 0002) | `workflows/apply.yaml` (auto-loaded) | `research → evaluate → write` chained via `depends_on` + `{{steps.*.output}}` (the *autonomous* counterpart to the gated `role-packet` flow) |
| **Subagent crew** | `register_subagent` in `__init__.py` | 3 purpose-built delegates (`company_researcher`, `job_evaluator`, `application_writer`) the workflow chains |
| **Agent tools** | `register_tools` | `careercoach_track_application`, `careercoach_list_applications`, `careercoach_search_jobs` (live search), `careercoach_get_profile` / `careercoach_update_profile` / `careercoach_import_experience` / `careercoach_export_experience` (the operator profile) |
| **Plugin middleware** (ADR 0032) | `register_middleware` | the `<operator_profile>` block — always-on operator context + completeness + the voice gate, delivered on every model call as an ephemeral context frame (`wrap_model_call`, the host's ADR 0108 D2 contract) |
| **Tunable Knobs** (`graph.sdk`) | `register_tools(make_knob_tools(...))` | the fit rubric's four weights as live knobs + presets (`careercoach_preset growth-first`) |
| **Background surface + watchdog** (ADR 0018) | `register_surface` + `graph.sdk.supervise` | the opt-in job-watch — a supervised loop that scans, records new matches, and emits an event |
| **Goal verifier** (ADR 0028/0067) | `register_goal_verifier` | `careercoach:new_matches` — arm a **WATCH** on your pipeline with `create_watch` |
| **Console rail view** (ADR 0026) | `register_router` + manifest `views:` | the dashboard — public page + a **gated** `/api/plugins/careercoach/state`, fleet-proxy-safe fetch |
| **Config / secrets / Settings** (ADR 0019) | manifest + `registry.config` | profile + `render_format` + the job-watch knobs in Settings; `jobs_api_key` → `secrets.yaml` |
| **Event bus** (ADR 0039) | `registry.emit` | `careercoach.application_tracked` / `careercoach.new_matches` light the rail icon |
| **Consumption SDK** (ADR 0043) | lazy `graph.sdk` imports | `Knobs` / `make_knob_tools` / `supervise`, kept lazy + guarded so it loads and tests host-free |

Read `__init__.py` top to bottom — it's commented as a tour. The pure logic (`rubric.py`, `state.py`)
has **no host imports**, so it's unit-tested directly; the host-touching paths are guarded so the
plugin loads and its suite runs with **no protoAgent present**.

### Layout
```
careercoach-plugin/
├─ protoagent.plugin.yaml     # manifest: config/secrets/settings, views, emits, provenance
├─ SOUL.md                    # recommended agent persona — copy into your agent's SOUL.md
├─ __init__.py                # register(): tools + knobs + crew + dashboard + the job-watch
├─ rubric.py                  # the weighted fit rubric — pure, tested
├─ state.py                   # the application tracker — instance-scoped JSON, tested
├─ profile.py                 # the operator profile + the shared store (instance root, lock, atomic write)
├─ jobsource.py               # live job search (JSearch / Remotive) — parsers + prescore, tested
├─ watch.py                   # the background-watch matcher — pure, tested
├─ packet.py                  # the role-packet workspace (folder-per-role scaffold/assemble) — pure, tested
├─ skills/
│  ├─ job-application-assistant/   # router SKILL.md + writing-style / job-evaluation / cv / cover-letter
│  ├─ role-packet/                 # the gated pipeline: SKILL.md + evidence-map / ats-skills-entry / recruiter-brief / qa-review
│  ├─ interview-coach/             # STAR bank + mock interviews with feedback
│  ├─ career-strategy/             # positioning, offers, negotiation, decisions (the coach)
│  └─ upskill/                     # gap heatmap + learning plan
├─ templates/                  # fill-in workspace starters (an importable Experience.md, story bank, reviewer, Humanize, improvements)
├─ workflows/apply.yaml        # the autonomous research → evaluate → write pipeline
└─ tests/                      # host-free (vendored testkit): rubric, state, packet, register() surface
```

---

## Quick start

1. **Install it.** In a running protoAgent: **Console → Plugins → Discover → _Career Coach_ → Install**. Or from the shell:
   ```sh
   python -m server plugin install https://github.com/protoLabsAI/careercoach-plugin
   ```
2. **Enable it.** Toggle it on in **Console → Plugins**, or ask the agent to `enable_plugin("careercoach")`.
   (Install ≠ enable ≠ trust — enabling is the trust decision.)
3. **Set your profile.** **Settings → Career Coach**: your name, location, and target roles. That's all it needs to start.
4. **Give it the coach's persona _(recommended)_.** The plugin ships a recommended persona in
   [`SOUL.md`](./SOUL.md). Enabling a plugin never touches your agent's identity — by design
   (see [#1771](https://github.com/protoLabsAI/protoAgent/issues/1771)) — so adopt it explicitly:
   paste it into **Settings → Identity** (or your agent's `config/SOUL.md`) and rename the identity
   line to your agent's name. Without it you still have the coaching *tools*; with it, the agent *is*
   a coach.
5. **Run `/setup-coach`.** The first-run interview: it builds your operator profile (importing an
   `Experience.md` you already keep, rather than re-asking), fills your story bank, captures your writing
   voice, and distills the lot into memory the coach recalls later. Everything downstream is anchored to what you say here, so this is
   the difference between a coach that knows you and one that guesses. Every step is skippable, and you can
   stop and resume any time.
6. **Talk to your agent.** A few things to try:
   - **Coach me** — "Help me think about what roles to target." · "Run a mock interview for the Acme ML role." · "Critique my CV for this posting." · "I got the offer — help me negotiate the salary."
   - **Find & apply** — "Find remote ML engineer jobs." · "Here's a posting, is it worth applying to?" (paste a URL or the text) · `run_workflow("apply", {"posting": "<url>"})`
   - **Work up a full packet** — "Build the role packet for this posting." The coach files a folder per role and produces each artifact one approved step at a time (see the workspace note below).

### Optional
- **Choose where your workspace lives.** `/setup-coach` seeds it for you, but you can point it somewhere
  specific first via **Settings → Career Coach → Role-packet workspace** (blank defaults to `~/CareerCoach`).
  It lays down fill-in starters — `Resume/Experience.md`, `Agent/story-bank.md`, reviewer + Humanize
  rules, an improvements log — and never clobbers your edits. These are plain markdown you can open and edit
  directly at any time. If you'd rather write your history into `Experience.md` than be interviewed, do
  that and ask the coach to import it: it previews what it will add to your profile and records it once you
  confirm. The coach never writes over that file.
- **Native Word (`.docx`) export.** Set **Settings → Career Coach → Document format = `docx`** and the CV +
  cover letter are produced as real, editable **Word files** (saved as versioned, downloadable artifacts)
  instead of HTML→PDF. **This path builds the document by running Python (`python-docx`), so it requires:**
  the [cowork](https://github.com/protoLabsAI/cowork-plugin) plugin (its `docx` skill), the **`execute_code`**
  plugin **enabled** — ⚠️ **`execute_code` lets the agent run arbitrary code; enabling it is a deliberate
  trust decision** — the `artifact` plugin, and a protoAgent **v0.108.0+** host. If any is missing, the
  coach **falls back to HTML→PDF and tells you what's absent**. Leave `render_format` on `html` if you'd
  rather not enable code execution.
  > **On the desktop app, v0.108.0 is the floor — not v0.107.0.** 0.107.0 bundled the document libraries,
  > but `execute_code` was hard-disabled on the packaged app (no standalone interpreter to spawn), so
  > `.docx` generation could not run there at all. protoAgent **0.108.0** ships the **managed Python
  > runtime** (ADR 0094): the first `execute_code` call provisions a pinned CPython plus the document
  > libraries — **one consented download, once per machine** — after which the docx path works on desktop.
  > On a server / Docker install there's nothing to provision.
- **Sharper job search.** Add a [RapidAPI **JSearch**](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) key
  under **Settings → Career Coach → Job-source API key** for Google-for-Jobs breadth. Without a key, search uses
  the keyless **Remotive** remote-jobs board — so it works out of the box.
- **Background job-watch.** Turn on **Background job-watch** in Settings and it periodically surfaces new roles
  matching your profile on the dashboard (and lights the rail icon). Off by default.
- **Tune the fit rubric.** `careercoach_preset growth-first` (or `careercoach_tune weight_career 45`) reweights
  scoring live.

---

## Design decisions

- **HTML → PDF, not LaTeX.** The upstream project's biggest tax is LaTeX page-break firefighting.
  We render via the **artifact plugin** (HTML → PDF) by default — the agent can *see* the rendered
  result — and keep `.tex`/moderncv as an option (with the upstream gotchas preserved, credited).
- **Native `.docx` too (`render_format: docx`).** Set it and the CV + cover letter are produced as
  real, editable **Word files** via [cowork](https://github.com/protoLabsAI/cowork-plugin)'s `docx`
  skill, then saved with `save_file_artifact` as **versioned, downloadable** artifacts (ADR 0092) — what
  most ATS forms actually want. It runs `python-docx`, so it needs cowork + the **`execute_code`** plugin
  enabled (which runs code — a trust decision) + a **v0.108.0+** host; falls back to HTML→PDF (and says
  what's missing) otherwise. A soft pairing: no hard dependency, `html` stays default. On desktop, 0.108.0
  is the floor because that's where `execute_code` gained a **managed Python runtime** (ADR 0094) —
  provisioned on first use; before it, code execution was unavailable on the packaged app entirely.
- **The operator profile is state, not a document — and it's always in front of the model.** Who the coach
  works for lives in a structured `profile.json` (`profile.py`) in the plugin's per-instance store —
  `<instance_root>/careercoach/`, via the host's `graph.sdk.plugin_store`, beside the tracker's
  `applications.json` — so the dev sandbox, each fleet member and a container's volume keep their own.
  (v0.6 used `~/.protoagent/careercoach/<instance>/`; a store found there is copied forward once and the
  old folder gets a `MIGRATED-TO` note. The old files stay put for a rollback, but they're never adopted
  again, so wiping the new store really does start over.) It reaches the model every call as an
  `<operator_profile>` block
  via plugin middleware (ADR 0032), carrying an explicit **completeness** picture: what's known, what's
  missing, how much of the picture exists. That's what stops the coach re-interviewing for facts it already
  holds — a real first run was abandoned partway through for exactly that reason, because a recall tool
  can't help an agent that doesn't already suspect there's something to recall.
- **One rule for the career record: the profile is the single source of truth.** Everything that reads
  your history — the always-on block, `careercoach_read_profile("experience")`, the drafting and scoring
  crew — reads the profile, and nothing reads `Resume/Experience.md` as a source. That file takes part in
  two explicit, one-way moves: **import** (`careercoach_import_experience` parses what you wrote, skipping
  the template's hint text, previews it, and records it through the same append/replace and `do_not_claim`
  rules as the agent once you confirm) and **export** (`careercoach_export_experience` writes a snapshot to
  `Resume/Experience (profile export).md`). The coach never writes `Experience.md` itself.
- **The block is a per-call frame, never state.** It's appended to the request with `wrap_model_call` —
  the host's own contract for derived context (ADR 0108 D2) — so it's never checkpointed and there's exactly
  one copy per call. The system prompt is untouched and the frame sits after the host's cache breakpoints,
  so it can't cost prompt caching. (v0.6 wrote it to a `context` state channel that protoAgent v0.155.0
  removed, so it silently stopped arriving — the test now drives a real `create_agent` and asserts on what
  the model receives.) The subagents the host builds without plugin middleware — `job_evaluator` and
  `application_writer` — read the profile through the profile tools instead.
- **The profile can't be lost to one bad write.** Writes are serialized (a thread lock everywhere, plus
  `flock` across processes on POSIX) and atomic, and each one first saves the version it replaces to
  `profile.json.bak`, so the last change can always be undone. A write that finds the file unreadable
  refuses instead of saving one field over everything else, and the agent and panel say why (an empty file
  is just an empty profile). Sections append by default, so recording roles one at a time keeps them all;
  a correction rewrites the section with `mode="replace"`.
- **Transparency is the differentiator.** The console panel shows every field held, every field missing, and
  the verbatim block the agent receives. The person being described should never have to open a file in
  Finder to see their own record.
- **The voice gate is always-on, not a step in one skill.** The writing discipline used to live only inside
  `job-application-assistant`, so a side-door request ("just convert my resume to a docx") produced a real
  deliverable at 16 em-dashes per 1000 words against the operator's stated limit of 3, carrying a phrase
  they'd explicitly retired. A rule that only binds when you enter through the front door isn't a rule, so
  it now rides in the always-on block alongside the `do_not_claim` guardrails.
- **The agent can't rewrite its own guardrails.** `careercoach_write_profile` accepts `experience` and
  `story-bank` — the candidate's own files. The discipline files it's *bound by* (experience-reviewer,
  Humanize, the improvements log) are read-only to the agent by design, and the profile's `do_not_claim`
  hard stops can be added to freely but not dropped or reworded without `confirm_removal=true` — the
  operator's explicit say-so. Profile values are defanged before injection, so recorded text can't close
  the `<operator_profile>` block and speak as it.
- **"Seeded" is not "filled in."** `read_source` compares the workspace copy against the shipped template
  instead of sniffing for placeholder syntax — the templates are full of realistic-looking hint text, so
  any "looks empty" heuristic gets it wrong. An untouched template reports as unfilled and the coach
  refuses to draft from it, which is the whole anti-fabrication contract holding.
- **A coach, not an autopilot.** `career-strategy` + `interview-coach` are human-in-the-loop by
  design; the `apply` workflow is the opt-in "do it for me" path.
- **Gated vs. autonomous, on purpose.** The full application exists in two shapes: the `apply`
  *workflow* runs research → evaluate → write autonomously (fast), while the `role-packet` *skill*
  drives the same work with a **human-approval gate before every phase** and files the result as
  editable artifacts. A static-DAG workflow can't pause between steps, so the gated flow is a
  skill (which stops and asks) plus tested scaffolding tools (`packet.py`) for the file mechanics —
  the artifacts are anchored to the operator profile so nothing unfounded reaches a resume.
- **Two control surfaces, on purpose.** The candidate *profile* is operator config (Settings, ADR 0019);
  the rubric *weights* are agent-tunable **Knobs** — because "score these more on growth than raw
  skills" is a live retune, not a settings edit.
- **Honesty is enforced, not hoped for.** The writing-style discipline's interview-backtrack test
  and "verify every company claim against a source" rule are load-bearing — the candidate has to
  defend every line in an interview.
- **Your voice, if you've taught it.** When the [cowork](https://github.com/protoLabsAI/cowork-plugin)
  `writing-voice` skill has saved a `my-writing-style` profile, the coach layers it over the writing
  discipline so drafts sound like *you* — detected at runtime via `list_skills`, ignored if absent
  (the honesty rules still win on any conflict). A soft pairing: no dependency, works standalone.
- **The job source is provider-abstracted + keyless by default.** Live search works out of the box via
  Remotive (remote jobs, no key); add a JSearch/RapidAPI key for Google-for-Jobs breadth. Only
  `jobsource.py` makes outbound calls, and the manifest declares exactly those two hosts.

## Found while building (filed upstream)

Building this surfaced protoAgent SDK/DX feedback, filed as issues on the host repo:

- **DX papercut** — a scaffolded standalone plugin that registers a subagent fails its own host-free
  smoke test, because `graph.subagents.config.SubagentConfig` isn't in the testkit's default host
  stubs (the import raises before `register()` can wire it). We guard for it here (`_register_subagents`),
  but the scaffolder encourages subagents, so the default stubs should include a permissive
  `SubagentConfig` (and `Knobs` / `make_knob_tools`). *(Filed as [protoAgent #1764](https://github.com/protoLabsAI/protoAgent/issues/1764),
  since fixed: the testkit ships record-only stand-ins for both, and this repo's vendored copy has them.)*
- **Missing seam** — this plugin ships a recommended persona ([`SOUL.md`](./SOUL.md)), but there's no
  host mechanism to *offer* it: `register_*` has no persona hook and the manifest has no key, so adopting
  it is a manual copy. The fix has to stay opt-in / load-on-demand (like `load_skill`) and must never
  auto-clobber the user's own SOUL. *(Filed as [protoAgent #1771](https://github.com/protoLabsAI/protoAgent/issues/1771).)*

If you find more, please open an issue on protoAgent — that feedback loop is half the point of this repo.

## Credits & license

MIT (see [`LICENSE`](./LICENSE)). Prompt-engineering IP adapted with credit from
[`MadsLorentzen/ai-job-search`](https://github.com/MadsLorentzen/ai-job-search) (MIT) — details in
[`CREDITS.md`](./CREDITS.md).
