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
    assert out.count("Draft — confirm before using") == 1
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
    assert "No resume built yet." in out
    assert "data-copy-path" not in out


def test_kit_cover_letter_branches(plugin):
    ctx = _pure_ctx(plugin)
    written = _call(ctx, "renderKit", {"cover_letter": {"text": "Dear team,"}}, [])
    assert "Dear team," in written and "data-copy-cover" in written
    not_needed = _call(ctx, "renderKit", {"cover_letter": {"needed": False}}, [])
    assert "Not needed for this posting." in not_needed
    not_written = _call(ctx, "renderKit", {"cover_letter": {"needed": True}}, [])
    assert "Not written yet." in not_written


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
    assert "if (!confirm(" in html  # confirm() gate
    assert "'/applications/' + encodeURIComponent(id) + '/applied'" in html
    # the Mark-applied button is wired to markApplied, and a success refreshes the drawer + board
    assert 'id="detail-applied"' in html or "id='detail-applied'" in html
    assert "applied.addEventListener('click', markApplied)" in html
    assert "return openDetail(id);" in html  # panel refreshes after applied


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
