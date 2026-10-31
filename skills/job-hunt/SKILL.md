---
name: job-hunt
description: >-
  Find fresh roles, manage a company watchlist, review the daily digest, or follow up on leads. Also handles first run: "set me up", "get started", "onboard me", "here's my resume", an empty profile, and /setup-coach.
user_facing: true
slash: setup-coach
tools: [careercoach_profile, careercoach_answers, careercoach_watchlist, careercoach_find_roles, careercoach_digest, careercoach_log_contact, careercoach_track, memory_recall, knowledge_ingest, list_skills, load_skill]
---

# Job hunt — find fresh roles, go in through people, apply early and few

The problem in a senior search is not too few postings. Applications per hire have roughly tripled
since 2021 (Ashby: 300+, technical roles ~600), recruiters read newest-first and stop once they have a
shortlist, and since LLMs a tailored cover letter has largely stopped helping applicants get hired.
What still works: **reaching a real opening early, through a person, with proof of work.** Every
step below serves one of those three.

## Where roles come from (best first)

1. **The watched companies' own job boards** — `careercoach_find_roles(source="watched")`. Ashby / Greenhouse / Lever
   publish every open role with the date it *first* went up; this is fresher than any aggregator and
   it's the employer's own data. Results come pre-filtered by Settings (target roles, title
   exclusions, locations, max age) and marked **NEW** (never seen by an earlier scan), **already
   tracked**, **you've applied to N roles here**, and **⚠ ghost warnings**.
2. **Market sweep** — `careercoach_find_roles(source="market", query=<target role>)` for companies
   not on the watchlist yet, using the configured job source. When a sweep turns up a company worth
   following, **add it to the watchlist** so future scans catch its new roles the day they post.
3. **Web search** for companies on Workday or their own careers site — the watchlist can't read those.

Never scrape LinkedIn or Indeed, and never automate submissions in bulk.

## Setting up (once)

- **Target roles / locations / exclusions** can be set in Settings → Career Coach. Blank target
  roles and locations fall back to the confirmed profile's `target_roles` and `target_locations`.
  The identity `location` is a home location and does not restrict searches.
  Each comma-separated
  target role is an all-words title match ("Design Engineer" matches "Senior Design Systems Engineer",
  not "Product Designer"). `locations` takes places plus `remote` (= remote and open to the US).
- **Watchlist:** `careercoach_watchlist(action="add", company="A, B, C")` looks each company up on all
  three ATSes. Seed it from the operator's target companies, the companies they've already applied to
  that are still worth watching, and good finds from sweeps. Report the ones it couldn't find.
- **History:** if past applications live outside the tracker (a notes file, a spreadsheet), read the
  source and record each application with `careercoach_track(action="track", company=…, role=…,
  status=…, notes=<original date and context>)`. Skip interview notes and to-dos. Reconcile with
  `careercoach_track(action="list")` before a sweep so already-applied roles are recognized.
  For bulk imports that preserve original dates, see the optional extended tools below.
- **Contacts:** ask who the operator knows at target companies and log relevant conversations and
  follow-ups with `careercoach_log_contact`. When one is finished or dropped, end it with `close=True`.
  Bulk connections import is optional (see below).

## The morning routine

1. `careercoach_digest` — new roles in the last few days, follow-ups due, applications gone quiet.
   (Offer to schedule it daily, e.g. weekdays 8am, with the scheduler tools if present.)
2. Triage the new roles with the operator — a quick read of each, not a full evaluation. Drop ghosts
   (open 45+ days, reposted, thin text) unless something specific says otherwise.
   `careercoach_track(action="inbox")` lists what's waiting; `action="keep"` or `"dismiss"` with its
   `inbox_id` decides a card. Tracking a role clears its card on its own.
3. For each keeper, ask who the operator knows there (or use the optional referral lookup below).
   If they know someone, draft the
   referral ask (3-5 sentences: the role link, why it fits in one line, one piece of proof of work)
   and log it with `careercoach_log_contact(…, next_action="follow up", due=<+5 days>)`. If not, offer
   to find the hiring manager's name from the posting / team page and draft a short note.
