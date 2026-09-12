# The master resume, and tailoring it per role

## Why one artifact

A resume kept as chat text or as markdown files multiplies. Six weeks in there are five
`resume.md` variants, three of them stale, and nobody can say which one was sent to which
company. An artifact fixes that structurally: it has one id, a version history, a live
preview, and edits land **in place**.

So: **one master artifact**, and **one artifact per tailored variant**. Nothing else is a
resume.

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
   `kind` must be `"html"` — that is one of the kinds the artifact plugin renders
   (`html`, `svg`, `mermaid`, `react`, `markdown`). Pass the complete, self-contained
   document: one file, inline `<style>`, no external fonts or images.
5. **Check it rendered:** `check_artifact(<id>)`. A clean verdict means the panel drew it.
   "No result yet" usually means the Artifact panel is closed — ask them to open it rather
   than calling again in a loop.
6. **Record the id.** Put it in the profile so the next session finds it:
   `careercoach_update_profile("notes", "Master resume artifact: <id> (html, <template>)")`.
   Without this the next conversation makes a second master, which is the problem all over
   again.

## Edit it

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

Never re-paste the resume into chat as "the new version". The artifact is the version.

## Tailor for a role

A tailored resume is a **new artifact**, not an edit of the master. The master stays the
canonical full history; the variant is aimed at one posting and can be cut hard.

1. `get_artifact(<master id>)` — start from the current master, not from an old variant.
2. Tailor per `cv-guide.md`: reframe emphasis, reorder skills to the posting, cut by
   relevance. **No new facts** — if the posting needs something the profile doesn't have,
   that's a gap to name, not a line to write. `do_not_claim` is a hard stop.
3. ```
   show_artifact(kind="html", code=<the tailored document>, title="<Name> — Resume — <Company> <Role>")
   ```
   Title it so `list_artifacts` reads like a pipeline. This is also the answer to "which
   one did I send them".
4. `check_artifact(<id>)`, then show the operator what changed from the master and why.

## Filing it in a role packet

When this is part of a `role-packet` flow, the packet's `tailored resume.md` becomes a
**snapshot with provenance**, not a competing document:

```
careercoach_write_artifact(company, role, "tailored-resume", content, req)
```

where `content` opens with:

```
> Source: artifact <id>, version <n> — edit the artifact, then re-file this snapshot.
> Rendered from template: <template name>
```

followed by the resume as plain readable markdown. The packet stays self-contained (it is
assembled into `role packet.md` and read offline), and the header says plainly where edits
belong. **The artifact is edited; the file is refreshed.** Never the other way round —
that's how the copies started diverging in the first place.

If the operator already has loose resume markdown files in the workspace from before this
flow, don't delete them. Offer to import the best one (`import.md`), build the master from
it, and tell them the old files are now history so they know which is live.

## Restyling

Swapping templates is a `rewrite_artifact` with the same content in a different shell.
Keep the ATS-critical CSS contract from `SKILL.md` — a restyle that introduces two
columns, a table layout, an icon font or a header-bar contact block is a regression, no
matter how it looks in the panel. When in doubt, run the ATS check afterwards.
