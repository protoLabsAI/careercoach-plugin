"""The job detail panel + apply-kit UI (S2b): the drawer the jobs view opens when a pipeline card
is clicked, rendered client-side from the S2a gated route ``GET /applications/{id}``.

Host-free, like ``test_jobs_page.py``: the page is a self-contained string, so these assert on its
markup/JS source and execute the shipped *pure* renderers (``renderAnswers`` / ``renderKit`` /
``renderTimeline`` / ``renderContacts`` / ``composeText``) in quickjs — no browser, no host. The
DATA routes the panel targets are exercised in ``test_pipeline.py`` / ``test_apply_kit.py``; here we
only assert they exist on the registered router so the page and the API can't drift apart.
"""

from __future__ import annotations

import json

import quickjs

from _plugin_testkit import FakeRegistry


def _pure_ctx(plugin):
    """A quickjs context with the page's ``esc`` plus the S2b *pure* renderer block loaded — the
    functions that build markup from data and depend on nothing but ``esc``."""
    ctx = quickjs.Context()
    html = plugin._DASHBOARD_HTML
    ctx.eval(html[html.index("  function esc(s)") : html.index("  var JOBS")])
    ctx.eval(html[html.index("  // >>> s2b pure renderers") : html.index("  // <<< s2b pure renderers")])
    return ctx


def _call(ctx, fn, *args):
    return ctx.eval(fn + "(" + ", ".join(json.dumps(a) for a in args) + ")")


# ── r1: the panel fetches GET /applications/{id} on card click and renders every section ─────
def test_card_click_fetches_the_application_through_apifetch(plugin):
    html = plugin._DASHBOARD_HTML
    # a plain card click opens the drawer...
    assert "openDetail(card.getAttribute('data-id'))" in html
    assert "addEventListener('click'" in html
    # ...which fetches the S2a route through the slug-aware authed apiFetch (via the `api` helper)
    assert "api('/applications/' + encodeURIComponent(id))" in html
    assert "kit.apiFetch('/api/plugins/careercoach' + path)" in html  # `api` goes through apiFetch


def test_board_card_click_opens_drawer_only_when_not_picked_up(plugin):
    """A board card opens the detail drawer on a plain click, but not while it is picked up for a
    keyboard move (LIFT_ID set) — so Space-to-drop can't also fire a drawer-opening click (R2 §3)."""
    html = plugin._DASHBOARD_HTML
    assert "if (!LIFT_ID) openDetail(card.getAttribute('data-id'))" in html


def test_detail_renders_all_sections(plugin):
    html = plugin._DASHBOARD_HTML
    for marker in (
        "detail-posting",
        "detail-fit",
        "detail-notes",
        "detail-timeline",
        "detail-contacts",
        "detail-kit",
        "detail-actions",
    ):
        assert marker in html, marker


def test_posting_section_has_a_safe_external_link_and_snippet(plugin):
    html = plugin._DASHBOARD_HTML
    assert 'target="_blank" rel="noopener"' in html
    assert "esc(posting.snippet)" in html
    assert "esc(url)" in html  # the posting link href is escaped


def test_timeline_is_date_ordered_list_from_the_route_payload(plugin):
    ctx = _pure_ctx(plugin)
    timeline = [
        {"date": "2026-01-02", "kind": "status", "status": "applied"},
        {"date": "2026-01-01", "kind": "kit", "what": "prepared resume"},
    ]
    out = _call(ctx, "renderTimeline", timeline)
    assert "2026-01-02" in out and "Status → applied" in out
    assert "2026-01-01" in out and "prepared resume" in out
    assert _call(ctx, "renderTimeline", []) == '<p class="empty">No activity yet.</p>'


def test_contacts_render_when_present_and_the_section_is_hidden_when_empty(plugin):
    html = plugin._DASHBOARD_HTML
    # the whole contacts section is only emitted when the list is non-empty
    assert "if (contacts.length)" in html
    ctx = _pure_ctx(plugin)
    out = _call(ctx, "renderContacts", [{"name": "Ada Lovelace", "title": "Eng Manager", "url": "https://x/ada"}])
    assert out.startswith('<ul class="contacts">')  # rendered output, not source
    assert "Ada Lovelace" in out and "Eng Manager" in out
    assert 'target="_blank" rel="noopener"' in out


