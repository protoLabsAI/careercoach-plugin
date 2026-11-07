# Tools

The agent tools this plugin registers. You don't call these yourself: the coach picks them from what you
ask. The default set is kept small on purpose and checked with a tool-selection eval
(`evals/tool_selection/`).

## The default set

| Tool | What it does |
|---|---|
| `careercoach_find_roles` | Find roles: your watchlist's own job boards (`source="watched"`, the default) or the wider market (`source="market"`). Results land in the Inbox |
| `careercoach_settings` | Read or change search and background-watch settings without restart |
| `careercoach_watchlist` | Manage the companies whose job boards are scanned |
| `careercoach_track` | Manage the pipeline and the Inbox: track or update a role, list, keep or dismiss |
| `careercoach_digest` | The morning digest: fresh fitting roles, follow-ups due, quiet applications, the pipeline at a glance |
| `careercoach_log_contact` | Log a networking touch and the next step. A due date puts the follow-up in the digest |
| `careercoach_profile` | Read and maintain your verified profile, and import or export `Experience.md` |
| `careercoach_read_sources` | Read-only view of the record a draft must trace to, plus a figure check for a draft (`source="check"`) |
| `careercoach_answers` | Your standard application answers (email, phone, work authorization, sponsorship and so on), as drafts or confirmed |
| `careercoach_render_resume` | Render your résumé to HTML, ready to print to a PDF |
| `careercoach_save_apply_kit` | Save a role's apply kit (résumé, cover letter, answers) and move a Saved role to Preparing |
| `careercoach_prepare_application` | Fetch an ATS's public application questions before you open a posting |
| `careercoach_role_packet` | The gated role-packet workspace: a folder per role, built one approved step at a time |
| `careercoach_knobs` · `careercoach_tune` · `careercoach_preset` | Read and retune the fit rubric's weights live |

## Browser-fill tools (beta)

Registered only when **Browser fill (beta)** lists at least one ATS. See [applying.md](applying.md).

| Tool | What it does |
|---|---|
| `careercoach_plan_fill` | Plan a form fill from your confirmed answers only |
| `careercoach_verify_fill` | Diff the filled form's read-back against the plan, until it reports `VERIFIED` |
| `careercoach_handoff` | Pause for a step only you can do, such as a captcha or login, in a visible browser |
| `careercoach_request_submit` | Ask you to approve one submit. Only an explicit approve unlocks one click, for 120 seconds |

## The extended set

Turning on **Extended tools** also registers every older, individual tool name, so scripts and prompts that
used them keep working: `careercoach_scan_boards`, `careercoach_search_jobs`, `careercoach_list_roles`,
`careercoach_track_application`, `careercoach_list_applications`, `careercoach_import_history`,
`careercoach_import_connections`, `careercoach_find_referrals`, `careercoach_get_profile`,
`careercoach_read_profile`, `careercoach_update_profile`, `careercoach_write_profile`,
`careercoach_import_experience`, `careercoach_export_experience`, `careercoach_get_answers`,
`careercoach_propose_answer`, `careercoach_confirm_answers`, `careercoach_init_workspace`,
`careercoach_scaffold_role`, `careercoach_write_artifact` and `careercoach_assemble_packet`.

## Subagents

The `apply` workflow chains three subagents: `company_researcher` (a sourced company brief),
`job_evaluator` (fit against the rubric, honest about gaps) and `application_writer` (builds and saves the
apply kit).
