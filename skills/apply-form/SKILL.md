---
name: apply
description: >-
  Evaluate a specific posting, draft its CV or cover letter, build a saved apply kit, or work up a full gated role packet. Use for "should I apply", "apply to this job", or "build the role packet".
tools: [careercoach_role_packet, careercoach_save_apply_kit, careercoach_profile, careercoach_answers, careercoach_render_resume, careercoach_prepare_application, careercoach_track, browser_open, browser_pdf, browser_form_read, browser_select, browser_fill, browser_upload, browser_click]
---

# Build an apply kit

The default is an apply kit that the operator uses to complete the application themselves.
The agent never opens or drives the employer's browser form in this flow. The local résumé
print step below is only PDF generation. Reference external tools by name only — never import
another plugin (ADR 0039).

For a fit assessment or individual CV/cover letter, follow
[Research and documents](application-guide.md) and stop at the requested deliverable.
For an explicitly requested full, filed packet, follow the
[gated packet flow](packet-flow.md), using `careercoach_role_packet` to file artifacts
and pausing for approval before every phase.
Record new operator facts with `careercoach_profile(action="update")` only after the
operator confirms them; honor `do_not_claim` in every mode.
Otherwise follow the kit-first steps below. The `apply` workflow remains the autonomous
research → evaluate → save-kit route. None of these modes authorizes browser submission.

## 1. Evaluate the posting

Use [Research and documents](application-guide.md) to evaluate fit against the operator's confirmed profile
and rubric, including sourced company research. Show strengths, gaps, and the recommendation.
If the verdict is Weak or Poor Fit, say so and stop rather than forcing an application.
Use `careercoach_prepare_application(url, company, role)` for available public questions;
otherwise ask the operator to provide the form questions. A live-form suggestion does not
change this default flow; browser automation belongs only to the opt-in beta below.

## 2. Build the apply kit

- Tailor the résumé using only confirmed profile facts. Show the exact wording and save the
  approved résumé with `careercoach_profile(action="update", field="resume", content=…, mode="replace")` only
  after the operator approves. Never include internal coaching annotations.
  `careercoach_render_resume(company)` → `browser_open <the returned local file:// url>` →
  `browser_pdf("resume-<company>.pdf")` produces the tailored PDF. Keep the returned PDF path
  and its version if known. If rendering refuses or a PDF tool is unavailable, report the
  blocker; do not claim a PDF was built.
- Write a cover letter ONLY if the posting asks for one. Follow the writing-style discipline
  and honesty test; verify company-specific claims against the research sources.
- Get every form answer from `careercoach_answers(action="get")`. Keep CONFIRMED and DRAFT answers
  distinct; drafts stay drafts and must not be presented as approved answers. Ask the operator
  about new or missing questions, and never invent a response or re-ask a confirmed answer.
  Use `careercoach_answers(action="propose")` for proposed standard answers and
  `careercoach_answers(action="confirm")` ONLY after the operator explicitly confirms the exact values.
  Read `careercoach_answers(action="get")` again after confirmation. Nothing is written in the operator's
  name that is not grounded in their confirmed profile or explicit answer.

## 3. Save the kit

Only AFTER rendering the résumé PDF, writing any requested cover letter, and collecting
answers with `careercoach_answers`, pass all three together to
`careercoach_save_apply_kit(company, role, resume_path=<returned PDF path>,
resume_version=<known version>, cover_letter=<text only if requested>,
cover_letter_needed=<whether the posting asks>, answers_json=<JSON question-to-answer object>)`.
Omit an unknown version. `cover_letter` is letter text; `answers_json` accepts a question-to-answer
object/dict or its JSON string, not the answer-bank report. Include the collected form answers,
including one-off answers discussed with the operator; standard answers remain in the answer bank.
This tool stores kit answers as DRAFT even when the
operator supplied them; do not relabel them as confirmed. If no letter is requested, pass
`cover_letter=""` and `cover_letter_needed=false` to clear any stale letter on this role.
Pass `no_form_questions=true` only when there genuinely are no form questions.
If the save reply says DRAFT, fill in the listed missing parts and save again with all three;
report any blocker without claiming the kit is complete.
Check that saving succeeded before announcing the handoff. Saving prepares the role; it does
not mark it applied.

## 4. Hand off to the operator

Tell the operator: Open the role in the **Career Coach view**, review the apply kit and draft
answers, attach the tailored PDF, and fill the employer's form using the **copy buttons**.
After completing and submitting the application themselves, press **Mark applied**.
The agent does not submit or mark the role applied in this default flow. Nothing auto-submits.

