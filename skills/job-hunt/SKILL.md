---
name: job-hunt
description: >-
  Use to FIND roles and work the search day to day — sourcing fresh postings, setting up and scanning
  the company watchlist, the morning digest, checking who the operator knows at a company before they
  apply, logging networking follow-ups, and bringing past applications into the tracker. Triggers:
  "find me jobs", "any new roles?", "run a sweep", "morning digest", "what's new today", "who do I know
  at X", "add X to my watchlist", "import my applications", "set up my job search".
tools: [careercoach_watchlist, careercoach_find_roles, careercoach_digest, careercoach_log_contact, careercoach_track]
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

- **Target roles / locations / exclusions** live in Settings → Career Coach. Each comma-separated
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
  follow-ups with `careercoach_log_contact`. Bulk connections import is optional (see below).

## The morning routine

1. `careercoach_digest` — new roles in the last few days, follow-ups due, applications gone quiet.
   (Offer to schedule it daily, e.g. weekdays 8am, with the scheduler tools if present.)
2. Triage the new roles with the operator — a quick read of each, not a full evaluation. Drop ghosts
   (open 45+ days, reposted, thin text) unless something specific says otherwise.
3. For each keeper, ask who the operator knows there (or use the optional referral lookup below).
   If they know someone, draft the
   referral ask (3-5 sentences: the role link, why it fits in one line, one piece of proof of work)
   and log it with `careercoach_log_contact(…, next_action="follow up", due=<+5 days>)`. If not, offer
   to find the hiring manager's name from the posting / team page and draft a short note.
4. Evaluate and apply to the strongest fits while they're fresh (job-application-assistant / apply-form).
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
