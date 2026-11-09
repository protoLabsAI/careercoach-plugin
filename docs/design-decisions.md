# Design decisions

Why the coach is built the way it is. What it does is in [how-it-works.md](how-it-works.md).

## A coach, not an autopilot

`strategy` and `interview` keep you in the loop by design. The default way to apply is an apply kit you file
yourself, and the `apply` workflow is the opt-in "do it for me" path that still stops short of submitting.
The point is to make you clearer and better prepared, not to send more applications.

## Gated and autonomous, on purpose

The full application comes in two shapes. The `apply` workflow runs research, evaluation and the apply kit
in one go. The role-packet skill does the same work with your approval before every phase, and files the
result as editable artifacts. A static workflow can't pause between steps, so the gated flow is a skill that
stops and asks, plus tested scaffolding tools (`applying/packet.py`) for the file mechanics.

## The profile is state, and it's always in front of the model

Your record is a structured profile, not a document, and it reaches the model on every call with an explicit
picture of what's known and what's missing. A recall tool can't help an agent that doesn't already suspect
there's something to recall: a real first run was abandoned partway because the coach kept re-asking for
facts it held.

The block is added per call through `wrap_model_call` (ADR 0108 D2), so it's never checkpointed and there's
exactly one copy per call. It sits after the host's cache breakpoints, so it doesn't cost prompt caching.
The subagents the host builds without plugin middleware read the profile through the profile tools instead.

## Imports are previewed, then bound to the preview

Importing `Experience.md` shows the exact lines it would add or remove and returns a `preview_id`. Applying
it is refused if the file, the profile or the options changed since, so what you approved is what gets
written. Sections merge line by line, re-importing an unchanged file changes nothing, a `do_not_claim` line
can't be dropped without your say-so, and the whole import lands in one save, so one restore undoes it.

## Rules that always apply ride on every turn

The writing discipline used to live only inside one skill. A side-door request ("just convert my résumé to a
docx") then produced a document at 16 em-dashes per 1,000 words, against the operator's stated limit of 3, carrying a
phrase they'd retired. A rule that only binds when you come in the front door isn't a rule, so the write
rules and the voice brief now ride in the always-on block.

## A skill's sub-files can't be loaded, so the style guide is its own skill

`load_skill` returns a skill's `SKILL.md` and nothing else. The writing guide used to be a sub-file of the
`apply` skill, so no run could actually load it. It's now the `writing-style` skill.

## The résumé is one versioned artifact

A résumé kept as chat text or markdown multiplies. Six weeks in there are five versions, three of them
stale, and no answer to "which one did I send". So the master résumé is one HTML artifact, versioned and
edited in place, and each tailored version is its own artifact. A role packet's tailored résumé is a snapshot
that records which artifact and version it came from.

The `resume` skill adds no tools of its own. It uses the artifact plugin, `browser_pdf` from agent_browser,
and cowork's `docx` skill, naming each owner and its fallback (ADR 0039). The ATS check comes from the same
setup: saving a PDF extracts its text, so the coach can read back what a parser actually sees.

## HTML and print CSS, not LaTeX

The upstream project's biggest cost is fighting LaTeX page breaks. The CV here is HTML with a real `@page`
and `break-inside: avoid` on every entry, so it prints the way it was laid out. LaTeX stays available as an
option.

## Submit-ness comes from the browser, not from the arguments

The coach's own submit check reads a click's arguments for words like "submit". A snapshot ref
(`browser_click("@e42")`) carries no words, so a click on the real Submit button by ref went straight
through. Only the browser plugin can resolve a ref to the element it hits, so on cores whose browser has
the action gate, the coach hands the decision to it: it registers a rule with the gate instead of guessing.
The rule allows a submit only with the one-shot grant from the coach's approval card, still bound to the
exact read-back you approved, so you get one card, not two. The grant is still minted in one place,
after your approval of the interrupt, so nothing the model writes can create it.

The rule only takes effect inside a tool call the coach's own middleware wrapped. The browser keeps its
list of rules when the coach reloads, and an old copy of the rule would see an empty grant table and
refuse every approved submit, so an old copy has to stay silent. The argument-text check still runs
first, as a floor: if the browser's probe can't read a page it sees no submit at all, and a click on
"Submit application" shouldn't get through just because of that. On older cores the argument-text
middleware stays exactly as it was.

## Word files are a soft pairing

`render_format: docx` makes real Word files, which is what many ATS forms want. It needs cowork and
`execute_code`, which runs code, so it's off unless you choose it, and it falls back to HTML and PDF with a
note about what's missing.

## The employer's own board beats an aggregator

Ashby, Greenhouse and Lever publish keyless, documented posting APIs with the date a role first went up,
which is fresher than any relister and needs no scraping. LinkedIn and Indeed are never scraped.

## Two control surfaces

Your profile basics are settings. The fit weights are live controls the agent can retune, because "score
these more on growth" is a quick adjustment, not a settings change.

## Transparency

The **About you** panel shows every field held, every field missing, and the exact block the agent receives.
You should never have to dig through files to see your own record.

## Your own voice, if you've taught it

When cowork's `writing-voice` skill has saved a `my-writing-style` profile, the coach layers it over the
writing rules so drafts sound like you. It's detected at runtime and ignored if absent, and the honesty rules
still win on any conflict.
