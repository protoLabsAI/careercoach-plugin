"""The jobs view PAGE (epic #85 R1): the rebuilt ``_DASHBOARD_HTML`` shell — header, Today band,
Pipeline/Inbox/Closed tabs on a one-viewport page, kit classes in place of hand-rolled CSS, the
loading / API-unreachable / partial-failure / brand-new states, and the About-you drawer that
replaces the page-foot disclosures.

Host-free. These assert on the page's own markup/JS source (the page is a self-contained string)
and execute the shipped view renderers in quickjs with a DOM stub — no browser, no host. The gated
DATA routes the page targets are exercised in ``test_pipeline.py`` / ``test_inbox.py``; here we only
check they exist on the registered router, so the page and the API can't drift apart.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import quote

import pytest
import quickjs

from _plugin_testkit import FakeRegistry

# The §2 board columns (mirrors state.COLUMNS); "Closed" maps the two dead statuses.
COLUMNS = {
    "Saved": ["considering"],
    "Preparing": ["preparing"],
    "Applied": ["applied"],
    "Interviewing": ["interviewing"],
    "Offer": ["offer"],
    "Closed": ["rejected", "passed"],
}

# A DOM stub: getElementById hands back a fake element (innerHTML + attrs + a no-op classList), so a
# render function can fill the shell regions without a browser. querySelectorAll returns [] — wiring
# loops are inert, which is fine when we assert on the produced innerHTML.
_STUB = r"""
var __els = {};
function __mkEl(id) {
  return { id: id, innerHTML: '', hidden: false, value: '', _a: {}, style: {},
    clientWidth: (id === 'cc' ? __W : 0),
    setAttribute: function (k, v) { this._a[k] = String(v); },
    getAttribute: function (k) { return (k in this._a) ? this._a[k] : null; },
    classList: { _s: {}, add: function (c) { this._s[c] = 1; }, remove: function (c) { delete this._s[c]; },
                 contains: function (c) { return !!this._s[c]; } },
    contains: function () { return false; }, focus: function () {}, onclick: null, onkeydown: null,
    querySelectorAll: function () { return []; }, querySelector: function () { return null; },
    addEventListener: function () {} };
}
var document = {
  getElementById: function (id) { if (!__els[id]) __els[id] = __mkEl(id); return __els[id]; },
  querySelector: function () { return null; }, querySelectorAll: function () { return []; },
  activeElement: null, addEventListener: function () {}
};
var window = { addEventListener: function () {} };
var parent = { postMessage: function () {} };
var localStorage = { getItem: function () { return null; }, setItem: function () {} };
"""


def _view_ctx(plugin, width=1200):
    """A quickjs context with the DOM stub and the page's view renderers (``esc`` through the
    ``// <<< view renderers`` marker) loaded — everything the shell needs, minus the module/async
    bootstrap and the init wiring (which never runs in these unit tests)."""
    ctx = quickjs.Context()
    ctx.eval("var __W = %d;" % width + _STUB)
    html = plugin._DASHBOARD_HTML
    ctx.eval(html[html.index("  function esc(s)") : html.index("  // <<< view renderers")])
    ctx.eval(html[html.index("  // >>> s2b pure renderers") : html.index("  // /jobs drives")])
    return ctx


def _inner(ctx, el_id):
    return ctx.eval("document.getElementById('%s').innerHTML" % el_id)


def _render_pipeline(ctx, apps, columns, inbox=None, followup_days=10):
    jobs = {"applications": apps, "columns": columns, "inbox": inbox or [], "followup_days": followup_days}
    ctx.eval("LOADING = false; JOBS = " + json.dumps(jobs) + ";")
    ctx.eval("var __B = {innerHTML: '', querySelectorAll: function () { return []; }}; renderPipeline(__B);")
    # Rendering must not silently normalize stored statuses.
    assert json.loads(ctx.eval("JSON.stringify(JOBS)")) == jobs
    return ctx.eval("__B.innerHTML")


def _render_closed(ctx, apps, columns):
    jobs = {"applications": apps, "columns": columns, "inbox": [], "followup_days": 10}
    ctx.eval("LOADING = false; JOBS = " + json.dumps(jobs) + ";")
    ctx.eval("var __B = {innerHTML: '', querySelectorAll: function () { return []; }}; renderClosedTab(__B);")
    return ctx.eval("__B.innerHTML")


# ── the pipeline board: open columns + the Closed stub (§2) ────────────────────────────
@pytest.mark.parametrize(
    "status", ["Applied", "interview", "legacy", "", None, "__proto__", "constructor", "<img src=x>"]
)
def test_board_shows_open_columns_other_and_closed_stub(plugin, status):
    ctx = _view_ctx(plugin)
    apps = [
        {"id": "known", "role": "Known role", "status": "applied"},
        {"id": "unknown", "role": "Legacy role", "status": status},
        {"id": "missing", "role": "Missing status"},
    ]
    html = _render_pipeline(ctx, apps, COLUMNS)
    # The board (≥600px) and the rail stage-list (<600px, §8) are both in the markup; CSS shows one
    # or the other by container width. Scope the per-region checks so the stage mirror doesn't read
    # as duplication.
    board, _, stages = html.partition('<div class="stages"')
    # the live open card renders in its column; the two unmappable rows fall to "Other statuses"
    assert 'Applied<span class="pl-count">1</span>' in board
    assert 'Other statuses<span class="pl-count">2</span>' in board
    for app in apps:
        assert board.count('data-id="' + app["id"] + '"') == 1  # once on the board, no duplication
    # Every live role remains accessible on the rail, including legacy statuses (§7).
    for app in apps:
        assert stages.count('data-id="' + app["id"] + '"') == 1
    assert 'data-stage="Other statuses" open>' in stages
    # the six-column contract holds as a narrow Closed stub, not a stack of dead cards
    assert 'class="col stub"' in board
    assert 'data-col="Closed"' in board
    assert "Drop here to close" in board
    # the legacy column is never a persisted status or a move destination
    assert 'value="Other statuses"' not in html
    assert 'data-col="Other statuses"' not in html
    assert "<img src=x>" not in html
    if status == "<img src=x>":
        assert "&lt;img src=x&gt;" in html


def test_closed_leaves_the_board_and_lives_on_its_own_tab(plugin):
    ctx = _view_ctx(plugin)
    apps = [{"id": s, "status": s} for statuses in COLUMNS.values() for s in statuses]
    board = _render_pipeline(ctx, apps, COLUMNS)
    # five live pl-board-col columns on the board; no legacy column; dead cards are off the board
    assert board.count('class="col pl-board-col"') == 5  # the kit board-column class (§9)
    assert "Other statuses" not in board
    assert board.count('class="pcard pl-board-card"') == 5
    assert ">rejected<" not in board and ">passed<" not in board
    # the Closed stub counts the two dead roles and links to the Closed tab
    assert 'class="col stub"' in board
    assert '<b class="stub-n">2</b>' in board
    # the Closed tab renders those two roles (carried card markup), each a drawer target
    closed = _render_closed(ctx, apps, COLUMNS)
    assert closed.count('class="pl-tr--interactive"') == 2
    assert "2 closed ·" in closed


def test_board_keeps_applications_visible_without_column_configuration(plugin):
    ctx = _view_ctx(plugin)
    html = _render_pipeline(ctx, [{"id": "legacy", "status": "interview"}], {})
    assert "No pipeline columns." in html
    assert _render_pipeline(ctx, [], {}) == '<p class="empty">No pipeline columns.</p>'


# ── the plugin-kit + fleet-proxy contract, and the gated calls the page makes ──────────
def test_page_keeps_plugin_kit_and_fleet_proxy_contract(plugin):
    html = plugin._DASHBOARD_HTML
    assert "location.pathname.split('/plugins/')[0]" in html  # fleet-proxy-safe base (ADR 0042)
    assert "/_ds/plugin-kit.css" in html
    assert "import(BASE + '/_ds/plugin-kit.js')" in html
    assert "kit.initPluginView" in html
    assert "apiFetch: function (p, i) { return fetch(BASE + p, i); }" in html
    assert "--pl-color-surface" not in html  # only kit-defined --pl-* tokens


def test_page_routes_every_write_through_kit_apifetch(plugin):
    html = plugin._DASHBOARD_HTML
    assert "kit.apiFetch('/api/plugins/careercoach' + path)" in html  # reads
    assert "kit.apiFetch('/api/plugins/careercoach' + path, {" in html  # writes (POST)
    assert "method: 'POST'" in html


def test_page_targets_jobs_keep_dismiss_and_move(plugin):
    html = plugin._DASHBOARD_HTML
    assert "api('/jobs')" in html  # the band + tabs data
    assert "api('/state')" in html  # the header subline + About-you drawer
    assert "write('/inbox/keep', { id: id })" in html  # URL-shaped inbox ids travel in the body
    assert "write('/inbox/dismiss', { id: id })" in html
    assert "'/inbox/' + encodeURIComponent(" not in html
    assert "headers: { 'Content-Type': 'application/json' }" in html
    assert "JSON.stringify(body)" in html
    assert "function inboxKeep(ids)" in html
    assert "function inboxDismiss(ids)" in html
    assert "'/applications/' + encodeURIComponent(id) + '/move'" in html


def test_page_target_routes_exist_on_the_gated_router(plugin):
    """The paths the page builds must be real routes on the gated router, so the two can't drift."""
    reg = FakeRegistry({"followup_days": 10}, plugin_id="careercoach")
    plugin.register(reg)
    _prefix, router = next((p, r) for p, r in reg.routers if p == "/api/plugins/careercoach")
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/jobs" in paths
    assert "/state" in paths
    assert "/inbox/keep" in paths
    assert "/inbox/dismiss" in paths
    ordered_paths = [getattr(r, "path", "") for r in router.routes]
    for action in ("keep", "dismiss"):
        assert ordered_paths.index(f"/inbox/{action}") < ordered_paths.index("/inbox/{item_id:path}/" + action)
    assert any(p.startswith("/applications/") and p.endswith("/move") for p in paths)
    assert any(p.startswith("/applications/") and p.endswith("/undo-move") for p in paths)  # the move Undo (§7)


