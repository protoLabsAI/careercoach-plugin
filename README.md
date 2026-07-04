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
- **Evaluate fit** against a weighted, tunable rubric, with sourced company research.
- **Tailor a CV + cover letter** — reframing emphasis, never fabricating (the interview-backtrack test).
- Run the whole thing with the **`apply` workflow**: `run_workflow("apply", {"posting": "<url or text>"})`.

Track it all on the **Career Coach dashboard** (a console rail view): your pipeline, fit scores, the rubric.

---

## The plugin system, exercised (the deep dive)

Every protoAgent extension surface, in one plugin:

| Surface | Where | What it shows |
|---------|-------|---------------|
| **SKILL.md skills** (progressive disclosure) | `skills/` (auto-loaded) | 4 skills; `job-application-assistant` uses **sub-files** (`writing-style.md`, `job-evaluation.md`, `cv-guide.md`, `cover-letter-guide.md`) read on demand |
| **Static-DAG workflow** (ADR 0002) | `workflows/apply.yaml` (auto-loaded) | `research → evaluate → write` chained via `depends_on` + `{{steps.*.output}}` |
| **Subagent crew** | `register_subagent` in `__init__.py` | 3 purpose-built delegates (`company_researcher`, `job_evaluator`, `application_writer`) the workflow chains |
| **Agent tools** | `register_tools` | `careercoach_track_application`, `careercoach_list_applications` |
| **Tunable Knobs** (`graph.sdk`) | `register_tools(make_knob_tools(...))` | the fit rubric's four weights as live knobs + presets (`careercoach_preset growth-first`) |
| **Console rail view** (ADR 0026) | `register_router` + manifest `views:` | the dashboard — public page + a **gated** `/api/plugins/careercoach/state`, fleet-proxy-safe fetch |
| **Config / secrets / Settings** (ADR 0019) | manifest + `registry.config` | profile basics + `render_format` in Settings; `jobs_api_key` → `secrets.yaml` |
| **Event bus** (ADR 0039) | `registry.emit` | `careercoach.application_tracked` lights the rail icon |
| **Consumption SDK** (ADR 0043) | lazy `graph.sdk` imports | `Knobs` / `make_knob_tools`, kept lazy + guarded so it loads and tests host-free |

Read `__init__.py` top to bottom — it's commented as a tour. The pure logic (`rubric.py`, `state.py`)
has **no host imports**, so it's unit-tested directly; the host-touching paths are guarded so the
plugin loads and its suite runs with **no protoAgent present**.

### Layout
```
careercoach-plugin/
├─ protoagent.plugin.yaml     # manifest: config/secrets/settings, views, emits, provenance
├─ __init__.py                # register(): tools + knobs + subagent crew + dashboard router
├─ rubric.py                  # the weighted fit rubric — pure, tested
├─ state.py                   # the application tracker — instance-scoped JSON, tested
├─ skills/
│  ├─ job-application-assistant/   # router SKILL.md + writing-style / job-evaluation / cv / cover-letter
│  ├─ interview-coach/             # STAR bank + mock interviews with feedback
│  ├─ career-strategy/             # positioning, offers, negotiation, decisions (the coach)
│  └─ upskill/                     # gap heatmap + learning plan
├─ workflows/apply.yaml        # the autonomous research → evaluate → write pipeline
└─ tests/                      # host-free (vendored testkit): rubric, state, register() surface
```

---

## Install & use

```sh
# From a running protoAgent (or: python -m server plugin install <git-url>)
python -m server plugin install https://github.com/protoLabsAI/careercoach-plugin
```
Then **enable** it (Console → Plugins, or `enable_plugin("careercoach")`) — install ≠ enable ≠ trust.
Set your profile in **Settings → Career Coach**, then just talk to your agent:

- "Help me think about what roles to target." · "Run a mock interview for the Acme ML role."
- "Here's a posting — is it worth applying to?" (paste URL/text) · "Critique my CV for this."
- "I got the offer. Help me negotiate." · `run_workflow("apply", {"posting": "<url>"})`

Tune how strictly it scores fit: `careercoach_preset growth-first` (or `careercoach_tune weight_career 45`).

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

## Found while building (filed upstream)

Building this surfaced protoAgent SDK/DX feedback, filed as issues on the host repo:

- **DX papercut** — a scaffolded standalone plugin that registers a subagent fails its own host-free
  smoke test, because `graph.subagents.config.SubagentConfig` isn't in the testkit's default host
  stubs (the import raises before `register()` can wire it). We guard for it here (`_register_subagents`),
  but the scaffolder encourages subagents, so the default stubs should include a permissive
  `SubagentConfig` (and `Knobs` / `make_knob_tools`). *(protoAgent issue — see the repo queue.)*

If you find more, please open an issue on protoAgent — that feedback loop is half the point of this repo.

## Extending (Phase 3)

- **A real job source.** Wire the optional `jobs_api_key` to a proper jobs API behind a tool (and flip
  `capabilities.network`). The shipped code makes no outbound calls of its own.
- **A background job-watch.** `graph.sdk.supervise` / `create_watch` (ADR 0067) → "3 new roles match
  your profile ≥ 75" as a nudge, lighting the rail icon via the event bus.

## Credits & license

MIT (see [`LICENSE`](./LICENSE)). Prompt-engineering IP adapted with credit from
[`MadsLorentzen/ai-job-search`](https://github.com/MadsLorentzen/ai-job-search) (MIT) — details in
[`CREDITS.md`](./CREDITS.md).
