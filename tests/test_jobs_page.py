"""The jobs view PAGE (S1c): the rebuilt ``_DASHBOARD_HTML`` — inbox list, drag/drop pipeline
board, follow-up nudge, and the collapsed profile disclosure.

Host-free. These assert on the page's own markup/JS source (the page is a self-contained string)
plus one check that the gated routes the page targets actually exist on the registered router, so
the page and the API can't drift apart. The DATA routes themselves are exercised in
``test_pipeline.py`` / ``test_inbox.py``.
"""

from __future__ import annotations

import json
import re

import pytest
import quickjs

from _plugin_testkit import FakeRegistry


def _render_board(plugin, applications, columns):
    """Execute the shipped renderer; DOM stub captures markup without a browser/host."""
    ctx = quickjs.Context()
    ctx.eval("""
        var board = {innerHTML: '', querySelectorAll: function () { return []; }};
        var document = {getElementById: function () { return board; }};
    """)
    html = plugin._DASHBOARD_HTML
    start = html.index("  function esc(s)")
    end = html.index("  function renderProfile(d)", start)
    ctx.eval(html[start:end])
    jobs = {"applications": applications, "columns": columns}
    ctx.eval("JOBS = " + json.dumps(jobs) + "; renderBoard();")
    # Rendering must not silently normalize stored statuses.
    assert json.loads(ctx.eval("JSON.stringify(JOBS)")) == jobs
    return ctx.eval("board.innerHTML")


@pytest.mark.parametrize(
    "status", ["Applied", "interview", "legacy", "", None, "__proto__", "constructor", "<img src=x>"]
)
def test_board_preserves_unknown_status_cards(plugin, state, status):
    apps = [
        {"id": "known", "role": "Known role", "status": "applied"},
        {"id": "unknown", "role": "Legacy role", "status": status},
        {"id": "missing", "role": "Missing status"},
    ]
    html = _render_board(plugin, apps, state.COLUMNS)
    assert html.count('class="pcard"') == 3
    assert 'Applied<span class="count">1</span>' in html
    assert 'Other statuses<span class="count">2</span>' in html
    for app in apps:
        assert html.count('data-id="' + app["id"] + '"') == 1
    fallback = html.split("Other statuses<span", 1)[1]
    assert fallback.count('draggable="true"') == 2
    assert fallback.count('class="movesel"') == 2
    for name in state.COLUMNS:
        assert '<option value="' + name + '">' in fallback
    assert 'value="Other statuses"' not in html
    assert 'data-col="Other statuses"' not in html
    assert "<img src=x>" not in html
    if status == "<img src=x>":
        assert "&lt;img src=x&gt;" in fallback

    # Once moved to a supported status, the same card belongs to that column.
    apps[1]["status"] = "applied"
    moved = _render_board(plugin, apps, state.COLUMNS)
    assert 'Applied<span class="count">2</span>' in moved
    assert 'Other statuses<span class="count">1</span>' in moved


def test_board_keeps_six_columns_when_all_statuses_are_known(plugin, state):
    apps = [{"id": s, "status": s} for statuses in state.COLUMNS.values() for s in statuses]
    html = _render_board(plugin, apps, state.COLUMNS)
    assert html.count('class="col"') == 6
    assert html.count('class="pcard"') == len(apps)
    assert "Other statuses" not in html
    assert 'Closed<span class="count">2</span>' in html
    assert '<span class="pill">rejected</span>' in html
    assert '<span class="pill">passed</span>' in html


def test_board_keeps_applications_visible_without_column_configuration(plugin):
    html = _render_board(plugin, [{"id": "legacy", "status": "interview"}], {})
    assert 'Other statuses<span class="count">1</span>' in html
    assert 'data-id="legacy"' in html
    assert "No pipeline columns." in _render_board(plugin, [], {})


# ── r1: the plugin-kit + fleet-proxy contract, and the gated calls the page makes ─────
def test_page_keeps_plugin_kit_and_fleet_proxy_contract(plugin):
    html = plugin._DASHBOARD_HTML
    # fleet-proxy-safe base derived from the iframe's own path (ADR 0042)
    assert "location.pathname.split('/plugins/')[0]" in html
    assert "/_ds/plugin-kit.css" in html
    assert "import(BASE + '/_ds/plugin-kit.js')" in html
    assert "kit.initPluginView" in html
    # the plain-fetch fallback for a host that can't serve the kit
    assert "apiFetch: function (p, i) { return fetch(BASE + p, i); }" in html
    # only kit-defined --pl-* tokens: no --pl-color-surface anywhere
    assert "--pl-color-surface" not in html


def test_page_routes_every_write_through_kit_apifetch(plugin):
    html = plugin._DASHBOARD_HTML
    # both the read helper and the write helper go through the slug-aware authed apiFetch
    assert "kit.apiFetch('/api/plugins/careercoach' + path)" in html  # reads
    assert "kit.apiFetch('/api/plugins/careercoach' + path, {" in html  # writes (POST)
    assert "method: 'POST'" in html


def test_page_targets_jobs_keep_dismiss_and_move(plugin):
    html = plugin._DASHBOARD_HTML
    assert "api('/jobs')" in html  # the board + inbox data
    assert "api('/state')" in html  # the profile/rubric disclosure
    # keep / dismiss are built as /inbox/<id>/<action>
    assert "'/inbox/' + encodeURIComponent(id) + '/' + action" in html
    assert "inboxAction(id, 'keep'" in html
    assert "inboxAction(id, 'dismiss'" in html
    # move is /applications/<id>/move with a {status} body
    assert "'/applications/' + encodeURIComponent(id) + '/move'" in html


