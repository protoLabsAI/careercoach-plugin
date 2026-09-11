---
name: setup-coach
description: >-
  First-run companion — work out what's already known about the operator, fill only the real gaps,
  and record it as a profile the coach carries into every future turn. Triggers: "/setup-coach",
  "set me up", "get started", "onboard me", or any session where the operator profile is thin.
user_facing: true
slash: setup-coach
tools: [careercoach_get_profile, careercoach_update_profile, careercoach_import_experience, careercoach_export_experience, careercoach_read_profile, careercoach_init_workspace, memory_recall, knowledge_ingest, list_skills, load_skill]
---

# First conversation

The coach can't do its job without knowing who it works for, and it must never invent the
answer. This skill fills that gap once, and records it as a **profile** that gets injected into
every future turn. The profile is the single source of truth: every draft, score and interview
answer reads it, and nothing reads a workspace `Resume/Experience.md` directly.

**Harvest before you ask.** You almost certainly already know things. Asking an operator for
their own name when it's sitting in memory is the fastest way to lose their trust, and it's the
failure this skill exists to prevent. Interrogation is the last resort, not the method.

## 0. What do you already know?

Before your first message, gather silently. None of this needs permission:

- `careercoach_get_profile` — the completeness picture. Anything under **known** is settled;
  only **missing** is in play. If it's already complete, skip to step 4.
- `careercoach_import_experience()` — if they keep their own `Resume/Experience.md`, this
  previews what it would add to the profile (the template's hint text and anything already
  recorded are skipped) and ends with a `preview_id`. That's a prior setup worth keeping: show them
  the preview and, once they confirm, call it again with `apply=true` and that `preview_id` —
  rather than asking it all again. If they edit the file later, re-import: only the new lines are
  added. Their file stays theirs, nothing writes over it.
  `careercoach_read_profile("story-bank")` may hold their STAR stories too.
- `memory_recall` for the operator's profile, background, abilities and voice.
- Look at what's around: existing CVs or resumes in the workspace, saved artifacts, notes.
- If they've ever shared a resume link or file, `knowledge_ingest` it — that handles PDFs and
  URLs properly. Don't try to read a PDF with a fetch tool and conclude you can't.

## 1. Open by showing what you found

Lead with what you already have, not with a question. Something like: *"Here's what I already
know about you — name, location, contact, and your last two roles from the CV in your workspace.
Three things are missing: work authorization, your story bank, and the list of claims you never
want me to make. Want to fill those in now, or go straight to work?"*

This does three things at once: proves you did the reading, makes the gaps concrete, and gives
them the option to leave. If they'd rather work, **go work** — you can fill gaps as they come up.

## 2. Fill the gaps, in as few questions as possible

Only ask about what's genuinely missing. Batch related items into one message rather than
serialising them; asking for email, phone, LinkedIn and portfolio in four separate turns is the
behaviour that got a real first run abandoned halfway through. Two or three focused exchanges
should finish most profiles.

Record each field as it's settled with `careercoach_update_profile(field, content)`. Sections
**append** by default, so record roles (or stories) one at a time as each is settled and every
one is kept. To **correct** something already recorded, read the section with
`careercoach_get_profile(field)`, fix it, and write the whole section back with `mode="replace"`
— appending a correction leaves the wrong line beside it. Identity facts hold one value each and
are set outright: for `contact` or `headlines`, pass the whole line.

| field | what goes in it |
|---|---|
| `name` `location` `work_auth` `contact` `headlines` | short facts, one line each |
| `roles` | most recent first: scope, what was genuinely *theirs*, impact **with the baseline**, stack, a checkable proof |
| `education` | only what's relevant and verifiable |
| `skills` | three honest levels: can lead on, independent, familiar-with-supervision |
| `do_not_claim` | the lines that would fail an interview backtrack **for them specifically** |
| `stories` | two or three STAR stories covering range: delivery, leadership or conflict, a recovered failure |
| `notes` | targets, industries to avoid, NDAs and sensitivities and how to handle them |

Saving per field means an abandoned interview still leaves everything up to that point.

Push where a claim is soft. "Improved performance" is not a metric; ask for the number and the
baseline. If they can't source it, write what's true instead and say so. Catching it here is
cheaper than a hiring manager catching it in the room.

**`do_not_claim` is the highest-value field in the profile.** It's injected in full on every
future turn and it's the only thing standing between an eager draft and a claim they can't
defend. Ask for it directly: *"what would you not want me to imply on your behalf?"* Adding a
line is always allowed; removing or rewording one is refused unless you pass
`confirm_removal=true`, which you do only when they've explicitly asked to drop that hard stop.

## 3. Voice — before anything gets written in their name

Check `list_skills` for `my-writing-style` (from cowork's `writing-voice` skill). If it exists,
`load_skill` it and say you'll use it. If not, ask for one or two samples of writing they
consider theirs and pull out the register: sentence length, formality, what they'd never say,
whether they open with the point.

Record it under `notes` or leave it to the voice memory domain, but **do this before drafting**,
not after. Voice rules that arrive after the document is written have already failed.

## 4. Prove it, then get out of the way

Offer one real piece of work: score a posting they care about, or search their target roles.
Run `careercoach_export_experience` so they have a portable copy of everything — it writes
`Resume/Experience (profile export).md` and never touches their own `Resume/Experience.md`. If
they later edit their own Experience.md, `careercoach_import_experience` brings the changes in
(preview, then `apply=true` with the `preview_id`).

Close by telling them where the record lives: the **Career Coach** panel in the console shows
every field you hold, what's still missing, and exactly what you're told each turn. It's a
read-only view — to change anything, they just tell you and you record it. Then stop talking and
let them work.