# ── header: eyebrow, title Jobs, target-roles subline, About-you ghost button (§2) ─────
def test_header_has_eyebrow_title_and_about_button(plugin):
    html = plugin._DASHBOARD_HTML
    assert '<div class="pl-eyebrow">Career Coach</div>' in html
    assert '<h1 class="t" id="title">Jobs</h1>' in html  # title Jobs, and (below) not accent-coloured
    assert 'id="about-btn"' in html and ">About you</button>" in html
    assert "pl-btn--ghost" in html  # About you is a ghost button
    css = html.split("</style>")[0]
    assert ".hd .t { font-size: 18px; font-weight: 600; margin: 2px 0 0; color: var(--pl-color-fg); }" in css


def test_header_subline_tracks_target_roles(plugin):
    ctx = _view_ctx(plugin)
    ctx.eval("STATE = {settings: {target_roles: 'Staff Platform Engineer'}}; renderHeader();")
    assert ctx.eval("document.getElementById('sub').hidden") is False
    assert ctx.eval("document.getElementById('sub').textContent") == "Targeting: Staff Platform Engineer"
    ctx.eval("STATE = {settings: {}}; renderHeader();")
    assert ctx.eval("document.getElementById('sub').hidden") is True


# ── the Today band: items per §2, max 3 rows (2 on the rail) then "+N more", hidden empty ─
def _today_jobs(**over):
    apps = [
        {
            "id": "f1",
            "role": "A",
            "company": "C",
            "status": "applied",
            "followup": True,
            "history": [{"status": "applied", "date": "2020-01-01"}],
        },
        {"id": "iv", "role": "Staff Eng", "company": "Northwind", "status": "interviewing"},
        {"id": "of", "role": "Lead", "company": "Lumen", "status": "offer"},
        {"id": "sv", "role": "SRE", "company": "Quarry", "status": "considering"},
    ]
    jobs = {
        "applications": apps,
        "columns": COLUMNS,
        "followup_days": 10,
        "inbox": [{"id": "in1", "title": "T", "company": "C", "first_seen": "2026-01-01"}],
    }
    jobs.update(over)
    return jobs


def test_today_band_renders_items_caps_at_three_with_more(plugin):
    ctx = _view_ctx(plugin)
    ctx.eval("LOADING = false; LAST_VISIT = 0; JOBS = " + json.dumps(_today_jobs()) + "; renderToday();")
    today = _inner(ctx, "today")
    assert ctx.eval("document.getElementById('today').hidden") is False
    assert "Needs you today" in today
    # five actionable items, capped at three rows on a wide view, then "+2 more"
    assert today.count('class="trow pl-row"') == 4  # 3 item rows + the "+N more" row
    assert "+2 more" in today
    assert "pl-chip--warning" in today  # the overdue-follow-up marker carries a tone, with text
    assert "had no reply in" in today
    assert "prep not started" in today


def test_today_band_caps_at_two_on_the_rail(plugin):
    ctx = _view_ctx(plugin, width=420)
    ctx.eval("LOADING = false; LAST_VISIT = 0; JOBS = " + json.dumps(_today_jobs()) + "; renderToday();")
    today = _inner(ctx, "today")
    assert today.count('class="trow pl-row"') == 3  # 2 item rows + "+3 more"
    assert "+3 more" in today


def test_today_band_hidden_when_nothing_needs_you(plugin):
    ctx = _view_ctx(plugin)
    quiet = {"applications": [{"id": "x", "status": "rejected"}], "columns": COLUMNS, "inbox": [], "followup_days": 10}
    ctx.eval("LOADING = false; JOBS = " + json.dumps(quiet) + "; renderToday();")
    assert ctx.eval("document.getElementById('today').hidden") is True
    assert _inner(ctx, "today") == ""


# ── tabs: WAI-ARIA pattern, counts in the accessible name, counts hidden at 0 (§2/§10) ──
def test_tab_strip_is_static_tablist_with_arrow_keys(plugin):
    html = plugin._DASHBOARD_HTML
    assert '<div class="tabs" role="tablist" aria-label="Jobs" id="tabs"></div>' in html
    assert "e.key === 'ArrowRight'" in html and "e.key === 'ArrowLeft'" in html
    assert "e.key === 'Home'" in html and "e.key === 'End'" in html


def test_tabs_render_aria_selected_counts_and_hide_zero(plugin):
    ctx = _view_ctx(plugin)
    apps = [{"id": "a%d" % i, "status": "applied"} for i in range(5)]  # pipeline count 5
    inbox = [{"id": "in%d" % i} for i in range(49)]  # inbox count 49
    jobs = {"applications": apps, "columns": COLUMNS, "inbox": inbox, "followup_days": 10}
    ctx.eval("LOADING = false; STATE = null; JOBS = " + json.dumps(jobs) + "; renderTabs();")
    tabs = _inner(ctx, "tabs")
    assert tabs.count('role="tab"') == 3
    assert 'id="tab-pipeline" aria-controls="body" aria-selected="true" tabindex="0"' in tabs
    assert 'id="tab-inbox" aria-controls="body" aria-selected="false" tabindex="-1"' in tabs
    assert 'aria-label="Inbox, 49"' in tabs  # count inside the accessible name
    assert 'aria-label="Pipeline, 5"' in tabs
    assert 'aria-label="Closed"' in tabs  # count 0 → no ", 0" in the name
    # a count badge only for the non-zero tabs (pipeline + inbox); Closed (0) carries none
    assert tabs.count('class="pl-count"') == 2


def test_tabs_render_without_counts_while_loading(plugin):
    ctx = _view_ctx(plugin)
    ctx.eval("LOADING = true; renderTabs();")
    tabs = _inner(ctx, "tabs")
    assert tabs.count('role="tab"') == 3  # the strip renders at once...
    assert 'class="pl-count"' not in tabs  # ...with no counts (§7 Loading)


# ── states: loading, API-unreachable, brand-new (§7) ───────────────────────────────────
def test_loading_paints_skeletons_in_band_and_body(plugin):
    ctx = _view_ctx(plugin)
    ctx.eval("LOADING = true; renderToday(); renderBody();")
    assert "pl-skel" in _inner(ctx, "today")
    assert "pl-skel" in _inner(ctx, "body")


def test_api_unreachable_shows_error_banner_with_retry_and_skeleton_bodies(plugin):
    ctx = _view_ctx(plugin)
    ctx.eval("LOADING = false; JOBS_FAILED = true; renderBanner(); renderBody();")
    banner = _inner(ctx, "banner")
    assert ctx.eval("document.getElementById('banner').hidden") is False
    assert "pl-banner--error" in banner
    assert "Couldn’t load your jobs." in banner
    assert 'id="retry"' in banner and ">Retry</button>" in banner
    assert "pl-skel" in _inner(ctx, "body")  # tab bodies stay skeletons


def test_brand_new_agent_shows_empty_with_setup_and_suggestions(plugin):
    ctx = _view_ctx(plugin)
    state = {"completeness": {"empty": True, "filled": 0, "total": 10}, "profile": {}, "settings": {}}
    jobs = {"applications": [], "columns": COLUMNS, "inbox": [], "followup_days": 10}
    ctx.eval("LOADING = false; STATE = " + json.dumps(state) + "; JOBS = " + json.dumps(jobs) + ";")
    ctx.eval("renderTabs(); renderToday(); renderBody();")
    assert ctx.eval("document.getElementById('tabs').hidden") is True  # no tabs
    assert ctx.eval("document.getElementById('today').hidden") is True  # no Today band
    body = _inner(ctx, "body")
    assert "pl-empty" in body
    assert "Your coach hasn’t started a search yet" in body
    assert 'id="setup"' in body and "Start setup" in body
    # the old coaching-tips paragraph is reborn here as three suggestion chips
    assert body.count("pl-suggestion") == 3
    for chip in ("Paste a posting and say /apply", "Critique my CV", "Run a mock interview"):
        assert chip in body