def test_page_target_routes_exist_on_the_gated_router(plugin):
    """The paths the page builds must be real routes on the gated router, so the two can't drift."""
    reg = FakeRegistry({"followup_days": 10}, plugin_id="careercoach")
    plugin.register(reg)
    _prefix, router = next((p, r) for p, r in reg.routers if p == "/api/plugins/careercoach")
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/jobs" in paths
    assert "/state" in paths
    assert any(p.startswith("/inbox/") and p.endswith("/keep") for p in paths)
    assert any(p.startswith("/inbox/") and p.endswith("/dismiss") for p in paths)
    assert any(p.startswith("/applications/") and p.endswith("/move") for p in paths)


# ── r2: draggable cards + dragover/drop AND a non-drag Move-to select per card ─────────
def test_cards_are_draggable_with_dragover_and_drop_handlers(plugin):
    html = plugin._DASHBOARD_HTML
    assert 'draggable="true"' in html  # HTML5 drag source
    assert "addEventListener('dragstart'" in html
    assert "addEventListener('dragover'" in html
    assert "addEventListener('drop'" in html
    assert "ev.preventDefault()" in html  # a column must preventDefault to be a drop target
    assert "moveToColumn(" in html  # a drop resolves to a column move


def test_every_card_has_a_keyboard_touch_move_select(plugin):
    html = plugin._DASHBOARD_HTML
    # the select is built inside the per-card template and wired to the same move path as drop
    assert '<select class="movesel"' in html
    assert "Move to…" in html
    assert '''aria-label="Move ' + esc(a.role) + ' to another column"''' in html
    assert ".movesel').forEach" in html
    assert "addEventListener('change'" in html


def test_dropping_on_closed_asks_rejected_or_passed_default_passed(plugin):
    html = plugin._DASHBOARD_HTML
    assert "isClosedColumn" in html
    assert "askClosedStatus()" in html
    # an inline chooser (sandboxed iframe may block native modals), with passed as the default
    assert "Mark passed (default)" in html
    assert "done('passed')" in html
    assert "done('rejected')" in html


def test_move_is_optimistic_and_rolls_back_with_a_toast(plugin):
    html = plugin._DASHBOARD_HTML
    # optimistic: set the new status + re-render BEFORE the request resolves
    assert "app.status = status;" in html
    # non-2xx (or error) rolls the status back, re-renders, and shows an inline toast
    assert "if (!r.ok) throw new Error" in html
    assert "app.status = prev;" in html
    assert "toast(" in html


# ── r3: live refresh — subscribe to careercoach.# and reload on an event ───────────────
def test_page_subscribes_to_careercoach_events_and_reloads(plugin):
    html = plugin._DASHBOARD_HTML
    assert "protoagent:subscribe" in html
    assert "patterns: ['careercoach.#']" in html
    assert "d.type === 'protoagent:event'" in html
    # a successful write also reloads (the board/inbox come back authoritative)
    assert re.search(r"if \(!r\.ok\) throw new Error\([^)]*\);\s*load\(\);", html)


# ── r4: the follow-up nudge renders ONLY for cards flagged followup ────────────────────
def test_followup_nudge_is_gated_on_the_followup_flag(plugin):
    html = plugin._DASHBOARD_HTML
    assert "follow up?" in html
    assert "No reply in " in html
    # the render branch is gated on the server-provided flag (a ternary on a.followup)
    assert "var nudge = a.followup" in html
    # the nudge markup is emitted in exactly one place — the gated branch — so an unflagged
    # card can never show it (the CSS selector below is `.nudge`, not the `class="nudge"` string)
    assert html.count('class="nudge"') == 1


# ── the inbox list: title, company, score, one-line reason, posting link, keep/dismiss ─
def test_inbox_item_renders_all_fields_and_a_safe_posting_link(plugin):
    html = plugin._DASHBOARD_HTML
    assert "esc(it.title)" in html
    assert "esc(it.company)" in html
    assert "esc(it.reason)" in html
    assert "Number(it.score || 0)" in html
    # the posting link opens in a new tab and is not an opener for the parent
    assert 'target="_blank" rel="noopener"' in html
    assert '''href="' + esc(it.url) + '"''' in html
    assert "data-keep" in html and "data-dismiss" in html
    # an empty state when the watch has surfaced nothing
    assert "Nothing new" in html


def test_all_posting_and_inbox_text_is_escaped(plugin):
    """No posting/inbox field is interpolated as raw HTML — every one goes through esc()."""
    html = plugin._DASHBOARD_HTML
    for frag in (
        "esc(it.title)",
        "esc(it.company)",
        "esc(it.reason)",
        "esc(it.url)",
        "esc(a.role)",
        "esc(a.company)",
        "esc(a.id)",
        "esc(it.id)",
    ):
        assert frag in html, frag


# ── r5 (part): the collapsed profile disclosure still reuses the /state rendering ──────
def test_profile_disclosure_is_collapsed_and_reuses_state_rendering(plugin):
    html = plugin._DASHBOARD_HTML
    assert "What your coach knows about you" in html
    assert "renderProfile" in html
    assert "context_block" in html  # the "exactly what it's told each turn" block
    assert 'id="profile-ctx"' in html
