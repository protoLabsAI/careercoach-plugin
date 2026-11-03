# Development

Contributor notes: the file layout, the tests, a tour of the plugin system as this repo uses it, releasing,
and feedback filed upstream.

## Layout

```
careercoach-plugin/
├─ protoagent.plugin.yaml   manifest: config, secrets, settings, view, events, provenance
├─ SOUL.md                  the recommended coach persona
├─ __init__.py              register(): tools, knobs, subagents, the Jobs view, the job-watch
├─ profile.py               the operator profile, its store, and the always-on block (voice and write rules)
├─ state.py                 the application tracker
├─ rubric.py                the weighted fit rubric
├─ jobsource.py             market search: Himalayas, Hacker News, JSearch, Remotive
├─ boards.py                company job boards: Ashby, Greenhouse, Lever
├─ watchlist.py             the watchlist, the first-seen log, ghost-job flags
├─ scan.py                  one watchlist scan, shared by the tool, the digest and the watch
├─ companies.py             company and role normalization for every dedup
├─ contacts.py              LinkedIn Connections import, referrals, follow-ups
├─ hunt.py                  the job-hunt tools
├─ answers.py               standard form answers, draft and confirmed
├─ grounding.py             tracing a draft's figures back to the record
├─ resume.py                résumé rendering
├─ packet.py                the role-packet workspace
├─ ats.py, formfill.py      the browser-fill beta: per-ATS planning and read-back
├─ submitgate.py            the consent gate for a submit click
├─ skills/                  apply-form (apply), career-strategy (strategy), interview-coach (interview),
│                           job-hunt, resume, writing-style
├─ templates/               workspace starters: Experience.md, story bank, reviewer, Humanize, improvements
├─ workflows/apply.yaml     research, evaluate, then build and save an apply kit
├─ evals/tool_selection/    the tool-selection eval
├─ docs/                    these pages, plus design/ (the Jobs view redesign spec)
└─ tests/                   host-free tests with a vendored testkit
```

Skill directory names are kept for history. The name the agent loads is the frontmatter `name`, shown in
parentheses above.

## Tests

The suite runs with no protoAgent present. The pure logic (`rubric.py`, `state.py`, `packet.py` and the
rest) has no host imports and is tested directly, and the host-touching paths are guarded so the plugin
loads and its tests run against the vendored testkit (`tests/_plugin_testkit.py`).

```sh
pip install -r requirements-dev.txt ruff==0.15.10
ruff check . && ruff format --check .
pytest -q
```

CI runs the same three commands. ruff is pinned, so run that version locally.

## The plugin system, exercised

This repo also serves as a worked example of how far a protoAgent plugin can go without forking core. Read
`__init__.py` top to bottom: it's commented as a tour.

| Surface | Where | What it shows |
|---|---|---|
| Skills, loaded on demand | `skills/` | Six skills. `apply` and `resume` keep reference sub-files beside their `SKILL.md` |
| Composition, not new tools (ADR 0039) | `skills/resume/` | The `resume` skill adds no tools. It names tools the artifact, agent_browser and execute_code plugins own, and cowork's `docx` skill, with a fallback for each |
| A slash command | `skills/job-hunt/` | `/setup-coach`, the first-run interview |
| A gated, filed pipeline | `skills/apply-form/packet-flow.md`, `packet.py`, `templates/` | Your approval before every phase, with artifacts filed per role |
| A static workflow (ADR 0002) | `workflows/apply.yaml` | `research → evaluate → build and save the apply kit`, chained with `depends_on` |
| Subagents | `__init__.py` | `company_researcher`, `job_evaluator` and `application_writer` |
| Agent tools | `__init__.py`, `hunt.py` | A lean default set measured by an eval, with older names behind `extended_tools` |
| Plugin middleware (ADR 0032) | `profile.py`, `__init__.py` | The `<operator_profile>` block on every model call, via `wrap_model_call` (ADR 0108 D2), and the submit gate's `wrap_tool_call` |
| Live controls (`graph.sdk`) | `__init__.py` | The fit rubric's weights as knobs, with presets |
| A background surface (ADR 0018) | `__init__.py` | The supervised job-watch loop |
| A goal verifier (ADR 0028, 0067) | `__init__.py` | `careercoach:new_matches`, for `create_watch` |
| A console rail view (ADR 0026) | `__init__.py`, manifest `views:` | The Jobs view: a public page plus gated `/api/plugins/careercoach` data routes |
| Config, secrets and Settings (ADR 0019) | manifest | Profile basics, documents, search and watch settings, with `jobs_api_key` routed to `secrets.yaml` |
| Events (ADR 0039) | `registry.emit` | `application_tracked`, `documents_generated`, `new_matches` and `inbox_changed` light the rail icon |
| The consumption SDK (ADR 0043) | lazy `graph.sdk` imports | `Knobs`, `make_knob_tools` and `supervise`, guarded so the plugin loads host-free |

## Releasing

Bump `version` in `protoagent.plugin.yaml` and `pyproject.toml` together (a test checks they match), merge,
then run the **Release** workflow, or merge with a squash subject of `chore: release vX.Y.Z`. The workflow
tags the release and writes the notes. Don't create releases by hand.

## Found while building

Building this surfaced protoAgent feedback, filed on the host repo:

- **A scaffolded plugin with a subagent failed its own host-free test**, because the testkit's default host
  stubs lacked `SubagentConfig`. Filed as [protoAgent #1764](https://github.com/protoLabsAI/protoAgent/issues/1764)
  and since fixed: the testkit ships stand-ins, and this repo's vendored copy has them.
- **There's no way for a plugin to offer a persona.** This plugin ships `SOUL.md`, but adopting it is a
  manual copy. Any fix should stay opt-in and never overwrite your own persona. Filed as
  [protoAgent #1771](https://github.com/protoLabsAI/protoAgent/issues/1771).
- **`load_skill` can't reach a skill's sub-files.** It returns only `SKILL.md`, so reference files such as
  `cv-guide.md` aren't loadable by the agent. The writing guide was promoted to its own skill to work around
  this.

If you find more, please open an issue on protoAgent.
