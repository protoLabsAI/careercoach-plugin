# Career Coach "Jobs" view: redesign spec

Status: **approved by the owner, 2026-10-31**. Owner: designSystem. Date: 2026-10-31. Rev 2: corrected against the owner's real data.
Interactive preview: see §12.
Replaces the single scroll in `careercoach-plugin` (the view shipped in #47; source: `_DASHBOARD_HTML` in `__init__.py`).
Volumes it is designed for: **49 inbox · ~2 saved/preparing · 15 applied · 1 interviewing · 0 offer · 86 closed (83 rejected, 3 passed).** Today the page is 18,205px tall and the pipeline starts 4,453px down.
Data facts the design rests on (owner's live instance, rev 2):
- Inbox `score` is a keyword title match, not a fit score. Watch rows are 100 or 0. Market sweeps can produce partial scores (e.g. 67).
- 96 of 101 applications have `fit_score` 0, which means unscored.
- No application has a stored kit yet.
- There is no interview-date field. An application has company, role, status, date, updated, history, fit_score, notes, source, posting, id, and `followup` from the server.
- Ghost-job warnings are rare.

**How to read this.** The spec wins where the spec and preview differ. Where the spec is silent, follow the preview. "+N more" expands the Today band in place to show every item. Block a slice only for something that can't be built, not for a small gap these rules settle.

## 1. Principles
1. **State first, list second.** The first screen answers "what needs me today?" and "where does everything stand?". Nobody should scroll to reach the pipeline.
2. **One question per surface.** Triage lives in Inbox, standing lives in Pipeline, sending lives in the drawer. They are tabs, not one stack.
3. **Dead roles stay out of the way.** Closed is counted, not shown. 86 cards of history never sit in the working board.
4. **Controls appear where there is something to act on.** No per-row button pairs or per-card selects at rest; actions show on focus, hover, selection or in the drawer.
5. **Only show signal.** A keyword-match percentage that mostly reads 100, or a 0 that means "not scored", is noise. Show it as text, or don't show it.
6. **Early is the point.** Recruiters read newest-first, and this product exists to reach a role early. Freshness organises the Inbox.
7. **House style.** `/_ds/plugin-kit.css` classes and `--pl-*` tokens only. No custom accent, no hex fallbacks, works in light and dark.

## 2. Information architecture
```
┌ Header: Career Coach / Jobs · "Targeting: …" ··························· [About you] ┐
├ TODAY band (only items that need the operator; max 3 rows, "+N more" expands)   ┤
│  ⚠ 3 applications have had no reply in 10+ days — follow up      [Review]          │
│  ● Interviewing: Staff Engineer @ Northwind, prep not started    [Prep]            │
│  ✦ 7 new roles since your last visit                             [Triage]          │
├ Tabs: [ Pipeline 18 ] [ Inbox 49 ] [ Closed 86 ]                                    ┤
│  body of the active tab (fills the remaining height, scrolls inside itself)         │
└─────────────────────────────────────────────────────────────────────────────────────┘
                     Job drawer (right) opens over any tab · About-you drawer (right)
```

| Region | Contents | Shows when |
|---|---|---|
| Header | eyebrow `Career Coach`, title `Jobs`, target-roles subline (hidden below 600px), **About you** ghost button | always |
| Today band | follow-ups due (server `followup`), roles in Interviewing (needs prep; no date exists), offers awaiting a decision, Saved/Preparing roles with no kit, count of inbox roles new since the last visit (fallback: last 24h) | at least one item exists; hidden otherwise |
| Tabs | Pipeline (default), Inbox, Closed, each with a `Count` | always (counts hide at 0) |
| Pipeline tab | board: Saved · Preparing · Applied · Interviewing · Offer, plus a collapsed **Closed** drop target | default tab |
| Inbox tab | grouped, sortable triage list | — |
| Closed tab | searchable, paged table of dead roles | — |
| Job drawer | posting, fit, apply kit, notes, timeline, contacts, actions | a row/card is opened |
| About-you drawer | profile, fit rubric, "exactly what it's told each turn" | About you pressed |

Why this shape:
- **Pipeline is the default tab** because it changes least and matters most. The Today band covers what changes daily (new roles, nudges), so the operator never has to open Inbox to learn whether Inbox matters.
- **Tabs, not one scroll.** Each question gets the full height. The page itself never scrolls past one viewport; the active tab body scrolls internally.
- **Closed leaves the board.** It stays a drop target (the six-column contract and "drag to Closed" both hold), but it renders as a narrow stub reading `Closed · 86`, linking to the Closed tab.
- **The drawer is the one place you act on a single role**, whichever tab you came from. Inbox rows open it too, so a posting can be read before Keep.

Implementation stays **server-rendered HTML + vanilla JS** in the iframe, using `/_ds/plugin-kit.css` classes. Nothing here needs React; a framework rewrite would add a build step for no user-visible gain. The base-path logic (`location.pathname.split('/plugins/')[0]`) stays as is.

## 3. The three tabs

### Pipeline (default)
- `pl-board` with five `pl-board-col` columns: **Saved · Preparing · Applied · Interviewing · Offer**, then a **Closed stub**, a narrow column showing only `Closed · 86` and "Drop here to close". It takes drops and links to the Closed tab. The six-column contract holds; the 86 cards never render on the board.
- A card shows role, company and age (`12d`). Fit shows only when `fit_score > 0` (today 5 of 101); a card never shows a 0. The status pill goes: the column already says it. If the server flagged a follow-up, the card adds one `pl-chip--warning` line, "No reply in 12 days".
- Each column sorts by what needs attention: flagged follow-ups first, then oldest move. Applied (15) gets no paging; its body scrolls inside the column.
- **Moving without drag (WCAG 2.5.7).** The per-card "Move to…" select is removed. A focused card takes **Space** to pick up, **←/→** to change column and **Space/Enter** to drop (announced in `role="status"`). The drawer header also has a **Status** `DropdownSelect`, the visible non-drag path for pointer users.
- A drop on Closed opens the rejected / passed chooser (a `pl-dialog`, Passed as the default). Undo shows in the toast for 6s.

### Inbox (49)
- **Grouped by freshness**, each a `pl-accordion` item with a `Count`: **Today**, **This week**, **Older**, then **Title didn't match**, which holds **only rows with score 0**. Any score above 0, a partial one like 67 included, stays in its freshness group. Today and This week open by default. Older and Title didn't match start collapsed with their count showing. Within a group, rows run newest first.
- New-since-last-visit rows carry a `pl-dot--info` plus the text "New". The last-visit time is stored client-side; the fallback is the last 24h.
- The toolbar has a **Sort** `DropdownSelect`: Newest (default) / Oldest. Freshness is the only sort that carries signal. The **Hide likely ghost jobs** filter `Chip` renders **only when at least one ghost warning exists**, and shows its count.
- A row shows title, company, posted date ("2h ago", "3d ago") and the match reason **as text**: "Title matches *Staff Engineer*" (score 100), "Title partly matches *Platform Engineer*" (score between 0 and 100), or "Title doesn't match your targets" in the 0 group. The numeric score is never shown. A ghost-job warning shows as a `pl-badge--warning` "Possible ghost job" with the evidence in a tooltip.
- **Keep / Dismiss / View posting appear only on hover, focus or selection.** At rest a row carries no buttons. Keyboard: ↑/↓ moves through rows, **K** keeps, **D** dismisses, **Enter** opens the drawer, **O** opens the posting. The shortcuts are listed in a `pl-kbd` hint under the toolbar.
- **Multi-select.** A checkbox shows on hover or focus. Choosing any row turns the toolbar into "3 selected · Keep · Dismiss". Each group header also has **Select all**, which is how you clear the "Title didn't match" or "Older" group in one move.
- Keep moves the role to Saved, and the toast says "Kept — in Saved". Dismiss removes the row with a 6s Undo.

### Closed (86)
- A `pl-table` with columns Role · Company · Outcome (Rejected / Passed `pl-badge`) · Closed on, newest first, **25 rows per page**.
- The toolbar holds a search `pl-input` (role or company) and an Outcome `Tabs variant="segmented"`: All · Rejected · Passed.
- A row opens the drawer read-only, apart from a **Reopen** action that moves the role back to Saved.
- Above the table sits a single summary line: "86 closed · 83 rejected · 3 passed". It is the only analytics on the page.
- The table has no Fit column, since almost every closed role is unscored. Where `fit_score > 0`, fit shows in the drawer only.

## 4. Job drawer
`Drawer side="right"` (`pl-drawer--right`), 560px, capped at the panel width. Inbox rows, board cards and Closed rows all open it. The sections run top to bottom by how often they're used:
1. **Header:** role, company, the Status `DropdownSelect` (the non-drag move), View posting, close ✕.
2. **Next step:** one primary button that depends on state. Inbox: **Keep**. Saved or Preparing with no kit: **Build apply kit**. Kit ready: **Mark applied**. Interviewing: **Prep me for this interview**. The other chat actions sit in a `Menu` labelled "Ask your coach".
3. **Apply kit.** Today no application has a kit, so the **no-kit state is the main design**, not a fallback:
   - It is one `pl-card` titled "Apply kit" with a three-step checklist (`pl-steps`): **Résumé** · **Cover letter** · **Form answers**. Each step reads "Not started", with one sentence on what the coach will do ("Tailors your résumé to this posting's title and requirements").
   - The primary action is **Build apply kit**, which drafts the existing kit message into the composer. A muted line under it reads "Your coach builds all three in chat. You review each before anything is sent."
   - Saved standard answers that already exist show under Form answers as "3 saved answers will be reused", so the kit isn't empty-handed.
   - Once a kit exists, each step turns into its block (résumé path, letter text, answers) with Copy. A draft keeps its `pl-badge--warning` "Draft: confirm before using", and a missing part still shows as its step with "Not started".
   - It is open by default on Saved and Preparing, and collapsed elsewhere.
4. **Fit:** score, verdict and reason, **only when `fit_score > 0`**. The section is hidden when unscored; there is no "Not scored yet" placeholder.
5. **Notes:** autosaves on blur and shows "Saved" status text. The Save button goes.
6. **Timeline** and **Contacts** (Contacts hidden when empty). Both start collapsed.
Mark applied keeps its inline confirm (a `pl-dialog`, never `window.confirm`). The chat actions still only draft into the composer and never send.

## 5. About-you drawer
"What your coach knows about you", "Fit rubric" and "Exactly what it's told each turn" move out of the page scroll into one `Drawer`, opened by **About you** in the header, with three `Accordion` items. All three stay read-only, with the line "To change anything, tell your coach in chat." Settings stay in Settings ▸ Career Coach.

## 6. Progressive disclosure: hidden vs removed
**Always visible:** header, Today band (when it has items), tab strip with counts, the active tab's body.

**Hidden until needed:**
- Keep / Dismiss / View posting on Inbox rows: hover, focus or selection.
- Worth a look (50–74) and Long shot (<50) groups: collapsed, count showing.
- The 86 closed roles: on their own tab, paged 25 at a time.
- Profile, rubric and injected context: the About-you drawer.
- Timeline, contacts, the secondary chat actions: collapsed in the job drawer.

**Removed outright:**
- the per-card "Move to…" `<select>` (keyboard pick-up + the drawer Status select replace it);
- the status pill on board cards (the column says it);
- the Closed column's card stack (a counted stub instead);
- the coaching-tips paragraph at the page foot (it moves to the brand-new empty state, §7, and to the chat);
- the drawer's "Save notes" button (autosave);
- the `h1` in accent colour and every hex fallback in `var(--pl-…, #…)`;
- the hand-rolled `.btn`, `.pill`, `.card`, overlay CSS (replaced by `pl-btn`, `pl-badge`, `pl-card`, `pl-dialog`, `pl-drawer`).

## 7. States

| State | What shows |
|---|---|
| **Brand-new agent** (no profile, nothing anywhere) | No tabs, no Today band. A single `Empty`: "Your coach hasn't started a search yet", description "Run /setup-coach in chat. It builds your profile, then a watch starts surfacing roles here." Action: **Start setup** (drafts `/setup-coach` into the composer). Under it, three `pl-suggestion` chips from the old tips paragraph: "Paste a posting and say /apply", "Critique my CV", "Run a mock interview". A stranger's first run ends on this screen. |
| **Profile set, watch not run yet** (`watch_last_run` empty on `/jobs`) | Tabs show. Inbox `Empty`: "Your watch is looking. New roles land here." Pipeline `Empty`: "Keep a role from Inbox, or paste a posting in chat." |
| **Inbox cleared** (0 new, pipeline has items) | Inbox `Empty` "All caught up", with the date the watch last ran (`watch_last_run` on `/jobs`). The Inbox count hides. |
| **Nothing needs you today** | The Today band is hidden entirely. Tabs move up. |
| **Loading** | `pl-skel` rows in the band and tab body; the tab strip renders at once with no counts. |
| **Partial failure** (`/state` fails, `/jobs` OK) | The view renders. The About-you drawer shows a `Callout tone="warning"` "Couldn't load your profile". |
| **API unreachable** (`/jobs` fails) | `Banner tone="error"` "Couldn't load your jobs" with **Retry**; tab bodies stay as skeletons. |
| **Move / keep / dismiss fails** | Optimistic change rolls back. `pl-toast--error` "Couldn't move that role. It's back where it was." |
| **Undo a move** | The toast's Undo calls `POST /applications/{id}/undo-move`, never a second move: it restores the prior status (legacy included), `updated` and history, so the follow-up clock and "oldest move" sort are untouched. If it fails, reload the board and show the error toast "Couldn't undo that. The role stayed where you moved it." |
| **Unknown legacy status** | An "Other" column appears at the end of the board only when such rows exist (current behaviour, kept). |
| **Live refresh** (`careercoach.#` event) | Re-render in place. Keep scroll, open groups, focus and the open drawer. Never jump tabs. |
| **A source didn't answer** | The Inbox status line or a Today band row, per §13. Nothing new when every source answered. |

## 8. Narrow widths and split-screen
The layout reads the view root's width through a container query, not the viewport, so the rail panel and split-screen both work.
- **≥ 900px:** the layout above. The board's five columns share the width, and the Closed stub is 120px.
- **600–899px:** the board wraps to three columns across, so every stage stays on screen with no sideways scroll. The console's rail and Work panel take about 495px, so a 1280px window lands in this band. The Today band and Inbox stay full width. The drawer becomes full width.
- **< 600px (rail panel):**
  - The Pipeline tab changes from a board to a **stacked list grouped by stage**: Saved, Preparing, Applied, Interviewing, Offer, each an `Accordion` item with its `Count`. Stages with items open; empty stages collapse to one line. Drag is off, and moving goes through the drawer Status select or keyboard pick-up (↑/↓ between stages).
  - Tabs use `responsive`, collapsing to a menu trigger if the counts don't fit.
  - The Today band caps at 2 rows plus "+N more".
  - The header's target-roles subline is hidden.
  - The Closed table drops the Company and Closed-on columns; both move under the role as one muted line.
  - The drawer becomes `side="bottom"` at 85dvh.

## 9. Component map (all exist in the kit today; no gap issues needed)
| Part | Kit class / @protolabsai/ui |
|---|---|
| Header, About you | `pl-panel-header`, `pl-eyebrow`, `pl-btn--ghost` |
| Today band | `pl-callout--info` container with `pl-row` items, `pl-chip--warning` for follow-ups, `pl-btn--sm` |
| Tabs + counts | `pl-tabs` (`Tabs`, `responsive`), `pl-count` |
| Board | `pl-board`, `pl-board-col`, `pl-board-card` |
| Inbox groups | `pl-accordion`, `pl-checkbox`, `pl-badge--warning`, `pl-tip`, `pl-kbd`, `pl-dropselect`, `pl-chip` |
| Closed | `pl-table` + `pl-tr--interactive`, `pl-input`, `pl-tabs--segmented`, `pl-badge` |
| Drawers, dialogs | `pl-drawer--right` / `--bottom`, `pl-dialog`, `pl-menu` |
| Feedback | `pl-toast` / `pl-toast-stack`, `pl-banner--error`, `pl-callout--warning`, `pl-skel` |
| Empty | `pl-empty`, `pl-suggestion` |
| No-kit checklist | `pl-card`, `pl-steps` / `pl-step`, `pl-btn--primary` |
| "New" marker | `pl-dot--info` + text |

## 10. Accessibility requirements (definition of done)
- Tabs follow the WAI-ARIA tab pattern (arrow keys, `aria-selected`, counts inside the accessible name: "Inbox, 49").
- Inbox is a `listbox` with `aria-multiselectable`; K / D / Enter / O shortcuts work only while focus is in the list, and never fire inside a text field.
- Board drag has a keyboard and a visible pointer alternative (WCAG 2.1.1, 2.5.7); every move is announced in one `role="status"` region.
- Drawer and dialogs trap focus, close on Esc, and return focus to the row or card that opened them. Live refresh never steals focus.
- Freshness, "New", ghost warnings and follow-ups never rely on colour alone: each carries text ("New", "Possible ghost job", "No reply in 12 days").
- Contrast: tokens only, checked in light and dark (1.4.3, 1.4.11). Honors `prefers-reduced-motion`.
- Hover-revealed actions are also revealed on focus (2.1.1) and persist while hovered (1.4.13).

## 11. Decisions
1. "New since your last visit" uses a last-visit timestamp in the iframe's `localStorage`, read in a `try/catch`. If storage isn't available, fall back to the last 24 hours. No server route, so it's per browser.
2. Freshness groups cut by `posted`. A role with no `posted` date uses `first_seen`.
3. Keep goes straight to Saved, with no question.
4. Closed shows 25 rows per page, newest first.

## 12. Interactive preview
`docs/design/jobs-view-preview.fragment.html` is the approved preview: an HTML fragment that needs `/_ds/plugin-kit.css` around it to render. All data is invented and seeded. Published copy: https://claude.ai/artifact/HTHvERSjqPEKpSNS2BR7eP (private to the owner).
- **Volumes:** 49 inbox (7 today, 21 this week, 12 older, 9 title didn't match, 1 ghost), 2 saved/preparing, 15 applied (3 flagged for follow-up), 1 interviewing, 86 closed (83 rejected, 3 passed). Fit shows on 4 roles only.
- **Controls:** Scenario (Real volumes / Nothing needs you today / Brand-new agent), Width (wide / 720px split / 420px rail), Theme (console / light / dark).
- The no-kit drawer also offers "I applied already: mark applied", so a role applied outside the coach can still move to Applied.

## 13. Source status

The watch searches several job sources each pass. When one doesn't answer, the view says so in one quiet line, and only then. When every source answered, nothing new renders.

### 13.1 What the data covers

- `watch_sources` holds one entry per **market** source the last pass asked: Himalayas, Hacker News, Jobicy, We Work Remotely, Remote OK, JSearch (or JSearch via OpenWeb Ninja), Remotive.
- The company boards are not in it. This section says nothing about them.
- A keyed source with no key saved is not in it, because that is the normal keyless setup. The view never mentions it.
- An entry with `ok: true` is never shown, even when `jobs` is 0. A quiet day is not news.
- An entry with `ok: false` and an empty `note` is never shown. Today that only happens when the month's last fifth of JSearch requests is held back for chat and the daily report, which is normal.

### 13.2 Four kinds of "didn't answer"

The view sorts each `ok: false` entry by its `note`. The first rule that matches wins. Matching is case-insensitive.

| Kind | `note` matches | How it reads | Where it shows |
|---|---|---|---|
| **Resting** | `resumes tomorrow` or `share of the plan` | normal, nothing to do | Inbox status line |
| **Needs a subscription** | `subscribed` or `subscription` | Josh can fix it (on RapidAPI) | Today band |
| **Needs its key** | `key`, `401`, `403`, `unauthori`, `forbidden` | Josh can fix it (in Settings → Career Coach) | Today band |
| **Asked again next pass** | anything else (`timed out`, `didn't answer`, any other text) | normal, the watch retries | Inbox status line |

The "Needs a subscription" rule runs before "Needs its key" because the RapidAPI subscription note also contains the word "key".

### 13.3 Display names

Use the short label: Himalayas, Hacker News, Jobicy, We Work Remotely, Remote OK, JSearch, Remotive. `openwebninja` also shows as **JSearch**. An unknown key shows as itself.

### 13.4 Where it goes

1. **Inbox status line.** One muted line (`muted sm`, `--pl-color-fg-muted`) directly under the Inbox toolbar, above the first group. It sits on its own line at every width, so it never competes with Sort or the shortcut hint. It shows only when at least one source is Resting or Asked again next pass. On the Inbox empty state ("All caught up") the same line sits under the existing description.
2. **Today band row.** Needs its key and Needs a subscription each add one row to the Today band (§2), after the other rows. The band shows on every tab, so Josh sees it from Pipeline. A source in the Today band is left out of the Inbox status line, so nothing is said twice.

No icon colour, no warning or error tone, no badge, no banner. The Today row uses the same neutral `●` marker as the Interviewing row, with `aria-hidden="true"`.

### 13.5 Exact copy

`{ago}` is the relative time of `watch_last_run` ("2h ago", "yesterday"), written the same way as Inbox posted dates.

| Case | What shows |
|---|---|
| **All answered** | Nothing new. The Inbox empty state keeps "Your watch last ran on {date}. New roles land here." |
| **One source resting until tomorrow** | Inbox line: `Last pass {ago}: JSearch is resting until tomorrow. Today's share of its requests is used.` |
| **One source timed out** | Inbox line: `Last pass {ago}: JSearch didn't answer in time. The watch asks again next pass.` |
| **One source refusing the key** | Today row: `JSearch isn't accepting the saved key. You can change it in Settings → Career Coach.` Button: **Details**. |
| **Account not subscribed (RapidAPI)** | Today row: `JSearch needs a subscription on your RapidAPI account. Once it has one, the watch uses it on the next pass.` Button: **Details**. |
| **Several not answering** | Inbox line, Resting first, then the rest: `Last pass {ago}: JSearch is resting until tomorrow. Jobicy and Remote OK didn't answer; the watch asks again next pass.` Plus a **Details** link button at the end of the line. A refused key in the same pass still goes to the Today band. |

Wording rules for the line:
- Lists of names: `A`, `A and B`, `A, B and C`. Four or more: `A, B and 2 more`. Every name is in Details.
- Plural verbs: "Jobicy and Remote OK **are** resting until tomorrow."
- A single source that didn't answer in time says "didn't answer in time". A source with any other note says "didn't answer". For several, the line always says "didn't answer".
- Never use "failed", "error", "broken" or "problem" in any of this copy.

### 13.6 Truncation rule for long notes

The line and the Today row never show a raw `note`. They always use the fixed copy above. The full `note` is only shown under **Details**.

- **Details** is a `pl-btn--ghost pl-btn--sm` button with `aria-expanded`. It is present when the line or row carries a source whose note adds something: any note other than `timed out`, `didn't answer` or the resting sentence.
- Pressing it shows one muted line per source under the line or row: `{Name}: {note}`, the note in full and wrapping. Pressing it again hides them.
- Details stays open across a live refresh (§7) while the same sources are listed.

### 13.7 Behaviour and accessibility

- It renders from `/jobs` data only and re-renders on live refresh in place. It is not a live region and announces nothing, so a background pass never interrupts a screen reader.
- If `watch_last_run` is empty (the watch hasn't run), nothing here shows. §7's "Your watch is looking" state stands.
- Text carries the meaning; nothing relies on colour (1.4.1). Muted text uses `--pl-color-fg-muted` on `--pl-color-bg`, which the kit already clears for AA in light and dark.
- Widths: the line is a block that wraps. At 600–899px and below 600px it wraps under the toolbar with no sideways scroll. The Today rows follow the band's existing cap (3 rows, or 2 below 600px, with "+N more").

### 13.8 Preview

`docs/design/jobs-source-status-preview.fragment.html` (an HTML fragment for `/_ds/plugin-kit.css`, invented data). Controls: Case (all six from §13.5), Inbox (with roles / cleared), Width (wide / 720px / 420px), Theme.

### 13.9 Open decision for Josh

The view finds the kind by matching words in `note`, because that is all PR #141 sends. That works for the notes we know. If the server later adds `"kind": "resting" | "key" | "subscription" | "retry"` to each entry, the view uses it and stops matching words. That change is small and optional; this spec works without it.

Approved by the owner on 2026-11-18. §13.9 is settled: the server sends `kind` on each entry (v0.17.7), and the view reads the note's words only when `kind` is absent.
