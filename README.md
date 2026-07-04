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

**In the background (opt-in):** turn on the **job-watch** and it periodically searches your target
roles, surfaces new matching postings on the dashboard, and lights the rail icon — or arm a **WATCH**
on your pipeline yourself via the `careercoach:new_matches` verifier.

Track it all on the **Career Coach dashboard** (a console rail view): your pipeline, fit scores, the rubric.

---

## The plugin system, exercised (the deep dive)

Every protoAgent extension surface, in one plugin:

| Surface | Where | What it shows |
|---------|-------|---------------|
| **SKILL.md skills** (progressive disclosure) | `skills/` (auto-loaded) | 4 skills; `job-application-assistant` uses **sub-files** (`writing-style.md`, `job-evaluation.md`, `cv-guide.md`, `cover-letter-guide.md`) read on demand |
| **Static-DAG workflow** (ADR 0002) | `workflows/apply.yaml` (auto-loaded) | `research → evaluate → write` chained via `depends_on` + `{{steps.*.output}}` |
| **Subagent crew** | `register_subagent` in `__init__.py` | 3 purpose-built delegates (`company_researcher`, `job_evaluator`, `application_writer`) the workflow chains |
| **Agent tools** | `register_tools` | `careercoach_track_application`, `careercoach_list_applications`, `careercoach_search_jobs` (live search) |
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
├─ __init__.py                # register(): tools + knobs + crew + dashboard + the job-watch
├─ rubric.py                  # the weighted fit rubric — pure, tested
├─ state.py                   # the application tracker — instance-scoped JSON, tested
├─ jobsource.py               # live job search (JSearch / Remotive) — parsers + prescore, tested
├─ watch.py                   # the background-watch matcher — pure, tested
├─ skills/
│  ├─ job-application-assistant/   # router SKILL.md + writing-style / job-evaluation / cv / cover-letter
│  ├─ interview-coach/             # STAR bank + mock interviews with feedback
│  ├─ career-strategy/             # positioning, offers, negotiation, decisions (the coach)
│  └─ upskill/                     # gap heatmap + learning plan
├─ workflows/apply.yaml        # the autonomous research → evaluate → write pipeline
└─ tests/                      # host-free (vendored testkit): rubric, state, register() surface
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
4. **Talk to your agent.** A few things to try:
   - **Coach me** — "Help me think about what roles to target." · "Run a mock interview for the Acme ML role." · "Critique my CV for this posting." · "I got the offer — help me negotiate the salary."
   - **Find & apply** — "Find remote ML engineer jobs." · "Here's a posting, is it worth applying to?" (paste a URL or the text) · `run_workflow("apply", {"posting": "<url>"})`

### Optional
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
- **A coach, not an autopilot.** `career-strategy` + `interview-coach` are human-in-the-loop by
  design; the `apply` workflow is the opt-in "do it for me" path.
- **Two control surfaces, on purpose.** The candidate *profile* is operator config (Settings, ADR 0019);
  the rubric *weights* are agent-tunable **Knobs** — because "score these more on growth than raw
  skills" is a live retune, not a settings edit.
- **Honesty is enforced, not hoped for.** The writing-style discipline's interview-backtrack test
  and "verify every company claim against a source" rule are load-bearing — the candidate has to
  defend every line in an interview.
- **The job source is provider-abstracted + keyless by default.** Live search works out of the box via
  Remotive (remote jobs, no key); add a JSearch/RapidAPI key for Google-for-Jobs breadth. Only
  `jobsource.py` makes outbound calls, and the manifest declares exactly those two hosts.

## Found while building (filed upstream)

Building this surfaced protoAgent SDK/DX feedback, filed as issues on the host repo:

- **DX papercut** — a scaffolded standalone plugin that registers a subagent fails its own host-free
  smoke test, because `graph.subagents.config.SubagentConfig` isn't in the testkit's default host
  stubs (the import raises before `register()` can wire it). We guard for it here (`_register_subagents`),
  but the scaffolder encourages subagents, so the default stubs should include a permissive
  `SubagentConfig` (and `Knobs` / `make_knob_tools`). *(Filed as [protoAgent #1764](https://github.com/protoLabsAI/protoAgent/issues/1764).)*

If you find more, please open an issue on protoAgent — that feedback loop is half the point of this repo.

## Credits & license

MIT (see [`LICENSE`](./LICENSE)). Prompt-engineering IP adapted with credit from
[`MadsLorentzen/ai-job-search`](https://github.com/MadsLorentzen/ai-job-search) (MIT) — details in
[`CREDITS.md`](./CREDITS.md).
