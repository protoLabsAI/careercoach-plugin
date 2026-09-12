# The master resume, and tailoring it per role

## Why one artifact

A resume kept as chat text or as markdown files multiplies. Six weeks in there are five
`resume.md` variants, three of them stale, and nobody can say which one was sent to which
company. An artifact fixes that structurally: it has one id, a version history, a live
preview, and edits land **in place**.

So: **one master artifact**, and **one artifact per tailored variant**. Those are the
working documents.

## Where the durable record lives

The Artifact panel is a working surface with a bounded memory, not an archive. Keep every
layer below current and the panel can forget anything without losing the resume:

| Layer | Holds | Where | Survives eviction |
|-------|-------|-------|-------------------|
| Operator profile | the facts — roles, dates, skills, `do_not_claim` | the profile | always |
| Registry lines in `notes` | each resume's artifact id, template and file paths | the profile | always |
| Master copy of record | the approved master's exact HTML — its wording, order and the operator's edits | `<workspace>/Resume/Master Resume.html` | always (needs `execute_code` to write) |
| Variant snapshot | each variant's wording, stamped with artifact id, version, template and export ids | `<role folder>/tailored resume.md` | always |
| Artifacts | the rendered, editable, versioned documents | the Artifact panel | until evicted |

**Without `execute_code`, the master has no copy of record.** The profile keeps the facts,
but the approved master's wording — the summary, the bullets, their order, the operator's
edits — lives only in the artifact and is lost on eviction unless the operator saves it
themselves with the panel's **Download** button. Say so when the master is approved, and
offer that.

## The registry in `notes`

Read it with `careercoach_get_profile("notes")` — read-only, and the first thing to do in any
session that touches a resume. One line per resume:

```
Master resume: artifact <id> · template <name> · copy of record <absolute path> · docx <id|none> · pdf <id|none>
Resume — <Company> <Role>: artifact <id> · snapshot <absolute path>
```

When an id or path changes, read `notes`, change that line, and write the whole section back
with `careercoach_update_profile("notes", <the full notes>, mode="replace")`. A variant's export
ids live in its snapshot header, beside the wording they were exported from. `notes` appears
in the always-on profile block only as an index entry, so the registry costs nothing per
model call.

## Build the master

1. **Read the facts first.** `careercoach_read_profile("experience")` plus
   `careercoach_get_profile("skills")` and `("do_not_claim")`. Thin profile → say so and
   offer `/setup-coach`. Don't build a resume from an empty record.
2. **Read the discipline.** `job-application-assistant/cv-guide.md` for section order and
   the two-page budget; `writing-style.md` before any bullet.
3. **Pick a template** from the catalogue in `SKILL.md` and fill it. Delete sections the
   profile has nothing for. No placeholders survive into a rendered resume.
4. **Create it:**
   ```
   show_artifact(kind="html", code=<the whole filled template>, title="<Name> — Master Resume")
   ```
   `kind` must be `"html"` — one of the kinds the artifact plugin renders (`html`, `svg`,
   `mermaid`, `react`, `markdown`). Pass the complete, self-contained document: one file,
   inline `<style>`, no external fonts or images.
5. **Check it rendered:** `check_artifact(<id>)`. A clean verdict means the panel drew it.
   "No result yet" usually means the Artifact panel is closed — ask them to open it rather
   than calling again in a loop.
6. **Find the workspace.** `careercoach_init_workspace` answers with the workspace's absolute
   path. On a seeded workspace it changes nothing; on a fresh one it lays down the fill-in
   templates, which you'd want anyway.
7. **When the operator approves it, write its copy of record.** `get_artifact(<id>)`, then
   `execute_code` to write that exact source to `<workspace>/Resume/Master Resume.html` — an
   absolute path; a relative one fails (see `export.md`). Do it again after every approved
   edit. No `execute_code`: tell them the wording has no copy of record and offer the
   Download button.
8. **Record it** as the `Master resume:` line in `notes`. Without this the next conversation
   makes a second master, which is the problem all over again.

## Eviction — verify the id before every edit

The artifact plugin keeps its `history` setting's worth of artifacts — **20 by default,
across the whole instance**, not just resumes: every chart, page and file from any
conversation counts — and drops the least recently touched past that. It also keeps at
most `max_versions` versions per artifact (50 by default), trimming the oldest.

So a recorded id can point at nothing. Before editing a resume artifact:

1. `get_artifact(<recorded id>)`, with the id from `notes`. If it answers "No artifact to
   read", it was evicted. **Tell the operator** — don't silently rebuild.
2. **The master:** read its copy of record back — `save_file_artifact("<copy of record path>")`
   then `get_artifact(<that id>)` returns the exact HTML — rebuild it with
   `show_artifact(kind="html", code=<that source>)`, then `delete_artifact(<the temporary file artifact>)`
   so the recovery doesn't push something else out, and update the `notes` line. With no copy
   of record, the facts can be rebuilt from the profile but **the approved wording is gone**:
   say exactly that, rebuild from the profile in the recorded template, and have the operator
   approve it again.
