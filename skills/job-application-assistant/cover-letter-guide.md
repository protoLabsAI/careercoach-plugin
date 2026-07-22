# Cover-Letter Rendering Guide

> Content discipline adapted with credit from Mads Lorentzen's `ai-job-search` (MIT),
> `06-cover-letter-templates.md`. Rendering defaults to **HTML → PDF via the artifact
> plugin**; LaTeX (`cover.cls`) is an option (appendix). Structure + voice live in
> `writing-style.md` — read it first.

## Rendering: `render_format`

- **`html`** (default) — one self-contained HTML letter with print CSS, rendered by the
  artifact plugin and exported to PDF. Match the CV's font + accent so the pair looks like a set.
- **`docx`** — a real editable **Word letter** to match a docx CV. Build it with cowork's
  **`docx`** skill (`load_skill('docx')`, `python-docx`), save to disk, then
  **`save_file_artifact(path, title="<Name> — Cover Letter — <Company>")`** for a versioned,
  downloadable file. Same requirements + fallback as the CV (`cv-guide.md`): needs cowork +
  `execute_code` + a v0.107.0+ host, else fall back to `html`.
- **`latex`** — the custom `cover.cls` (Lato/Raleway, XeLaTeX); see the appendix.

### HTML letter — house style
- `@page { size: A4; margin: 22mm; }`, single accent colour, generous line-height.
- **Exactly one page**, signature block included. **Body budget: 250-300 words** (350 overflows).
- Structure = 3 blocks (opening + body-with-bullets + close); add a 4th only if the others are short.

## Structure (see `writing-style.md` for the full shape)

1. **Salutation** — a named person if known ("Dear [First Last],"), else the team
   ("Dear [Company] hiring team,"). Avoid "To whom it may concern".
2. **Opening** — role + a specific hook (not a template opener).
3. **Why this company** (early) — specifics from the posting + `company_researcher` brief.
4. **Body** — forward-looking, task-solving; 3-5 concrete bullets; 1-2 brief past examples as evidence.
5. **Personal fit** — behavioral strengths, team contribution.
6. **Close** — brief, confident. Then signature.

## Checklist before finalizing
- [ ] No em-dashes; no clichés or filler (`writing-style.md`)
- [ ] Every claim backed by a specific example
- [ ] Forward-looking: focuses on the tasks the candidate will solve
- [ ] The "why this company" section cites *this* company's specifics (verified against sources)
- [ ] Company name + role correct throughout; date current
- [ ] Fits one page; language matches the posting; salutation appropriate
- [ ] Headline is specific, not generic

## Submission
Export as PDF. Name files clearly ("[Name] CV", "[Name] Cover Letter"). Submit only what's
requested, and follow any anonymity/format instructions in the posting.

---

## Appendix — LaTeX `cover.cls` (optional, `render_format: latex`)

Compile with **XeLaTeX** (fontspec). The one trap worth memorizing from the upstream project:

**`\lettercontent{}` appends `\\`, which breaks when its argument ends in `\end{itemize}`**
(`! LaTeX Error: There's no line here to end.`, no PDF). Close `\lettercontent{}` *before* the
list, then wrap the list in the matching Raleway font so typography stays consistent:

```latex
\lettercontent{Here is how my experience maps:}

{\raggedright\fontspec[Path = OpenFonts/fonts/raleway/]{Raleway-Medium}\fontsize{11pt}{13pt}\selectfont
\begin{itemize}
    \item ...
\end{itemize}\par}

\lettercontent{[next paragraph]}
```

The `\fontspec` wrapper is mandatory — moving `itemize` outside `\lettercontent{}` without it
renders bullets in the body font (Lato) and visually mismatches the letter. Compile → confirm
1 page → read the PDF → iterate.
