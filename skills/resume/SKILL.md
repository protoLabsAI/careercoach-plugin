---
name: resume
description: >-
  Use when the user wants a RESUME/CV itself worked on — "build my resume", "import this
  resume", "parse my CV", "turn my profile into a resume", "restyle it", "make a PDF /
  Word version", "will this get through an ATS", "which resume is the current one". The
  resume is worked on as ONE versioned artifact, tailored variants branch off it, and
  exports become downloadable files. For scoring a posting or writing a cover letter see
  job-application-assistant; for the full filed application see role-packet; for the
  first-run interview see setup-coach.
tools: [careercoach_read_profile, careercoach_get_profile, careercoach_update_profile, careercoach_import_experience, careercoach_write_artifact, careercoach_scaffold_role, show_artifact, get_artifact, update_artifact, rewrite_artifact, list_artifacts, check_artifact, save_file_artifact, delete_artifact, show_component, load_skill]
---

# Resume — build, parse, export, check

This skill adds **no tools of its own**. It composes capabilities other plugins already
own. Three ideas carry the whole thing:

1. **The resume is worked on as an artifact.** One `html` artifact is the master. It is
   versioned, it renders in the Artifact panel, and it is edited in place — never
   re-pasted into chat, never copied into a second markdown file. That is the cure for the
   failure mode this skill exists to fix: a workspace that accumulates five diverging
   copies of a `tailored resume.md` and no answer to "which one is current".
2. **The artifact is the working surface, not the vault.** The artifact plugin keeps a
   bounded history — 20 artifacts across the whole instance by default — and evicts the
   least recently touched. So the durable record lives elsewhere: the **profile** holds
   the facts, and each role packet's `tailored resume.md` holds a snapshot of that
   variant, stamped with the artifact and version it came from. Verify an artifact id
   before you edit it — `master-and-tailor.md`, *Eviction*.
3. **The profile is the source of truth for facts.** The resume is a *presentation* of
   the operator profile. It never introduces an employer, title, date or credential the
   profile doesn't have. A fact that turns up while working on a resume goes into the
   profile first, then into the document.

## What composes what

Reference every external tool **by name only** — never import another plugin (ADR 0039).
If a tool isn't in your toolset, that path is off: say which tool is missing and which
plugin owns it, then offer the fallback. Never silently switch routes. Judge by what's in
your toolset, never by a plugin's install default — operators often have cowork and
execute_code enabled.

| Step | Tool | Owned by | If it's absent |
|------|------|----------|----------------|
| Read the facts | `careercoach_read_profile`, `careercoach_get_profile` | careercoach (this plugin) | always present |
| Correct the facts | `careercoach_update_profile`, `careercoach_import_experience` | careercoach | always present |
| File or find a role's snapshot | `careercoach_write_artifact`, `careercoach_scaffold_role` | careercoach | always present |
| Create the resume | `show_artifact(kind="html", …)` | **artifact** (bundled, on by default) | no artifact route at all — say so and fall back to showing the resume as chat markdown, which is a draft, not a deliverable |
| Read it back / parse a file | `get_artifact`, `list_artifacts` | artifact | as above |
| Edit it | `update_artifact`, `rewrite_artifact` | artifact | as above |
| Confirm it rendered | `check_artifact` | artifact | skip the verdict step; don't loop |
| Turn a file into a download | `save_file_artifact` | artifact | no Download button; give the file path instead |
| Clean up a temporary read | `delete_artifact` | artifact | leave it; it ages out |
| Word file | the `docx` skill via `load_skill("docx")` | **cowork** (off in a fresh install — check your toolset) | offer the PDF route, or the HTML download |
| Write HTML / DOCX bytes to disk | `execute_code` | **execute_code** (off in a fresh install — enabling it is a code-execution trust decision) | neither agent-made file route works; offer the HTML download |
| Print HTML → PDF | `browser_pdf` (after `browser_open`) | **agent_browser** (off in a fresh install, and `browser_pdf` isn't in a release yet) | offer the DOCX route, or the HTML download |
| Show results as a table | `show_component` | host core | fall back to a markdown table |

`browser_pdf` arrives in protoAgent with **PR #3451** (the agent_browser plugin vendored
into core). Until a core release includes it, the agent-produced PDF route doesn't exist.
The route that produces a real file **today** is DOCX, wherever cowork and execute_code
are enabled; the
fallback that needs no extra plugin at all is the operator downloading the HTML artifact
and printing it from their own browser. Details in `export.md`.

## The flow

Each step is a natural stopping point. Don't chain all five silently.

### 1. Import (only when there's something to import)
An operator drops a resume in chat, or points at one on disk. See **`import.md`** —
it covers both, and the rule that every email, phone, URL, employer and date you record
must appear in the source text you actually read. Facts land in the **profile**. Skip
this step entirely when the profile is already the better record.

