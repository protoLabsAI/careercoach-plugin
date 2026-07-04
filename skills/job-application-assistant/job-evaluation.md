# Job-Fit Evaluation Framework

> Adapted with credit from Mads Lorentzen's `ai-job-search` (MIT), `04-job-evaluation.md`.
> The four dimension **weights are live knobs** (`weight_technical`, `weight_experience`,
> `weight_behavioral`, `weight_career`) with named presets — read the current values and
> retune with `careercoach_tune` / `careercoach_preset`. Defaults: 30 / 25 / 15 / 30.

Score four dimensions 0-100, apply a pass/fail location gate, then take the weighted overall.

## 1. Technical skills match (0-100)
How well the required/preferred skills align with the candidate's capabilities.

| Score | Meaning |
|-------|---------|
| 80-100 | Core requirements are the candidate's primary skills |
| 60-79 | Most requirements match; 1-2 learnable gaps |
| 40-59 | Partial match; significant upskilling needed |
| 0-39 | Fundamental mismatch |

## 2. Experience match (0-100)
Does the work history line up with what they're hiring for?

| Score | Meaning |
|-------|---------|
| 80-100 | Direct experience, same domain + role type |
| 60-79 | Related experience; transferable skills are clear |
| 40-59 | Adjacent experience; the candidate would have to make the case |
| 0-39 | Unrelated experience |

## 3. Behavioral / culture fit (0-100)
Does the role + company culture match the candidate's behavioral profile? **Research red flags**
via the `company_researcher` brief: department disorganization, maintenance-heavy work,
leadership chemistry, culture mismatch. Check reviews, media, and network contacts.

## 4. Career alignment & motivation (0-100)
Does this advance the candidate's direction *and* contain work that energizes them?

| Score | Meaning |
|-------|---------|
| 80-100 | Strongly aligned; clear growth path |
| 60-79 | Good role, only partly aligned with long-term goals |
| 40-59 | Decent job, doesn't build toward the goal |
| 0-39 | Dead end or a step backward |

**Motivation filter:** evaluate not just whether the candidate *can* do the tasks, but whether
the tasks will *energize* them. Weigh energizing vs. draining tasks, autonomy, leadership style,
and life-situation constraints (security, flexibility, growth). This is where coaching matters
most — a high-skills, low-energy job is a trap worth naming out loud.

## Location & logistics (pass/fail gate, not weighted)
- Within commute range / remote-friendly → **PASS**
- Requires relocation the candidate ruled out → **FAIL** (deal-breaker)
- Frequent travel → **FLAG** and discuss

## Output format

```
## Job Fit: [Role] at [Company]

| Dimension        | Score  | Notes |
|------------------|--------|-------|
| Technical skills | XX/100 | … |
| Experience match | XX/100 | … |
| Behavioral fit   | XX/100 | … |
| Career alignment | XX/100 | … |
| Location         | PASS/FAIL | … |

**Overall: XX/100** (weighted by the current rubric)  ·  **Verdict: [band]**

**Strengths for this role:** …
**Gaps to address:** …
**Recommendation:** [apply / apply with caveats / skip] — one or two sentences.
```

## Verdict bands (on the weighted overall)
- **75+ Strong Fit** — definitely apply; tailor everything.
- **60-74 Good Fit** — apply; address the gaps in the cover letter.
- **45-59 Moderate Fit** — consider carefully; talk it through first.
- **30-44 Weak Fit** — probably skip unless there's a strategic reason.
- **<30 Poor Fit** — skip.

After scoring, record the result with `careercoach_track_application` (company, role, fit_score,
status `considering`, the posting URL as `source`) so it lands on the dashboard and feeds `/upskill`.

## Pre-application: should the candidate call first?
Only if there are **substantive** questions (unclear requirements, vague day-to-day, a named
contact inviting questions) — never just to "be remembered". Good questions: primary challenges
of the role, how time splits across responsibilities, what success looks like in 6-12 months.
The call is for *gathering information* to tailor the application; a natural reference to it in
the cover letter ("After speaking with [name]…") is a strong signal.
