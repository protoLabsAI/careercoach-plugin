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
| Master copy of record | the approved master's exact HTML — wording, order, the operator's edits — with a comment recording its ids | `<workspace>/Resume/Master Resume.html` | always (needs `execute_code` to write) |
| Variant snapshot | each variant's wording, stamped with artifact id, version, template and export ids | `<role folder>/tailored resume.md` | always |
| Artifacts | the rendered, editable, versioned documents | the Artifact panel | until evicted |

**The resume's bookkeeping never goes into the profile.** The profile is the operator's record
of facts, and its free-form sections are theirs. Artifact ids and file paths live in the
resume's own files.

**Without `execute_code`, the master has no copy of record.** The profile keeps the facts,
but the approved master's wording — the summary, the bullets, their order, the operator's
edits — lives only in the artifact and is lost on eviction unless the operator saves it
themselves with the panel's **Download** button. Say so when the master is approved, and
offer that.

## Finding things in a later session

- **A live artifact:** `list_artifacts`, by its title. The titles this skill requires —
  `<Name> — Master Resume`, `<Name> — Resume — <Company> <Role>`, and each export's title with
  `(DOCX)` or `(PDF)` on the end — are how you find them. An id only matters while its artifact
  exists, and while it exists it's in that list.
- **The workspace:** `careercoach_init_workspace` answers with its absolute path. On a seeded
  workspace it changes nothing; on a fresh one it lays down the fill-in templates, which you'd
  want anyway. The master's copy of record is `<workspace>/Resume/Master Resume.html`.
- **A role's folder:** `careercoach_list_roles` prints each role's absolute folder path. The
  variant's snapshot is `tailored resume.md` in it, and its exports are written beside it.

## Build the master

1. **Read the facts first.** `careercoach_read_profile("experience")` plus
   `careercoach_get_profile("skills")` and `("do_not_claim")`. Thin profile → say so and
   offer `/setup-coach`. Don't build a resume from an empty record.
2. **Read the discipline.** `job-application-assistant/cv-guide.md` for section order and
   the two-page budget; `writing-style.md` before any bullet.
3. **Pick a template** from the catalogue in `SKILL.md` and fill it. Delete sections the
   profile has nothing for. No placeholders survive into a rendered resume.
4. **Create it,** titled exactly so the next conversation finds it instead of making a
   second one:
   ```
   show_artifact(kind="html", code=<the whole filled template>, title="<Name> — Master Resume")
   ```
   `kind` must be `"html"` — one of the kinds the artifact plugin renders (`html`, `svg`,
   `mermaid`, `react`, `markdown`). Pass the complete, self-contained document: one file,
   inline `<style>`, no external fonts or images.
5. **Check it rendered:** `check_artifact(<id>)`. A clean verdict means the panel drew it.
   "No result yet" usually means the Artifact panel is closed — ask them to open it rather
   than calling again in a loop.
6. **When the operator approves it, write its copy of record.** `get_artifact(<id>)`, then
   `execute_code` writes this line followed by that exact source to
   `<workspace>/Resume/Master Resume.html` (an absolute path — a relative one fails, see
   `export.md`):
   ```
   <!-- careercoach master · artifact <id> · template <name> · docx <id|none> · pdf <id|none> -->
   ```
   Write it again after every approved edit, and after the master's first DOCX or PDF export
   (to record that id). No `execute_code`: tell them the wording has no copy of record and
   offer the Download button.

## Eviction — verify it exists before every edit

The artifact plugin keeps its `history` setting's worth of artifacts — **20 by default,
across the whole instance**, not just resumes: every chart, page and file from any
conversation counts — and drops the least recently touched past that. It also keeps at
most `max_versions` versions per artifact (50 by default), trimming the oldest.

So a resume artifact can vanish between conversations. Before editing one:

1. Find it with `list_artifacts` by its title, then `get_artifact(<id>)`. If it isn't in the
   list, or `get_artifact` answers "No artifact to read", it was evicted. **Tell the
   operator** — don't silently rebuild.
2. **The master:** read its copy of record back — `save_file_artifact("<workspace>/Resume/Master Resume.html")`
   then `get_artifact(<that id>)` returns the exact HTML — rebuild it with
   `show_artifact(kind="html", code=<that source>)`, then `delete_artifact(<the temporary file artifact>)`
   so the recovery doesn't push something else out, and rewrite the copy of record with the
   new id. With no copy of record, the facts can be rebuilt from the profile but **the approved
   wording is gone**: say exactly that, rebuild from the profile, and have the operator approve
   it again.
3. **A variant:** the same, from `tailored resume.md` in the folder `careercoach_list_roles`
   prints for that role. Read it back with `save_file_artifact` + `get_artifact`, rebuild the
   variant in the template its header names, `delete_artifact` the temporary read, and re-file
   the snapshot with the new artifact id.
4. A snapshot's `version <n>` can be gone (trimmed past `max_versions`) while the artifact
   survives. The snapshot's own text is still the record of what was filed.

Keep the panel lean so this rarely happens: reuse the recorded export ids for every
re-export and ATS re-save (`export.md`), and delete temporary file artifacts once you've read
them. If the operator works many roles at once, the artifact plugin's **"Artifacts kept"**
setting (`history`, under Settings → Plugins → Artifact) raises the cap. Worth suggesting —
never a substitute for the copies of record.

## Edit it

Verify it exists first (above), then:

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

1. Find the master (`list_artifacts`), confirm it still exists (see *Eviction*), and start
   from its current source — not from an old variant.
2. Tailor per `cv-guide.md`: reframe emphasis, reorder skills to the posting, cut by
   relevance. **No new facts** — if the posting needs something the profile doesn't have,
   that's a gap to name, not a line to write. `do_not_claim` is a hard stop.
3. ```
   show_artifact(kind="html", code=<the tailored document>, title="<Name> — Resume — <Company> <Role>")
   ```
   Title it so `list_artifacts` reads like a pipeline. This is also the answer to "which
   one did I send them".
4. `check_artifact(<id>)`, then show the operator what changed from the master and why.
5. **File its snapshot now** — inside a role-packet flow or not (next section).

## The snapshot

```
careercoach_write_artifact(company, role, "tailored-resume", content, req)
```

**One folder per role, across both flows.** The folder is named from `role` and `req`, so
pass the posting's requisition id whenever it has one — exactly as the role-packet flow does.
If `careercoach_list_roles` already shows a folder for this company and role, reuse it: pass
the same `req` it was filed under (empty if it has none), even if you've since learned the
req. Otherwise one role ends up with two folders. Filing a snapshot creates the folder if it
isn't there, so the role then shows in `careercoach_list_roles` as a started packet (for
example `1/11 artifacts`) — tell the operator that's where the resume is filed, not that a
full packet is under way.

The tool answers `Wrote tailored resume.md → <absolute path>` (or `Updated tailored resume.md → <absolute path>` on a re-file).
That folder is where this role's exports are written (`export.md`). `content` opens with:

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
