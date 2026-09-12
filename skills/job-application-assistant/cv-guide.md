# CV Tailoring & Rendering Guide

> Content discipline adapted with credit from Mads Lorentzen's `ai-job-search` (MIT),
> `05-cv-templates.md`. **The rendering mechanism is different here:** the upstream project
> compiles LaTeX/moderncv (and spends most of its guide firefighting page-breaks). This
> plugin defaults to a **print-correct HTML artifact** — no LaTeX toolchain, no
> orphaned-entry rescue. LaTeX remains an option (appendix) for anyone who wants it.

> **This file is the content discipline.** The *mechanics* — building the CV as a versioned
> artifact, the ATS-safe templates, getting a real PDF or `.docx` file out, and the ATS
> check — live in the **`resume`** skill (`load_skill("resume")`). Read this for what goes
> on the page; read that for how the document is produced.

Follow `writing-style.md` for tone and the honesty test **before** writing any bullet.

**Draft only from the operator profile** — `careercoach_read_profile("experience")`, the single
source of truth — and treat its `do_not_claim` lines as hard stops. A workspace
`Resume/Experience.md` is never a source here; if the operator has updated theirs, bring it into
the profile with `careercoach_import_experience` first (preview, then apply with its `preview_id`).

## Rendering: `render_format`

- **`html`** (default) — build a clean, semantic one-file HTML CV with print CSS and hand it
  to the **artifact plugin** (`show_artifact(kind="html", …)`). It becomes a versioned artifact
  the operator can see and you can edit in place, and it needs no LaTeX knowledge. Two things
  to be straight about, because the loop depends on them:
  - You do **not** see the rendered page. `check_artifact` gives you a render *verdict*
    (clean / failed with the error / no result yet) — that's the feedback channel. Judge
    length and layout from the content against the page budget below, and ask the operator
    to confirm; don't claim you looked at it.
  - **The artifact plugin does not make PDFs.** A real file comes from the routes in the
    `resume` skill's `export.md`: a `.docx` via cowork (wherever cowork + execute_code are
    enabled), a PDF via `browser_pdf`
    (the **agent_browser** plugin, arriving with protoAgent PR #3451, plus **execute_code**),
    or, as the fallback, the operator downloading the HTML artifact and printing it from
    their own browser. Offer the route that's actually available; never say "exported to
    PDF" when what exists is an HTML artifact.
- **`docx`** — a real, editable **Word file** (what many ATS forms and recruiters expect). Build
  it with cowork's **`docx`** skill: `load_skill('docx')`, author the CV with `python-docx`
  following the content discipline in this guide + `writing-style.md`, save it to disk, then
  register it with **`save_file_artifact(path, title="<Name> — CV — <Role>")`** so it lands in the
  Artifact panel as a **versioned, downloadable** file with a text preview. Needs the cowork plugin
  + the **`execute_code`** plugin (which runs the `python-docx` code — enabling it is a code-execution
  trust decision) + a protoAgent **v0.108.0+** host. On the **desktop app** that's the floor: 0.108.0
  ships the managed Python runtime (ADR 0094), so the first `execute_code` call provisions a pinned
  CPython + the doc libraries (one consented download, once per machine); before it, code execution
  couldn't run on the packaged app at all. On server/Docker there's nothing to provision. If any is missing,
  **name exactly what's absent and fall back to `html`** — never silently skip. Save a revised CV as a
  new version by passing the same `artifact_id`.
- **`latex`** — produce a moderncv `.tex` (see the appendix). Use only if the user asks for it.

### HTML CV — house style
**Start from a template, don't hand-roll the shell.** The `resume` skill ships ATS-safe
templates (`skills/resume/templates/*.html`) that already carry the whole contract: one
self-contained file with inline `<style>`, a declared `@page` (Letter or A4),
`break-inside: avoid` on every entry so a role's title never separates from its bullets
(the declarative equivalent of the upstream's `\needspace` rescue), ligatures off, a system
font stack, real semantics (`<section>`, `<h2>`) and no table layout. Read
`resume/SKILL.md` for the catalogue and the rule list before restyling anything.

Keep it to **2 pages**. You can't see the rendered page, so judge length from the budget
below and confirm with the operator: a CV that ends mid-page-2 looks unfinished; one that
spills to page 3 needs cutting (see below), not squeezing.

## Section-by-section tailoring

- **Profile statement** (top, 3-4 lines) — the most important section to customize. A concise
  "elevator pitch" for *this* role: what the employer gains. Keep 2-3 variants for your main role types.
- **Core competencies / skills** (5-7) — reorder + emphasize per the posting; bold category labels.
- **Experience** — rewrite bullets to lead with what's relevant; 4-5 bullets for the most recent role,
  3-4 for the previous, 2-3 for older. Emphasize measurable results ("reduced processing time 40%").
- **Education** — brief for senior roles; include thesis topics only when relevant.
- **Publications / awards / references** — select the most relevant; "References available on request."

### Recommended section order
- **Technical / data / ML:** profile → skills → experience → education → publications/awards.
- **Domain specialist:** profile → skills → education (credentials qualify) → experience → publications.

## Relevance-weighted cutting (how to shrink a CV the right way)

**Cut by signal, not by section.** An older-role bullet that speaks directly to the posting beats
a recent-role bullet that doesn't. For each candidate line, score three things:
1. **Relevance to THIS posting** — does it hit a named tool, keyword, or stated responsibility?
2. **Uniqueness** — is this the only place the claim appears?
3. **Narrative load** — does the cover letter lean on it? (If cutting it forces a cover-letter rewrite, it's load-bearing.)

Cut the lowest-total-score line first, wherever it sits. Practical order: redundancy → profile fluff
→ low-relevance experience bullets → low-relevance supporting content → low-relevance publications →
last-resort structural cuts (oldest education, collapsing certifications). **Never** cut the one
concrete example the cover letter depends on. For a near-miss (2.05 pages), tighten spacing before cutting content.

## Page budget (2-page hard limit)

| Section | Max |
|---------|-----|
| Profile | 3-4 lines |
| Skills | 5 items, 1-2 lines each |
| Recent role | 4-5 bullets |
| Previous role | 2-3 bullets |
| Older roles | 2 bullets |
| Education | 2-3 entries |
| Publications | 2-3 |
| References | one line |

**If in doubt, cut rather than squeeze** — a cramped CV reads worse than a shorter one.

---

## Appendix — LaTeX / moderncv (optional, `render_format: latex`)

If the user wants the classic `.tex` output, use moderncv `banking`/`blue`, compile with **lualatex**
(pdflatex fails on modern MiKTeX with fontawesome5 font-expansion errors), and keep these
hard-won gotchas from the upstream project:

- **Force the accent colour** on names + section headings (`\renewcommand*{\sectionstyle}` etc.) —
  the banking style leaves them black on lualatex+MiKTeX otherwise.
- **Prevent orphaned entries:** `\usepackage{needspace}` + `\needspace{5\baselineskip}` immediately
  before a `\cventry` so its title never sits alone at the bottom of a page.
- **Rescue a near-miss:** `\enlargethispage{2-3\baselineskip}` before a late section that just barely spills.
- **Don't** put `\vspace{...}` *between* `\item`s in an `itemize` — it intermittently produces one
  oversized gap. Let the list use its native `\itemsep`.
- Compile → read the PDF → confirm exactly 2 pages, no orphaned titles → iterate.
