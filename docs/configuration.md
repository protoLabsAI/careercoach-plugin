# Configuration

Every setting lives under **Settings → Career Coach**, and in YAML under `careercoach:` in your agent's
`config/langgraph-config.yaml`. The job-source API key is stored in the gitignored `secrets.yaml`.

## Your profile basics

| Setting | Key | Default | What it does |
|---|---|---|---|
| Your name | `full_name` | blank | Used on generated CVs and cover letters, to greet you in the view, and as the starting draft for your name answers when the profile has no name |
| Location | `location` | blank | City or country and any commute limits. Feeds the location check in job evaluation |
| Target roles | `target_roles` | blank | The role types you're aiming for, comma-separated. Seeds strategy and search |

## Documents

| Setting | Key | Default | What it does |
|---|---|---|---|
| Document format | `render_format` | `html` | `html` renders through the artifact plugin to a PDF. `docx` makes real Word files (needs cowork, execute_code and protoAgent 0.108.0+). `latex` is kept as an option |
| Role-packet workspace | `packet_root` | blank | The folder for role packets and the workspace starters. Blank uses this agent's own folder inside its data directory |

## Finding roles

| Setting | Key | Default | What it does |
|---|---|---|---|
| Job source | `jobs_provider` | `auto` | Which source market searches use: `auto` (JSearch when a key is set, else Himalayas), `jsearch`, `himalayas`, `hn` or `remotive` |
| Job-source API key | `jobs_api_key` | blank | Optional RapidAPI key for JSearch (Google for Jobs, including LinkedIn and Indeed listings). Stored as a secret |
| Search country | `search_country` | blank | Narrows keyless remote-board results to roles open in this country, for example `United States` |
| Locations | `locations` | blank | Where a role may be: places, plus `remote` for remote and open to the US. Blank means anywhere |
| Exclude titles with | `title_exclude` | `intern, internship, new grad` | Words or phrases that drop a title from scans |
| Max posting age (days) | `max_age_days` | `30` | Board scans skip postings older than this. `0` means any age |

## The background watch and follow-ups

| Setting | Key | Default | What it does |
|---|---|---|---|
| Background job-watch | `watch_enabled` | off | Periodically scans your watchlist and the job source and queues new matches for the digest. Never writes your board |
| Watch interval (min) | `watch_interval_min` | `180` | How often the watch scans, in minutes. The minimum is 5 |
| Watch match threshold | `watch_min_score` | `75` | Only surface postings whose quick keyword score is at least this, from 0 to 100. The coach still runs the full evaluation |
| Follow-up after (days) | `followup_days` | `10` | An applied role with no change for this many days is flagged on the board as due a follow-up |

## Advanced

| Setting | Key | Default | What it does |
|---|---|---|---|
| Browser fill (beta) | `browser_fill` | blank | Opt-in: a comma-separated list of ATS types the coach may fill itself (`greenhouse`, `lever`, `ashby`, `workable`). Blank turns it off. See [applying.md](applying.md) |
| Extended tools | `extended_tools` | off | Registers every older, individual tool name alongside the lean default set. See [tools.md](tools.md) |

## The fit rubric

The rubric's four weights (technical, experience, behavioral and career) aren't settings. They're live
controls the agent can retune in chat with `careercoach_tune` and `careercoach_preset`.

## Environment

`CAREERCOACH_PACKET_DIR` overrides the workspace folder. A relative path resolves under your home directory.
