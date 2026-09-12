# Import — parsing a resume the operator already has

The goal of this step is **not** a document. It is to get the facts out of an existing
resume and into the **operator profile**, so every later draft has one record to work
from. The imported resume is evidence; the profile is the record.

## Two ways in

### A. They drop it in the chat
Read what the host gives you. PDF text extraction works in current releases. `.docx`
extraction for chat attachments is merged into protoAgent core (**#3444**) but **not yet in
a release** — until one includes it, a `.docx` dropped in chat may not arrive as text. If it
doesn't, ask for the file's path and use B below (the artifact plugin extracts `.docx`
itself wherever python-docx is installed). If you get bytes you can't read rather than
text, say exactly that instead of guessing at the content.

### B. It's a file on disk
Ask for the absolute path (an old resume in their role-packet workspace, in Downloads,
anywhere). Then use the artifact plugin as a reader:

```
save_file_artifact("/abs/path/Resume 2024.pdf", title="Imported resume (source)")
  → "Saved file artifact <id> → v1 …"
get_artifact("<id>")
  → the artifact's current source, which for a file artifact IS the extracted text
```

`save_file_artifact` extracts a text preview when it stores the file, and `get_artifact`
hands that text back to you. That is the whole parser — no new tool, and the operator
gets the original filed in the Artifact panel with a Download button as a side effect.
That costs one slot in the panel's bounded history (`master-and-tailor.md`, *Eviction*):
once the facts are in the profile, offer to `delete_artifact` it.

**What extraction covers and where it stops:**

- `.pdf` (via pypdf), `.docx` (via python-docx), `.xlsx`, `.pptx`; `.txt` / `.md` /
  `.html` decode verbatim.
- **PDF stops at 50 pages** and appends `… (more pages — download for all)`.
- The stored preview is **clipped at 64 KB** (the artifact plugin's `max_preview_kb`) and
  appends `… (preview truncated — download the file for the full content)`.
- **`.docx` extraction reads paragraphs only — text inside a Word table is dropped.** If a
  `.docx` resume comes back suspiciously empty or missing its whole experience section,
  that is the signal: the resume is built in a table. Tell the operator, because a table
  layout is also the single most common reason a resume parses badly in an ATS (see
  `ats-check.md`).
- If a note comes back instead of text (a missing extractor library), say which one and
  ask them to paste the resume instead.
- `.doc`, `.rtf`, `.odt` and other formats have no extractor: they come back as
  `(binary file · <mime> · <n> bytes — download to open)`. Say so, and ask for a PDF, a
  `.docx`, or the pasted text.

If the artifact plugin isn't available, ask them to paste the text. Do not try to read a
PDF with a fetch tool and conclude it can't be read.

*(`knowledge_ingest` is a different job: it makes a document searchable later. Use it when
the operator wants the resume remembered, not when you need to read it now — the two are
complementary, not alternatives.)*

## Structuring what you read

Turn the extracted text into the profile's shape. The rule, without exception:

> **Every employer, job title, date, degree, certification, email address, phone number
> and URL you record must appear in the source text you actually read.**

Not inferred, not normalized into something that reads better, not completed from what
you know about that company. Concretely:

- **Dates.** Record the range as written. If the source says `2019 – 2024`, don't invent
  months. If a date is ambiguous or garbled by extraction, ask — a wrong date on a resume
  is a reference-check failure.
- **Employers and titles.** Verbatim. "Senior Software Engineer II" doesn't become
  "Senior Engineer".
- **Contact details.** An email, phone or URL is copied character for character, then read
  back to the operator for confirmation. Extraction is exactly where a digit gets lost.
- **Numbers.** A metric with no baseline in the source stays as written; don't add one.
- **Anything unreadable** goes on an explicit list you show the operator, rather than
  being quietly dropped or quietly guessed.

Then present, before writing anything: what you found, what you'll add to the profile,
what conflicts with what's already there, and what you couldn't read. Ask them to confirm.

## Where it lands

The **profile**, never a second store, and never a new markdown file beside the resume.

- **New facts** → `careercoach_update_profile(field, content)`. Appends by default. Fields:
  `roles`, `education`, `skills`, `do_not_claim`, `stories`, `notes`, or an identity field.
- **Corrections** → the same tool with `mode="replace"`. Read the current value with
  `careercoach_get_profile(field)` first, edit it, then replace — that is how you fix a
  wrong date without losing the rest of the section.
- **A whole `Resume/Experience.md` the operator keeps** → `careercoach_import_experience`,
  which previews the exact lines it would add and hands back a `preview_id`; apply with
  `apply=true` and that id once they confirm. Use this rather than retyping the file
  through `careercoach_update_profile`.

After the import, `careercoach_read_profile("experience")` should tell the whole story. If
it doesn't, the import isn't finished — don't move on to building a resume.

## Then what

With the profile current, go build the master resume (`master-and-tailor.md`). If the
operator keeps the imported file artifact, it is provenance, **not** the resume: the moment
the master artifact exists, that is the current document, and the imported PDF is history.