3. **A variant:** the same, from the snapshot path in `notes`. Read it back with
   `save_file_artifact` + `get_artifact`, rebuild the variant in the template the snapshot
   names, `delete_artifact` the temporary read, re-file the snapshot with the new artifact id,
   and update `notes`. If `notes` has no path for it, `careercoach_list_roles` shows which
   roles have packets (read-only, but it doesn't print paths) — ask the operator where the
   snapshot lives rather than guessing a folder name.
4. A snapshot's `version <n>` can be gone (trimmed past `max_versions`) while the artifact
   survives. The snapshot's own text is still the record of what was filed.

Keep the panel lean so this rarely happens: reuse the recorded export ids for every
re-export and ATS re-save (`export.md`), and delete temporary file artifacts once you've read
them. If the operator works many roles at once, the artifact plugin's **"Artifacts kept"**
setting (`history`, under Settings → Plugins → Artifact) raises the cap. Worth suggesting —
never a substitute for the copies of record.

## Edit it

Verify the id first (above), then:

| You want to | Call | Result |
|-------------|------|--------|
| Change a bullet, a date, a heading | `update_artifact(old_string, new_string, artifact_id)` | new version, targeted |
| Swap template / restyle / large rewrite | `rewrite_artifact(code, artifact_id=…)` | new version, whole document, kind preserved |
| See what's in there now | `get_artifact(artifact_id)` | the current source |
| Find it again | `list_artifacts` | ids, kinds, titles, version counts |

`old_string` must match the stored source exactly and match exactly once — read it back
with `get_artifact` and copy from what you read, don't reconstruct it from memory.

**Compose, then save once.** Three full-body writes to the same artifact inside ten
minutes gets you a batching note from the plugin, and rightly: every `rewrite_artifact`
round-trips the entire document. Work out the whole change, then write it. Targeted
`update_artifact` calls are exempt and are the right tool for small fixes.

**Re-file the snapshot after every edit** to a variant — `careercoach_write_artifact` with
the new version in its header — and rewrite the master's copy of record after every approved
master edit. An edit that exists only in the artifact is an edit the next eviction deletes.

Never re-paste the resume into chat as "the new version". The artifact is the version.

## Tailor for a role

A tailored resume is a **new artifact**, not an edit of the master. The master stays the
canonical full history; the variant is aimed at one posting and can be cut hard.

1. `get_artifact(<master id>)` — confirm it still exists (see *Eviction*) and start from the
   current master, not from an old variant.
2. Tailor per `cv-guide.md`: reframe emphasis, reorder skills to the posting, cut by
   relevance. **No new facts** — if the posting needs something the profile doesn't have,
   that's a gap to name, not a line to write. `do_not_claim` is a hard stop.
3. ```
   show_artifact(kind="html", code=<the tailored document>, title="<Name> — Resume — <Company> <Role>")
   ```
   Title it so `list_artifacts` reads like a pipeline. This is also the answer to "which
   one did I send them".
4. `check_artifact(<id>)`, then show the operator what changed from the master and why.
5. **File its snapshot now** — inside a role-packet flow or not. `careercoach_write_artifact`
   creates the role folder if it isn't there yet (next section). Add the variant's
   `Resume — <Company> <Role>:` line to `notes` with the snapshot path the tool returned.

## The snapshot

```
careercoach_write_artifact(company, role, "tailored-resume", content, req)
```

It answers `Wrote tailored resume.md → <absolute path>`. That path goes in `notes`, and its
folder is where this role's exports are written (`export.md`). `content` opens with:

```
> Source: artifact <id>, version <n> — edit the artifact, then re-file this snapshot.
> Template: <template name>
> DOCX export: file artifact <id, or none yet>
> PDF export: file artifact <id, or none yet>
> This snapshot is the copy of record: if that artifact has been evicted from the
> Artifact panel, rebuild it from this text.
```

followed by the resume as plain readable markdown. In a role packet it is assembled into
`role packet.md` and read offline, so the packet stays self-contained; the header says where
edits belong and which file artifacts to reuse; and the text outlives the artifact. **The
artifact is edited; the file is refreshed** — after every edit, and after every first export
(to record its id). Never the other way round — that's how the copies started diverging in
the first place.

If the operator already has loose resume markdown files in the workspace from before this
flow, don't delete them. Offer to import the best one (`import.md`), build the master from
it, and tell them the old files are now history so they know which is live.

## Restyling

Swapping templates is a `rewrite_artifact` with the same content in a different shell.
Keep the ATS-critical CSS contract from `SKILL.md` — a restyle that introduces two
columns, a table layout, an icon font or a header-bar contact block is a regression, no
matter how it looks in the panel. When in doubt, run the ATS check afterwards.
