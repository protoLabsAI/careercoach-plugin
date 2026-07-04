---
name: upskill
description: >-
  Use to turn the jobs someone is chasing into a concrete learning plan — "what should I
  learn", "where are my skill gaps", "how do I become competitive for X roles", "make me a
  study plan". Aggregates the tracked pipeline (or a single posting) against the profile,
  builds a prioritized gap heatmap, and produces a learning plan with real, web-searched resources.
tools: [careercoach_list_applications, company_researcher]
---

# Upskill

> Two-pass gap analysis adapted with credit from Mads Lorentzen's `ai-job-search` (MIT),
> `upskill/SKILL.md`.

Two modes:
- **Aggregate** (default) — analyse the whole tracked pipeline (`careercoach_list_applications`).
- **Targeted** — analyse a single posting the user gives you (fetch it with `fetch_url`).

## Step 1 — Load
Read the candidate profile (Settings › Career Coach + anything shared in the conversation). In
aggregate mode, pull the tracked applications and their `fit_score`s (lower fit = the role exposed
more gaps → weight it heavier). In targeted mode, fetch and parse the one posting.

## Step 2 — Pass 1: hard-skill diff
Extract required + preferred technical skills from each source. Build a **frequency map**; in
aggregate mode weight each skill's contribution by `(100 - fit_score) / 100`. **Diff against the
profile and be generous** — if the profile shows a skill in any form ("Python" covers "Python
scripting"), it's not a gap. What remains is the hard-skill gap list, ranked by score.

## Step 3 — Pass 2: synthesis (what the diff misses)
Reason holistically about gaps a keyword diff can't catch, and tag each:
- `[domain]` — unfamiliarity with the industry/problem space (cybersecurity, quant finance, climate…)
- `[soft]` — ways of working, communication, leadership expectations the postings emphasize
- `[tooling]` — frameworks, cloud, methodologies (MLOps, CI/CD, agile-at-scale) recurring across jobs
- `[credential]` — a certification multiple postings prefer

Don't duplicate Pass 1; only add what it missed.

## Step 4 — Gap heatmap (show this before the plan)
Combine both passes into one prioritized table (Critical / High / Medium / Low), and print it:

| Priority | Skill / Area | Type | Gap source |
|----------|-------------|------|------------|
| Critical | Kubernetes | Hard | 4/5 jobs, score 3.2 |
| High | Security domain | Domain | synthesis |

## Step 5 — Learning plan
For every Critical + High gap (and Mediums if there are fewer than five gaps total):
1. **WebSearch** for current, well-regarded resources — include the current year in the query.
2. Pick 2-3 (prefer hands-on labs, official docs for tooling, books for domain). Name + URL + one-line why.
3. Write a **study direction tailored to the candidate's background** — what to skip, where to start.
4. Estimate time to working proficiency (be realistic, err high).

Group by theme (Cloud & Infra, MLOps, Domain, Soft skills, Certs), then add a **Suggested study
order** with dependencies first, critical before high, quick wins early, domain knowledge alongside projects.

## Rules
1. **Never fabricate a resource.** Only cite real WebSearch results — no invented courses, URLs, or authors.
2. **Search with the current year** so results stay fresh.
3. **Be generous with profile matching** — avoid false-positive gaps.
4. **Show the heatmap before the plan.**
5. Offer to save the plan as an **artifact** (or a note) the candidate can revisit and track progress against;
   next time, diff against it to show gaps closed + new gaps.