4. Evaluate and apply to the strongest fits while they're fresh (the **apply** skill).
5. Track every decision — `careercoach_track(action="track")` with the posting URL as `source`.

## Rules

- **Few, strong applications.** The tracker warns past 3 live applications at one company; recruiters
  see them all in one ATS and many cap repeat applicants. Pick the best-fit role there.
- **Fresh first.** Sort by first-posted date; a role posted today beats a slightly better fit posted
  five weeks ago.
- **Re-test before you call a tool broken.** A friction entry or a memory that a tool "returns
  junk" is history, not the current state — plugins update under you. Call it, read the output, then
  say what it did.
- **Don't confuse "I didn't find it" with "it doesn't exist".** A board the scan couldn't read is
  listed under "Couldn't read" — say so rather than concluding the company isn't hiring.
- **Proof of work over prose.** Outreach and applications point at real artifacts (repos, demos,
  shipped work); no flattery, no generic cover letters.

## First run / set me up

Trigger on **"set me up"**, **"here's my resume"**, or an **empty profile**.
Aim for the first scan within five minutes. **Ask at most one confirmation question
before the first scan.** Missing voice samples, work authorization, contacts or API keys
must not delay scanning. Never invent facts.

1. **Import profile.** Read `careercoach_profile(action="get")` and harvest the pasted
   resume text, existing artifacts and memory. Extract LinkedIn profile PDF text with
   `knowledge_ingest`; never scrape LinkedIn. Prepare a field-by-field preview containing
   every document claim and nothing invented. Leave unsupported fields empty; do not infer
   metrics, skill levels or work authorization. For their own Experience.md,
   `careercoach_profile(action="import_experience")` returns a preview and `preview_id`.
2. **Confirm targets once.** Show the extracted profile and propose `target_roles`
   (comma-separated) and search `target_locations` from the document, clearly labeled as proposals.
   Keep the documented home `location` separate from the confirmed search geography.
   Ask ONE combined question: "May I save this profile and search these target roles and
   location, or what should I correct?" Include any missing resume, targets or location
   in this same question. Wait for confirmation; never treat silence as consent.
   Save confirmed fields with `careercoach_profile(action="update", field=…, content=…)`,
   including `field="target_roles"` and `field="target_locations"`; save the documented home
   location with `field="location"`. Apply a confirmed import
   using `apply=true` and its `preview_id`. Preserve every source claim across the
   appropriate sections; report write refusals, never silently truncate. Unconfirmed
   proposals stay unsaved; scan only confirmed targets or explicit search terms.
3. **Seed answers.** Call `careercoach_answers(action="get")`, whose first use invokes
   `answers.seed_from_profile`. Keep all seeded answers as **drafts**, never confirmed.
4. **Find roles.** Run `careercoach_find_roles(source="market", provider="himalayas")` with the saved targets
   and location. Default `jobs_provider: auto` without a key uses **himalayas**; **hn**
   is also keyless. Use keyless sources only; never ask for an API key.
   Check `careercoach_watchlist(action="list")` and also run
   `careercoach_find_roles(source="watched")` if boards exist. Results go to the
   **inbox** for the view, not the application tracker. Report empty results and errors honestly.
5. **Open the view.** Point the operator at the **Career Coach** view to review their
   profile and inbox results. End setup; deeper coaching is optional after the first scan.

### Optional enrichment after the first scan

The existing interview below is available after the first scan. Its additional questions
must not run before that scan.

The coach can't do its job without knowing who it works for, and it must never invent the
answer. This skill fills that gap once, and records it as a **profile** that gets injected into
every future turn. The profile is the single source of truth: every draft, score and interview
answer reads it, and nothing reads a workspace `Resume/Experience.md` directly.

**Harvest before you ask.** You almost certainly already know things. Asking an operator for
their own name when it's sitting in memory is the fastest way to lose their trust, and it's the
failure this skill exists to prevent. Interrogation is the last resort, not the method.

### 0. What do you already know?

Before your first message, gather silently. None of this needs permission:

