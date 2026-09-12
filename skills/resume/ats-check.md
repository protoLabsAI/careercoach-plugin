# The ATS check — three parts, no new tools

An "ATS check" that just asserts the resume is fine is worth nothing. This one has three
parts, each with evidence behind it, and each honest about what it can't see.

Run it after an export, and again whenever the layout changes. Present every part with
`show_component` — a table the operator can scan, not a paragraph of reassurance.

---

## Part 1 — what a parser actually reads

The only part that tests reality rather than rules. It works because
`save_file_artifact` extracts a text preview when it stores a file, and `get_artifact`
hands that text back.

1. Have the exported file (`export.md`). If the operator printed it themselves (Route 3),
   ask them for the PDF — without a file, **say this part didn't run**; don't substitute
   the HTML source and call it a parser view.
2. Get it into a file artifact **once**. Routes 1 and 2 already saved it — use that id, don't
   save it again. For a file the operator hands back:
   `save_file_artifact("/abs/path/resume.pdf", title="<Name> — Resume — <Role> (PDF)", artifact_id=<this role's export artifact, if it has one>)`.
   Every extra save without an id is one more panel entry pushing the master toward
   eviction (`master-and-tailor.md`, *Eviction*). Note the `(mime, NN KB)` in the save
   result — that's Part 2's file-size check for free.
3. `get_artifact("<that id>")` → the extracted text. **This is approximately what an ATS
   sees.** Read it as if you'd never seen the resume.
4. Diff it against the document you wrote, and report:

| Symptom | What it means | What to do |
|---------|---------------|------------|
| An employer, title, date or whole section is **missing** | Extraction dropped it — usually a table, a text box, a header/footer, or an image of text | Move it into normal body flow |
| Words **run together** or letters are lost (`efciency`) | Ligatures, or a font that doesn't map to Unicode cleanly | `font-variant-ligatures: none`; use a standard font |
| Text arrives in the **wrong order** | Multi-column or absolutely-positioned layout — reading order isn't visual order | Single column |
| Contact line is missing or repeated on every page | It's in a running header/footer | Put it in the body |
| Odd characters: U+FB00-FB06 (ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ), U+E000-F8FF (private use), stray U+00AD | Ligatures and icon-font glyphs | Remove; no icon fonts |

Three limits to state rather than paper over:

- Extraction stops at **50 PDF pages** (`… (more pages — download for all)`) and the
  stored preview is clipped at **64 KB** (`… (preview truncated …)`). If you see either
  note, the tail is a preview limit, **not** a parse failure. Say which.
- The extractor joins pages with a newline, so **you cannot count pages from this text**.
  Page count is Part 2, and it's judged from the document, not from here.
- `.docx` extraction reads paragraphs only, so table content vanishes. For a `.docx`
  resume that's a finding, not a tooling caveat: a parser will very likely do the same.

Present it:

```
show_component("table", {
  "columns": ["Item", "In the resume", "Survived extraction", "Note"],
  "rows": [["Acme Corp", "yes", "yes", ""],
           ["2019 - 2024", "yes", "NO", "inside a layout table"]]
}, title="What a parser reads")
```

---

## Part 2 — the rules checklist

Applied by reading the resume's own source (`get_artifact` on the HTML artifact). No tool
does this for you; you check it. Report **pass / fail / not checked** for each, with the
offending text quoted on a fail.

| # | Rule | How to check |
|---|------|--------------|
| 1 | **Single column** | No multi-column grid, no `float`, no `position: absolute`, no `<table>` used for layout |
| 2 | **Standard headings** | Summary / Skills / Experience / Education / Certifications / Publications. Not "Where I've Been" |
| 3 | **No tables or text boxes for layout** | Search the source for `<table`, `<img`, absolutely-positioned blocks |
| 4 | **Contact details in the body** | Not in a running header or footer, not in an image |
| 5 | **One date format throughout** | Pick `Mon YYYY - Mon YYYY` or `MM/YYYY - MM/YYYY` and hold it everywhere, including Education |
| 6 | **No ligature or private-use characters** | `font-variant-ligatures: none` is set, and the source has no U+FB00-FB06 / U+E000-F8FF |
| 7 | **File under ~1 MB** | From `save_file_artifact`'s `(mime, NN KB)` line |
| 8 | **Page count** | 2 pages (1 for early career). Judge it against the budget in `job-application-assistant/cv-guide.md` and **confirm with the operator or the printed PDF** — you can't read page count out of extracted text, so don't claim it as verified when it isn't |
| 9 | **Real text, not an image** | If extraction returned almost nothing from a PDF, the resume is a picture — a guaranteed ATS zero |
| 10 | **Standard fonts, no web fonts** | A system stack ending in a generic family; nothing loaded over the network |

Present it:

```
show_component("table", {
  "columns": ["Check", "Result", "Detail"],
  "rows": [["Single column", "PASS", ""],
           ["Date format", "FAIL", "'2019-2024' in Experience, 'Jan 2019' in Education"]]
}, title="ATS rules")
```

---

## Part 3 — keyword coverage against the posting

Only meaningful with a specific job description. Without one, say so and skip it — a
generic "keyword score" is noise.

1. Pull required and preferred skills, tools and named responsibilities from the posting
   (the raw JD in the role packet, or `fetch_url` on the posting).
2. For each, check the resume text for the term **and** the obvious variants the posting
   uses ("K8s" / "Kubernetes", "ETL" / "data pipelines").
3. Classify:
   - **Covered in context** — it appears in an experience bullet, where it carries proof.
   - **Skills list only** — present, but unevidenced. A human reader discounts these.
   - **Missing but true** — the profile supports it and the resume just doesn't say it.
     This is the actionable bucket: add it, with evidence.
   - **Missing and not true** — a real gap. **Name it; never add it.** Hand it to the
     `upskill` skill.

Present it:

```
show_component("table", {
  "columns": ["Requirement", "Priority", "Coverage", "Where"],
  "rows": [["Kubernetes", "required", "Covered in context", "Acme, bullet 2"],
           ["Terraform", "required", "Missing (not in profile)", "gap — do not add"]]
}, title="Keyword coverage")
```

And a one-glance summary:

```
show_component("keyvalue", {
  "items": [{"label": "Parser view", "value": "3 items lost"},
            {"label": "Rules", "value": "8/10 pass"},
            {"label": "Required keywords", "value": "9/12 covered"},
            {"label": "Real gaps", "value": "Terraform, Go"}]
}, title="ATS check")
```

---

## Closing the check

Fix what's fixable in the artifact (`update_artifact` / `rewrite_artifact`), re-export into
the same file artifact (pass its `artifact_id`), and re-run Part 1 — a layout fix is only proven by the extraction changing. Report the real
gaps as gaps. **Never close an ATS check by adding a keyword the profile doesn't support**:
that's the exact failure this plugin's anti-fabrication rule exists to stop, and it's the
one an interviewer catches.

If `show_component` isn't available, print the same tables as markdown. If the artifact
plugin isn't available, only Part 2 and Part 3 can run, against whatever text you have —
say which parts ran.