### 2. Master resume
One `html` artifact built from a template, filled from the profile. See
**`master-and-tailor.md`**. Record its artifact id and template in the profile so the next
session finds it instead of making a second one — and verify the id before every edit.

### 3. Tailor per role
A variant is **its own artifact**, branched from the master, aimed at one posting. The
role packet files a snapshot of it that records the artifact id — the copy of record if the
artifact is ever evicted. See **`master-and-tailor.md`**.

### 4. Export
HTML artifact → a real file the operator can attach: DOCX (wherever cowork + execute_code
are enabled), PDF via
`browser_pdf` (once a release includes it), or the operator's own download-and-print as the
fallback. What each needs is in **`export.md`**.

### 5. ATS check
Three parts — what a parser actually reads, the rules checklist, and keyword coverage
against the posting. Results go out as `show_component` tables, not prose. See
**`ats-check.md`**.

## Reference files

Read one when you reach its step, not all up front.

| File | When to read it |
|------|-----------------|
| `import.md` | Step 1 — parsing an incoming resume into the profile |
| `master-and-tailor.md` | Steps 2-3 — the artifact mechanics: create, edit, version, branch, recover |
| `export.md` | Step 4 — DOCX / PDF / download routes and what each one needs |
| `ats-check.md` | Step 5 — the three-part check and how to present it |
| `templates/*.html` | Step 2 — the ATS-safe starting points (catalogue below) |

Content discipline is **not** duplicated here. Before writing a single bullet, read
`job-application-assistant/cv-guide.md` (section order, relevance-weighted cutting, the
two-page budget) and `job-application-assistant/writing-style.md` (tone, banned clichés,
the interview-backtrack test). For the filed, gated, one-role-at-a-time version of this
work, read `role-packet/SKILL.md` — this skill is what its Phase 3 renders with.

If a reference file won't open, say so and work from this page rather than inventing a
procedure.

## Template catalogue

| Template | Page | Use it for |
|----------|------|------------|
| `templates/classic-letter.html` | US Letter | The default. Reverse-chronological, most roles, most markets. |
| `templates/compact-a4.html` | A4 | A long career that all earns its place and still has to fit two pages, and most non-US markets. |
| `templates/credential-first-a4.html` | A4 | Education / certification / publications lead: regulated, clinical, research-adjacent roles. |

**The two A4 templates print on US Letter through `browser_pdf`** — it has no page-size
option. For a real A4 file use the DOCX route or the operator's own print (see `export.md`).

Pick one, fill it from the profile, delete the sections the profile has nothing for. Never
leave a bracketed placeholder in a rendered resume, and never invent a value to fill one.

### The ATS-critical CSS contract

Every template holds to this, and so must anything you hand-author or restyle. It is what
keeps the file an ATS reads faithful to the document the operator reviewed.

- **`@page { size: Letter|A4; margin: …; }`** — a declared page, so print isn't a guess.
- **Single column.** No multi-column grid or flex row, no `float`, no `position: absolute`,
  no table used for layout. A parser reads the DOM in order; two columns interleave into
  nonsense.
- **`break-inside: avoid`** (plus the legacy `page-break-inside` alias) on every entry, so
  a job title never separates from its bullets across a page break.
- **`font-variant-ligatures: none`.** An `fi` ligature reaches a text extractor as U+FB01
  and turns "efficiency" into a word no keyword filter matches.
- **A system font stack that ends in a generic family** (`sans-serif` / `serif`). No web
  fonts: nothing external loads in the panel or in a print job.
- **No `<img>`, no icons, no background colours.** Contact details in the document body as
  visible text — never in a running header or footer, never only inside a link's `href`.
- **Semantic headings** (`h1`, `h2`) with the boring standard words: Summary, Skills,
  Experience, Education, Certifications, Publications.
- **Declare everything yourself.** The Artifact panel injects the console's design-system
  stylesheet into every html artifact; a standalone print gets none of it. Set box-sizing,
  font weight, line-height, letter-spacing, colour and background on `body` and the
  headings explicitly, so fonts, weights and spacing agree between the panel and the file.
  The panel also renders html artifacts in quirks mode (a known issue in the core artifact
  shell), so vertical spacing can still differ slightly: **the exported file is what the
  employer gets — check that, not the panel.**

## Anti-fabrication

The same rule as everywhere else in this plugin, restated because a resume is where it
breaks first:

- Facts come from `careercoach_read_profile("experience")` and
  `careercoach_get_profile("skills")`. If the profile is thin, **say so and offer
  `/setup-coach`** — rendering a resume from an empty record is inventing a career.
- `do_not_claim` is a hard stop, not a preference.
- Reframe emphasis; never add an employer, title, date, degree or tool the profile
  doesn't hold. A number needs its baseline.
- Anything the operator tells you mid-flow goes into the profile
  (`careercoach_update_profile`) **before** it goes into the document.
- When parsing a resume you didn't write, every recorded fact must be traceable to text
  you actually read — see `import.md`.
