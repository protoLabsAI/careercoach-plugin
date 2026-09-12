# Export — turning the artifact into a file they can attach

An HTML artifact is the working document. An application form wants a **file**. Three
routes get you one, in this order of preference. They need different plugins, so check
what you have before you promise anything, and **name what's missing rather than silently
switching route**.

Honour the operator's `render_format` setting: `docx` → Route 1, `html` → Route 2 (Route 3
while `browser_pdf` isn't available), `latex` → the moderncv appendix in
`job-application-assistant/cv-guide.md`.

**One file artifact per export type per resume, not one per save.** Every
`save_file_artifact` call without an `artifact_id` adds a panel entry, and the panel keeps only
20 by default across the whole instance, evicting the least recently touched — resumes
included. Save a resume's first DOCX (or PDF) without an id, **record the id it returns** —
in the variant's snapshot header (`DOCX export:` / `PDF export:`) or in the comment at the top
of the master's copy of record — and pass that same id on every later export and ATS re-save of
that type. Title every export with the resume's own title plus `(DOCX)` or `(PDF)`, so
`list_artifacts` finds it while it exists (an id only matters while it does). Never
reuse a DOCX id for a PDF or the other way round: the plugin accepts it and mixes file types
in one version history. See `master-and-tailor.md`, *Eviction*.

**Always write to an absolute path.** The server runs with its working directory at `/`,
which is read-only on the desktop app, so a relative save fails (`Read-only file system`)
and `save_file_artifact` then finds nothing. Write into the variant's role folder — the
path `careercoach_list_roles` prints for it — or into `<workspace>/Resume/` for the master, and pass that same absolute path to
`save_file_artifact`.

---

## Route 1 — DOCX (works wherever cowork + execute_code are enabled; what most ATS forms want)

**Is it available?** `execute_code` in your toolset **and** `docx` in your available skills (or `load_skill("docx")` succeeds). cowork registers no tools, only skills, so don't look for it among your tools.

**Needs:** the **cowork** plugin's `docx` skill, the **execute_code** plugin (it runs
`python-docx`), and a protoAgent **v0.108.0+** host — on the desktop app that's the floor,
because that's where `execute_code` gained its managed Python runtime, provisioned on first
use.

