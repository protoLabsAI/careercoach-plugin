# Export — turning the artifact into a file they can attach

An HTML artifact is the working document. An application form wants a **file**. Three
routes get you one; they need different plugins, so check what you have before you promise
anything, and **name what's missing rather than silently switching route**.

Honour the operator's `render_format` setting: `html` → Route A or B, `docx` → Route C,
`latex` → the moderncv appendix in `job-application-assistant/cv-guide.md`.

---

## Route A — the operator prints it (always available)

The templates carry a real `@page` rule and `break-inside: avoid` on every entry, so the
Artifact panel's render and a print of it are the same document.

**Tell the operator exactly how, because the obvious way is wrong.** The panel shows an
html artifact inside a frame and has no print button of its own, so Cmd/Ctrl-P prints the
*console*, not the resume. Instead: **right-click inside the resume → "Print frame…"**
(Chrome / Edge; Firefox: "This Frame → Print Frame") → destination "Save as PDF", margins
"Default", "Background graphics" off (the templates use none). If their browser offers no
frame-print option, this route isn't available to them — go to Route B or C.

This works with **no extra plugin at all**, and today it is the PDF path that is actually
live. Say plainly that on this route *you* can't produce the file: the operator does, and
it lands in their Downloads, not in the Artifact panel. That means the parser-view half of
the ATS check (`ats-check.md`) can only run if they hand the PDF back — offer that.

---

## Route B — agent-produced PDF (`browser_pdf`)

**Needs:** the **execute_code** plugin (to write the HTML to disk) and the
**agent_browser** plugin (to print it), with the `agent-browser` binary on PATH. Both ship
disabled; enabling `execute_code` is a code-execution trust decision.

> `browser_pdf` arrives in protoAgent with **PR #3451** (agent_browser vendored into core).
> Until a core release includes it, this route does not exist: if `browser_pdf` isn't in
> your toolset, say exactly that — "the PDF tool `browser_pdf` isn't in this build yet; it
> ships with the agent_browser plugin" — and offer Route A or Route C.

The chain:

1. `get_artifact(<resume artifact id>)` — the HTML source.
2. `execute_code` — write that HTML to a file and print its absolute path.
   (The generic `write_file` can't help here: it only reaches managed fs projects, and the
   coach's workspace isn't one.)
3. `browser_open("file:///<absolute path to the html>")` — the browser needs a URL, and a
   local file is the one it can reach. If agent_browser is configured with
   `allowed_domains`, a `file://` URL may be refused; that's a config change for the
   operator, so report it rather than working around it.
4. `browser_pdf("<name>.pdf")` — Chrome's print-to-PDF. **`path` must be a filename or a
   relative path**; the file lands in agent_browser's own capture directory and an absolute
   path outside it is refused, not redirected. It returns the absolute path of the PDF.
5. `save_file_artifact("<that returned path>", title="<Name> — Resume — <Role> (PDF)")` —
   now the operator has a Download button, a text preview, and a version history. Pass the
   same `artifact_id` on a later export to make it v2 of the same file artifact rather than
   a new panel entry.

Then run the ATS check — the file artifact you just made is what makes part 1 of it
possible.

**Why the printed PDF matches the panel.** The panel injects the console's design-system
stylesheet into every html artifact (element rules on `body`, headings and links, plus a
theme background). A `file://` page printed by `browser_pdf` gets none of that — it is the
document exactly as written. The templates declare every property the panel would
otherwise supply (box model, font weight, line-height, letter-spacing, colours,
backgrounds), so both paths render the same thing. That's pinned by a test. If you
hand-author or restyle a shell, hold to the same rule, or the panel will show the operator
something the PDF isn't.

**Gotchas.** A file artifact can't be edited by `update_artifact` / `rewrite_artifact`;
you re-export and save a new version. If `save_file_artifact` says the file is too large,
that's the plugin's `max_blob_kb` — and a resume PDF over ~1 MB is an ATS problem anyway
(see `ats-check.md`), so shrink the document rather than raising the cap.

---

## Route C — DOCX (the Word file most ATS forms want)

**Needs:** the **cowork** plugin's `docx` skill, the **execute_code** plugin (it runs
`python-docx`), and a protoAgent **v0.108.0+** host — on the desktop app that's the floor,
because that's where `execute_code` gained its managed Python runtime, provisioned on first
use.

This route is already documented: read `job-application-assistant/cv-guide.md` for the
full procedure and the version floors. In short: `load_skill("docx")`, author the CV with
`python-docx` following `cv-guide.md` + `writing-style.md`, save it to disk, then
`save_file_artifact(path, title="<Name> — CV — <Role>")`.

Two things this skill adds on top:

- **Keep the DOCX ATS-safe the same way the templates are.** `python-docx` makes it easy
  to reach for a table for a two-column layout — don't. Single column, real heading
  styles, contact details in the body, no text boxes. The artifact plugin's own `.docx`
  text extractor reads **paragraphs only and drops table content**, which is a free preview
  of what a parser will do to a table-based layout.
- **The DOCX is a sibling of the HTML artifact, not a replacement.** The HTML artifact
  stays the editable master; the `.docx` is an export of a specific version. Say which
  version it came from.

If cowork or `execute_code` is off, name the one that's missing and fall back to Route A
or B — don't silently produce HTML and call it a Word file.

---

## What to tell the operator

Whichever route ran, close the loop with: which file exists, where it is, which artifact
version it was made from, and what the ATS check said about it. A resume they can't find
is a resume they won't send.
