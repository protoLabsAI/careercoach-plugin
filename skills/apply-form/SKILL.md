---
name: apply-form
description: >-
  Use when the user wants the coach to actually FILL OUT and submit a job application form in
  the browser — "apply to this for me", "fill in this application", "complete the Greenhouse/
  Ashby form", "submit my application". The end-to-end form loop: confirm standard answers,
  render + attach the résumé, read the live form, plan the fill from CONFIRMED answers only,
  ask only about what's unmapped, fill, verify the read-back, hand off for a captcha/login,
  and submit ONCE behind the operator approval card. For scoring a posting or drafting the
  CV/cover letter see job-application-assistant; for the résumé document itself see resume.
tools: [careercoach_get_answers, careercoach_propose_answer, careercoach_confirm_answers, careercoach_render_resume, careercoach_prepare_application, careercoach_plan_fill, careercoach_verify_fill, careercoach_handoff, careercoach_request_submit, careercoach_track_application, browser_open, browser_pdf, browser_form_read, browser_select, browser_fill, browser_upload, browser_click]
---

# Apply through an ATS form

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

## Support scope

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

## The loop

Work the steps in order. Each is a real checkpoint; don't chain them silently past a point where the
operator should see what's happening.

### 1. Confirm the standard answers
Call `careercoach_get_answers`. Only CONFIRMED answers ever fill a form. If something is a DRAFT or
missing, propose it with `careercoach_propose_answer`, show the operator the exact values, and
confirm with `careercoach_confirm_answers` ONLY on their explicit say-so — never confirm on their
behalf. An improvised answer is exactly the failure this loop exists to prevent.

### 2. Render and capture the résumé as a PDF
`careercoach_render_resume(company)` → `browser_open <the file:// url>` → `browser_pdf("resume-<company>.pdf")`.
`browser_upload` only accepts files already in the browser plugin's own capture folder, and printing
the opened `file://` page with `browser_pdf` is the only way a file lands there. Keep the path
`browser_pdf` returns — you upload it in step 6 (or step 5 on Ashby).

### 3. Prepare, then read the live form
- For a Greenhouse URL, call `careercoach_prepare_application(url, company, role)` first — it returns
  a plan from the public schema plus an "Ask the operator about ONLY these" list.
- Open the application and `browser_form_read` the live form. Pass that exact JSON array to
  `careercoach_plan_fill(form_json, company, role, extra_answers, ats)` — set `ats="ashby"` for an
  `jobs.ashbyhq.com` form. The plan draws values ONLY from confirmed answers plus any one-off
  `extra_answers` the operator gave; it never fuzzy-matches a select answer and never invents one.
- The live form is the source of truth. The schema prep is a head start, not a substitute for the
  read-back.

### 4. Ask the operator about ONLY the unmapped items
The plan ends with an explicit list: required fields with no confirmed answer, selects whose answer
matched no option (with the options), and any answer still only a draft. Ask about THOSE and nothing
else. Confirm any newly agreed draft with `careercoach_confirm_answers`; pass one-off answers back
through `careercoach_plan_fill`'s `extra_answers`. Don't re-ask for anything already confirmed.

### 5. On Ashby, upload the résumé first
Ashby's autofill reads the résumé and can rewrite fields it thinks it recognises. So on an Ashby
form, `browser_upload` the PDF from step 2 BEFORE filling anything else, then `browser_form_read`
again so the plan reflects what autofill did.

### 6. Fill in the planned order
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

### 7. Read back and verify until VERIFIED
`browser_form_read` the filled form and pass it to `careercoach_verify_fill(session_id, form_json)`.
It reports `VERIFIED` only when every planned field holds its planned value and no required field is
empty; otherwise it lists the mismatches. Fix them and verify again. Do not move on until it says
VERIFIED — a react-select that silently reverted is exactly what this catches.

### 8. Hand off for a captcha, login, or attestation
If the page throws a captcha, a login/sign-in wall, or a legal attestation only the operator can
agree to, call `careercoach_handoff(session_id, reason)` with `reason` one of
`captcha` / `login` / `attestation` / `other`. It pauses for the operator to complete ONLY that step
in the visible browser (the Browser panel, or the real window if the browser's `headed` setting is
on) and the coach touches none of it. A captcha or login can re-render the form and clear filled
fields, so when the operator reports Done, go back to step 7: `browser_form_read` and
`careercoach_verify_fill` again before anything else. If the turn is headless, the handoff refuses
without pausing — stop and report that a human has to finish the step interactively.

### 9. Request submit, then click exactly once
Only once the form is VERIFIED, call `careercoach_request_submit(session_id)`. It refuses — without
asking anyone — unless the session is VERIFIED and unchanged since, and it refuses on a headless
turn. On the operator's explicit approval it unlocks exactly ONE submit click for 120 seconds. Then
`browser_click` the form's real submit button ONCE. A job board's "Apply" / "Apply now" button only
opens the form and is not the submit — don't spend the gate on it.

### 10. Confirm and track
After the click, `browser_form_read` (or a snapshot) to confirm you're on the confirmation page, not
still on the form with a validation error. Then record it with `careercoach_track_application(company,
role, status="applied", source=<url>)`.

## Anti-fabrication, restated
Every value that fills a field comes from a CONFIRMED standard answer, an operator-supplied one-off,
or the verified profile the résumé is rendered from. Nothing is guessed, no select answer is nudged
to a nearest option, and no required field is left to the model's imagination. If you can't fill a
field truthfully from what the operator has confirmed, ask — don't invent.
