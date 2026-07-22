# Role evidence map

The bridge artifact, and the honesty backbone of the whole packet. Before writing a single resume
bullet, map **each requirement in the posting** to **the candidate's actual proof**. The resume,
skills list, and cover letter then draw *only* on what this map supports — so nothing unfounded can
leak downstream. Build it from `Resume/Experience.md`, `Agent/story-bank.md`, and the recruiter brief.

## How to build it

1. Extract every **required** and **preferred** item from the raw JD — skills, tools, responsibilities,
   domain, seniority signals. One row each. Don't paraphrase away a specific tool or number.
2. For each, find the candidate's strongest **evidence** in the source of truth: the role/story, the
   concrete action, and the verifiable result. Cite where it comes from (which role / which story).
3. **Classify the match honestly:**
   - **Direct** — done exactly this, provable.
   - **Adjacent** — a defensibly similar thing (name the difference; this is the "reframe emphasis" zone).
   - **Stretch** — thin or inferred. Flag it; the user decides keep/soften/drop.
   - **Gap** — no real evidence. Say so. Gaps feed `upskill` and honest cover-letter framing, never a fake bullet.
4. Note the **best single proof** per requirement — the line the resume/cover letter should lead with.

## Format (write to `role evidence map.md`)

```
# Role evidence map — <Role> @ <Company>

| JD requirement | Match | Evidence (source) | Best proof to lead with |
|----------------|-------|-------------------|-------------------------|
| e.g. "5+ yrs product management" | Direct | 6 yrs PM @ X (Experience.md) | Owned X roadmap, $Y impact |
| e.g. "clinical trial domain" | Adjacent | Regulated-data work @ X | Shipped under HIPAA audit |
| e.g. "manage a team of 8" | Stretch | Led 3 ICs + 2 contractors | flag: never had 8 directs |
| e.g. "SAS programming" | Gap | — | (none — see upskill / omit) |

## Summary
- Strengths to foreground: …
- Stretches surfaced (for the user's call): …
- Gaps (omit / address in cover letter / upskill): …
```

## Guardrails
- A **Stretch** or **Gap** is information, not a failure — it's what protects the candidate in the
  interview. Surface them plainly; never launder a gap into a confident bullet.
- If more than a couple of core requirements are Gaps, that's a fit conversation with the user before
  drafting, not a green light to over-reframe.
