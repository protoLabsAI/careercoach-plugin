---
name: role-packet
description: >-
  Use when the user wants the FULL, filed application worked up for a specific role — "build the
  role packet", "do the whole application for this job", "tailor everything for the Merck AD role",
  or when they want a resume + evidence map + ATS skills list + cover letter produced as saved files
  they can review and edit. A gated, human-approved pipeline that files a folder per role and
  produces each artifact one confirmed step at a time. For a quick fit read or a single document,
  use job-application-assistant instead; for the fast autonomous version, run the `apply` workflow.
tools: [careercoach_init_workspace, careercoach_scaffold_role, careercoach_write_artifact, careercoach_assemble_packet, careercoach_list_roles, careercoach_track_application, company_researcher]
---

# Role packet — the gated application pipeline

The deliberate, filed version of the coach's "do it for me" mode. Where `job-application-assistant`
drives one document conversationally and the `apply` workflow runs autonomously, this produces a
**complete, saved packet** for a single role, **pausing for the user's approval before every phase**.

The output is a real folder tree the user can open and edit:

```
<packet_root>/Companies/<Company>/Roles/<Role - Req>/
  job description (raw).md · process_log.md
  recruiter brief.md · hiring manager profile.md (if found)
  role evidence map.md · tailored resume.md
  application skills entry list.md · cover letter.md
  prompt transcript.md · role packet.md · orchestration log.md
```

## Two rules that never bend

1. **Gate every phase.** Before each phase, state in one or two lines what you're about to do and
   ask the user to confirm. Do not chain phases silently. This is the whole point of this flow — it
   is a coach working *with* the user, not an autopilot.
2. **Anchor everything to the truth.** Every claim in the evidence map, resume, skills list, and
   cover letter must trace to `Resume/Experience.md` (the source of truth) or `Agent/story-bank.md`,
   and pass the interview-backtrack test in `job-application-assistant/writing-style.md`. If it isn't
   there and true, it doesn't ship. Honor the story bank's "do NOT claim" guardrails.

## Before you start

- **Workspace seeded?** If this is the user's first packet, run `careercoach_init_workspace` to lay
  down the fill-in templates, and confirm `Resume/Experience.md` is filled in. Everything is anchored
  to it, so if it's empty, stop and help the user populate it first (that's a coaching session, not a
  drafting one).
- The reference files live in two places: the **per-candidate** ones in the workspace
  (`Resume/Experience.md`, `Agent/story-bank.md`, `Agent/experience-reviewer.md`,
  `Skills/Humanize/SKILL.md`, `workflow-audit/improvements.md`) and the **discipline** ones in the
  `job-application-assistant` skill (read via `load_skill`). Read each when its phase says to, not up front.

## The six phases

Write every authored artifact with `careercoach_write_artifact(company, role, artifact, content, req)`
so it lands in the right file and the process log stays honest.

### Phase 1 — Intake  ·  *gate: confirm the role*
Confirm company, role title, and requisition id, and get the posting. Then
`careercoach_scaffold_role(company, role, req, raw_jd=<the full posting>)` — this files the folder and
seeds `job description (raw).md` + `process_log.md`. Track it with `careercoach_track_application`
(status `considering`). Read the raw JD closely; pull required/preferred skills, responsibilities, and
keywords — you'll reuse them in every later phase.

### Phase 2 — Recruiter / screener  ·  *gate: confirm before researching*
Read `recruiter-brief.md`. Delegate company + role research to the **`company_researcher`** subagent
(it returns a *sourced* brief). Write **`recruiter-brief`**. If — and only if — a credible, specific
hiring manager is identifiable from the posting or research, research them and write
**`hiring-manager-profile`**; otherwise say so and skip it (it's conditional).

### Phase 3 — Resume  ·  *gate: confirm before drafting*
Read `evidence-map.md`, then `job-application-assistant`'s `cv-guide.md` and **`writing-style.md`**
(read before writing any bullet). First build the **`evidence-map`**: every JD requirement mapped to
your proof from `Experience.md` / the story bank, with gaps and stretches flagged honestly. *Then*
write the **`tailored-resume`**, drawing only on what the evidence map supports. Surface any stretch
line for the user to keep, soften, or drop. Render per `render_format` if the user wants a PDF.

### Phase 4 — Application skills / ATS entry  ·  *gate: confirm*
Read `ats-skills-entry.md`. Produce the **`skills-entry`** list — the discrete skills an ATS form asks
you to enter, ranked to the posting, each with the exact phrasing and backed by the evidence map. Flag
any the candidate can't substantiate rather than padding the list.

### Phase 5 — Cover letter  ·  *gate: confirm*
Read `job-application-assistant`'s `cover-letter-guide.md` + `writing-style.md`, then
`Skills/Humanize/SKILL.md` for the final de-slop pass. Write the **`cover-letter`** (forward-looking,
task-solving; verify every company-specific claim against the recruiter brief's sources). Capture the
prompts/decisions you used in **`prompt-transcript`** so the packet is reproducible.

### Phase 6 — QA / improvement  ·  *gate: confirm, then hand back to the user*
Read `qa-review.md`. Apply `Agent/experience-reviewer.md`'s truth + voice rules to every artifact and
report a pass/fail line each. Then `careercoach_assemble_packet(company, role, req)` to build
`role packet.md` + `orchestration log.md`. Note any *process* fix worth keeping in
`workflow-audit/improvements.md`. Present the packet path, the completeness summary, and any unresolved
flags — then stop. The user reviews and submits; you don't submit for them.

## After the packet
Suggest the **interview-coach** skill to prepare, and **upskill** if the evidence map surfaced real
gaps. Update the tracked application's status as it moves. Run `careercoach_list_roles` any time to see
what's in flight.