# ── r2: every answer row has its own copy control; drafts get a distinct badge ───────────────
def test_answer_rows_copy_control_and_draft_badge(plugin):
    ctx = _pure_ctx(plugin)
    answers = [
        {"key": "Why do you want to work here?", "value": "Because of the mission.", "status": "draft"},
        {"key": "email", "value": "me@example.com", "status": "confirmed"},
    ]
    out = _call(ctx, "renderAnswers", answers)
    # one copy control per answer
    assert out.count("Copy</button>") == 2
    # the draft carries the visible badge and distinct styling; the confirmed one does not
    assert out.count("Draft: confirm before using") == 1
    assert 'class="answer draft"' in out
    assert out.count('class="answer"') == 1  # the confirmed row is plain
    # labels + values both present
    assert "Why do you want to work here?" in out
    assert "me@example.com" in out


def test_answer_renderer_handles_empty_and_escapes_values(plugin):
    ctx = _pure_ctx(plugin)
    assert _call(ctx, "renderAnswers", []) == '<p class="empty">No saved answers yet.</p>'
    out = _call(ctx, "renderAnswers", [{"key": "<b>k</b>", "value": "<script>x</script>", "status": "draft"}])
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<b>k</b>" not in out and "&lt;b&gt;k&lt;/b&gt;" in out


def test_kit_answers_combine_one_off_and_standard_answers(plugin):
    ctx = _pure_ctx(plugin)
    kit = {"answers": [{"key": "Why us?", "value": "mission", "status": "draft"}]}
    standard = [{"key": "email", "value": "me@x.com", "status": "confirmed"}]
    out = _call(ctx, "renderKit", kit, standard)
    assert "Why us?" in out and "email" in out
    assert out.count("Copy</button>") == 2


# ── the apply kit: resume (copy-path / no-kit), cover letter branches ────────────────────────
def test_kit_resume_shows_version_built_path_with_copy(plugin):
    ctx = _pure_ctx(plugin)
    kit = {"resume": {"version": "v3", "built": "2026-01-01", "path": "/tmp/resume.pdf"}, "cover_letter": {}}
    out = _call(ctx, "renderKit", kit, [])
    assert "v3" in out and "2026-01-01" in out and "/tmp/resume.pdf" in out
    assert "data-copy-path" in out and "Copy path" in out


def test_kit_without_a_resume_says_so(plugin):
    ctx = _pure_ctx(plugin)
    out = _call(ctx, "renderKit", {"resume": {"path": "", "version": "", "built": ""}, "cover_letter": {}}, [])
    assert "Not started" in out
    assert "data-copy-path" not in out


def test_kit_cover_letter_branches(plugin):
    ctx = _pure_ctx(plugin)
    written = _call(ctx, "renderKit", {"cover_letter": {"text": "Dear team,"}}, [])
    assert "Dear team," in written and "data-copy-cover" in written
    not_needed = _call(ctx, "renderKit", {"cover_letter": {"needed": False}}, [])
    assert "Not needed for this posting." in not_needed
    not_written = _call(ctx, "renderKit", {"cover_letter": {"needed": True}}, [])
    assert "Not started" in not_written


# ── r3: the three action buttons post protoagent:chat:compose with a role/company/id template ─
def test_action_buttons_post_chat_compose(plugin):
    html = plugin._DASHBOARD_HTML
    assert "parent.postMessage({ type: 'protoagent:chat:compose', text: text }" in html
    # the three buttons, each wired to composeAction
    assert 'data-act="kit"' in html and 'data-act="tailor"' in html and 'data-act="prep"' in html
    assert "composeAction(b.getAttribute('data-act'))" in html
    # labels
    assert "Build apply kit" in html and "Tailor resume" in html and "Prep me for this interview" in html


def test_compose_text_names_action_role_company_url_and_id(plugin):
    ctx = _pure_ctx(plugin)
    app = {"role": "ML Engineer", "company": "Acme", "id": "abc123", "source": "https://jobs/x"}
    kit = _call(ctx, "composeText", "kit", app)
    assert "Build the apply kit for ML Engineer at Acme" in kit
    assert "https://jobs/x" in kit and "application abc123" in kit
    assert "careercoach_save_apply_kit" in kit

    for kind in ("kit", "tailor", "prep"):
        text = _call(ctx, "composeText", kind, app)
        assert "ML Engineer" in text and "Acme" in text and "abc123" in text
        assert len(text) <= 4000  # stays under the 4 KB compose cap

    assert "Tailor my resume for ML Engineer at Acme" in _call(ctx, "composeText", "tailor", app)
    assert "Prep me for the interview for ML Engineer at Acme" in _call(ctx, "composeText", "prep", app)


