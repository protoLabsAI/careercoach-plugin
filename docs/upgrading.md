# Upgrading

Version notes that change behavior. Release notes for every version are on the
[releases page](https://github.com/protoLabsAI/careercoach-plugin/releases).

## 0.17.7

- **The watch queues the best new roles first.** A pass over every role and source finds far more new
  postings than the fifteen it looks up and queues. It used to take the fifteen newest. It now scores
  them all first, takes the fifteen strongest (newest among equals), and settles at once any whose full
  posting already puts them under your bar, so they stop waiting in line.
- **Each source report says what you can do.** `watch_sources` entries that didn't answer carry a
  `kind`: `resting` (a quota or cooldown that ends by itself), `key` (the saved key is rejected),
  `subscription` (the vendor account needs one) or `retry` (the watch asks again next pass). A source
  held back by the monthly reserve now says so.

## 0.17.6

- **SmartRecruiters boards can be watched.** Adding a company to the watchlist now also looks for it on
  SmartRecruiters, where many large employers post (ServiceNow, Bosch, Experian, Western Digital). These
  boards can hold thousands of roles, so a scan searches them once per target role and reads the full
  text only for titles that match.
- **Paste a board's URL to pin it.** `careercoach_watchlist(action="add", company="ServiceNow",
  slug="https://jobs.smartrecruiters.com/ServiceNow")` works, as do Ashby, Greenhouse and Lever board
  links. A SmartRecruiters name with no postings is refused, because SmartRecruiters answers a misspelled
  company with an empty board instead of an error.
- **A role found elsewhere uses your watchlist pin.** When a role from Himalayas, JSearch or a feed is
  looked up on the company's own board, a company on your watchlist is read from the board you pinned.
- **Workday is still not supported.** Its careers sites answer this plugin with "too many requests" from
  the first request, so Workday companies need a web search, as before.

## 0.17.5

- **Every chat knows which applications are in motion.** Applications at an interview or offer stage
  are listed in front of every turn, each with its latest tracker note. A new chat no longer describes
  an interview by what an older chat knew. The coach is also told to update the tracker in the same
  turn you report progress.
- **The digest says when a job source didn't answer.** Each watch pass records which market sources
  answered. If one timed out, refused the key or used its day's requests, the next digest names it and
  says the others were searched as usual. The same report is in the Jobs data as `watch_sources`.
- **The company's own posting decides where a job is.** When a board lists a role as remote and the
  company's own posting says a city, the card takes the company's location, and the watch checks it
  against your locations again. A board's "Remote (US)" for an on-site role no longer gets through.
- **A recruiter is known by its own words.** A posting that says "our client", "on behalf of a client"
  or "confidential employer" is capped like a recruiter's listing, whatever the company name.
- **Aggregator page titles are trimmed.** "Software Engineer job at CGI in Knoxville" becomes
  "Software Engineer".

## 0.17.4

- **Another field's engineer no longer scores as a match.** Every word of "Design Engineer" is also in
  "Utility Infra Design Engineer", and "Automation Engineer" is in "Pharmacy Process & Automation
  Engineer". Titles naming another discipline (marketing, network, pharmacy, civil, mechanical,
  electrical, manufacturing and similar) are now treated as off-domain and capped at 45, unless your
  profile names that field.
- **Recruiter and reposter listings can't top the inbox.** A posting whose "company" is a recruiter or a
  reposting site (Jobgether, Recruiting from Scratch, anything "Staffing" or "Recruiting") is capped at
  55 and says why. It still appears in a search you run; it stays under the default inbox bar of 65.
- **One card per role across sources.** The same opening from a company's own board, Himalayas and a
  feed made one card each. The inbox now keeps the first and notes the other sources on it.

## 0.17.3

- **The watch searches the market for every target role.** It used to search for the first five only.
  Roles that are respellings of each other ("Full Stack Engineer", "Fullstack Engineer", "Software
  Engineer Full-Stack") share one search. Free search boards are asked about every role each pass, and
  the feeds are read whole.
- **A keyed source takes your roles in turn.** JSearch is asked about the next few roles each pass
  (**Keyed searches per watch pass**, default 3) and continues where it stopped, so the whole list is
  covered over a few passes within the plan's allowance.
- **A JSearch timeout costs one search.** One slow answer used to end JSearch for the pass. Now the watch
  moves on to the next role, and stops only after two timeouts in a row or a refusal. JSearch requests
  wait up to 45 seconds, since one in five took longer than 20.
- **A timed-out request still counts against the allowance.** The request was sent, so the budget
  assumes it was spent.

## 0.17.2

- **Three more free job sources.** Job source accepts `jobicy` (remote roles open to the US, with pay),
  `weworkremotely` (the programming and design feeds) and `remoteok`. They need no key. Each is a feed
  of recent postings, downloaded at most once an hour and matched against your search locally. Every
  role links back to the board it came from.
- **Roles from JSearch and the feeds get the company's own apply link.** JSearch links to whichever
  aggregator syndicated a posting (JobLeads, Jobgether, Indeed). The watch now looks each company up on
  Ashby, Greenhouse and Lever, as it already did for Himalayas, and shows the company's own posting when
  it finds one. The aggregator link stays as the fallback.

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
