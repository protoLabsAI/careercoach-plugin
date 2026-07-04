# CV Tailoring & Rendering Guide

> Content discipline adapted with credit from Mads Lorentzen's `ai-job-search` (MIT),
> `05-cv-templates.md`. **The rendering mechanism is different here:** the upstream project
> compiles LaTeX/moderncv (and spends most of its guide firefighting page-breaks). This
> plugin defaults to **HTML → PDF via the artifact plugin** — no LaTeX toolchain, no
> orphaned-entry rescue. LaTeX remains an option (appendix) for anyone who wants it.

Follow `writing-style.md` for tone and the honesty test **before** writing any bullet.

## Rendering: `render_format`

- **`html`** (default) — build a clean, semantic one-file HTML CV with print CSS, hand it to
  the **artifact plugin** to render, then export/print to PDF. The agent can *see* the rendered
  result (it's a real page), so the "render → inspect → fix" loop needs no LaTeX knowledge.
- **`latex`** — produce a moderncv `.tex` (see the appendix). Use only if the user asks for it.

### HTML CV — house style
- One file, self-contained (inline `<style>`), A4/Letter print CSS: `@page { size: A4; margin: 16mm; }`,
  a body font stack, `--accent` custom property for the one accent colour.
- Structure with real semantics (`<header>`, `<section>`, `<h2>`), not a table layout.
- Keep it to **exactly 2 pages** when printed — use a `page-break-inside: avoid;` on each entry
  block so a role's title never separates from its bullets (the HTML equivalent of the upstream's
  `\needspace` rescue — but declarative and reliable).
- Render, look at it, iterate. A CV that ends mid-page-2 looks unfinished; one that spills to page 3
  needs cutting (see below), not squeezing.

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