# ── About-you drawer replaces the page-foot disclosures (§5/§6) ─────────────────────────
def test_about_scrim_selector_matches_shell_sibling_and_covers_detail(plugin):
    class BodyChildren(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.ids = []

        def handle_starttag(self, tag, attrs):
            if self.stack == ["html", "body"]:
                self.ids.append(dict(attrs).get("id"))
            if tag not in {"meta", "link", "input", "br", "hr"}:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            assert self.stack.pop() == tag

    html = plugin._DASHBOARD_HTML
    parser = BodyChildren()
    parser.feed(html)
    assert parser.ids.index("cc") < parser.ids.index("about-scrim")
    css = html.split("</style>")[0]
    assert "#about-scrim { display: none; }" in css
    assert "#cc.about-open ~ #about-scrim { display: block; }" in css
    scrim = re.search(r"\.scrim \{([^}]+)\}", css).group(1)
    assert "position: fixed; inset: 0;" in scrim

    def z_index(selector):
        return int(re.search(re.escape(selector) + r" \{[^}]*z-index: (\d+)", css)[1])

    assert z_index("#detail") < z_index(".scrim") < z_index("#about")


@pytest.mark.parametrize("dismiss", ["scrim", "close", "escape"])
def test_about_drawer_dismissal_clears_scrim_state_and_returns_focus(plugin, dismiss):
    ctx = _view_ctx(plugin)
    ctx.eval("""
      ['about-btn', 'about-close', 'about-scrim', 'about'].forEach(function (id) {
        var el = document.getElementById(id);
        el.addEventListener = function (type, handler) { this['on' + type] = handler; };
        el.focus = function () { document.activeElement = this; };
      });
    """)
    html = plugin._DASHBOARD_HTML
    ctx.eval(html[html.index("  // About-you drawer: open") : html.index("  // The job detail drawer dismisses")])
    ctx.eval("document.getElementById('about-btn').onclick();")
    assert ctx.eval("document.getElementById('cc').classList.contains('about-open')")
    assert ctx.eval("document.getElementById('about').getAttribute('aria-hidden')") == "false"
    assert ctx.eval("document.activeElement.id") == "about-close"
    if dismiss == "escape":
        ctx.eval("document.getElementById('about').onkeydown({key: 'Escape', stopPropagation: function () {}});")
    else:
        ctx.eval("document.getElementById('about-%s').onclick();" % dismiss)
    assert not ctx.eval("document.getElementById('cc').classList.contains('about-open')")
    assert ctx.eval("document.getElementById('about').getAttribute('aria-hidden')") == "true"
    assert ctx.eval("document.activeElement.id") == "about-btn"


def test_about_focus_trap_keeps_accordion_summaries_reachable(plugin):
    """The drawer's focus trap must count the three accordion toggles (``<summary>`` elements) as
    focusable, not just the ✕ button. Otherwise Tab/Shift+Tab bounce off the ✕ and the collapsed
    'Fit rubric' / 'Exactly what it's told each turn' accordions can never be opened by keyboard —
    a regression against the keyboard-reachable page-foot disclosures this drawer replaced."""
    ctx = _view_ctx(plugin)
    # A drawer whose focusables are, in DOM order, the ✕ button then the three <summary> toggles.
    # __root.querySelectorAll resolves the trap's real selector against these nodes by tag name.
    ctx.eval(r"""
      var __nodes = [
        {id: 'about-close', tag: 'button'},
        {id: 's0', tag: 'summary'}, {id: 's1', tag: 'summary'}, {id: 's2', tag: 'summary'}
      ];
      __nodes.forEach(function (n) { n.focus = function () { document.activeElement = this; }; n.getClientRects = function () { return [1]; }; n.closest = function () { return null; }; });
      var __root = { contains: function () { return true; }, querySelectorAll: function (sel) {
        var tags = sel.toLowerCase().split(',').map(function (p) { return p.split(':')[0].split('[')[0].trim(); });
        return __nodes.filter(function (n) { return tags.indexOf(n.tag) !== -1; });
      }};
    """)
    # Forward Tab from the LAST focusable wraps to the first — only true if the summaries are counted.
    ctx.eval("document.activeElement = __nodes[3];")
    ctx.eval("trap({key: 'Tab', shiftKey: false, preventDefault: function () {}}, __root);")
    assert ctx.eval("document.activeElement.id") == "about-close"
    # Shift+Tab from the ✕ wraps back to the last summary (the third accordion toggle).
    ctx.eval("document.activeElement = __nodes[0];")
    ctx.eval("trap({key: 'Tab', shiftKey: true, preventDefault: function () {}}, __root);")
    assert ctx.eval("document.activeElement.id") == "s2"


def test_page_foot_disclosures_are_gone(plugin):
    html = plugin._DASHBOARD_HTML
    assert 'class="coach"' not in html  # the coaching-tips paragraph left the page foot
    assert 'details class="disc"' not in html  # the inline profile/rubric disclosures are gone
    assert "careercoach_preset growth-first" not in html


def test_about_drawer_has_three_readonly_accordions(plugin):
    html = plugin._DASHBOARD_HTML
    assert 'id="about"' in html and 'class="pl-drawer pl-drawer--right"' in html
    assert "To change anything, tell your coach in chat." in html
    ctx = _view_ctx(plugin)
    state = {
        "settings": {},
        "profile": {"identity": {"name": "Alex"}, "sections": {}},
        "completeness": {"filled": 1, "total": 10, "updated": "2026-10-31"},
        "field_labels": {"name": "Name"},
        "context_block": "Candidate: Alex",
        "weights": {"role_match": 35},
    }
    ctx.eval("STATE = " + json.dumps(state) + "; STATE_FAILED = false; renderAboutBody();")
    about = _inner(ctx, "about-body")
    assert about.count("pl-accordion") == 3
    assert "What your coach knows about you" in about
    assert "Fit rubric" in about
    assert "Exactly what it’s told each turn" in about
    assert "Candidate: Alex" in about  # the injected context, read-only


def test_about_drawer_shows_warning_callout_on_partial_failure(plugin):
    ctx = _view_ctx(plugin)
    ctx.eval("STATE = null; STATE_FAILED = true; renderAboutBody();")
    about = _inner(ctx, "about-body")
    assert "pl-callout--warning" in about  # /state failed → Callout tone="warning"
    assert "Couldn’t load your profile" in about


# ── R2 board card (§3): role/company/age, conditional fit, no pill, pl-chip--warning line ──
def test_cards_are_draggable_with_dragover_and_drop_handlers(plugin):
    html = plugin._DASHBOARD_HTML
    assert 'draggable="true"' in html
    assert "addEventListener('dragstart'" in html
    assert "addEventListener('dragover'" in html
    assert "addEventListener('drop'" in html
    assert "ev.preventDefault()" in html
    assert "moveToColumn(" in html


def test_board_card_has_no_select_no_pill_and_keyboard_attrs(plugin):
    """§3/§6: the per-card 'Move to…' select and the status pill are gone from the board; a card is
    a keyboard-movable button (role=button, tabindex 0, aria-pressed) as well as draggable."""
    ctx = _view_ctx(plugin)
    html = _render_pipeline(ctx, [{"id": "x", "role": "Dev", "company": "Acme", "status": "applied"}], COLUMNS)
    assert "<select" not in html and "movesel" not in html  # no per-card select on the board
    assert 'role="button"' in html and 'tabindex="0"' in html and 'draggable="true"' in html
    assert 'aria-pressed="false"' in html  # the pick-up state is exposed to AT
    assert "pl-badge" not in html and ">applied<" not in html  # no status pill — the column says it


def test_board_card_shows_fit_only_when_scored(plugin):
    """§3: fit shows only when fit_score > 0 — a card never shows a 0."""
    ctx = _view_ctx(plugin)
    scored = [{"id": "s", "role": "R", "company": "C", "status": "applied", "fit_score": 82}]
    assert '<span class="fit">fit 82</span>' in _render_pipeline(ctx, scored, COLUMNS)
    unscored = [{"id": "u", "role": "R", "company": "C", "status": "applied", "fit_score": 0}]
    assert 'class="fit"' not in _render_pipeline(ctx, unscored, COLUMNS)  # no fit span, never a 0


def test_board_followup_adds_one_pl_chip_warning_line(plugin):
    """§3/§10: a server-flagged follow-up adds exactly ONE pl-chip--warning line carrying text; an
    unflagged card has none."""
    ctx = _view_ctx(plugin)
    flagged = [
        {
            "id": "f",
            "role": "R",
            "company": "C",
            "status": "applied",
            "followup": True,
            "history": [{"status": "applied", "date": "2020-01-01"}],
        }
    ]
    out = _render_pipeline(ctx, flagged, COLUMNS)
    assert out.count("pl-chip--warning") == 1  # exactly one follow-up line, carrying a tone...
    assert "No reply in" in out and "days" in out  # ...and text, never colour alone
    assert "follow up?" not in out  # the old board-card copy is gone
    plain = [{"id": "p", "role": "R", "company": "C", "status": "applied", "followup": False}]
    assert "pl-chip--warning" not in _render_pipeline(ctx, plain, COLUMNS)  # gated on the flag


def test_columns_sort_followups_first_then_oldest_move(plugin):
    """§3: each column sorts flagged follow-ups first, then oldest move (earliest last-move date)."""
    ctx = _view_ctx(plugin)
    apps = [
        {
            "id": "new",
            "role": "N",
            "company": "C",
            "status": "applied",
            "history": [{"status": "applied", "date": "2026-02-01"}],
        },
        {
            "id": "old",
            "role": "O",
            "company": "C",
            "status": "applied",
            "history": [{"status": "applied", "date": "2025-01-01"}],
        },
        {
            "id": "flag",
            "role": "F",
            "company": "C",
            "status": "applied",
            "followup": True,
            "history": [{"status": "applied", "date": "2026-03-01"}],
        },
    ]
    out = _render_pipeline(ctx, apps, COLUMNS)
    order = [out.index('data-id="flag"'), out.index('data-id="old"'), out.index('data-id="new"')]
    assert order == sorted(order)  # flagged, then oldest move, then newest


def test_applied_column_scrolls_inside_itself_not_the_page(plugin):
    """§3: Applied (and any tall column) scrolls inside the column — no paging — so the board stays
    one viewport. Cards live in a bounded, scrollable col-list."""
    html = plugin._DASHBOARD_HTML
    css = html.split("</style>")[0]
    assert ".col-list { display: grid; gap: 8px; max-height: 62vh; overflow: auto; }" in css
    ctx = _view_ctx(plugin)
    out = _render_pipeline(ctx, [{"id": "a", "role": "R", "company": "C", "status": "applied"}], COLUMNS)
    assert 'class="col-list"' in out


def test_pipeline_hint_describes_keyboard_pickup_not_a_select(plugin):
    ctx = _view_ctx(plugin)
    out = _render_pipeline(ctx, [{"id": "a", "role": "R", "company": "C", "status": "applied"}], COLUMNS)
    assert "pl-kbd" in out and "to pick up" in out and "to drop" in out
    assert "Move to…" not in out  # the hint no longer points at a select


def test_pipeline_empty_state_when_no_live_roles(plugin):
    """§7 'Profile set, watch not run' Pipeline Empty: only closed roles (which never count) → no
    board, just the empty copy."""
    ctx = _view_ctx(plugin)
    out = _render_pipeline(ctx, [{"id": "r", "status": "rejected"}, {"id": "p", "status": "passed"}], COLUMNS)
    assert "No live applications" in out
    assert "Keep a role from Inbox, or paste a posting in chat." in out
    assert "pl-board" not in out  # the board isn't drawn when there are no live roles


# ── R2 keyboard pick-up (WCAG 2.5.7): Space / ←→ / Space-Enter, announced in one status region ──
def test_keyboard_pickup_arrow_drop_and_cancel(plugin):
    ctx = _view_ctx(plugin)
    ctx.eval("""
      function setTimeout(fn, ms) { if (fn) fn(); return 0; }
      var __moves = [];
      moveToColumn = function (id, col) { __moves.push([id, col]); };
      var __cols = ['Saved', 'Preparing', 'Applied', 'Interviewing', 'Offer', 'Closed'].map(function (name) {
        return { getAttribute: function (k) { return k === 'data-col' ? name : null; },
          classList: { toggle: function () {}, remove: function () {} } };
      });
      var __board = { querySelectorAll: function () { return __cols; }, querySelector: function () { return null; } };
      function mkCard(id) {
        return { _attr: {}, getAttribute: function (k) { return k === 'data-id' ? id : (this._attr[k] || null); },
          setAttribute: function (k, v) { this._attr[k] = String(v); },
          classList: { add: function () {}, remove: function () {} }, focus: function () {} };
      }
    """)
    jobs = {
        "applications": [{"id": "ap1", "role": "SRE", "company": "Acme", "status": "applied"}],
        "columns": COLUMNS,
        "inbox": [],
        "followup_days": 10,
    }
    ctx.eval("LOADING = false; JOBS = " + json.dumps(jobs) + "; var __card = mkCard('ap1');")
    # Space picks the card up, starting in its own column, and announces in the live region
    ctx.eval("boardKey({key: ' ', preventDefault: function () {}}, __board, __card);")
    assert ctx.eval("LIFT_ID") == "ap1"
    assert ctx.eval("LIFT_COL") == "Applied"
    assert ctx.eval("__card._attr['aria-pressed']") == "true"
    assert "Picked up SRE" in ctx.eval("document.getElementById('live').textContent")
    # ArrowRight walks the target column toward Closed
    ctx.eval("boardKey({key: 'ArrowRight', preventDefault: function () {}}, __board, __card);")
    assert ctx.eval("LIFT_COL") == "Interviewing"
    # Space drops: routes the move through moveToColumn and clears the lift
    ctx.eval("boardKey({key: ' ', preventDefault: function () {}}, __board, __card);")
    assert ctx.eval("JSON.stringify(__moves)") == '[["ap1","Interviewing"]]'
    assert ctx.eval("LIFT_ID") is None
    # A fresh pick-up then Escape cancels with no further move
    ctx.eval("boardKey({key: ' ', preventDefault: function () {}}, __board, __card);")
    ctx.eval("boardKey({key: 'Escape', preventDefault: function () {}}, __board, __card);")
    assert ctx.eval("LIFT_ID") is None
    assert ctx.eval("__moves.length") == 1  # Escape did not move anything
    assert "Move cancelled." in ctx.eval("document.getElementById('live').textContent")


def test_moves_are_announced_in_a_single_status_region(plugin):
    html = plugin._DASHBOARD_HTML
    assert '<div id="live" class="sr-only" role="status" aria-live="polite"></div>' in html
    assert "function announce(msg)" in html
    assert "document.getElementById('live')" in html
    assert "announce(role + ' moved to ' + dest" in html  # every move funnels through announce


def test_dropping_on_closed_asks_rejected_or_passed_default_passed(plugin):
    html = plugin._DASHBOARD_HTML
    assert "isClosedColumn" in html
    assert "askClosedStatus()" in html
    assert "Mark passed (default)" in html
    assert "done('passed')" in html
    assert "done('rejected')" in html


def test_move_is_optimistic_and_rolls_back_with_a_toast(plugin):
    html = plugin._DASHBOARD_HTML
    assert "app.status = status;" in html  # optimistic
    assert "if (!r.ok) throw new Error" in html
    assert "app.status = prev;" in html  # rollback
    assert "toast(" in html


def test_move_failure_rolls_back_with_the_section7_error_toast(plugin):
    html = plugin._DASHBOARD_HTML
    assert "Couldn’t move that role. It’s back where it was." in html  # §7 copy
    assert "{ error: true }" in html  # the pl-toast--error variant
    assert "pl-toast--error" in html


def test_every_move_offers_a_six_second_undo_calling_undo_move(plugin):
    """§7: the move toast carries a 6s Undo that calls POST /undo-move — never a second move. An
    undo failure reloads the board and shows the §7 error toast."""
    html = plugin._DASHBOARD_HTML
    assert "{ undo: function () { undoMove(id); } }" in html
    assert "function undoMove(id)" in html
    assert "'/applications/' + encodeURIComponent(id) + '/undo-move'" in html
    assert "opts.undo ? 6000 : 4000" in html  # 6s while an Undo is offered
    assert "Couldn’t undo that. The role stayed where you moved it." in html


def test_closed_tab_opens_read_only_drawer_instead_of_inline_move_select(plugin):
    ctx = _view_ctx(plugin)
    apps = [{"id": "r", "role": "Old role", "company": "C", "status": "rejected"}]
    closed = _render_closed(ctx, apps, COLUMNS)
    assert '<select class="movesel pl-select"' not in closed
    assert 'class="pl-tr--interactive"' in closed


# ── live refresh — subscribe to careercoach.# and reload in place on an event (§7) ──────
def test_page_subscribes_to_careercoach_events_and_reloads(plugin):
    html = plugin._DASHBOARD_HTML
    assert "protoagent:subscribe" in html
    assert "patterns: ['careercoach.#']" in html
    assert "d.type === 'protoagent:event'" in html
    # a successful write reloads for the authoritative board/inbox
    assert re.search(r"if \(!r\.ok\) throw new Error\([^)]*\);\s*load\(\);", html)
    # live refresh preserves the active tab, body scroll and focused row (re-render in place)
    assert "var scroll = body ? body.scrollTop : 0;" in html
    assert "if (body) body.scrollTop = scroll;" in html


# ── the board follow-up line renders ONLY for flagged cards, in exactly one gated branch ──
def test_board_followup_line_is_one_gated_pl_chip_warning(plugin):
    html = plugin._DASHBOARD_HTML
    assert "var nudge = a.followup" in html  # gated on the server follow-up flag
    assert html.count('pl-chip--warning">No reply in ') == 1  # one board branch, not duplicated


# ── the Inbox tab (§3): freshness groups, text reasons, ghost filter, multi-select, triage ──
# NOW is pinned so freshness buckets (cut by posted, <24h / <168h) are deterministic in quickjs.
_FIXED_NOW = "var __NOW = new Date('2026-10-31T00:00:00Z').getTime(); Date.now = function () { return __NOW; };"


def _inbox_item(iid, score, posted, **extra):
    it = {
        "id": iid,
        "title": extra.get("title", "Staff Platform Engineer"),
        "company": extra.get("company", "Northwind"),
        "score": score,
        "reason": extra.get("reason", 'title matches "Staff Platform Engineer" (4/4 words)'),
        "posted": posted,
        "first_seen": extra.get("first_seen", posted),
        "ghost": extra.get("ghost", []),
    }
    if "url" in extra:
        it["url"] = extra["url"]
    if "snippet" in extra:
        it["snippet"] = extra["snippet"]
    return it


def _render_inbox(
    ctx,
    inbox,
    *,
    watch_last_run="2026-10-20T09:00:00Z",
    last_visit_iso="2026-10-30T00:00:00Z",
    sort="newest",
    hide_ghost=False,
    sel=None,
    open_map=None,
    hidden=None,
):
    ctx.eval(_FIXED_NOW)
    jobs = {
        "applications": [],
        "columns": COLUMNS,
        "inbox": inbox,
        "followup_days": 10,
        "watch_last_run": watch_last_run,
    }
    ctx.eval("LOADING = false; JOBS = " + json.dumps(jobs) + ";")
    ctx.eval("LAST_VISIT = new Date(%s).getTime();" % json.dumps(last_visit_iso))
    ctx.eval("INBOX_SORT = %s; HIDE_GHOST = %s;" % (json.dumps(sort), "true" if hide_ghost else "false"))
    ctx.eval("INBOX_SEL = %s; INBOX_HIDDEN = %s;" % (json.dumps(sel or {}), json.dumps(hidden or {})))
    if open_map is not None:
        ctx.eval("INBOX_OPEN = %s;" % json.dumps(open_map))
    ctx.eval(
        "var __B = {innerHTML: '', querySelectorAll: function () { return []; }, "
        "querySelector: function () { return null; }}; renderInbox(__B);"
    )
    return ctx.eval("__B.innerHTML")


def test_inbox_groups_by_freshness_with_spec_defaults(plugin):
    """§3: four groups — Today, This week, Older, then Title didn't match (score-0 ONLY). Today and
    This week open by default; Older and Title didn't match collapsed. A partial score stays put."""
    ctx = _view_ctx(plugin)
    inbox = [
        _inbox_item("today", 100, "2026-10-30T18:00:00Z"),
        _inbox_item("week", 100, "2026-10-28T00:00:00Z"),
        _inbox_item("older_partial", 67, "2026-10-01T00:00:00Z"),  # partial score, NOT a miss
        _inbox_item("miss", 0, "2026-10-20T00:00:00Z", reason="No target title words matched"),
    ]
    out = _render_inbox(ctx, inbox)
    for label in ("Today", "This week", "Older", "Title didn’t match"):
        assert label in out
    assert out.index("Today") < out.index("This week") < out.index("Older") < out.index("Title didn’t match")
    # open defaults
    assert re.search(r'data-g="today"[^>]* open>', out)
    assert re.search(r'data-g="week"[^>]* open>', out)
    assert re.search(r'data-g="older"(?![^>]*open)', out)  # collapsed
    assert re.search(r'data-g="miss"(?![^>]*open)', out)  # collapsed
    # the 67 row stays in its freshness group (Older), never in Title didn't match
    assert out.index('data-id="older_partial"') < out.index('data-g="miss"')
    # only the score-0 row sits under Title didn't match
    assert out.index('data-g="miss"') < out.index('data-id="miss"')
    assert "pl-accordion" in out and "pl-count" in out


def test_inbox_sort_orders_rows_newest_or_oldest(plugin):
    """§3: newest first within a group by default; the Sort select flips to oldest first."""
    ctx = _view_ctx(plugin)
    inbox = [
        _inbox_item("earlier", 100, "2026-10-30T02:00:00Z"),
        _inbox_item("later", 100, "2026-10-30T20:00:00Z"),
    ]
    newest = _render_inbox(ctx, inbox, sort="newest")
    assert 'id="ibx-sort"' in newest
    assert newest.index('data-id="later"') < newest.index('data-id="earlier"')
    oldest = _render_inbox(ctx, inbox, sort="oldest")
    assert oldest.index('data-id="earlier"') < oldest.index('data-id="later"')


def test_inbox_match_reason_is_text_and_shows_no_numeric_score(plugin):
    """§3/§1.5: the reason is text ('Title matches / partly matches / doesn't match'); the numeric
    score never shows anywhere."""
    ctx = _view_ctx(plugin)
    inbox = [
        _inbox_item("full", 100, "2026-10-30T18:00:00Z"),
        _inbox_item(
            "part",
            67,
            "2026-10-30T18:00:00Z",
            title="Platform Eng",
            reason='title matches "Platform Engineer" (2/3 words)',
        ),
        _inbox_item("miss", 0, "2026-10-30T18:00:00Z", title="Data Scientist", reason="No target title words matched"),
    ]
    out = _render_inbox(ctx, inbox)
    assert "Title matches <em>Staff Platform Engineer</em>" in out
    assert "Title partly matches <em>Platform Engineer</em>" in out
    assert "Title doesn’t match your targets" in out
    assert "100" not in out and ">67<" not in out and "fit 67" not in out  # no numeric score


def test_inbox_ghost_warning_badge_and_conditional_filter_chip(plugin):
    """§3: ghost = a 'Possible ghost job' pl-badge--warning with evidence; the Hide-ghost chip (with
    count) renders ONLY when a ghost exists, and hiding filters the row out."""
    ctx = _view_ctx(plugin)
    clean = [_inbox_item("a", 100, "2026-10-30T18:00:00Z")]
    out = _render_inbox(ctx, clean)
    assert "Possible ghost job" not in out
    assert "Hide likely ghost jobs" not in out  # no chip without a ghost
    ghosted = clean + [
        _inbox_item("g", 100, "2026-10-30T18:00:00Z", title="G", ghost=["reposted 4 times in 60 days", "open 80 days"])
    ]
    out2 = _render_inbox(ctx, ghosted)
    assert 'class="pl-badge pl-badge--warning"' in out2 and "Possible ghost job" in out2
    assert "reposted 4 times in 60 days; open 80 days" in out2  # evidence joined (tooltip + inline text)
    assert "Hide likely ghost jobs · 1" in out2  # chip with its count
    hidden = _render_inbox(ctx, ghosted, hide_ghost=True)
    assert 'data-id="g"' not in hidden  # the ghost row is filtered out
    assert "✓ Hide likely ghost jobs · 1" in hidden  # chip reads active


def test_inbox_is_a_multiselect_listbox_with_hover_focus_actions(plugin):
    """§3/§6/§10: rows are a listbox with aria-multiselectable; Keep / Dismiss / View posting and the
    checkbox reveal only on hover, focus or selection — never at rest (enforced by the CSS)."""
    html = plugin._DASHBOARD_HTML
    css = html.split("</style>")[0]
    assert ".ibx-acts { display: flex; gap: 6px; flex: none; visibility: hidden; }" in css
    assert (
        ".ibx-row:hover .ibx-acts, .ibx-row:focus .ibx-acts, .ibx-row:focus-within .ibx-acts, "
        ".ibx-row.sel .ibx-acts { visibility: visible; }" in css
    )
    assert ".ibx-ck { flex: none; margin-top: 2px; visibility: hidden; }" in css
    ctx = _view_ctx(plugin)
    out = _render_inbox(ctx, [_inbox_item("a", 100, "2026-10-30T18:00:00Z", url="https://jobs.example.com/1")])
    assert 'role="listbox" aria-multiselectable="true"' in out
    assert 'role="option" aria-selected="false" tabindex="0" data-id="a"' in out
    assert 'type="checkbox" class="pl-checkbox ibx-ck"' in out
    assert 'data-a="keep"' in out and 'data-a="dismiss"' in out and 'data-a="open"' in out
    assert 'target="_blank" rel="noopener"' in out and 'href="https://jobs.example.com/1"' in out


def test_inbox_new_marker_comes_from_the_last_visit(plugin):
    """§3/§11.1: a row surfaced since the last visit carries the pl-dot--info + text 'New'; older
    rows do not."""
    ctx = _view_ctx(plugin)
    inbox = [
        _inbox_item("fresh", 100, "2026-10-30T18:00:00Z", title="Fresh"),
        _inbox_item("stale", 100, "2026-10-28T00:00:00Z", title="Stale"),
    ]
    out = _render_inbox(ctx, inbox, last_visit_iso="2026-10-30T00:00:00Z")
    assert '<span class="pl-dot pl-dot--info"' in out and ">New<" in out
    assert out.count('class="ibx-new"') == 1  # exactly the fresh row is marked


def test_inbox_selection_turns_the_toolbar_into_keep_dismiss(plugin):
    """§3: selecting any row turns the toolbar into 'N selected · Keep · Dismiss'; each group header
    carries Select all."""
    ctx = _view_ctx(plugin)
    inbox = [_inbox_item("a", 0, "2026-10-20T00:00:00Z", reason="No target title words matched")]
    plain = _render_inbox(ctx, inbox)
    assert 'id="ibx-sort"' in plain and "selected</" not in plain
    assert 'data-all="miss"' in plain and ">Select all<" in plain  # per-group Select all
    sel = _render_inbox(ctx, inbox, sel={"a": 1})
    assert "1 selected" in sel
    assert 'id="ibx-bkeep">Keep<' in sel and 'id="ibx-bdismiss">Dismiss<' in sel
    assert 'aria-selected="true"' in sel and "ibx-multi" in sel


def test_inbox_select_all_selects_every_visible_row_in_one_group(plugin):
    ctx = _view_ctx(plugin)
    ctx.eval(_FIXED_NOW)
    inbox = [
        _inbox_item("m1", 0, "2026-10-20T00:00:00Z", reason="No target title words matched"),
        _inbox_item("m2", 0, "2026-10-19T00:00:00Z", reason="No target title words matched"),
        _inbox_item("t1", 100, "2026-10-30T18:00:00Z"),
    ]
    jobs = {"applications": [], "columns": COLUMNS, "inbox": inbox, "followup_days": 10}
    ctx.eval("LOADING = false; JOBS = " + json.dumps(jobs) + ";")
    ctx.eval("INBOX_SEL = {}; INBOX_HIDDEN = {}; HIDE_GHOST = false; render = function () {};")
    ctx.eval("inboxSelectAll('miss');")
    assert json.loads(ctx.eval("JSON.stringify(INBOX_SEL)")) == {"m1": 1, "m2": 1}  # the two misses only


# ── Keep / Dismiss: optimistic, Keep writes at once, Dismiss HOLDS its write for the 6s Undo ──
_TIMER_STUBS = """
  var __timers = [];
  function setTimeout(fn, ms) { __timers.push({ fn: fn, ms: ms }); return __timers.length; }
  function clearTimeout(h) { if (h) __timers[h - 1] = null; }
  var __writes = [];
  write = function (p, b) { __writes.push([p, b]); return { then: function () { return this; }, catch: function () { return this; } }; };
  var __toasts = [];
  toast = function (m, o) { __toasts.push({ msg: m, opts: o || null }); };
  render = function () {};
  load = function () {};
  announce = function () {};
"""


def _inbox_action_ctx(plugin, inbox):
    ctx = _view_ctx(plugin)
    ctx.eval(_TIMER_STUBS)
    jobs = {"inbox": inbox, "applications": [], "columns": COLUMNS}
    ctx.eval("JOBS = " + json.dumps(jobs) + "; INBOX_HIDDEN = {}; DISMISS_TIMERS = {}; INBOX_SEL = {};")
    return ctx


def test_keep_removes_the_row_writes_at_once_and_toasts_kept_in_saved(plugin):
    ctx = _inbox_action_ctx(plugin, [{"id": "in1", "title": "Staff Eng", "company": "C", "score": 100}])
    ctx.eval("inboxKeep(['in1']);")
    assert ctx.eval("!!INBOX_HIDDEN['in1']") is True  # optimistically removed
    assert json.loads(ctx.eval("JSON.stringify(__writes)")) == [["/inbox/keep", {"id": "in1"}]]  # immediate
    assert ctx.eval("__toasts[0].msg") == "Kept — in Saved."  # §3 copy
    assert ctx.eval("__toasts[0].opts") is None  # Keep offers no Undo


def test_dismiss_holds_the_write_until_the_six_second_window_closes(plugin):
    ctx = _inbox_action_ctx(plugin, [{"id": "in1", "title": "Staff Eng", "company": "C", "score": 100}])
    ctx.eval("inboxDismiss(['in1']);")
    assert ctx.eval("!!INBOX_HIDDEN['in1']") is True  # row gone immediately
    assert ctx.eval("__writes.length") == 0  # but the dismiss write is HELD
    assert ctx.eval("__timers[__timers.length - 1].ms") == 6000  # for the 6s Undo window
    assert ctx.eval("!!(__toasts[0].opts && __toasts[0].opts.undo)") is True  # Undo is offered
    ctx.eval("__timers[__timers.length - 1].fn();")  # the window closes
    assert json.loads(ctx.eval("JSON.stringify(__writes)")) == [["/inbox/dismiss", {"id": "in1"}]]


def test_dismiss_undo_cancels_the_held_write_and_restores_the_row(plugin):
    ctx = _inbox_action_ctx(plugin, [{"id": "in1", "title": "Staff Eng", "company": "C", "score": 100}])
    ctx.eval("inboxDismiss(['in1']);")
    ctx.eval("__toasts[0].opts.undo();")  # the operator hits Undo inside the window
    assert ctx.eval("!!INBOX_HIDDEN['in1']") is False  # the row is back
    ctx.eval("__timers.forEach(function (t) { if (t && t.fn) t.fn(); });")  # any stale timer fires
    assert ctx.eval("__writes.length") == 0  # the dismiss write never went


def test_inbox_keep_and_dismiss_roll_back_with_the_section7_error_toast(plugin):
    html = plugin._DASHBOARD_HTML
    assert html.count("Couldn’t move that role. It’s back where it was.") >= 2  # move + inbox keep/dismiss reuse
    assert "write('/inbox/keep', { id: id })" in html
    assert "write('/inbox/dismiss', { id: id })" in html
    assert "}, 6000);" in html  # the dismiss write is deferred to the end of the window


def test_inbox_empty_states_watch_not_run_vs_all_caught_up(plugin):
    """§7: an empty watch_last_run means the watch has never run ('Your watch is looking'); once it
    has run, a cleared inbox reads 'All caught up' with the last-run date."""
    ctx = _view_ctx(plugin)
    looking = _render_inbox(ctx, [], watch_last_run="")
    assert "Your watch is looking" in looking and "New roles land here." in looking
    assert "All caught up" not in looking
    caught = _render_inbox(ctx, [], watch_last_run="2026-10-20T09:30:00Z")
    assert "All caught up" in caught
    assert "Your watch last ran on 2026-10-20." in caught


# ── the inbox posting link stays safe (carried guard, rebuilt markup) ───────────────────
def test_inbox_item_renders_all_fields_and_a_safe_posting_link(plugin):
    ctx = _view_ctx(plugin)
    out = _render_inbox(
        ctx,
        [_inbox_item("a", 100, "2026-10-30T18:00:00Z", url="https://jobs.example.com/1")],
    )
    assert "Staff Platform Engineer" in out and "Northwind" in out  # title + company
    assert "h ago" in out or "d ago" in out  # relative posted date
    assert ">Keep<" in out and ">Dismiss<" in out and ">View posting<" in out
    html = plugin._DASHBOARD_HTML
    assert 'target="_blank" rel="noopener"' in html
    assert '''href="' + esc(it.url) + '"''' in html


def test_all_posting_and_inbox_text_is_escaped(plugin):
    """No posting/inbox field is interpolated as raw HTML — every one goes through esc()."""
    html = plugin._DASHBOARD_HTML
    for frag in (
        "esc(it.title)",
        "esc(it.company)",
        "esc(it.url)",
        "esc(a.role)",
        "esc(a.company)",
        "esc(a.id)",
        "esc(it.id)",
    ):
        assert frag in html, frag


# ── §6 removals: no hex fallbacks, no hand-rolled .btn/.pill/.card, no accent h1 ───────
def test_no_hex_fallbacks_or_hand_rolled_component_css(plugin):
    html = plugin._DASHBOARD_HTML
    assert not re.search(r"var\(--pl-[a-z0-9-]+,\s*#", html)  # no hex fallback in var(--pl-…, #…)
    assert "rgba(" not in html  # overlays use the --pl-color-overlay token, not a hardcoded rgba
    css = html.split("</style>")[0]
    for selector in (".btn ", ".btn{", ".btn,", ".pill ", ".pill{", ".card ", ".card{"):
        assert selector not in css, selector  # no hand-rolled .btn/.pill/.card rules
    assert 'class="btn' not in html and 'class="pill"' not in html and 'class="card"' not in html
    # the h1 is not accent-coloured (the removed accent h1)
    assert "h1 { font-size: 20px; margin: 0 0 2px; color: var(--pl-color-accent" not in html


def test_overlays_use_kit_drawer_and_dialog_classes(plugin):
    html = plugin._DASHBOARD_HTML
    assert "pl-drawer--right" in html  # the About-you and job drawers
    assert "pl-dialog" in html  # the rejected/passed chooser + mark-applied confirm boxes
    assert "pl-toast" in html
    assert "var(--pl-color-overlay)" in html


# ── the dynamic-path id scan (unchanged guard: inbox ids in the body, not the path) ────
def _dynamic_id_resources(html):
    token = r"""(?:'[^']*'|"[^"]*"|encodeURIComponent\([^)]*\)|[A-Za-z_$][\w.$]*)"""
    paths = re.findall(rf"\b(?:api|write|apiFetch)\(\s*({token}(?:\s*\+\s*{token})*)", html)
    paths += re.findall(r"""['"][^'"]*/['"]\s*\+\s*encodeURIComponent\([^)]*\)""", html)
    resources = set()
    for path in paths:
        if path == "'/inbox/' + action":
            continue
        resources.update(
            re.findall(r""" /([\w-]+)/['"]\s*\+\s*(?:encodeURIComponent\(|[A-Za-z_$])""", path, re.VERBOSE)
        )
    return resources


def _assert_fleet_safe_id(item_id):
    encoded = quote(item_id, safe="-_.!~*'()")
    assert not re.search(r"%2f|%2e|%5c|%25", encoded, re.IGNORECASE), encoded
    assert not any(char in item_id for char in ("/", "\\", "%")), item_id


def test_dynamic_path_ids_are_fleet_safe(plugin, state, tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(tmp_path / "cc"))
    producers = {
        "applications": lambda: state.track_application(company="Acme", role="Engineer")["id"],
    }
    resources = _dynamic_id_resources(plugin._DASHBOARD_HTML)
    assert "applications" in resources  # the scan must not silently match nothing
    assert "inbox" not in resources, "Inbox ids belong in the JSON body"
    assert not resources - producers.keys(), (
        f"Add a real id producer for dynamic path resources: {resources - producers.keys()}"
    )
    for resource in resources:
        _assert_fleet_safe_id(producers[resource]())

    with pytest.raises(AssertionError):
        _assert_fleet_safe_id("himalayas:https://himalayas.app/companies/acme/jobs/123?ref=x&y=1")


@pytest.mark.parametrize(
    "source",
    [
        "write('/inbox/' + encodeURIComponent(id) + '/' + action, null)",
        "api('/new_resource/' + id + '/detail')",
        "write('/new_resource/' + row.id, {})",
        "kit.apiFetch('/api/plugins/careercoach/new_resource/' + encodeURIComponent(row.id))",
        "var path = '/new_resource/' + encodeURIComponent(id); api(path)",
    ],
)
def test_dynamic_path_scan_detects_encoded_and_raw_ids(source):
    expected = "inbox" if "/inbox/" in source else "new_resource"
    assert _dynamic_id_resources(source) == {expected}


@pytest.mark.parametrize("drop_key", [" ", "Enter"])
@pytest.mark.parametrize("choice", ["passed", "rejected", "cancel", "escape"])
def test_keyboard_closed_drop_keeps_modal_focus(plugin, drop_key, choice):
    ctx = _view_ctx(plugin)
    ctx.eval("JOBS = " + json.dumps({"columns": COLUMNS, "applications": [{"id": "a", "status": "offer"}]}) + ";")
    ctx.eval("""
      var card = __mkEl('card');
      card.setAttribute('data-id', 'a'); card.isConnected = true;
      card.focus = function () { document.activeElement = card; };
      document.querySelector = function () { return card; };
      var board = { querySelectorAll: function () { return []; } };
      ['choose-passed', 'choose-rejected', 'choose-cancel'].forEach(function (id) {
        var el = document.getElementById(id);
        el.focus = function () { document.activeElement = el; };
      });
      var moves = [];
      moveApp = function (id, status) { moves.push([id, status]); };
      card.focus(); LIFT_ID = 'a'; LIFT_COL = 'Closed';
    """)
    ctx.eval("boardKey({key: " + json.dumps(drop_key) + ", preventDefault: function () {}}, board, card);")
    assert ctx.eval("document.activeElement.id") == "choose-passed"
    assert ctx.eval("LIFT_ID") is None
    assert ctx.eval("moves.length") == 0
    if choice == "escape":
        ctx.eval(
            "document.getElementById('chooser').onkeydown({key: 'Escape', preventDefault: function () {}, stopPropagation: function () {}});"
        )
    else:
        ctx.eval("document.getElementById('choose-" + choice + "').onclick();")
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("document.activeElement.id") == "card"
    assert ctx.eval("document.getElementById('chooser').style.display") == "none"
    expected = [] if choice in ("cancel", "escape") else [["a", choice]]
    assert json.loads(ctx.eval("JSON.stringify(moves)")) == expected


def test_closed_table_pages_filters_and_keeps_global_summary(plugin):
    ctx = _view_ctx(plugin)
    apps = [
        {
            "id": str(i),
            "role": f"Role {i:02}",
            "company": "Acme" if i % 2 else "Other",
            "status": "rejected" if i % 2 else "passed",
            "updated": f"2026-10-{i + 1:02}",
            "fit_score": 90,
        }
        for i in range(30)
    ]
    out = _render_closed(ctx, apps, COLUMNS)
    assert "30 closed · 15 rejected · 15 passed" in out
    assert out.count('class="pl-tr--interactive"') == 25
    assert out.index("Role 29") < out.index("Role 28")
    assert "Role 04" not in out
    assert "<th>Fit" not in out and "fit 90" not in out
    assert 'class="pl-tabs pl-tabs--segmented"' in out
    ctx.eval("document.getElementById('closed-next').onclick();")
    out = ctx.eval("__B.innerHTML")
    assert out.count('class="pl-tr--interactive"') == 5
    assert "26–30 of 30" in out
    ctx.eval("CLOSED_Q = 'ACME'; CLOSED_OUT = 'rejected';")
    out = _render_closed(ctx, apps, COLUMNS)
    assert ctx.eval("CLOSED_PAGE") == 0  # clamp after filtering or refresh removes a page
    assert out.count('class="pl-tr--interactive"') == 15
    assert "30 closed · 15 rejected · 15 passed" in out
    ctx.eval("CLOSED_Q = 'role 29';")
    out = _render_closed(ctx, apps, COLUMNS)
    assert out.count('class="pl-tr--interactive"') == 1
    ctx.eval("CLOSED_OUT = 'passed';")
    assert "No closed roles match" in _render_closed(ctx, apps, COLUMNS)


def test_closed_date_uses_last_move_not_later_note_update(plugin):
    ctx = _view_ctx(plugin)
    out = _render_closed(
        ctx,
        [
            {
                "id": "a",
                "role": "Earlier",
                "status": "passed",
                "updated": "2026-10-31",
                "history": [{"date": "2026-10-01"}],
            },
            {"id": "b", "role": "Later", "status": "rejected", "updated": "2026-10-02"},
        ],
        COLUMNS,
    )
    assert out.index("Later") < out.index("Earlier")
    assert "2026-10-31" not in out
    assert "2026-10-01" in out


# ── R5 §8: narrow widths switch on a CONTAINER query on the view root, not the viewport ──
def test_layout_bands_switch_on_a_container_query_not_the_viewport(plugin):
    css = plugin._DASHBOARD_HTML.split("</style>")[0]
    # the view root is the query container (inline-size), so the rail panel and split-screen read ITS
    # width, never the viewport's
    assert ".cc { container: cc / inline-size;" in css
    # ≥900px: the five columns share the width, the Closed stub is 120px (§8)
    assert ".board { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)) 120px;" in css
    # the bands are @container rules at 900 and 600 — never an @media width query
    assert "@container cc (max-width: 899px)" in css
    assert "@container cc (max-width: 599px)" in css
    assert "@media (max-width" not in css and "@media (min-width" not in css


def test_split_screen_band_scrolls_the_board_sideways_with_snap_columns(plugin):
    css = plugin._DASHBOARD_HTML.split("</style>")[0]
    band = css.split("@container cc (max-width: 899px)", 1)[1].split("@container cc (max-width: 599px)", 1)[0]
    # 600–899px: columns fix at 220px, the board scrolls sideways with scroll-snap, stub 110px (§8)
    assert "grid-template-columns: repeat(5, 220px) 110px;" in band
    assert "overflow-x: auto;" in band
    assert "scroll-snap-type: x mandatory;" in band
    assert ".col { scroll-snap-align: start; }" in band
    # the drawers go full width in this band (§8)
    assert "#detail .detail-panel, #about { width: 100%; }" in css


def test_rail_band_swaps_board_for_stages_folds_closed_and_bottoms_the_drawer(plugin):
    css = plugin._DASHBOARD_HTML.split("</style>")[0]
    rail = css.split("@container cc (max-width: 599px)", 1)[1]
    # <600px: the board is hidden and the stage-list shown; the drawer is bottom-side at 85dvh; the
    # Closed table folds Company/Closed-on under the role; the header's target-roles subline hides (§8)
    assert ".board { display: none; }" in rail
    assert ".stages { display: block; }" in rail
    assert "#detail .detail-panel { top: auto; bottom: 0; height: 85dvh; }" in rail
    assert ".closed-company, .closed-date { display: none; }" in rail
    assert ".closed-sub { display: block; }" in rail
    assert ".hd .s { display: none; }" in rail
    assert "#about, #detail .detail-panel { width: 100%; }" in rail


def test_reduced_motion_is_honoured_for_chevrons_and_the_toast(plugin):
    rule = plugin._DASHBOARD_HTML.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".grp > summary .chev { transition: none; }" in rule
    assert "#toast { transition: none; }" in rule  # §10 honour reduced-motion for the toast fade too


# ── R5 §8: <600px Pipeline is a stage-grouped Accordion list (drag off) ─────────────────
def test_rail_pipeline_is_a_stage_grouped_accordion_list(plugin):
    ctx = _view_ctx(plugin)
    apps = [
        {"id": "s1", "role": "Saved role", "company": "Acme", "status": "considering"},
        {"id": "a1", "role": "Applied role", "company": "Lumen", "status": "applied"},
        {"id": "a2", "role": "Flagged", "company": "Quarry", "status": "applied", "followup": True},
    ]
    html = _render_pipeline(ctx, apps, COLUMNS)
    stages = html.partition('<div class="stages"')[2]
    # one accordion per stage, in spec order, each with a Count (§8)
    assert '<div class="stages" aria-label="Pipeline by stage">' in html
    for stage in ("Saved", "Preparing", "Applied", "Interviewing", "Offer"):
        assert 'data-stage="' + stage + '"' in stages
    assert (
        stages.index('data-stage="Saved"')
        < stages.index('data-stage="Preparing"')
        < stages.index('data-stage="Applied"')
    )
    # stages with items open; empty stages collapse to one line (count still showing)
    assert 'data-stage="Saved" open>' in stages
    assert 'data-stage="Applied" open>' in stages
    assert 'data-stage="Preparing"><summary>' in stages  # empty → collapsed, not open
    assert 'data-stage="Offer"><summary>' in stages
    # the live roles are rail rows (not board cards) carrying role, company and the follow-up TEXT
    assert stages.count('class="srow"') == 3
    assert "Applied role" in stages and "Lumen" in stages and "No reply" in stages
    assert 'class="pcard' not in stages  # the board card markup never leaks into the stage list
    # a Closed link row rounds off the list (keyboard-operable)
    assert 'id="to-closed2"' in stages
    assert "Closed · 0" in stages


def test_rail_stage_empty_pipeline_renders_no_stages(plugin):
    """§7 Pipeline Empty (no live roles): neither the board NOR the stage-list is drawn."""
    ctx = _view_ctx(plugin)
    out = _render_pipeline(ctx, [{"id": "r", "status": "rejected"}, {"id": "p", "status": "passed"}], COLUMNS)
    assert "No live applications" in out
    assert 'class="stages"' not in out and "pl-board" not in out


def test_rail_stage_list_keyboard_pickup_moves_between_stages(plugin):
    """§8/§10: drag is off in the rail, so a focused stage row is picked up with Space, the target
    stage chosen with ↑/↓ (the vertical analogue of the board's ←/→), and Space drops — every step
    announced in the single live region. Mirrors the board's keyboard pick-up."""
    ctx = _view_ctx(plugin)
    ctx.eval("""
      function setTimeout(fn, ms) { if (fn) fn(); return 0; }
      var __moves = [];
      moveToColumn = function (id, col) { __moves.push([id, col]); };
      var __cols = ['Saved', 'Preparing', 'Applied', 'Interviewing', 'Offer', 'Closed'].map(function (name) {
        return { getAttribute: function (k) { return k === 'data-col' ? name : null; } };
      });
      var __board = { querySelectorAll: function (sel) { return sel === 'details[data-stage]' ? [] : __cols; },
        querySelector: function () { return null; } };
      function mkRow(id) {
        return { _attr: {}, getAttribute: function (k) { return k === 'data-id' ? id : (this._attr[k] || null); },
          setAttribute: function (k, v) { this._attr[k] = String(v); },
          classList: { add: function () {}, remove: function () {} }, focus: function () {} };
      }
    """)
    jobs = {
        "applications": [{"id": "ap1", "role": "SRE", "company": "Acme", "status": "applied"}],
        "columns": COLUMNS,
        "inbox": [],
        "followup_days": 10,
    }
    ctx.eval("LOADING = false; JOBS = " + json.dumps(jobs) + "; var __row = mkRow('ap1');")
    # Space picks the row up, starting in its own stage, and announces (mentioning up/down arrows)
    ctx.eval("stageKey({key: ' ', preventDefault: function () {}}, __board, __row);")
    assert ctx.eval("LIFT_ID") == "ap1"
    assert ctx.eval("LIFT_COL") == "Applied"
    assert ctx.eval("__row._attr['aria-pressed']") == "true"
    live = ctx.eval("document.getElementById('live').textContent")
    assert "Picked up SRE" in live and "up and down arrows" in live
    # ArrowDown walks toward Closed; ArrowUp walks back
    ctx.eval("stageKey({key: 'ArrowDown', preventDefault: function () {}}, __board, __row);")
    assert ctx.eval("LIFT_COL") == "Interviewing"
    ctx.eval("stageKey({key: 'ArrowUp', preventDefault: function () {}}, __board, __row);")
    assert ctx.eval("LIFT_COL") == "Applied"
    ctx.eval("stageKey({key: 'ArrowDown', preventDefault: function () {}}, __board, __row);")
    # Space drops: routes through the shared moveToColumn and clears the lift
    ctx.eval("stageKey({key: ' ', preventDefault: function () {}}, __board, __row);")
    assert ctx.eval("JSON.stringify(__moves)") == '[["ap1","Interviewing"]]'
    assert ctx.eval("LIFT_ID") is None
    # a fresh pick-up then Escape cancels with no further move
    ctx.eval("stageKey({key: ' ', preventDefault: function () {}}, __board, __row);")
    ctx.eval("stageKey({key: 'Escape', preventDefault: function () {}}, __board, __row);")
    assert ctx.eval("LIFT_ID") is None
    assert ctx.eval("__moves.length") == 1  # Escape did not move anything
    assert "Move cancelled." in ctx.eval("document.getElementById('live').textContent")


def test_rail_stage_row_opens_the_drawer_not_a_move_on_plain_enter(plugin):
    html = plugin._DASHBOARD_HTML
    # a row at rest opens the drawer on click / Enter; the pick-up is behind Space (§8)
    assert "if (!LIFT_ID) openDetail(row.getAttribute('data-id'));" in html
    assert "if (k === 'Enter') { ev.preventDefault(); openDetail(id); return; }" in html
    # focus restoration lands on the VISIBLE copy (board or stage-list), never the hidden one (§8)
    assert "function visibleInBody(id)" in html
    assert "els[i].offsetParent !== null" in html


# ── R5 §8: tabs use `responsive`, collapsing to a menu trigger when the counts don't fit ──
def _fit_tabs(ctx, scroll_width):
    ctx.eval(
        "var __t = document.getElementById('tabs');"
        "__t.scrollWidth = %d; __t.clientWidth = 100; fitTabs();" % scroll_width
    )
    return ctx.eval("document.getElementById('tabs').innerHTML")


def test_tabs_collapse_to_a_menu_trigger_when_they_dont_fit(plugin):
    ctx = _view_ctx(plugin)
    apps = [{"id": "a%d" % i, "status": "applied"} for i in range(5)]  # pipeline 5
    inbox = [{"id": "in%d" % i} for i in range(49)]  # inbox 49
    jobs = {"applications": apps, "columns": COLUMNS, "inbox": inbox, "followup_days": 10}
    ctx.eval("LOADING = false; STATE = null; JOBS = " + json.dumps(jobs) + "; renderTabs();")
    # they fit (scrollWidth <= clientWidth) → the WAI-ARIA tablist stays untouched
    fits = _fit_tabs(ctx, 90)
    assert 'role="tab"' in fits and "tabs-trigger" not in fits
    # they overflow → collapse to one menu trigger that opens a menu of the same tabs (§8)
    collapsed = _fit_tabs(ctx, 500)
    assert 'id="tabs-trigger"' in collapsed and 'aria-haspopup="menu"' in collapsed
    assert 'role="menu"' in collapsed
    assert collapsed.count('role="menuitemradio"') == 3
    assert 'aria-checked="true"' in collapsed  # the active tab is marked in the menu
    # counts ride along in the accessible name and as a badge (§10: counts in the accessible name)
    assert 'aria-label="Pipeline, 5, choose a view"' in collapsed
    assert 'aria-label="Inbox, 49"' in collapsed
    assert 'class="pl-count"' in collapsed


def test_width_change_re_evaluates_today_cap_tabs_and_drawer_side(plugin):
    html = plugin._DASHBOARD_HTML
    assert "function onResize() { sizeDetail(); renderTabs(); renderToday(); }" in html
    assert "new ResizeObserver(onResize)" in html  # the container's width drives it, not the viewport
    # the drawer goes bottom-side below 600px (§8)
    assert "var bottom = document.getElementById('cc').clientWidth < 600;" in html
    assert "panel.classList.toggle('pl-drawer--bottom', bottom);" in html


@pytest.mark.parametrize("known", [False, True])
def test_rail_legacy_roles_remain_openable(plugin, known):
    ctx = _view_ctx(plugin, width=420)
    apps = [{"id": "old", "role": "Legacy role", "status": "legacy"}]
    if known:
        apps.append({"id": "new", "status": "applied"})
    stages = _render_pipeline(ctx, apps, COLUMNS).partition('<div class="stages"')[2]
    assert 'data-stage="Other statuses" open>' in stages
    assert 'data-id="old"' in stages
    assert 'draggable="true"' not in stages
    assert 'data-col="Other statuses"' not in stages
    ctx.eval("""
      var row = __mkEl('legacy-row');
      row.setAttribute('data-id', 'old');
      row.addEventListener = function (name, fn) { this[name] = fn; };
      var opened = [];
      openDetail = function (id) { opened.push(id); };
      wireStages({querySelectorAll: function () { return [row]; }});
      row.click();
      row.keydown({key: 'Enter', preventDefault: function () {}});
    """)
    assert json.loads(ctx.eval("JSON.stringify(opened)")) == ["old", "old"]


@pytest.mark.parametrize("rail", [False, True])
@pytest.mark.parametrize("connected", [False, True])
def test_drawer_close_returns_to_visible_role_copy(plugin, rail, connected):
    ctx = _view_ctx(plugin)
    ctx.eval("""
      function card(name, visible) {
        var el = __mkEl(name);
        el.isConnected = true;
        el.offsetParent = visible ? {} : null;
        el.setAttribute('data-id', 'role');
        el.focus = function () { document.activeElement = this; };
        return el;
      }
    """)
    ctx.eval("var board = card('board', %s), stage = card('stage', %s);" % (json.dumps(not rail), json.dumps(rail)))
    ctx.eval("DETAIL_RETURN = " + ("stage" if not rail else "board") + ";")
    if not connected:
        ctx.eval("DETAIL_RETURN = card('removed', true); DETAIL_RETURN.isConnected = false;")
    ctx.eval("""
      document.getElementById('body').querySelectorAll = function () { return [board, stage]; };
      closeDetail();
    """)
    assert ctx.eval("document.activeElement.id") == ("stage" if rail else "board")
    assert ctx.eval("DETAIL_RETURN") is None


@pytest.mark.parametrize("collapsed", [False, True])
@pytest.mark.parametrize("hidden_role", [False, True])
def test_drawer_close_falls_back_to_current_tab_control(plugin, collapsed, hidden_role):
    ctx = _view_ctx(plugin)
    ctx.eval("""
      DETAIL_RETURN = __mkEl('removed');
      DETAIL_RETURN.setAttribute('data-id', 'gone');
      DETAIL_RETURN.isConnected = false;
      var hidden = __mkEl('hidden');
      hidden.setAttribute('data-id', 'gone');
      hidden.isConnected = true; hidden.offsetParent = null;
      var control = __mkEl('tab-control');
      control.focus = function () { document.activeElement = this; };
      var get = document.getElementById;
    """)
    ctx.eval("var collapsed = %s, hiddenRole = %s;" % (json.dumps(collapsed), json.dumps(hidden_role)))
    ctx.eval("""
      document.getElementById('body').querySelectorAll = function () { return hiddenRole ? [hidden] : []; };
      document.getElementById = function (id) {
        if (id === 'tabs-trigger') return collapsed ? control : null;
        if (id === 'tab-pipeline') return collapsed ? null : control;
        return get(id);
      };
      closeDetail();
    """)
    assert ctx.eval("document.activeElement === control") is True


def test_board_columns_share_the_width_instead_of_the_kits_min_width(plugin):
    """The kit's .pl-board-col sets min-width: 200px, which overlapped the columns under about 1,200px."""
    css = plugin._DASHBOARD_HTML
    assert re.search(r"\.col \{[^}]*min-width: 0;", css)


def test_a_long_target_list_stays_on_one_line_and_keeps_the_full_text(plugin):
    css = plugin._DASHBOARD_HTML
    assert "#sub { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }" in css
    assert "sub.title = roles" in css
