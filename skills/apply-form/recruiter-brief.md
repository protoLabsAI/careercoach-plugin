# Recruiter brief & hiring-manager profile

Phase 2's job: understand who you're really applying to before you tailor anything. The brief gives
the resume and cover letter their specifics (and the honesty rule its source list); the optional
hiring-manager profile sharpens the framing when a credible manager is identifiable.

## Recruiter brief

Delegate the research to the **`company_researcher`** subagent — it returns a *sourced* brief where
every company-specific claim carries a link. Then shape its output into `recruiter brief.md` focused
on **this role**:

```
# Recruiter brief — <Role> @ <Company>

## The company (sourced)
- What they do, mission, recent news (funding, launches, layoffs, restructuring) — each with a link.
- Employee sentiment (Glassdoor etc.), size, trajectory. Red / green flags for a candidate.

## This role in context
- Why this role exists now (team, product line, the problem they're hiring to solve).
- The domain/regulatory context that matters (e.g. clinical trials, fintech compliance).
- Keywords and priorities the posting signals hardest.

## For the application
- Specifics safe to reference (verified) in the cover letter.
- Questions worth asking in a screen. Things to verify before applying.
```

**The source list is load-bearing:** the cover letter may only state company facts that appear here
with a source. Anything unverifiable gets generalized or cut.

## Hiring-manager profile (conditional)

Only produce `hiring manager profile.md` if a **credible, specific** hiring manager is identifiable —
named in the posting, on the team page, or clearly inferable and confirmable. If not, say so and skip
it; do **not** guess a name or invent a background. When you do have one:

```
# Hiring manager profile — <Name>, <Title> @ <Company>

- Background and focus (sourced): prior roles, what they've built/written/spoken about.
- What they likely care about in this hire (from their history + the role).
- How to frame the candidate for them: which proof to foreground, tone, what to avoid.
```

Use it to *tune emphasis* in the resume and cover letter — never to fabricate a personal connection.
