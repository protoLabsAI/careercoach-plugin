# Role packet — the gated application pipeline

The deliberate, filed version of the coach's "do it for me" mode. Where the research and documents guide
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
   cover letter must trace to the operator profile — the single source of truth,
   `careercoach_profile(action="read", doc="experience")` — or `Agent/story-bank.md`, and pass the
   interview-backtrack test in
   [writing-style.md](writing-style.md). If it isn't there and true, it doesn't ship. Honor
   the "do NOT claim" guardrails — the story bank's and the profile's `do_not_claim`.

## Before you start

- **Workspace seeded?** If this is the user's first packet, run `careercoach_profile(action="init_workspace")` to lay
  down the fill-in templates.
- **Read the truth before drafting.** `careercoach_profile(action="read", doc="experience")` — this is the only
  path to the source of truth (the generic `read_file` reaches managed fs projects, which the
  workspace is not). It always returns the operator profile, never the workspace
  `Resume/Experience.md`. If it says no profile is recorded, **stop**: run `/setup-coach` (or
  interview the user yourself), and if they keep their own Experience.md, bring it in with
  `careercoach_profile(action="import_experience")` (preview, then `apply=true` with its `preview_id` once they
  confirm). Drafting from an
  empty record means inventing a career, which this flow forbids. A fact they give you mid-packet
  goes into the profile with `careercoach_profile(action="update")` before you use it.
- The reference files live in two places: the **per-candidate** ones, read with
  `careercoach_profile(action="read", doc=doc)` — `experience` (the profile), `story-bank`, `reviewer`,
  `humanize`, `improvements` — and the **discipline** ones in this apply skill directory. Read each when its phase says to, not up front.

## The six phases

Write every authored artifact with `careercoach_role_packet(action="write", company=company, role=role, artifact=artifact, content=content, req=req)`
so it lands in the right file and the process log stays honest.

### Phase 1 — Intake  ·  *gate: confirm the role*
Confirm company, role title, and requisition id, and get the posting. If `careercoach_role_packet(action="list")`
already shows a folder for this company and role (the `resume` skill files a tailored resume there),
reuse it: pass the same `req` it was filed under, so the packet and the resume share one folder. Then
`careercoach_role_packet(action="scaffold", company=company, role=role, req=req, raw_jd=<the full posting>)` — this files the folder and
seeds `job description (raw).md` + `process_log.md`. Track it with `careercoach_track(action="track")`
(status `considering`). Read the raw JD closely; pull required/preferred skills, responsibilities, and
keywords — you'll reuse them in every later phase.

### Phase 2 — Recruiter / screener  ·  *gate: confirm before researching*
Read [recruiter-brief.md](recruiter-brief.md). Delegate company + role research to the **`company_researcher`** subagent
(it returns a *sourced* brief). Write **`recruiter-brief`**. If — and only if — a credible, specific
hiring manager is identifiable from the posting or research, research them and write
**`hiring-manager-profile`**; otherwise say so and skip it (it's conditional).

### Phase 3 — Resume  ·  *gate: confirm before drafting*
Read [evidence-map.md](evidence-map.md), then [cv-guide.md](cv-guide.md) and **[writing-style.md](writing-style.md)**
(read before writing any bullet). First build the **`evidence-map`**: every JD requirement mapped to
your proof from the profile / the story bank, with gaps and stretches flagged honestly. *Then*
write the **`tailored-resume`**, drawing only on what the evidence map supports. Surface any stretch
line for the user to keep, soften, or drop.

To *render* it, use the **`resume`** skill (`load_skill("resume")`): the resume becomes a versioned
`html` artifact built from an ATS-safe template, and `tailored resume.md` becomes a snapshot that
records which artifact and version it came from — so the packet stays readable without becoming a
second, diverging copy. That skill also owns the export routes (`html` / `docx` / `latex`), what each
one needs, and the ATS check. **Never silently switch route — name what's missing** (DOCX
needs `execute_code` in your toolset and `docx` in your available skills; cowork adds skills,
not tools).

### Phase 4 — Application skills / ATS entry  ·  *gate: confirm*
Read [ats-skills-entry.md](ats-skills-entry.md). Produce the **`skills-entry`** list — the discrete skills an ATS form asks
you to enter, ranked to the posting, each with the exact phrasing and backed by the evidence map. Flag
any the candidate can't substantiate rather than padding the list.

### Phase 5 — Cover letter  ·  *gate: confirm*
Read [cover-letter-guide.md](cover-letter-guide.md) + [writing-style.md](writing-style.md) (which layers in the
operator's learned `my-writing-style` voice if it exists), then `Skills/Humanize/SKILL.md` for the
final de-slop pass. Write the **`cover-letter`** (forward-looking,
task-solving; verify every company-specific claim against the recruiter brief's sources). Capture the
prompts/decisions you used in **`prompt-transcript`** so the packet is reproducible.

### Phase 6 — QA / improvement  ·  *gate: confirm, then hand back to the user*
Read [qa-review.md](qa-review.md). Apply `Agent/experience-reviewer.md`'s truth + voice rules to every artifact and
report a pass/fail line each. Then `careercoach_role_packet(action="assemble", company=company, role=role, req=req)` to build
`role packet.md` + `orchestration log.md`. Note any *process* fix worth keeping in
`workflow-audit/improvements.md`. Present the packet path, the completeness summary, and any unresolved
flags — then stop. The user reviews and submits; you don't submit for them.

## After the packet
Suggest the **interview** skill to prepare, and **strategy** if the evidence map surfaced real
gaps. Update the tracked application's status as it moves. Run `careercoach_role_packet(action="list")` any time to see
what's in flight.