- `careercoach_profile(action="get")` — the completeness picture. Anything under **known** is settled;
  only **missing** is in play. If it's already complete, skip to the enrichment wrap-up.
- `careercoach_profile(action="import_experience")` — if they keep their own `Resume/Experience.md`, this
  previews what it would add to the profile (the template's hint text and anything already
  recorded are skipped) and ends with a `preview_id`. That's a prior setup worth keeping: show them
  the preview and, once they confirm, call it again with `apply=true` and that `preview_id` —
  rather than asking it all again. If they edit the file later, re-import: only the new lines are
  added. Their file stays theirs, nothing writes over it.
  `careercoach_profile(action="read", doc="story-bank")` may hold their STAR stories too.
- `memory_recall` for the operator's profile, background, abilities and voice.
- Look at what's around: existing CVs or resumes in the workspace, saved artifacts, notes.
- If they've ever shared a resume link or file, `knowledge_ingest` it — that handles PDFs and
  URLs properly. Don't try to read a PDF with a fetch tool and conclude you can't.

### 1. Open by showing what you found

Lead with what you already have, not with a question. Something like: *"Here's what I already
know about you — name, location, contact, and your last two roles from the CV in your workspace.
Three things are missing: work authorization, your story bank, and the list of claims you never
want me to make. Want to fill those in now, or go straight to work?"*

This does three things at once: proves you did the reading, makes the gaps concrete, and gives
them the option to leave. If they'd rather work, **go work** — you can fill gaps as they come up.

### 2. Fill the gaps, in as few questions as possible

Only ask about what's genuinely missing. Batch related items into one message rather than
serialising them; asking for email, phone, LinkedIn and portfolio in four separate turns is the
behaviour that got a real first run abandoned halfway through. Two or three focused exchanges
should finish most profiles.

Only record operator facts after the operator confirms them. Memory and imported material are
evidence to review, not permission to write new facts.

Record each field as it's settled with `careercoach_profile(action="update", field=field, content=content)`. Sections
**append** by default, so record roles (or stories) one at a time as each is settled and every
one is kept. To **correct** something already recorded, read the section with
`careercoach_profile(action="get", field=field)`, fix it, and write the whole section back with `mode="replace"`
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

### 3. Voice — before anything gets written in their name

Check `list_skills` for `my-writing-style` (from cowork's `writing-voice` skill). If it exists,
`load_skill` it and say you'll use it. If not, ask for one or two samples of writing they
consider theirs and pull out the register: sentence length, formality, what they'd never say,
whether they open with the point.

Record it under `notes` or leave it to the voice memory domain, but **do this before drafting**,
not after. Voice rules that arrive after the document is written have already failed.

### 4. Prove it, then get out of the way

Offer one real piece of work: score a posting they care about, or search their target roles.
Run `careercoach_profile(action="export_experience")` so they have a portable copy of everything — it writes
`Resume/Experience (profile export).md` and never touches their own `Resume/Experience.md`. If
they later edit their own Experience.md, `careercoach_profile(action="import_experience")` brings the changes in
(preview, then `apply=true` with the `preview_id`).

Close by telling them where the record lives: the **Career Coach** panel in the console shows
every field you hold, what's still missing, and exactly what you're told each turn. It's a
read-only view — to change anything, they just tell you and you record it. Then stop talking and
let them work.

## Optional extended tools

These tools require `extended_tools: true` in Settings → Career Coach **and must be present in
the available tool list before calling them**. If absent, use the per-role tracking and operator
contact discussion above; explain the setting if the operator wants bulk import or stored referral
lookup. Do not claim an import or lookup happened when it did not.

- `careercoach_import_history`: import extracted application records, preserving their dates.
- `careercoach_import_connections`: import the operator-provided LinkedIn Connections export
  (Settings → Data privacy → Get a copy of your data → Connections).
- `careercoach_find_referrals(company=…)`: search imported connections before drafting a referral ask.
- `careercoach_search_jobs(query=…, source="himalayas"|"hn"|"jsearch")`: choose a provider per search.
  Himalayas is keyless and remote; HN searches "Who is hiring?"; JSearch requires an API key.
