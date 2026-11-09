# Applying through ATS forms

The coach never submits an application on its own. This page covers the default apply kit, the opt-in
browser-fill beta, and the gate that keeps submitting in your hands.

## The default: an apply kit you file

The coach prepares, and you apply. It:

1. tailors the résumé from your profile and renders it to a PDF (`careercoach_render_resume`, then
   `browser_open` and `browser_pdf`),
2. writes a cover letter only when the posting asks for one,
3. gathers every form answer: your confirmed standard answers, plus drafts for anything new,
4. saves it all on the role with `careercoach_save_apply_kit`.

Then it tells you to open the role in the **Career Coach** view, copy the answers into the employer's form,
attach the PDF, submit it yourself and press **Mark applied**. It never opens or drives the employer's form
in this flow.

## Browser fill (opt-in beta)

Only when an ATS type is listed in the **Browser fill (beta)** setting (blank means off) does the coach fill
the live form itself. It runs one fixed loop, using tools this plugin tests without a host:

1. **Confirm the standard answers.** Only answers you've confirmed (`careercoach_answers`, actions `get` and
   `confirm`) ever fill a form. A draft never does.
2. **Render the résumé** to a PDF the browser can upload.
3. **Prepare, then read the live form.** On Greenhouse, `careercoach_prepare_application` fetches the job's
   public question schema first. A company careers page that embeds the job (`?gh_jid=`) keeps the form in
   an iframe the form reader can't see, so prepare finds the job's board and hands back Greenhouse's
   embeddable form (`job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>`) to open directly.
   Either way, the form is read with `browser_form_read` and turned into a plan by `careercoach_plan_fill`,
   drawing values only from confirmed answers (your name, first and last name included).
4. **Ask only about what's unmapped:** required fields with no confirmed answer, dropdowns where no option
   matched, and every legal, eligibility or attestation question (below). It never guesses or fuzzy-matches
   a dropdown. Your answers go back into a fresh plan, so the plan always holds every value that will be
   on the form.
5. **Fill** with `browser_select` (country before number on phone fields; on Greenhouse the "Country"
   picker just before the phone is the phone's country), `browser_fill`, `browser_upload`, and, for a checkbox question, ticking
   exactly the options the plan names: with `browser_select` per option on protoAgent cores that read
   the question as one field, or a click on each box on older ones. A location field that suggests
   places as you type is picked with `browser_select`, never with Enter. Where the form may word an
   answer differently (a decline, "United States of America"), the plan passes the wordings as
   `A|B|C` alternatives. On Ashby the résumé goes first, because its autofill can overwrite typed values.
6. **Check the read-back.** It reads the form again and runs `careercoach_verify_fill` until it reports
   `VERIFIED`. A dropdown that silently reverted is exactly what this catches. So is a value nobody planned:
   a field the agent filled on its own, a box it ticked, a skipped field that now holds something. Values
   the page showed before anything was typed are allowed, and marked. The check accepts what a widget
   legitimately commits for your answer: "they/them/theirs" for "they/them", "United States of America"
   for "United States", "Portland, OR, USA" for "Portland, Oregon, United States", and a hidden city
   field the address picker fills from the address you typed.
7. **Hand off what only a person can do.** For a captcha, login or attestation, `careercoach_handoff` pauses
   so you can finish that one step in a visible browser: the console's Browser panel, or the real window the
   browser plugin's `headed` setting opens. The coach never solves or bypasses a captcha. Because those steps
   can re-render the form, a fresh read-back and verify are required afterwards.
8. **Submit once, behind the gate.** `careercoach_request_submit` asks you, you approve, and exactly one
   submit click is allowed. Then it checks the confirmation page and marks the role applied.

**Legal, eligibility and attestation questions are yours.** Work authorization anywhere, citizenship,
passport or nationality, export control, age, criminal history, non-competes, consents, and "everything
above is true" declarations are filled only from a confirmed answer on exactly that question (US work
authorization, sponsorship) or from your own answer for this application. The coach never works one out
from your profile, and an "authorized to work in the US" answer never fills an EU or UK question. These
answers are marked on the plan and on the approval card.

**What's supported.** Greenhouse gets public schema prep plus the live form. Ashby uses the live form only,
so the résumé uploads first and every field is re-read after autofill. Lever and Workable are recognized
from their URLs and filled through the live form, with no schema prep, so the coach says so and leans on the
read-back. Workday isn't supported.

## The submit gate

Submitting without your explicit consent is the one thing this plugin must never do. A rule in a skill
can't enforce that, and neither can a tool the agent decides to call, so the gate has two parts
(`applying/submitgate.py`, tested without a host).

**Consent comes through an interrupt, not a model decision.** `careercoach_request_submit` refuses, without
asking anyone, unless the fill session is `VERIFIED` and hasn't been re-planned or changed since. Any new or
rebuilt plan starts unverified. Only a verified session pauses the turn and shows you an approval card with
the company, the role and everything the form holds as it was read back, not only the planned fields.
Values that aren't from your answers (already on the page, or filled by the page from your address) and
legal or eligibility answers are marked. An explicit approve grants one submit, valid
for 120 seconds. A decline, a vague answer, or a turn with no one there to answer all refuse, and an
unattended turn never prompts at all. Nothing else in the plugin creates a grant.

**Submit clicks are blocked, not trusted.** How depends on the protoAgent version.

- **On a core whose browser plugin has the action gate** (protoAgent releases after 0.234.0), the core works out what
  each click, Enter press or script will actually hit, a snapshot `@e` ref included, before it runs. The
  coach registers a rule with that gate (the `agent_browser.add_gate_policy` service). When the core reads
  an action as a submit, the rule allows it only if you approved this application on the coach's card and
  the session is still the exact `VERIFIED` read-back you approved, and it uses up the grant. Any other
  submit is refused. So you see one approval card, the coach's, not a second one from the browser. A job
  board's **Apply now**, a search or a filter isn't a submit to the core, so it passes. The older check
  below still runs first as a floor: a call whose arguments read as a submit needs the grant even if the
  core couldn't read the page. A bare Enter press names no target, so on this path the core's verdict
  decides it; Enter to pick a suggestion in a dropdown isn't blocked.
- **On an older core**, middleware inspects every tool call and stops any submit-like browser call (a
  submit or finish click, an Enter press, a `.submit()` or `requestSubmit` script) unless a grant exists
  and the session it was approved for is still `VERIFIED`. It then uses up the grant. It judges the call
  by its arguments, so it can't see what a `@e` ref points at. A blocked call returns a message on the
  same tool call, and the browser tool never runs. A job board's **Apply** button, which only opens the
  form, and listing controls such as **Apply filters** aren't submit-like, so they pass through.

The coach checks for the gate on every call, so it switches over by itself when the browser plugin
updates or reloads.

**Page text is marked as page data.** Newer cores wrap what `browser_form_read` returns in
`AGENT_BROWSER_PAGE_CONTENT` markers, so the model can tell a page's text from instructions.
`careercoach_plan_fill` and `careercoach_verify_fill` take the output with or without the markers.

**The limits.** This guards the agent's use of the browser tools. It doesn't sandbox the browser, and it
can't stop a person: you can always submit by hand in the visible browser. On a host with neither the
browser's action gate nor the middleware hook, the gate falls back to the browser tools' own confirmations.
