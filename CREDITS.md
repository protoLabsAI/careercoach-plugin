# Credits

This plugin's prompt-engineering IP is **adapted, with credit, from**:

### [`MadsLorentzen/ai-job-search`](https://github.com/MadsLorentzen/ai-job-search) — MIT © Mads Lorentzen

An excellent Claude Code job-application framework. What we ported and adapted:

| From upstream | Into this plugin |
|---------------|------------------|
| `03-writing-style.md` (anti-slop rules, the interview-backtrack honesty test, forward-looking framing) | `skills/job-application-assistant/writing-style.md` |
| `04-job-evaluation.md` (weighted 4-dimension fit rubric, bands, "call the employer" playbook, motivation filter) | `skills/job-application-assistant/job-evaluation.md` + the live `careercoach` rubric knobs |
| `05-cv-templates.md` / `06-cover-letter-templates.md` (content discipline: 2-page limit, relevance-weighted cutting, the LaTeX gotchas) | `skills/job-application-assistant/cv-guide.md` / `cover-letter-guide.md` |
| `07-interview-prep.md` (STAR framework, question banks, roleplay) | `skills/interview-coach/SKILL.md` |
| `upskill/SKILL.md` (two-pass gap analysis, heatmap, learning plan, anti-fabrication rules) | `skills/upskill/SKILL.md` |

**What changed in the adaptation:**

- **Rendering.** Upstream compiles LaTeX/moderncv; most of its CV/cover-letter guides are
  LaTeX page-break firefighting. Here the default is **HTML → PDF via the protoAgent artifact
  plugin** — no toolchain, no orphaned-entry rescue. The upstream LaTeX gotchas are preserved
  as an optional appendix for anyone who wants `.tex`.
- **Reframed around coaching.** The upstream is application-centric; this adds a `career-strategy`
  skill (positioning, offer evaluation, salary negotiation, decisions) and an `interview-coach`
  built around *practice with feedback*, not just tips. The autonomous evaluate→tailor→apply
  pipeline is one mode, not the whole product.
- **Repackaged for protoAgent** — skills, a static-DAG workflow, a subagent crew, agent tools +
  a tunable Knobs rubric, a console dashboard, and config/secrets/Settings, installable by git URL.
- **Left behind on purpose** — the Denmark-specific job-board scraper CLIs (jobnet/jobindex/
  jobdanmark/jobbank) and the ToS-gray LinkedIn HTML scraper. Job intake here is
  paste-a-URL/text → the agent's own `fetch_url`, with an optional proper job-source integration
  as a documented Phase-3 extension.

We are not affiliated with the upstream project; this is an independent adaptation under its MIT license.