def test_compose_text_truncates_to_4kb(plugin):
    ctx = _pure_ctx(plugin)
    app = {"role": "ML Engineer", "company": "Acme", "id": "x", "source": "https://x/" + ("q" * 6000)}
    assert len(_call(ctx, "composeText", "kit", app)) == 4000


def test_compose_text_without_a_url_still_names_the_application(plugin):
    ctx = _pure_ctx(plugin)
    app = {"role": "ML Engineer", "company": "Acme", "id": "abc123"}
    text = _call(ctx, "composeText", "kit", app)
    assert "(application abc123)" in text


# ── r4: "Mark applied" confirms, then POSTs /applications/{id}/applied ───────────────────────
def test_mark_applied_confirms_then_posts_applied(plugin):
    html = plugin._DASHBOARD_HTML
    assert "function markApplied()" in html
    # gated by an INLINE confirm overlay, never window.confirm: a sandboxed iframe without
    # allow-modals silently suppresses native modals (returning false), so Mark applied could
    # never succeed. Mirrors the askClosedStatus chooser.
    assert "function askConfirm(" in html
    assert "askConfirm(" in html
    assert 'id="confirm"' in html and 'id="confirm-ok"' in html and 'id="confirm-cancel"' in html
    assert "window.confirm(" not in html  # no native modal call anywhere on the page
    assert "if (!confirm(" not in html  # the old native gate is gone
    assert "'/applications/' + encodeURIComponent(id) + '/applied'" in html
    # the Mark-applied button is wired to markApplied, and a success refreshes the drawer + board
    assert 'id="detail-applied"' in html or "id='detail-applied'" in html
    assert "applied.addEventListener('click', markApplied)" in html
    assert "return openDetail(id);" in html  # panel refreshes after applied


def test_mark_applied_inline_confirm_sits_above_the_drawer(plugin):
    """The confirm overlay must stack above #detail (z-index 40), or a sandboxed-iframe user would
    see a dimmed-but-unreachable dialog behind the open drawer."""
    html = plugin._DASHBOARD_HTML
    assert "#confirm { position: fixed; inset: 0; z-index: 50;" in html
    assert "#detail { position: fixed; inset: 0; z-index: 40;" in html


def test_open_detail_ignores_a_stale_response(plugin):
    """A late GET for a card the user has closed/clicked past is dropped via the DETAIL_ID guard,
    so it can't reopen or overwrite the drawer."""
    html = plugin._DASHBOARD_HTML
    assert "if (DETAIL_ID !== id) return;" in html


# ── editable notes saved via PATCH ───────────────────────────────────────────────────────────
def test_notes_are_editable_and_saved_via_patch(plugin):
    html = plugin._DASHBOARD_HTML
    assert 'id="detail-notes-text"' in html
    assert "method: 'PATCH'" in html
    assert "/applications/' + encodeURIComponent(app.id) + '/notes'" in html
    assert "saveNotes" in html


# ── copy uses the clipboard API with a textarea+execCommand fallback ─────────────────────────
def test_copy_uses_clipboard_with_execcommand_fallback(plugin):
    html = plugin._DASHBOARD_HTML
    assert "navigator.clipboard.writeText" in html
    assert "document.createElement('textarea')" in html
    assert "document.execCommand('copy')" in html


# ── the drawer is dismissable: Esc, the ✕ and the backdrop ───────────────────────────────────
def test_detail_drawer_is_dismissable(plugin):
    html = plugin._DASHBOARD_HTML
    assert 'id="detail"' in html and 'id="detail-close"' in html
    assert "detail-backdrop" in html
    assert "function closeDetail()" in html
    assert "e.key === 'Escape'" in html
    assert "close.addEventListener('click', closeDetail)" in html


# ── r1/r4/r5: the routes the panel targets are real on the gated router (no drift) ───────────
def test_detail_routes_exist_on_the_gated_router(plugin):
    reg = FakeRegistry({"followup_days": 10}, plugin_id="careercoach")
    plugin.register(reg)
    _prefix, router = next((p, r) for p, r in reg.routers if p == "/api/plugins/careercoach")
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/applications/{app_id}" in paths  # GET detail
    assert any(p.startswith("/applications/") and p.endswith("/applied") for p in paths)
    assert any(p.startswith("/applications/") and p.endswith("/notes") for p in paths)


