# QA / improvement phase

The last gate before the packet goes back to the user. Two jobs: **verify** every artifact is true and
in-voice, and **capture** anything that would make the next packet better. Then assemble and hand off.

## 1. Verify against the reviewer rules
Read `Agent/experience-reviewer.md` (the candidate's own truth + voice rules) and check every artifact
against it. Report a **pass/fail line per artifact**, quoting the specific offending text on a fail:

- **Anchored** — every resume bullet, evidence-map claim, skills entry, and cover-letter fact traces
  to `Experience.md` or the story bank. No orphan claims.
- **Interview-backtrack** — no line the candidate would have to walk back in an interview.
- **Company claims sourced** — each is backed by a source in the recruiter brief, or it's cut.
- **No inflation** — emphasis reframed, scope/level/metrics not upgraded.
- **Guardrails honored** — nothing from the story bank's "do NOT claim" list appears.
- **Voice** — reads in the candidate's register: honors `Agent/experience-reviewer.md`'s voice rules and
  the operator's learned `my-writing-style` voice (if that skill exists); apply `Skills/Humanize/SKILL.md`
  if a draft still reads generic. No em-dashes, no clichés.
- **Consistency** — resume, skills list, and cover letter agree; the same story isn't told two ways.
- **Rendering** (if a PDF was produced) — resume ~2 pages, cover letter ~1 page, no layout breakage.

If anything fails, fix it (re-write the artifact via `careercoach_write_artifact`) or surface it to the
user for a decision — don't assemble over a known failure.

## 2. Assemble
Run `careercoach_assemble_packet(company, role, req)`. It concatenates the produced artifacts into
`role packet.md` and refreshes `orchestration log.md` with a completeness checklist. Read back the
present/missing summary; if a body artifact is unexpectedly missing, that's a gap to close, not to ship.

## 3. Capture improvements
If this role surfaced a **process** lesson — a step that was slow, a draft that came back weak, a
reference file that needed better data — append it to `workflow-audit/improvements.md` (newest first:
what happened, the fix, which phase it applies to). This is how the system gets better across roles.

## 4. Hand off (do not submit)
Present: the packet path, the completeness summary, any stretches/flags the user still needs to decide,
and the suggested next step (interview-coach to prep, upskill for real gaps). Then **stop** — the user
reviews and submits the application themselves. If they answer a new evidence question or change a call,
revise the affected artifact and re-run this phase; don't re-do the whole packet.
