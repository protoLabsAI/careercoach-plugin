# Using it

Day to day you talk to the coach, and you can work the board by hand in the Career Coach view.
The tools behind each request are listed in [tools.md](tools.md).

## The Career Coach view

Open **Career Coach** from the console's rail. It has three parts:

- **Needs you today.** Applications that have gone quiet, interviews with no prep yet, offers waiting on a
  decision, and saved roles with no apply kit.
- **Pipeline, Inbox and Closed.** Drag a card between Saved, Preparing, Applied, Interviewing and Offer, or
  drop it on Closed and choose rejected or passed. With a keyboard, focus a card, press Space to pick it
  up, use the arrow keys to choose a column and press Space to drop. In the Inbox, hover a new role and
  press **Keep** or **Dismiss** (K and D work too). A dismiss can be undone for six seconds.
- **The role drawer.** Click a card to see its status, the posting, the next step and its apply kit: the
  résumé file, the cover letter and the form answers, each with a copy button. After you submit the
  application yourself, press **Mark applied**.

**About you** shows every profile field the coach holds, every field still missing, and the exact block
it receives each turn. It's read-only: to change anything, tell the coach in chat.

## Skills

The coach loads these on demand. One is also a chat command.

| Skill | What it covers |
|---|---|
| `job-hunt` | Finding roles: the watchlist, board scans, market sweeps, referrals and the morning digest. Also the `/setup-coach` first-run interview |
| `apply` | Evaluating a posting, the apply kit, the gated role packet, and the opt-in browser-fill beta |
| `resume` | Your résumé as one versioned artifact: import, master and tailor, PDF or Word export, an ATS check |
| `interview` | A STAR answer bank and mock interviews with feedback |
| `strategy` | Positioning, which roles to target, offers, negotiation, and a skills-gap learning plan |
| `writing-style` | How anything in your name should read: a human voice, no filler, the honesty test |

## Things to try

- **Coaching.** "Help me think about which roles to target." "Run a mock interview for the Acme role."
  "Critique my CV for this posting." "I got the offer. Help me negotiate."
- **Finding and applying.** "Find remote frontend roles." "Here's a posting, is it worth applying to?"
  (paste a URL or the text). "Build my apply kit for this one."
- **Keeping the board current.** "Cedar & Slate wants a phone screen, Mossbank turned me down, and I
  applied to Fernhollow." "Give me my morning digest."
- **The résumé itself.** "Import this résumé" (drop the PDF or give a path). "Build my master résumé."
  "Tailor it for this role." "Make me a PDF." "Will this get through an ATS?"
- **People.** With **Extended tools** on, import your LinkedIn Connections export and the coach tells you
  who you know at a company before you apply. "Log that I emailed the recruiter, follow up Friday."

## Finding roles

`careercoach_find_roles` has two sources:

- **Your watchlist** (the default). It reads each watched company's own Ashby, Greenhouse or Lever board,
  keyless, with the date each role first went up. It filters to your target roles, locations and maximum
  age, and marks what's new, what you've already tracked, and likely ghost jobs (long open, reposted, or
  thin on detail).
- **The market.** The keyless Himalayas remote board, this month's Hacker News "Who is hiring?", or JSearch
  with an API key.

Both put results in the Inbox. The tracker dedups by posting URL and by normalized company name ("Arize AI"
is "Arize"), warns past three live applications at one company, and can import past applications kept
elsewhere so sweeps don't suggest them again.

## The morning digest

`careercoach_digest` lists fitting roles posted in the last few days that you haven't tracked, contact
follow-ups that are due, applications that have gone quiet, and the pipeline at a glance.

## The apply workflow

For a hands-off run, the `apply` workflow chains three subagents: research the company, evaluate the fit,
then build and save an apply kit.

```
run_workflow("apply", {"posting": "<url or text>"})
```

It stops if the fit is weak rather than forcing a kit. You still file the application yourself.

## Role packets

Say "build the role packet" for the gated version of the same work. The coach files a folder per role
(`Companies/<Company>/Roles/<Role - Req>/`) and produces each piece, from a recruiter brief and evidence map
to the tailored résumé, ATS skills list, cover letter and assembled packet, one approved step at a time.

## Watches

Besides the background job-watch (see [install.md](install.md#background-job-watch)), you can arm a
**WATCH** on your pipeline yourself with `create_watch` and the `careercoach:new_matches` verifier.