# ── r5: theme contract + no undefined surface tokens still hold after the additions ──────────
def test_detail_additions_keep_the_theme_contract(plugin):
    html = plugin._DASHBOARD_HTML
    assert "--pl-color-surface" not in html
    # the drawer styling reuses the kit's own tokens
    assert "#detail .detail-panel" in html


# ── epic #85 R1: the carried drawer is restyled with kit classes (no hand-rolled .btn/.pill) ─
def test_carried_drawer_uses_kit_classes(plugin):
    html = plugin._DASHBOARD_HTML
    # the drawer panel declares the kit drawer component; its backdrop uses the overlay token
    assert 'class="detail-panel pl-drawer pl-drawer--right"' in html
    assert "#detail .detail-backdrop { position: absolute; inset: 0; background: var(--pl-color-overlay); }" in html
    # the status chip and the action buttons are kit atoms now, not hand-rolled .pill / .btn
    assert 'class="pl-badge">' in html  # the status chip in the drawer header
    assert 'class="pl-btn act"' in html  # the "Ask your coach" action buttons
    assert 'class="pl-btn pl-btn--primary" type="button" id="detail-applied"' in html  # Mark applied
    assert 'class="btn' not in html and 'class="pill"' not in html


def test_drawer_status_select_lists_destinations_and_preserves_legacy_status(plugin):
    from test_jobs_page import COLUMNS, _inner, _view_ctx

    ctx = _view_ctx(plugin)
    html = plugin._DASHBOARD_HTML
    ctx.eval(html[html.index("  // >>> s2b pure renderers") : html.index("  // /jobs drives")])
    ctx.eval("JOBS.columns = " + json.dumps(COLUMNS) + ";")
    ctx.eval('renderDetail({application: {id: "a", status: "legacy<&"}});')
    header = _inner(ctx, "detail-title")
    assert '<label for="detail-status">Status</label>' in header
    assert 'class="pl-select pl-dropselect" aria-label="Status"' in header
    assert '<option value="legacy&lt;&amp;" selected>legacy&lt;&amp;</option>' in header
    for status in [s for statuses in COLUMNS.values() for s in statuses]:
        assert f'<option value="{status}"' in header


def test_drawer_status_change_uses_move_undo_and_failure_rollback(plugin):
    from test_jobs_page import COLUMNS, _view_ctx

    ctx = _view_ctx(plugin)
    html = plugin._DASHBOARD_HTML
    ctx.eval(html[html.index("  // >>> s2b pure renderers") : html.index("  // /jobs drives")])
    ctx.eval(
        "JOBS = "
        + json.dumps({"columns": COLUMNS, "applications": [{"id": "a", "status": "considering", "role": "Dev"}]})
        + ";"
    )
    ctx.eval("""
      DETAIL_ID = 'a';
      var select = document.getElementById('detail-status');
      select.addEventListener = function (event, fn) { if (event === 'change') this.change = fn; };
      var requests = [], ok = true, feedback;
      write = function (path, body) { requests.push([path, body]); return Promise.resolve({ok: ok, status: 500}); };
      renderBody = function () {};
      load = function () { syncDetailStatus(); };
      announce = function () {};
      toast = function (msg, opts) { feedback = opts; };
      wireDetail();
      select.value = 'interviewing'; select.change();
    """)
    assert ctx.eval('findApp("a").status') == "interviewing"
    while ctx.execute_pending_job():
        pass
    assert json.loads(ctx.eval("JSON.stringify(requests[0])")) == ["/applications/a/move", {"status": "interviewing"}]
    ctx.eval("feedback.undo();")
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("requests[1][0]") == "/applications/a/undo-move"
    ctx.eval("ok = false; select.value = 'offer'; select.change();")
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("select.value") == "interviewing"
    assert ctx.eval('findApp("a").status') == "interviewing"
    assert ctx.eval("feedback.error") is True


def _drawer(plugin, status="considering", **data):
    from test_jobs_page import COLUMNS, _view_ctx

    ctx = _view_ctx(plugin)
    app = {"id": "a", "status": status, "role": "Dev", "company": "Acme", **data.pop("application", {})}
    payload = {"application": app, **data}
    ctx.eval(
        "JOBS.columns = "
        + json.dumps(COLUMNS)
        + "; DETAIL = "
        + json.dumps(payload)
        + "; DETAIL_ID = 'a'; renderDetail(DETAIL);"
    )
    return ctx


