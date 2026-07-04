"""Career Coach — a protoAgent showcase plugin.

A career coach and job-hunt copilot. It coaches (strategy, mock interviews, honest
CV feedback, offer evaluation, salary negotiation, decisions) and, when you ask, it
executes (evaluate a posting, tailor the CV + cover letter, prep the interview).

This repo is a **deep-dive reference** for the plugin system — it exercises the full
contribution surface in one place:

  • SKILL.md skills (progressive disclosure, with sub-files)  → skills/            (auto-loaded)
  • a static-DAG workflow (the autonomous "apply" pipeline)   → workflows/apply.yaml (auto-loaded)
  • a delegate subagent (company research)                    → register_subagent
  • agent tools + a tunable Knobs control surface (the rubric)→ register_tools + graph.sdk.Knobs
  • a console rail view (the Career Coach dashboard)          → register_router + manifest views:
  • config / secrets / Settings fields                        → manifest + registry.config (ADR 0019)
  • event-bus topics (rail notification dots)                 → registry.emit (ADR 0039)

Adapted, with credit, from Mads Lorentzen's ``ai-job-search`` (MIT) — see CREDITS.md.
The prompt-engineering IP (fit rubric, writing-style discipline, upskill gap analysis)
is ported; the LaTeX toolchain is replaced by artifact-rendered HTML → PDF; and the
whole thing is reframed around coaching.

Host imports (``graph.*``) stay **lazy and guarded** so the plugin loads and its tests
run with no protoAgent present (the host-free testkit stubs the host and the guards let
the core register anyway).
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

# NOTE: relative imports (`from . import state`) are kept LAZY, inside the functions that
# use them — a standalone plugin's root __init__.py is imported *bare* by pytest during
# collection (not as a package), and a top-level relative import fails there. The host-free
# testkit loads this as a real package, so the lazy imports resolve fine at register() time.

log = logging.getLogger("protoagent.plugins.careercoach")


def register(registry) -> None:
    """Entry point (ADR 0018) — called once at load with a PluginRegistry."""
    cfg = registry.config  # this plugin's resolved config + secrets (ADR 0019)

    _register_tracker_tools(registry)
    _register_rubric_knobs(registry)
    _register_subagents(registry)
    _register_views(registry, cfg)

    # skills/ and workflows/ auto-load from their conventional dirs — no call needed.
    log.info("[careercoach] registered: tracker tools + fit-rubric knobs + company_researcher + dashboard")


# ── agent tools: the application tracker ──────────────────────────────────────
def _register_tracker_tools(registry) -> None:
    from . import state

    @tool
    def careercoach_track_application(
        company: str,
        role: str,
        fit_score: int = 0,
        status: str = "considering",
        notes: str = "",
        source: str = "",
    ) -> str:
        """Record or update a job in your pipeline: company, role, the 0-100 fit score from
        the evaluation, status (considering/applied/interviewing/offer/rejected/passed),
        free-text notes, and the posting URL. Feeds the Career Coach dashboard and /upskill."""
        state.track_application(
            company=company, role=role, fit_score=fit_score, status=status, notes=notes, source=source
        )
        try:
            registry.emit("application_tracked", {"company": company, "role": role, "status": status})
        except Exception:  # noqa: BLE001 — the event bus is best-effort chrome
            pass
        n = len(state.load_applications())
        return f"Tracked {role} @ {company} (fit {fit_score}, {status}). {n} role(s) in your pipeline."

    @tool
    def careercoach_list_applications(status: str = "") -> str:
        """List the jobs in your pipeline, optionally filtered by status. Use it to review
        where things stand or pick what to prepare for next."""
        rows = state.load_applications(status=status or None)
        if not rows:
            return "Your pipeline is empty — paste me a posting, or track a role with careercoach_track_application."
        return "\n".join(
            f"- {r['role']} @ {r['company']} — fit {r.get('fit_score', 0)}, {r.get('status', '?')}" for r in rows
        )

    registry.register_tools([careercoach_track_application, careercoach_list_applications])


# ── a tunable control surface: the fit rubric as live Knobs (graph.sdk) ───────
def _register_rubric_knobs(registry) -> None:
    """Expose the four rubric weights as agent-tunable knobs + named presets, so the fit
    evaluation can be retuned live (``careercoach_tune weight_career 45``, ``careercoach_preset
    growth-first``). Guarded: if the host SDK isn't present (host-free tests), the core still
    registers — only the knob tools are skipped."""
    from .rubric import DEFAULT_WEIGHTS, DIMENSIONS, PRESETS

    try:
        from graph.sdk import Knobs, make_knob_tools
    except Exception as e:  # noqa: BLE001
        log.info("[careercoach] knob tools unavailable (host-free?): %s", e)
        return
    try:
        knobs = Knobs()
        for dim in DIMENSIONS:
            knobs.define(f"weight_{dim}", DEFAULT_WEIGHTS[dim], lo=0, hi=100)
        for name, preset in PRESETS.items():
            knobs.preset(name, {f"weight_{k}": v for k, v in preset.items()})
        registry.register_tools(make_knob_tools(knobs, prefix="careercoach"))
    except Exception as e:  # noqa: BLE001
        log.warning("[careercoach] failed to wire rubric knobs: %s", e)


# ── a delegate crew: research → evaluate → write (register_subagent) ──────────
# Three purpose-built subagents that the `apply` workflow chains (ADR 0002). Each carries a
# COMPACT prompt and is granted `load_skill` so it can pull the full discipline from the
# job-application-assistant skill (the source of truth) rather than duplicating it here.
def _register_subagents(registry) -> None:
    try:
        from graph.subagents.config import SubagentConfig
    except Exception as e:  # noqa: BLE001
        # graph.subagents.config isn't in the host-free testkit's default stubs, so this
        # import fails there. The guard keeps register() green host-free; see README §
        # "Found while building" (filed upstream as an SDK papercut).
        log.info("[careercoach] subagent registration skipped (host-free?): %s", e)
        return

    company_researcher = SubagentConfig(
        name="company_researcher",
        description=(
            "Researches a company before you apply or interview: mission, products, recent news, "
            "funding/layoffs, Glassdoor sentiment, team, and likely interviewers. Returns a SOURCED "
            "brief — every company-specific claim carries a link, so nothing unverified reaches a cover letter."
        ),
        system_prompt=(
            "You are company_researcher, the Career Coach's research delegate. Given a company (and "
            "optionally a role), gather a concise, SOURCED brief: what they do; mission and values; "
            "recent news (funding, launches, layoffs, restructuring); Glassdoor/employee sentiment; "
            "team size and notable people; and any red or green flags for a candidate. Use web_search "
            "and fetch_url. Cite a URL for every factual claim; if you cannot verify something, say so "
            "rather than guessing. Finish with two short lists: 'Questions worth asking in an interview' "
            "and 'Things to verify before applying'."
        ),
        tools=["web_search", "fetch_url", "current_time"],
    )

    job_evaluator = SubagentConfig(
        name="job_evaluator",
        description=(
            "Scores a job posting against the Career Coach's weighted fit rubric and returns the "
            "evaluation table, overall score, verdict, strengths, gaps, and a straight recommendation."
        ),
        system_prompt=(
            "You are job_evaluator, the Career Coach's fit assessor. Score a posting on four "
            "dimensions (defaults: technical 30, experience 25, behavioral 15, career 30) plus a "
            "location pass/fail gate, then take the weighted overall and map it to a verdict band. "
            "For the full rubric, bands, and exact output format, call load_skill('job-application-"
            "assistant') and read its job-evaluation.md. Use web_search/fetch_url for missing context. "
            "Output the evaluation table, weighted overall, verdict, strengths, gaps, and a clear "
            "apply/apply-with-caveats/skip recommendation, then record it with "
            "careercoach_track_application. Be honest: a weak fit is a weak fit — never inflate a score."
        ),
        tools=["load_skill", "list_skills", "web_search", "fetch_url", "current_time", "careercoach_track_application"],
    )

    application_writer = SubagentConfig(
        name="application_writer",
        description=(
            "Given an evaluation + company research, drafts a tailored CV and cover letter following "
            "the writing-style discipline and the honesty test — reframes emphasis, never fabricates."
        ),
        system_prompt=(
            "You are application_writer, the Career Coach's drafter. Given a fit evaluation and a "
            "company-research brief, produce a tailored CV and a cover letter. Follow the writing-style "
            "discipline: no em-dashes, no clichés, and the interview-backtrack honesty test (reframe "
            "emphasis, NEVER claim experience the candidate lacks). For the full guidance call "
            "load_skill('job-application-assistant') and read writing-style.md, cv-guide.md, and "
            "cover-letter-guide.md. Verify every company-specific claim against the research brief's "
            "sources before including it. If the verdict was Weak or Poor Fit, say so and stop rather "
            "than forcing a draft. End with a verification checklist (factual accuracy, targeting, "
            "company claims verified, style)."
        ),
        tools=["load_skill", "list_skills", "careercoach_track_application", "current_time"],
    )

    for cfg in (company_researcher, job_evaluator, application_writer):
        registry.register_subagent(cfg)


# ── console rail view: the Career Coach dashboard (ADR 0026) ──────────────────
def _register_views(registry, cfg) -> None:
    """Serve the dashboard PAGE (public, un-gated — an iframe load can't carry a bearer)
    and its DATA route (gated under /api/plugins/careercoach). Models the view-vs-data
    gating rule and the fleet-proxy-safe fetch (ADR 0026/0042), like the notes plugin."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse, JSONResponse

    from . import state
    from .rubric import DEFAULT_WEIGHTS

    data = APIRouter()

    @data.get("/state")
    async def _state():
        return JSONResponse(
            {
                "profile": {
                    "name": cfg.get("full_name", ""),
                    "location": cfg.get("location", ""),
                    "target_roles": cfg.get("target_roles", ""),
                },
                "weights": DEFAULT_WEIGHTS,  # the rubric defaults; the agent tunes live via knobs
                "applications": state.load_applications(),
            }
        )

    registry.register_router(data, prefix="/api/plugins/careercoach")  # GATED

    page = APIRouter()

    @page.get("/view")
    async def _view():
        return HTMLResponse(_DASHBOARD_HTML)

    registry.register_router(page, prefix="/plugins/careercoach")  # PUBLIC page


# The dashboard page. Self-contained: links the DS plugin-kit for theming, derives a
# fleet-proxy-safe API base from its own iframe path, waits for the console's
# `protoagent:init` handshake (bearer + theme), then fetches the gated /state route.
_DASHBOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Career Coach</title>
<script>
  // Fleet-proxy-safe base: "" on the host, "/agents/<slug>" behind the proxy (ADR 0042).
  var BASE = location.pathname.split('/plugins/')[0];
  var link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = BASE + '/_ds/plugin-kit.css';
  document.head.appendChild(link);
</script>
<style>
  :root { --gap: 16px; }
  body { margin: 0; padding: 28px; background: var(--pl-color-bg, #0a0f14);
         color: var(--pl-color-fg, #e6e6e6);
         font-family: var(--pl-font-sans, ui-sans-serif, system-ui, -apple-system, sans-serif); }
  h1 { font-size: 20px; margin: 0 0 2px; color: var(--pl-color-accent, #9b87f2); }
  .sub { color: var(--pl-color-fg-muted, #9aa0aa); font-size: 13px; margin: 0 0 22px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: var(--gap); margin-bottom: 24px; }
  .card { background: var(--pl-color-surface, #12181f); border: 1px solid var(--pl-color-border, #232b36);
          border-radius: 12px; padding: 16px 18px; }
  .card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
             color: var(--pl-color-fg-muted, #9aa0aa); margin: 0 0 10px; font-weight: 600; }
  .big { font-size: 26px; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--pl-color-border, #232b36); }
  th { color: var(--pl-color-fg-muted, #9aa0aa); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px;
          background: var(--pl-color-surface-2, #1b2330); color: var(--pl-color-fg-muted, #9aa0aa); }
  .fit { font-variant-numeric: tabular-nums; font-weight: 600; }
  .bar { height: 6px; border-radius: 4px; background: var(--pl-color-accent, #9b87f2); }
  .row { display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 13px; }
  .row .name { width: 92px; color: var(--pl-color-fg-muted, #9aa0aa); text-transform: capitalize; }
  .track { flex: 1; height: 6px; border-radius: 4px; background: var(--pl-color-border, #232b36); overflow: hidden; }
  .empty { color: var(--pl-color-fg-muted, #9aa0aa); font-size: 14px; padding: 18px 0; }
  .coach { margin-top: 24px; padding: 16px 18px; border-radius: 12px;
           border: 1px dashed var(--pl-color-border, #232b36); color: var(--pl-color-fg-muted, #9aa0aa); font-size: 13px; line-height: 1.6; }
  code { background: var(--pl-color-surface-2, #1b2330); padding: 1px 6px; border-radius: 5px; color: var(--pl-color-accent, #9b87f2); }
</style></head>
<body>
  <h1 id="title">Career Coach</h1>
  <p class="sub" id="sub">Loading your pipeline…</p>

  <div class="grid" id="stats"></div>

  <div class="card">
    <h2>Pipeline</h2>
    <div id="pipeline"><p class="empty">Loading…</p></div>
  </div>

  <div class="card" style="margin-top:16px">
    <h2>Fit rubric — how a posting is scored</h2>
    <div id="weights"></div>
  </div>

  <div class="coach">
    This is your coach, not just an auto-applier. Ask it to <b>run a mock interview</b>,
    <b>critique your CV</b>, <b>weigh an offer</b>, or <b>rehearse a salary negotiation</b> —
    or paste a posting and say <code>/apply</code> to have it evaluate fit and draft a tailored
    CV + cover letter. Tune how strictly it scores with <code>careercoach_preset growth-first</code>.
  </div>

<script>
  var TOKEN = null;
  function api(path) {
    var h = {};
    if (TOKEN) h['Authorization'] = 'Bearer ' + TOKEN;
    return fetch(BASE + '/api/plugins/careercoach' + path, { headers: h }).then(function (r) { return r.json(); });
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }

  function render(d) {
    var apps = d.applications || [];
    var name = (d.profile && d.profile.name) || '';
    document.getElementById('title').textContent = name ? ('Career Coach — ' + name) : 'Career Coach';
    document.getElementById('sub').textContent = (d.profile && d.profile.target_roles)
      ? ('Targeting: ' + d.profile.target_roles) : 'Set your profile in Settings › Career Coach.';

    var byStatus = {};
    apps.forEach(function (a) { byStatus[a.status] = (byStatus[a.status] || 0) + 1; });
    var interviewing = (byStatus['interviewing'] || 0) + (byStatus['offer'] || 0);
    var stats = [
      ['In pipeline', apps.length],
      ['Applied', byStatus['applied'] || 0],
      ['Interviewing / offer', interviewing]
    ];
    document.getElementById('stats').innerHTML = stats.map(function (s) {
      return '<div class="card"><h2>' + s[0] + '</h2><div class="big">' + s[1] + '</div></div>';
    }).join('');

    if (!apps.length) {
      document.getElementById('pipeline').innerHTML =
        '<p class="empty">Nothing tracked yet. Paste a posting in chat, or ask your coach where to focus.</p>';
    } else {
      var rows = apps.map(function (a) {
        return '<tr><td>' + esc(a.role) + '</td><td>' + esc(a.company) + '</td>' +
          '<td class="fit">' + (a.fit_score || 0) + '</td>' +
          '<td><span class="pill">' + esc(a.status) + '</span></td>' +
          '<td>' + esc(a.updated || a.date || '') + '</td></tr>';
      }).join('');
      document.getElementById('pipeline').innerHTML =
        '<table><thead><tr><th>Role</th><th>Company</th><th>Fit</th><th>Status</th><th>Updated</th></tr></thead><tbody>' +
        rows + '</tbody></table>';
    }

    var w = d.weights || {};
    document.getElementById('weights').innerHTML = Object.keys(w).map(function (k) {
      return '<div class="row"><span class="name">' + esc(k) + '</span>' +
        '<span class="track"><span class="bar" style="width:' + w[k] + '%"></span></span>' +
        '<span class="fit">' + w[k] + '</span></div>';
    }).join('');
  }

  function load() { api('/state').then(render).catch(function () {
    document.getElementById('sub').textContent = 'Could not load pipeline data.'; }); }

  // Console handshake (ADR 0026): grab the bearer + theme, then load gated data.
  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.type === 'protoagent:init') { TOKEN = m.token || null; load(); }
  });
  // Also try immediately (host context may not post an init) so the panel isn't blank.
  load();
</script>
</body></html>"""