The procedure is already documented: read `job-application-assistant/cv-guide.md` for the
full steps and the version floors. In short: `load_skill("docx")`, author the CV with
`python-docx` following `cv-guide.md` + `writing-style.md`, save it to an **absolute** path in
the role folder (cowork's skill defaults to a project folder or `output_dir`; neither fits
the coach's workspace, so pass the path explicitly), then
`save_file_artifact("<that same absolute path>", title="<Name> — Resume — <Company> <Role - Req> (DOCX)", artifact_id=<the DOCX export id from the snapshot header, if any>)`.

Two things this skill adds on top:

- **Keep the DOCX ATS-safe the same way the templates are.** `python-docx` makes it easy
  to reach for a table for a two-column layout — don't. Single column, real heading
  styles, contact details in the body, no text boxes. The artifact plugin's own `.docx`
  text extractor reads **paragraphs only and drops table content**, which is a free preview
  of what a parser will do to a table-based layout.
- **Set the page size you mean.** A4 or Letter is a property of the document here, so the
  A4 templates' intent carries through properly — which is why this is the route for A4.
- **The DOCX is a sibling of the HTML artifact, not a replacement.** The HTML artifact
  stays the editable master; the `.docx` is an export of a specific version. Say which
  version it came from.

If either half of the availability test fails, name which — `execute_code` missing from your
toolset, or `docx` missing from your available skills (the cowork plugin) — and offer Route 2
or 3. Don't silently produce HTML and call it a Word file.

---

## Route 2 — agent-produced PDF (`browser_pdf`)

> **Not in any release yet.** `browser_pdf` arrives with protoAgent **PR #3451**
> (agent_browser vendored into core). If `browser_pdf` isn't in your toolset, say exactly
> that — "the PDF tool `browser_pdf` isn't in this build yet; it ships with the
> agent_browser plugin" — and offer Route 1 or Route 3.

**Needs:** the **execute_code** plugin (to write the HTML to disk) and the
**agent_browser** plugin (to print it), with the `agent-browser` binary on PATH. Both ship
disabled; enabling `execute_code` is a code-execution trust decision.

The chain:

1. `get_artifact(<resume artifact id>)` — the HTML source.
2. `execute_code` — write that HTML to an **absolute** path in the role folder (never a
   relative one) and print it.
   (The generic `write_file` can't help here: it only reaches managed fs projects, and the
   coach's workspace isn't one.)
3. `browser_open("file:///<absolute path to the html>")` — the browser needs a URL, and a
   local file is the one it can reach. If agent_browser is configured with
   `allowed_domains`, a `file://` URL may be refused; that's a config change for the
   operator, so report it rather than working around it.
4. `browser_pdf("<name>.pdf")` — Chrome's print-to-PDF. **`path` must be a filename or a
   relative path**; the file lands in agent_browser's own capture directory and an absolute
   path outside it is refused, not redirected. The result is the browser tool's output
   followed by a line `Saved to <absolute path>`, or a line starting `Error:`. **Take the
   path from the `Saved to` line** — don't pass the whole result on.
5. `save_file_artifact("<that path>", title="<Name> — Resume — <Company> <Role - Req> (PDF)", artifact_id=<the PDF export id from the snapshot header, if any>)`
   — now the operator has a Download button, a text preview, and a version history. If this
   was the first PDF, record its id in the snapshot header and re-file the snapshot.

Then run the ATS check on this file artifact — it is what makes part 1 of the check possible.

**It prints on US Letter.** `browser_pdf` passes no page options and the agent-browser CLI
has no page-size flag, so the PDF comes out at US Letter (612 × 792 pt) whatever the
template's `@page` says. `classic-letter` is unaffected. The two A4 templates come out on a
page that is slightly wider and about 6% shorter than they were laid out for: lines re-wrap
and a resume that exactly filled two A4 pages can spill onto a third. **Say this before
exporting an A4 template this way**, and offer Route 1 (DOCX, set to A4) or Route 3 (pick A4
in the print dialog) when real A4 matters.

**The PDF versus what the panel showed.** The panel injects the console's design-system
stylesheet into every html artifact, and a `file://` page printed by `browser_pdf` gets none
of it. The templates declare every property that stylesheet sets, so fonts, weights and
spacing agree between the two. The panel still isn't a faithful print preview: it applies the
console's theming (body margin 0, design-system typography), and a live theme change writes
the theme's text colour and background onto `<body>` as inline styles, overriding the page's
own colours. **Treat the PDF, not the panel, as what the employer gets.**

**Gotchas.** A file artifact can't be edited by `update_artifact` / `rewrite_artifact`; you
re-export and save a new version with the same `artifact_id`. If `save_file_artifact` says
the file is too large, that's the plugin's `max_blob_kb` — and a resume PDF over ~1 MB is an
ATS problem anyway (see `ats-check.md`), so shrink the document rather than raising the cap.

---

## Route 3 — the operator downloads the HTML and prints it (fallback)

Needs no extra plugin. The Artifact panel's **Download** button works on an html artifact
too: it saves the current version's source as `artifact-<id>-v<n>.html`. Opened directly in
a browser, that file renders on its own — no injected stylesheet — and the browser's print →
"Save as PDF" lays it out on the template's `@page`. Check the paper size in the print
dialog before saving; for the A4 templates, choose A4.

Be straight about the limits:

- **This is the operator's job, not yours.** The file lands in their downloads, not in the
  Artifact panel, so the parser-view part of the ATS check needs the PDF handed back.
- **Printing from inside the console is not this.** Cmd/Ctrl-P there prints the console, not
  the resume — the resume sits in a nested frame. Send them to the Download button.
- **Unverified in the desktop app.** The desktop app has no download handling of its own, so
  the button may do nothing there. If nothing lands, this route isn't available to them —
  say so and offer Route 1.

---

## What to tell the operator

Whichever route ran, close the loop with: which file exists, where it is, which artifact
version it was made from, and what the ATS check said about it. A resume they can't find
is a resume they won't send.
