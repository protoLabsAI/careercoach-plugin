---
name: job-application-assistant
description: >-
  Use when the user shares a job posting (URL or text) or asks to evaluate fit, tailor a
  CV/resume, or write a cover letter — "should I apply to this", "evaluate this job",
  "tailor my CV for X", "write a cover letter for the role at Y", "apply to this".
  The execution half of the Career Coach: score fit against the rubric, then draft
  tailored, honest, verified documents. For interview practice see interview-coach;
  for positioning / offers / negotiation see career-strategy; for skill gaps see upskill.
tools: [careercoach_track_application, careercoach_list_applications, company_researcher]
---

# Job Application Assistant

The "do it for me" half of the coach. Two ways in: the user can drive each step
conversationally, or run the whole thing with the `apply` workflow. Either way, **fit
is evaluated and shown before anything is drafted**, and every draft is honest and verified.

This skill uses reference files in this directory — read the relevant one when you reach
that step (progressive disclosure), don't inline them all up front:

| File | When to read it |
|------|-----------------|
| `job-evaluation.md` | Step 1 — the weighted fit rubric + output format |
| `writing-style.md`  | Steps 2-3 — tone, the banned-cliché list, the honesty test (**read before drafting anything**) |
| `cv-guide.md`       | Step 2 — tailoring a CV, rendering it (HTML → PDF via the artifact plugin, or LaTeX) |
| `cover-letter-guide.md` | Step 3 — cover-letter structure + rendering |

## Workflow

### Step 1 — Research & evaluate fit (always first)
- Fetch the posting (`fetch_url` for a URL); pull required/preferred skills, responsibilities, keywords.
- Delegate company research to the **`company_researcher`** subagent — it returns a *sourced* brief.
- Score the posting with the rubric in `job-evaluation.md` (weights come from the live
  `careercoach` knobs; the operator can retune with `careercoach_preset` / `careercoach_tune`).
- Present the evaluation table + verdict. Record it with `careercoach_track_application`
  (fit score + status `considering`).
- **Do not silently proceed.** Give a straight recommendation (apply / apply-with-caveats /
  skip) and ask the user whether to draft. If experience match < 50, warn that honest
  tailoring will only stretch so far before continuing.

### Step 2 — Tailor the CV (on request)
- Read `cv-guide.md` and `writing-style.md`.
- Start from the user's existing CV/profile; reframe *emphasis*, never fabricate (see the
  honesty test in `writing-style.md`).
- Render per the operator's `render_format` (default `html` → the artifact plugin → PDF;
  `docx` → a real Word file via cowork's `docx` skill → `save_file_artifact`; `latex` → moderncv
  `.tex`). See `cv-guide.md` for each path. Inspect the rendered result; iterate until it's clean.

### Step 3 — Write the cover letter (on request)
- Read `cover-letter-guide.md` and `writing-style.md`.
- Forward-looking and task-solving: which of *their* problems can the candidate solve, and how.
- Verify every company-specific claim against the `company_researcher` brief's sources before including it.

### Step 4 — Hand off to prep
- Once documents are done, suggest the **interview-coach** skill to prepare, and **upskill**
  if the evaluation surfaced real gaps. Update the tracked application's status as it moves.

## Verification (before showing any document)
Re-read the generated document and report a pass/fail checklist:
- **Factual accuracy** — every claim matches the real profile; no invented skills/experience/dates.
- **Company claims verified** — each is backed by a source from the research brief, or it's cut.
- **Targeting** — opening + bullets are specific to this role, not generic.
- **Rendering** — the rendered CV is ~2 pages, the cover letter ~1 page, no layout breakage.
- **Style** — no em-dashes, no clichés (see `writing-style.md`), correct names/dates throughout.

## Coaching stance
Even in "do it for me" mode, act like a coach: explain *why* a bullet was reframed, flag any
stretch and let the user decide, and never oversell. The candidate has to defend every line in
an interview — protect them from claims they can't stand behind.