def test_drawer_no_kit_order_and_disclosures(plugin):
    from test_jobs_page import _inner

    ctx = _drawer(plugin, answers=[{"key": "email", "value": "a@b", "status": "confirmed"}])
    out = _inner(ctx, "detail-body")
    assert (
        out.index("detail-actions") < out.index("detail-kit") < out.index("detail-notes") < out.index("detail-timeline")
    )
    assert 'id="detail-kit" class="pl-card pl-accordion" open' in out
    assert out.count("Not started") == 3
    assert 'class="pl-steps"' in out and out.count('pl-step"') == 3
    assert "1 saved answers will be reused" in out
    assert "Your coach builds all three in chat. You review each before anything is sent." in out
    assert "I applied already: mark applied" in out
    assert 'id="detail-fit"' not in out
    assert 'id="detail-contacts"' not in out
    assert 'id="detail-timeline" class="pl-accordion"><summary>' in out
    assert "Save notes" not in out
    assert 'role="menu" hidden' in out


def test_drawer_state_specific_next_step_and_fit(plugin):
    from test_jobs_page import _inner

    cases = [
        ("considering", {}, "Build apply kit"),
        ("preparing", {"status": "complete"}, "Mark applied"),
        ("interviewing", {}, "Prep me for this interview"),
    ]
    for status, kit_data, label in cases:
        ctx = _drawer(
            plugin,
            status,
            kit=kit_data,
            application={"fit_score": 82, "notes": "Matches platform experience"},
            contacts=[{"name": "Ada"}],
        )
        out = _inner(ctx, "detail-body")
        next_step = out.split("</section>", 1)[0].split('id="detail-more"', 1)[0]
        assert label in next_step
        assert out.index("detail-kit") < out.index("detail-fit") < out.index("detail-notes")
        assert "Strong fit" in out and "Matches platform experience" in out
        assert 'id="detail-contacts" class="pl-accordion"><summary>' in out
        if status == "interviewing":
            assert 'id="detail-kit" class="pl-card pl-accordion"><summary>' in out


def test_closed_drawer_read_only_reopen_uses_saved_route_status(plugin):
    from test_jobs_page import _inner

    ctx = _drawer(plugin, "rejected")
    out = _inner(ctx, "detail-body")
    assert 'id="detail-reopen">Reopen' in out
    assert 'aria-label="Status" disabled' in _inner(ctx, "detail-title")
    assert 'class="pl-textarea notes-ta" readonly' in out
    assert "data-act=" not in out and "data-applied" not in out
    ctx.eval(
        "var moved; moveApp = function (id, status) { moved = [id, status]; }; document.getElementById('detail-reopen').onclick();"
    )
    assert json.loads(ctx.eval("JSON.stringify(moved)")) == ["a", "considering"]


def test_notes_autosave_serializes_writes_and_reports_failure(plugin):
    ctx = _drawer(plugin)
    ctx.eval("""
      var requests = [], completions = [];
      var kit = {apiFetch: function (path, options) {
        requests.push([path, JSON.parse(options.body)]);
        return new Promise(function (resolve) { completions.push(resolve); });
      }};
      toast = function () {};
      var ta = document.getElementById('detail-notes-text');
      ta.value = 'first'; saveNotes();
      ta.value = 'second'; saveNotes();
    """)
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("requests.length") == 1
    ctx.eval("completions[0]({ok: true});")
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("requests.length") == 2
    assert ctx.eval("document.getElementById('detail-notes-status').textContent") != "Saved"
    ctx.eval("completions[1]({ok: false, status: 500});")
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("NOTE_DRAFTS.a") == "second"
    assert "Could not save" in ctx.eval("document.getElementById('detail-notes-status').textContent")
    ctx.eval("saveNotes();")
    while ctx.execute_pending_job():
        pass
    ctx.eval("completions[2]({ok: true});")
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("DETAIL.application.notes") == "second"
    assert ctx.eval("document.getElementById('detail-notes-status').textContent") == "Saved"
    assert ctx.eval("Object.prototype.hasOwnProperty.call(NOTE_DRAFTS, 'a')") is False
    assert json.loads(ctx.eval("JSON.stringify(requests[0])")) == [
        "/api/plugins/careercoach/applications/a/notes",
        {"notes": "first"},
    ]
    ctx.eval("DETAIL.application.status = 'passed'; ta.value = 'forbidden'; saveNotes();")
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("requests.length") == 3