## Browser fill (opt-in beta)

This section applies ONLY when the posting's ATS is explicitly enabled in the `browser_fill`
setting, a comma-separated list of ATS types. Blank means off. The browser-fill tools
are optional: check that they are present in the available tool list before calling them. Check the setting through
`careercoach_prepare_application`; a kit handoff or refusal means stay with the default flow.
Pass the posting's actual ATS to `careercoach_plan_fill`; never substitute another ATS to bypass
its refusal. The support notes and live-form fallbacks below are subject to this opt-in gate.
Even when enabled, nothing auto-submits: the operator approval card is mandatory.

### Apply through an ATS form

This is the doctrine for the last mile: taking a verified profile and an evaluated posting and
actually completing the application form in the browser. It ties together tools this plugin owns
(the confirmed standard answers, the fill plan + read-back diff, the submit gate, the handoff) and
the browser tools the **agent_browser** plugin owns (`browser_open`, `browser_form_read`,
`browser_select`, `browser_fill`, `browser_upload`, `browser_pdf`, `browser_click`). Reference every
external tool **by name only** — never import another plugin (ADR 0039). If a browser tool isn't in
your toolset, say so and stop: there is no form to fill without it.

**Two rules stand above the loop, and neither bends:**

- **Never submit without the approval card.** The only path to a submit click is
  `careercoach_request_submit` → the operator approves its interrupt → you click submit exactly
  once. The submit-gate middleware BLOCKS any submit-like click without that live, one-shot grant,
  so there is no point trying to click around it.
- **Never solve or bypass a captcha.** A captcha, a login wall, or a legal attestation is a human's
  job. Fill everything else, then `careercoach_handoff` so the operator completes that one step in a
  visible browser. The coach does not read, answer, or click through a bot check.

### Support scope

- **Greenhouse** — supported, with a head start: `careercoach_prepare_application` fetches the job's
  PUBLIC question schema before the page is even open, so you can resolve genuinely new required
  questions with the operator up front. The live read-back on the open page is still required.
- **Ashby** — supported through the live form. Ashby publishes no public per-job form schema, so
  there is nothing to prepare up front; `careercoach_prepare_application` returns the live-form path.
  Ashby ALWAYS requires a résumé file and its "autofill from résumé" can overwrite typed values, so
  upload the résumé FIRST and re-read every field after.
- **Lever and Workday are NOT supported yet.** There is no adapter and no schema prep for them. You
  can still apply through the live-form path below, but say so plainly to the operator first, go
  slowly, and lean harder on the read-back diff — treat every field as unverified until
  `careercoach_verify_fill` reports VERIFIED.

### The loop

Work the steps in order. Each is a real checkpoint; don't chain them silently past a point where the
operator should see what's happening.

#### 1. Confirm the standard answers
Call `careercoach_answers(action="get")`. Only CONFIRMED answers ever fill a form. If something is a DRAFT or
missing, propose it with `careercoach_answers(action="propose")`, show the operator the exact values, and
confirm with `careercoach_answers(action="confirm")` ONLY on their explicit say-so — never confirm on their
behalf. An improvised answer is exactly the failure this loop exists to prevent.

#### 2. Render and capture the résumé as a PDF
`careercoach_render_resume(company)` → `browser_open <the file:// url>` → `browser_pdf("resume-<company>.pdf")`.
`browser_upload` only accepts files already in the browser plugin's own capture folder, and printing
the opened `file://` page with `browser_pdf` is the only way a file lands there. Keep the path
`browser_pdf` returns — you upload it in step 6 (or step 5 on Ashby).

`careercoach_render_resume` prints ONLY the operator-approved `resume` profile section plus their
`name`. It never reads the coaching record (`location`, `contact`, `headlines`, `roles`, `skills`,
`education`, `do_not_claim`, `stories`, `notes`) — those carry internal coaching annotations that
must never reach an employer (a 2026-10-05 PDF leaked exactly that). The `resume` body is: optional
`Headline:` / `Location:` / `Contact:` header lines and a summary before the first `## ` heading,
then `## ` sections (`### ` role headings and `-` bullets inside). It refuses, writing nothing, when
the body is empty, has no `Contact:` line, or carries a coach annotation (it quotes the lines) —
fix the body WITH the operator and save it with `careercoach_profile(action="update", field="resume", content=…,
mode="replace")` only after they approve; it is never silently stripped.

