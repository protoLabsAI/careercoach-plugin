# How it works

The parts that make the coach grounded, honest and human. The reasoning behind each choice is in
[design-decisions.md](design-decisions.md).

## Your profile is the source of truth

Who the coach works for lives in a structured profile (`profile.json`, managed by `profile.py`) in this
agent's own plugin store, `<instance_root>/careercoach/`, beside the tracker's `applications.json`. Each
agent, sandbox and container keeps its own.

The profile reaches the model on every call as an `<operator_profile>` block, added by plugin middleware.
It carries what's known, what's missing, and the `do_not_claim` hard stops in full. That's what stops the
coach asking for facts it already has.

Everything that reads your history reads the profile. `Resume/Experience.md` is yours: the coach never
reads it as a source and never writes over it. It moves into the profile only by an import you approve, and
`careercoach_profile(action="export_experience")` writes a snapshot back out.

Writes are serialized and atomic, and each one first saves the version it replaces to `profile.json.bak`,
so the last change can always be undone. `do_not_claim` is capped at 3,000 characters, identity facts at
500 each and narrative sections at 20,000, so nothing can quietly fill every model call.

## The voice rules

The same block carries a short brief on how the coach sounds: react to the news before doing anything with
it, say the answer first in plain words, have an opinion, write in prose, don't narrate tools or read back
a status, and stay under about 100 words unless the question needs more. It rides on every turn because the
persona in `SOUL.md` only applies if it was pasted in.

## The write rules

Before writing anything in your name (a CV, cover letter, application answer, email, bio or export), the
coach loads the `writing-style` skill and checks the draft against your `do_not_claim` list. A
`do_not_claim` item can't come back as implied experience either: "deepen my Kubernetes experience" still
claims Kubernetes. If you've taught it your own voice with cowork's `writing-voice` skill, it layers your
`my-writing-style` on top, and the honesty rules still win on any conflict.

## Honesty is enforced

- **The interview test.** Reframing emphasis is fine. Claiming experience you don't have is not. Every line
  has to be something you could explain in an interview without backtracking.
- **Real numbers only.** A metric must come from your profile or story bank. If it's missing, the draft
  says `[fill in: <what>]` instead of estimating, and `careercoach_read_sources(source="check")` flags any
  figure it can't trace.
- **Unfilled is not filled.** The workspace starters are full of realistic hint text, so the coach compares
  each file against the shipped template. An untouched template counts as empty, and the coach won't draft
  from it.
- **It can't rewrite its own guardrails.** `do_not_claim` lines can be added freely, but removing or
  rewording one needs your explicit confirmation. The discipline files it's bound by are read-only to it,
  and profile values are escaped before injection so recorded text can't break out of the block.

## The apply kit

The default way to apply. The coach tailors the résumé from your profile and renders it to a PDF, writes a
cover letter only when the posting asks for one, drafts any new form answers, and saves it all on the role
with `careercoach_save_apply_kit`, which moves a Saved role to Preparing. You open the role in the view,
copy what you need, attach the PDF, submit it yourself and press **Mark applied**. Details, and the opt-in
form-filling beta, are in [applying.md](applying.md).

## Job sources

Ashby, Greenhouse and Lever publish keyless posting APIs with the date a role first went up. They don't
search, so `boards.py` filters each whole board locally, and a first-seen log (`seen.json`) decides what's
new and spots reposts, because boards reset a posting's date when it's relisted.

Market search is provider-abstracted and keyless by default: Himalayas and Hacker News, or JSearch with a
key. Only `jobsource.py` and `boards.py` make outbound calls, and the manifest declares exactly those hosts.
LinkedIn and Indeed are never scraped.

## The background watch

When it's on, a supervised loop scans your watchlist and the job source on the interval you set, queues
postings it has never seen for the digest, and emits an event that lights the rail icon. It never writes
your pipeline.

## The fit score

Fit is scored against a weighted rubric (technical, experience, behavioral, career). The weights are live
controls you can retune in chat, with presets such as `growth-first`. Your profile basics are settings, and
the weights are controls, because "weigh growth more" is a quick retune, not a settings change.
