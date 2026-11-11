# Upgrading

Version notes that change behavior. Release notes for every version are on the
[releases page](https://github.com/protoLabsAI/careercoach-plugin/releases).

## 0.15.0

- **The browser checks submits.** On a core offering `agent_browser.add_gate_policy`, a submit is allowed
  only by spending the one-shot grant from `careercoach_request_submit`, bound to the exact form read-back
  you approved. This covers clicks by snapshot ref too. Older cores retain the argument-text submit guard;
  the optional service does not raise the minimum protoAgent version. Form JSON works with or without
  `AGENT_BROWSER_PAGE_CONTENT` boundary markers.
- **The whole form must be ready.** Unplanned values block verification, and the approval card shows the
  full read-back. Unchanged page defaults are recognized from the original plan, even across re-plans.
  Answers you give mid-fill need a fresh plan, and legal or attestation questions require your own answer.
  Dropdown and location checks also recognize supported equivalent values returned by the browser.
- **Answers show where they came from.** Extra answers are checked against what you said in the
  conversation; unsupported answers are labeled as the agent's, and cannot verify legal or attestation
  fields. Verification reads the browser's form directly when supported, otherwise uses the latest
  `browser_form_read` result from the conversation and rejects stale reads. If neither is available, the
  supplied read-back must still include every field from the plan.

## 0.14.0

- **Needs protoAgent 0.231.0 or newer.** 0.231.0 renamed the agent's memory tools (ADR 0125), and the skills
  now call the new ones: `memory_search`, `memory_save` and `knowledge_search`. On an older core, update
  protoAgent first; the plugin's `min_protoagent_version` refuses to load otherwise.
- **Named résumé variants.** A tailored résumé renders as its own variant and never overwrites your master.

## 0.13.10

- **The code moved into folders.** The logic modules now live in `record/`, `search/` and `applying/`, and the
  Jobs view is `view/jobs.html`. Installing or updating through protoAgent handles this. If you import a module
  directly, for example `careercoach.profile`, use the new path: `careercoach.record.profile`.
- **Packet files are private to you.** They're written atomically now, and a new file is readable by your user
  only, the same as the profile.

## 0.13.9

The coach's voice rules now ride on every turn, and the writing guide is its own `writing-style` skill. If a
prompt or skill of yours told the agent to read `apply-form/writing-style.md`, point it at
`load_skill("writing-style")` instead.

## 0.13.2

A blank **Role-packet workspace** (`packet_root`) now uses this agent's own folder inside its data directory
(`<agent data dir>/workspace`), so agents on one machine never share files.

If your agent relied on the old blank setting to read `~/CareerCoach`, set `packet_root: ~/CareerCoach` in
**Settings → Career Coach → Role-packet workspace** before upgrading to keep using that folder. Otherwise
your role packets and `Experience.md` stay where they are and aren't moved. The plugin logs a one-time
warning when it sees a populated `~/CareerCoach` it no longer reads.

## 0.8.0

A relative `packet_root` or `CAREERCOACH_PACKET_DIR` now resolves under your home directory, not the
server's working directory. This only matters for source or dev runs: on the desktop app the working
directory is `/`, which is read-only, so a relative workspace never worked there. An absolute path or `~/…`
behaves as before.

## 0.7

The profile and tracker moved from `~/.protoagent/careercoach/<instance>/` to this agent's plugin store,
`<instance_root>/careercoach/`. A store found in the old place is copied forward once, and the old folder
gets a `MIGRATED-TO` note. The old files stay for a rollback but are never adopted again, so wiping the new
store really does start over. If you restore a machine from a backup taken before 0.7, that note is already
there, so copy the files across by hand or delete the note.