#### 3. Prepare, then read the live form
- For a Greenhouse URL, call `careercoach_prepare_application(url, company, role)` first — it returns
  a plan from the public schema plus an "Ask the operator about ONLY these" list.
- Open the application and `browser_form_read` the live form. Pass that exact JSON array to
  `careercoach_plan_fill(form_json, company, role, extra_answers, ats)` — set `ats` to the ATS type
  you're applying through (`greenhouse`/`lever`/`ashby`/`workable`), which also tells the fill opt-in
  whether this ATS is enabled; `ats="ashby"` additionally adds the résumé upload. The plan draws values
  ONLY from confirmed answers plus any one-off
  `extra_answers` the operator gave; it never fuzzy-matches a select answer and never invents one.
- The live form is the source of truth. The schema prep is a head start, not a substitute for the
  read-back.

#### 4. Ask the operator about ONLY the unmapped items
The plan ends with an explicit list: required fields with no confirmed answer, selects whose answer
matched no option (with the options), and any answer still only a draft. Ask about THOSE and nothing
else. Confirm any newly agreed draft with `careercoach_answers(action="confirm")`; pass one-off answers back
through `careercoach_plan_fill`'s `extra_answers`. Don't re-ask for anything already confirmed.

#### 5. On Ashby, upload the résumé first
Ashby's autofill reads the résumé and can rewrite fields it thinks it recognises. So on an Ashby
form, `browser_upload` the PDF from step 2 BEFORE filling anything else, then `browser_form_read`
again so the plan reflects what autofill did.

#### 6. Fill in the planned order
Follow the plan line by line:
- `browser_select` for every dropdown and every phone **country** picker — and always set the
  country BEFORE the number, because changing the country rewrites the number field.
- `browser_fill` for text inputs.
- `browser_upload` for file fields (the résumé PDF, and anything else the plan marks as an upload).
- For a self-ID select whose options the reader couldn't list (Gender, Hispanic/Latino, Veteran
  status often read back with no options), the plan carries your stored "Decline to self-identify"
  verbatim. Pick the form's OWN decline-to-answer option — its exact wording varies ("Decline To
  Self Identify", "I don't wish to answer") — and `careercoach_verify_fill` accepts any decline
  wording for that field, so you don't need its exact text.

#### 7. Read back and verify until VERIFIED
`browser_form_read` the filled form and pass it to `careercoach_verify_fill(session_id, form_json)`.
It reports `VERIFIED` only when every planned field holds its planned value and no required field is
empty; otherwise it lists the mismatches. Fix them and verify again. Do not move on until it says
VERIFIED — a react-select that silently reverted is exactly what this catches.

#### 8. Hand off for a captcha, login, or attestation
If the page throws a captcha, a login/sign-in wall, or a legal attestation only the operator can
agree to, call `careercoach_handoff(session_id, reason)` with `reason` one of
`captcha` / `login` / `attestation` / `other`. It pauses for the operator to complete ONLY that step
in the visible browser (the Browser panel, or the real window if the browser's `headed` setting is
on) and the coach touches none of it. A captcha or login can re-render the form and clear filled
fields, so when the operator reports Done, go back to step 7: `browser_form_read` and
`careercoach_verify_fill` again before anything else. If the turn is headless, the handoff refuses
without pausing — stop and report that a human has to finish the step interactively.

#### 9. Request submit, then click exactly once
Only once the form is VERIFIED, call `careercoach_request_submit(session_id)`. It refuses — without
asking anyone — unless the session is VERIFIED and unchanged since, and it refuses on a headless
turn. On the operator's explicit approval it unlocks exactly ONE submit click for 120 seconds. Then
`browser_click` the form's real submit button ONCE. A job board's "Apply" / "Apply now" button only
opens the form and is not the submit — don't spend the gate on it.

#### 10. Confirm and track
After the click, `browser_form_read` (or a snapshot) to confirm you're on the confirmation page, not
still on the form with a validation error. Then record it with `careercoach_track(action="track", company=company, role=role, status="applied", source=<url>)`.

### Anti-fabrication, restated
Every value that fills a field comes from a CONFIRMED standard answer, an operator-supplied one-off,
or the verified profile the résumé is rendered from. Nothing is guessed, no select answer is nudged
to a nearest option, and no required field is left to the model's imagination. If you can't fill a
field truthfully from what the operator has confirmed, ask — don't invent.
