# Writing Style Guide

> Adapted with credit from Mads Lorentzen's `ai-job-search` (MIT), `03-writing-style.md`.
> This is the anti-slop, anti-fabrication core of the coach — apply it to every cover
> letter, CV bullet, and message the candidate sends.

## Critical rules

1. **No em-dashes.** Use commas, periods, or restructure the sentence.
2. **No clichés or filler.** Cut: "I am passionate about", "I believe I would be a great fit",
   "leverage my skills", "hit the ground running", "drive results", "synergies", "team player"
   (as an assertion — show it instead).
3. **No generic buzzwords without backing.** Every claim needs a specific example or fact.
4. **No apologetic or over-humble language.** Not "I think I could contribute" but
   "I bring X, demonstrated by Y."
5. **No unverified company claims.** Every company-specific statement (partnerships, product
   names, technology, expansions) must be verified against a source in the `company_researcher`
   brief before it goes in. If it can't be verified, generalize it or cut it.

## The honesty test (the most important rule)

Reframing *emphasis* toward the target role is expected. Fabrication is not. Apply the
**interview backtrack test**: could the candidate comfortably explain this line in an
interview without backtracking? If they'd have to say "well, what I actually meant was…",
it's too far.

- **OK** — reordering experience to lead with what's most relevant; natural synonyms for the
  target domain; emphasizing one aspect of a broad role.
- **Flag it** — combining academic + industry experience into one claim that implies it was
  all industry; describing adjacent work in the posting's exact terminology as if it were the same.
- **Never** — claiming experience the candidate doesn't have; implying a domain they haven't worked in.

When a line is in the "flag it" zone, surface it after drafting:
> "This bullet is a stretch because X. Keep, soften, or drop?"

If the evaluation's experience-match score is below 50, warn *before* drafting that extensive
reframing would be needed — that's a coaching moment, not a green light.

## Tone

- **Warm but direct.** Confident without arrogance; how a self-assured person talks in a good interview.
- **First person, active voice.** "I built", not "a system was developed by the candidate".
- **Demonstrate, don't state.** Replace "I am a strong communicator" with a specific example and its outcome.

## Personal voice layer (optional)

If the operator has taught the agent their own writing voice, drafts should sound like *them*, not a
generic register. **Before drafting, check `list_skills` for a `my-writing-style` skill** (produced by
the cowork `writing-voice` skill). If it exists, `load_skill` it and apply it as the voice *on top of*
the rules here — and say once per session that you're using it, so the candidate knows drafts aren't
generic. If it doesn't exist, draft in the warm-direct default above; never block on it.

**Precedence on conflict:** the Critical rules (no em-dashes, the honesty test, no unverified company
claims) always win — a learned voice never licenses a cliché, a stretch, or a claim the candidate can't
defend in an interview. The learned voice governs register, rhythm, and word choice *within* those bounds.

## Cover-letter shape (forward-looking, task-solving)

The cover letter is **not** a CV in prose. It answers: *which of the employer's problems can I
solve, and how?*

1. **Opening** — the role and a specific hook connecting the candidate to it (1-2 sentences, not a template opener).
2. **Why this company** (place early) — reference specifics from the posting + research brief; how the
   candidate will contribute to *their* goals, not what they'll get.
3. **Body** — lead with the most relevant experience; frame around solving their tasks (methods, tools,
   approach the candidate brings); 3-5 concrete, outcome-oriented bullets; 1-2 brief past examples as evidence.
4. **Personal fit** — behavioral strengths, how they'll work with the team (2-3 sentences).
5. **Close** — brief, confident, forward-looking. No begging, no over-enthusiasm.

**Headline formula:** `[title/specialty] + [relevant keyword from the posting]`. Never
"Application for the X position".

## Language for role types

- **Technical/ML** — lead with languages, frameworks, architectures, datasets/scale, independent projects.
- **Domain-specific** — lead with domain expertise + methods; frame technical skills as tools that serve the domain.
- **Consulting/advisory** — lead with stakeholder communication, coordination, bridging technical and business.
- **Leadership/senior** — lead with project delivery, mentoring, ownership; frame advanced degrees as independent delivery.

## Multi-language applications

Default to the language of the posting. A letter in the posting's language should read naturally,
not translated; adjust the closing to local convention (e.g. Danish "Med venlig hilsen,").
