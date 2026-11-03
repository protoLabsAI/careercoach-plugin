# Install and setup

Get the coach running on a protoAgent agent. Every setting is described in
[configuration.md](configuration.md).

## Requirements

- **protoAgent 0.34.0 or later.** The manifest declares `min_protoagent_version: 0.34.0`, and the loader
  refuses the plugin on older cores. Some features need a newer host, noted where they come up: the
  agent-made résumé PDF needs `browser_pdf` (protoAgent
  [#3451](https://github.com/protoLabsAI/protoAgent/pull/3451)), and Word export needs 0.108.0.
- **A model.** Bring your own: a gateway, a local model or a subscription. The coach adds no hosted service.
- Optional plugins it works with when they're enabled: **artifact** (the résumé as a versioned document),
  **agent_browser** (PDFs and, in the beta, form filling), **cowork** (Word files and your own writing voice)
  and **execute_code** (Word files).

## The fast way

In the protoAgent desktop app (v0.221.0 or later), create a new agent and pick **Career Coach**. It installs
the plugin and the coach persona together. Paste your résumé or a LinkedIn PDF and it builds your profile,
then runs a first scan from keyless job sources.

## On an agent you already have

1. **Install it.** In a running protoAgent: **Console → Plugins → Discover → Career Coach → Install**. Or from
   the shell:

   ```sh
   python -m server plugin install https://github.com/protoLabsAI/careercoach-plugin
   ```

2. **Enable it.** Turn it on in **Console → Plugins**, or ask the agent to `enable_plugin("careercoach")`.
   Installing fetches code, and enabling is the trust decision.
3. **Set the basics.** In **Settings → Career Coach**, enter your name, location and target roles. That's
   enough to start.
4. **Give it the coach persona (recommended).** The plugin ships one in [`SOUL.md`](../SOUL.md). Enabling a
   plugin never changes your agent's identity, so adopt it on purpose: paste it into **Settings → Identity**
   (or your agent's `config/SOUL.md`) and change the name line to your agent's name. The coach's voice rules
   ride along on every turn either way, so the persona mostly adds depth.
5. **Run `/setup-coach`.** It checks what it already knows (memory, saved files, a résumé you've shared),
   asks only about the gaps, fills your story bank, captures your writing voice, and records the result as
   your profile. Every step can be skipped, and you can stop and pick it up later.

## Optional extras

### Choose where your workspace lives

**Settings → Career Coach → Role-packet workspace** sets the folder for role packets and the fill-in
starters (`Resume/Experience.md`, `Agent/story-bank.md`, reviewer and Humanize rules, an improvements log).
Blank uses this agent's own folder inside its data directory, so agents on one machine never share files.
The starters are plain markdown you can edit at any time, and the coach never overwrites your edits.

If you'd rather write your history into `Experience.md` than be interviewed, do that and ask the coach to
import it. It shows you exactly what it would add to your profile and records it once you confirm. It never
writes over that file.

### Word files instead of PDF

Set **Document format** to `docx` and the CV and cover letter come out as editable Word files, saved as
versioned, downloadable artifacts. This builds the file by running Python (`python-docx`), so it needs:

- the [cowork](https://github.com/protoLabsAI/cowork-plugin) plugin (its `docx` skill),
- the **execute_code** plugin enabled, which lets the agent run code, so enabling it is a trust decision,
- the **artifact** plugin,
- protoAgent 0.108.0 or later. On the desktop app, 0.108.0 is the first version where code execution works:
  the first run downloads a managed Python runtime once per machine, with your consent.

If anything is missing, the coach falls back to HTML and PDF and tells you what's absent. Leave it on `html`
if you'd rather not enable code execution.

### Point the search at you

Set **Target roles** (comma-separated, each one an all-words title match), **Locations** (places plus
`remote`, which means remote and open to the US, for example `remote, portland`), **Exclude titles with**
and **Max posting age**. Then build a watchlist: "add Anthropic, OpenAI and Vercel to my watchlist". Each
company is looked up on Ashby, Greenhouse and Lever.

### Wider market search

Add a [RapidAPI JSearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) key under **Job-source API
key** for Google-for-Jobs coverage, which includes LinkedIn and Indeed listings and local roles. Without a
key, search uses the keyless Himalayas remote board (set **Search country** to narrow it) or Hacker News
"Who is hiring?". Keyless results are loose, so they're filtered and ranked against your query locally, and
every result says which source it came from.

### Background job-watch

Turn on **Background job-watch** and, every **Watch interval**, it queues new matching roles for your digest
and lights the rail icon. It never adds roles to your board by itself. It's off by default.

### Tune the fit score

The fit rubric's weights are live controls: `careercoach_preset growth-first`, or
`careercoach_tune weight_career 45`, reweights scoring straight away.