def test_focus_trap_skips_collapsed_and_hidden_controls(plugin):
    from test_jobs_page import _view_ctx

    ctx = _view_ctx(plugin)
    ctx.eval("""
      var nodes = ['first', 'collapsed', 'last', 'menu'].map(function (id) {
        return {id: id, getClientRects: function () { return id === 'collapsed' ? [] : [1]; },
          closest: function () { return id === 'menu' ? {} : null; },
          focus: function () { document.activeElement = this; }};
      });
      var root = {contains: function () { return true; }, querySelectorAll: function () { return nodes; }};
      document.activeElement = nodes[2];
      trap({key: 'Tab', preventDefault: function () {}}, root);
    """)
    assert ctx.eval("document.activeElement.id") == "first"
    ctx.eval("trap({key: 'Tab', shiftKey: true, preventDefault: function () {}}, root);")
    assert ctx.eval("document.activeElement.id") == "last"


def test_notes_reverting_to_original_while_save_pending_is_not_lost(plugin):
    ctx = _drawer(plugin, application={"notes": "original"})
    ctx.eval("""
      var writes = [], resolveFirst;
      var kit = {apiFetch: function (path, opts) {
        writes.push(JSON.parse(opts.body).notes);
        return writes.length === 1 ? new Promise(function (resolve) { resolveFirst = resolve; }) : Promise.resolve({ok: true});
      }};
      var ta = document.getElementById('detail-notes-text');
      ta.value = 'change'; saveNotes();
      ta.value = 'original'; saveNotes();
    """)
    while ctx.execute_pending_job():
        pass
    ctx.eval("resolveFirst({ok: true});")
    while ctx.execute_pending_job():
        pass
    assert json.loads(ctx.eval("JSON.stringify(writes)")) == ["change", "original"]
    assert ctx.eval("DETAIL.application.notes") == "original"


def test_same_id_late_fetch_cannot_replace_newer_drawer(plugin):
    from test_jobs_page import _view_ctx

    ctx = _view_ctx(plugin)
    ctx.eval("""
      var resolves = [], rendered = [];
      api = function () { return new Promise(function (resolve) { resolves.push(resolve); }); };
      renderDetail = function (d) { rendered.push(d.application.role); };
      showDetail = function () {};
      openDetail('a'); openDetail('a');
      resolves[1]({application: {id: 'a', role: 'New'}});
    """)
    while ctx.execute_pending_job():
        pass
    ctx.eval("resolves[0]({application: {id: 'a', role: 'Stale'}});")
    while ctx.execute_pending_job():
        pass
    assert json.loads(ctx.eval("JSON.stringify(rendered)")) == ["New"]
    ctx.eval("openDetail('a'); closeDetail(); resolves[2]({application: {id: 'a', role: 'Closed'}});")
    while ctx.execute_pending_job():
        pass
    assert json.loads(ctx.eval("JSON.stringify(rendered)")) == ["New"]


def test_confirm_escape_cancels_and_returns_focus_without_closing_drawer(plugin):
    from test_jobs_page import _view_ctx

    ctx = _view_ctx(plugin)
    ctx.eval("""
      var opener = {isConnected: true, focus: function () { document.activeElement = this; }};
      document.activeElement = opener;
      var result;
      askConfirm('Mark applied?').then(function (ok) { result = ok; });
      document.getElementById('confirm').onkeydown({key: 'Escape', preventDefault: function () {}, stopPropagation: function () {}});
    """)
    while ctx.execute_pending_job():
        pass
    assert ctx.eval("result") is False
    assert ctx.eval("document.activeElement === opener") is True
    assert ctx.eval("document.getElementById('confirm').style.display") == "none"


def test_close_restores_replaced_opener_after_live_refresh(plugin):
    from test_jobs_page import _view_ctx

    ctx = _view_ctx(plugin)
    ctx.eval("""
      DETAIL_RETURN = {isConnected: false, getAttribute: function () { return 'a'; }};
      var replacement = {isConnected: true, getAttribute: function () { return 'a'; }, focus: function () { document.activeElement = this; }};
      document.getElementById('body').querySelectorAll = function () { return [replacement]; };
      closeDetail();
    """)
    assert ctx.eval("document.activeElement === replacement") is True
