# Upgrading

Version notes that change behavior. Release notes for every version are on the
[releases page](https://github.com/protoLabsAI/careercoach-plugin/releases).

## 0.17.1

- **JSearch on RapidAPI works again.** RapidAPI removed the `/search` endpoint, so every search with a
  RapidAPI key answered 404. Searches now call `/search-v2`. Remote-only searches use its
  `work_from_home` filter: the old `remote_jobs_only` is ignored there and on-site roles came back.
- **A key JSearch rejects no longer turns market search off.** Under Job source `auto`, when JSearch
  refuses the key, runs out of requests or is down, the search is answered by Himalayas and the reply
  says why JSearch didn't answer. Before, the failure was the whole answer, and the watch found no
  market roles until the key was removed. Job source `jsearch` still reports the failure as is.
- **The watch sweeps both.** With a key under `auto`, each pass asks Himalayas for every role and asks
  JSearch as well, so a key adds roles and never replaces the free ones.
- **The watch budgets JSearch requests.** RapidAPI's free plan is 200 requests a month, and the watch
  would spend that in five days. It now reads how many requests the plan has left and when they refill,
  and spends an even share each day (about 6 a day on the free plan). A bigger plan gets a bigger
  share. A search you ask for in chat is not held back; its reply says how many requests are left.

## 0.17.0

- **A JSearch key bought from OpenWeb Ninja works.** Set **Job source** to `openwebninja` and paste the
  key under **Job-source API key**. `auto` and `jsearch` still mean a RapidAPI key, which is a different
  key: the two can't be told apart by format, so the source is never guessed.

## 0.16.2

- **An excerpt can't score high.** A card with under 600 characters of posting text (most cards queued
  before 0.16.0) is capped at 70 and its reason says "excerpt only"; it scored 80–94 off one sentence.
- **Pay in another currency isn't compared to your floor.** A range in CAD, EUR, GBP… leaves the pay part
  neutral and says so, and a role resolved to the company's own posting takes that posting's range.
- **A market search over your target roles searches each one.** With no `query`, every target role went
  out as one search, which matched nothing and made Hacker News answer 400. It now runs one search per
  role (the first 6).
- **`show` finds a card by an id copied without its `?utm_source=api` tail.**

## 0.16.1

- **Monthly pay isn't read as a salary.** A Himalayas range posted per month (or week, day, hour) is
  labelled with its period and leaves the pay part of the fit score neutral, instead of reading as an annual
  salary far under your floor.

## 0.16.0

- **Inbox scores mean something.** The watch used to score a role by the same title match it had just
  filtered on, so every role it surfaced scored 100. The quick fit score now reads the posting: how much
  of its stack your profile names (an off-domain stack or title, like a compiler or chip role, caps it),
  the seniority in the title (junior roles cap at 50), the title match, and pay against the new
  **Minimum salary** setting. The default **Watch match threshold** drops from 75 to 65 to match; a value
  you set yourself is kept, so check it. Cards already in the inbox keep their old score.
- **Himalayas roles can be read and applied to.** Himalayas' job pages refuse automated reads, and only a
  400-character excerpt was kept. Cards now keep the full posting text, and the watch looks each
  Himalayas role up on the company's own Ashby, Greenhouse or Lever board (cached for a week), so the card
  links to the real application page. `careercoach_track(action="show")` returns one card in full.

## 0.15.0

- **The browser checks submits.** On a core offering `agent_browser.add_gate_policy`, a submit is allowed
  only by spending the one-shot grant from `careercoach_request_submit`, bound to the exact form read-back
  you approved. This covers clicks by snapshot ref too. Older cores retain the argument-text submit guard;
  the optional service does not raise the minimum protoAgent version. Form JSON works with or without
  `AGENT_BROWSER_PAGE_CONTENT` boundary markers.
- **The whole form must be ready.** Unplanned values block verification, and the approval card shows the
  full read-back. Unchanged page defaults are recognized from the original plan, even across re-plans.
  Answers you give mid-fill need a fresh plan, and legal or attestation questions require your own answer.
  Dropdown and location checks also recognize supported equivalent values returned by the browser.
- **Answers show where they came from.** Extra answers are checked against what you said in the
  conversation; unsupported answers are labeled as the agent's, and cannot verify legal or attestation
  fields. Verification reads the browser's form directly when supported, otherwise uses the latest
  `browser_form_read` result from the conversation and rejects stale reads. If neither is available, the
  supplied read-back must still include every field from the plan.

## 0.14.0

- **Needs protoAgent 0.231.0 or newer.** 0.231.0 renamed the agent's memory tools (ADR 0125), and the skills
  now call the new ones: `memory_search`, `memory_save` and `knowledge_search`. On an older core, update
  protoAgent first; the plugin's `min_protoagent_version` refuses to load otherwise.
- **Named résumé variants.** A tailored résumé renders as its own variant and never overwrites your master.

## 0.13.10

- **The code moved into folders.** The logic modules now live in `record/`, `search/` and `applying/`, and the
  Jobs view is `view/jobs.html`. Installing or updating through protoAgent handles this. If you import a module
  directly, for example `careercoach.profile`, use the new path: `careercoach.record.profile`.
- **Packet files are private to you.** They're written atomically now, and a new file is readable by your user
  only, the same as the profile.

## 0.13.9

The coach's voice rules now ride on every turn, and the writing guide is its own `writing-style` skill. If a
prompt or skill of yours told the agent to read `apply-form/writing-style.md`, point it at
`load_skill("writing-style")` instead.

## 0.13.2

A blank **Role-packet workspace** (`packet_root`) now uses this agent's own folder inside its data directory
(`<agent data dir>/workspace`), so agents on one machine never share files.

If your agent relied on the old blank setting to read `~/CareerCoach`, set `packet_root: ~/CareerCoach` in
**Settings → Career Coach → Role-packet workspace** before upgrading to keep using that folder. Otherwise
your role packets and `Experience.md` stay where they are and aren't moved. The plugin logs a one-time
warning when it sees a populated `~/CareerCoach` it no longer reads.

## 0.8.0

A relative `packet_root` or `CAREERCOACH_PACKET_DIR` now resolves under your home directory, not the
server's working directory. This only matters for source or dev runs: on the desktop app the working
directory is `/`, which is read-only, so a relative workspace never worked there. An absolute path or `~/…`
behaves as before.

## 0.7

The profile and tracker moved from `~/.protoagent/careercoach/<instance>/` to this agent's plugin store,
`<instance_root>/careercoach/`. A store found in the old place is copied forward once, and the old folder
gets a `MIGRATED-TO` note. The old files stay for a rollback but are never adopted again, so wiping the new
store really does start over. If you restore a machine from a backup taken before 0.7, that note is already
there, so copy the files across by hand or delete the note.
