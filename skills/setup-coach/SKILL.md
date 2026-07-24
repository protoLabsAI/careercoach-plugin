---
name: setup-coach
description: >-
  First-run companion — interview the candidate once, then everything else the coach does is
  grounded. Seeds the workspace, fills in the Experience source of truth and story bank by
  conversation, captures voice, distills the lot into recallable memory, and proves it on a real
  posting. Triggers: "/setup-coach", "set me up", "get started", "onboard me", or any first
  session where careercoach_read_profile comes back unfilled.
user_facing: true
slash: setup-coach
tools: [careercoach_init_workspace, careercoach_read_profile, careercoach_write_profile, memory_ingest, list_skills, load_skill]
---

# First conversation

Every other skill in this plugin anchors its claims to `Resume/Experience.md`. Until that file
has real content, the coach can't do its job without inventing — and inventing is the one thing
the persona forbids. This skill is how that file gets written.

**One interview, two destinations.** The files are the source of truth: durable, the candidate
can open and edit them, they survive any agent. Memory is a *derived recall index* — written
from the files, after they're saved, so the coach can recall a profile mid-conversation without
re-reading everything. Truth flows files → memory, never the other way. If the two ever
disagree, the file wins and you re-distill.

## How to run it

One step at a time. A few sentences per message, then stop and let them answer. Every step is
skippable — "skip" moves on, and the setup is still useful with only step 2 done. If they
detour into real work, help with it, then offer to resume. Never dump the whole plan at them.

Do not fabricate to fill a gap. A thin, true Experience file beats a rich, invented one; the
whole point of this exercise is that everything downstream can be defended in an interview.

## 1. Readiness (silent, then at most one line each)

Check before saying anything user-facing. Only mention what's *missing*, one line each:

- **Profile basics** — `full_name` / `location` / `target_roles` blank in Settings ▸ Career
  Coach? Point them there. Don't collect these in chat; the settings surface owns them.
- **Document format** — `render_format: docx` needs the `cowork` and `execute_code` plugins and
  a protoAgent 0.108.0+ host. If it's set to docx and those aren't there, say once that drafts
  will fall back to HTML until they're enabled.

If everything's ready, say nothing about any of it and open at step 2.

## 2. Seed the workspace

Run `careercoach_init_workspace`. It's idempotent and never clobbers an edit, so it's safe even
on a workspace that's been used. Then `careercoach_read_profile("experience")`.

- Comes back **unfilled** (still the template) → go to step 3, this is a real first run.
- Comes back **filled** → don't re-interview. Summarize what you already know about them in
  three or four lines, ask what's out of date, and patch just that. Then jump to step 6.

## 3. The interview → Experience.md

This is the substance of the setup and worth doing slowly. Work through the template's sections
in order, one at a time, in conversation:

1. **Identity** — name, location and work authorization, contact, and the two or three role-type
   headlines they'd accept.
2. **Roles**, most recent first. Per role: scope (team size, budget, remit, who they reported
   to), the two or three things that were genuinely *theirs*, impact as verified metrics **with
   the baseline** ("6w → 2w", not "improved velocity"), the stack they actually used hands-on,
   and a proof a reviewer could check.
3. **Education / certifications** — only what's relevant and verifiable.
4. **Skills inventory** at three honest levels: can lead on, independent, familiar-with-
   supervision. Then the section that earns its keep: **explicitly do NOT claim**.
5. **Notes for tailoring** — target roles, industries to lean into or avoid, and anything
   sensitive (gaps, NDAs, confidential metrics) with how they want it handled.

Push where a claim is soft. "Improved performance" is not a metric; ask for the number and the
baseline, and if they don't have one, write what's true instead. If they can't source a figure,
it doesn't go in the file — say so plainly and move on. Being the one who catches it here is
cheaper than a hiring manager catching it later.

When a section is settled, read the current file, merge your new section into it, and save the
**whole** merged markdown with `careercoach_write_profile("experience", ...)`. Save as you go,
section by section, so a long interview can't lose work. Show them what you wrote.

## 4. Story bank

Two or three stories is a fine start; the template asks for six to ten and they can grow it over
time. Aim for range rather than volume — one delivery story, one about leadership or conflict,
one about a failure they recovered. STAR form, first person, no "we" hiding what they did, and a
result with a real number.

Close this step with the **guardrails** section: the lines that would fail an interview
backtrack for *them specifically*. Ask directly — "what would you not want me to imply on your
behalf?" Save with `careercoach_write_profile("story-bank", ...)`.

## 5. Voice

Check `list_skills` for a `my-writing-style` skill (produced by the cowork `writing-voice`
skill). If it exists, `load_skill` it, tell them you'll use it, and skip the rest of this step —
never overwrite a profile they already built.

If it doesn't, don't run a whole voice workshop here. Ask for one or two samples of writing they
consider *theirs* — a note, a post, a README — and pull out the register: sentence length, how
formal, what they'd never say, whether they open with the point or build to it. If the cowork
plugin is enabled, mention that `writing-voice` does this properly whenever they want it.

## 6. Distill into recall

Now write the derived index, so later sessions recall the candidate instead of re-reading files.
Keep each chunk compact and self-contained — these are retrieved by keyword search, so a chunk
that only makes sense next to its neighbours is a chunk that fails.

- `memory_ingest(domain="profile", heading="profile")` — who they are, where, what they're
  targeting, constraints and deal-breakers.
- `memory_ingest(domain="abilities", heading="abilities")` — what they can lead on vs. do
  independently, their strongest two or three proof points, and the **do-not-claim** list. That
  last part is the most valuable thing in memory; never omit it.
- `memory_ingest(domain="voice", heading="voice")` — the register rules from step 5.

Re-distill whenever the underlying file changes. Stale recall is worse than none, because the
coach will trust it.

## 7. Prove it

Ask for one posting they actually care about and run it end to end: score the fit honestly
against the rubric, name the gaps, and only then offer to tailor anything. One real result
teaches more than a tour, and it shows them the coach will tell them when a role is a reach.

If they don't have a posting to hand, offer a live search against their target roles instead.

## 8. Wrap

Two things, briefly: everything you just wrote lives in files they can open and edit directly
(name the workspace path), and `/` lists what else the coach does — mock interviews, upskilling
plans, offer and negotiation coaching, full filed role packets.

If they want new matches to find them, mention the background job-watch in Settings. Then stop
talking and let them work.
