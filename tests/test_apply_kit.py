"""Apply-kit DATA + the save-apply-kit tool + the gated detail routes (S2a).

The apply kit lives on the application row (same store, lock and atomic write as the pipeline), so
the record of what the coach prepared — and, on mark_applied, what was sent — stays with the
application. Host-free: the state helpers are exercised directly, the tool through the registered
surface, and the routes through a FastAPI TestClient fed the plugin's own gated router."""

from __future__ import annotations

import importlib

import pytest

from _plugin_testkit import FakeRegistry


@pytest.fixture
def st(plugin, iso, monkeypatch):
    """The ``state`` module with an isolated store."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".state")


@pytest.fixture
def tools(plugin, iso, monkeypatch):
    """The registered agent tools by name, sharing the ``st`` store (same CAREERCOACH_DIR)."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    reg = FakeRegistry({}, plugin_id="careercoach")
    plugin.register(reg)
    return {t.name: t for t in reg.tools}


@pytest.fixture
def client(plugin, iso, monkeypatch):
    """A TestClient over the GATED data router, plus the FakeRegistry (assert `emitted`)."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    reg = FakeRegistry({"followup_days": 10}, plugin_id="careercoach")
    plugin.register(reg)
    prefix, router = next((p, r) for p, r in reg.routers if p == "/api/plugins/careercoach")
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return TestClient(app), reg


@pytest.fixture
def resume_pdf(tmp_path):
    """A real file on disk, so a kit's résumé passes the ``Path(path).is_file()`` completeness check."""
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    return str(p)


# ── set_kit ─────────────────────────────────────────────────────────────────────
def test_set_kit_merges_parts_and_appends_a_dated_event(st):
    row = st.track_application(company="Acme", role="ML Engineer", status="considering")
    st.set_kit(row["id"], resume={"path": "/tmp/r.pdf", "version": "v1", "built": st._today()})
    # a partial dict part merges — the path it didn't touch survives
    st.set_kit(row["id"], resume={"version": "v2"}, cover_letter={"text": "Dear team", "needed": True})

    kit = st.load_applications()[0]["kit"]
    assert kit["resume"] == {"path": "/tmp/r.pdf", "version": "v2", "built": st._today()}
    assert kit["cover_letter"] == {"text": "Dear team", "needed": True}
    assert kit["updated"] == st._today()
    assert [e["date"] for e in kit["events"]] == [st._today(), st._today()]
    assert len(kit["events"]) == 2  # one event appended per set_kit call


def test_set_kit_unknown_id_raises_keyerror(st):
    st.track_application(company="Acme", role="ML Engineer")
    with pytest.raises(KeyError):
        st.set_kit("does-not-exist", resume={"version": "v1"})


def test_set_kit_refuses_an_unreadable_store(st):
    app_id = st.track_application(company="Acme", role="ML Engineer")["id"]
    st._path().write_text("{not json", encoding="utf-8")
    with pytest.raises(st.StoreUnreadable):
        st.set_kit(app_id, resume={"version": "v1"})


# ── mark_applied ──────────────────────────────────────────────────────────────────
def test_mark_applied_snapshots_kit_answers_preserving_status(st):
    row = st.track_application(company="Acme", role="ML Engineer", status="preparing")
    st.set_kit(
        row["id"],
        resume={"path": "/tmp/r.pdf", "version": "v3", "built": st._today()},
        answers=[{"key": "Why us?", "label": "Why us?", "value": "Mission", "status": "draft"}],
    )
    applied = st.mark_applied(row["id"])

    assert applied["status"] == "applied"
    assert applied["history"][-1] == {"status": "applied", "date": st._today()}
    assert applied["applied_with"] == {
        "date": st._today(),
        "resume_version": "v3",
        "answers": [{"key": "Why us?", "value": "Mission", "status": "draft"}],  # draft stays draft
    }


def test_mark_applied_falls_back_to_saved_answers_with_their_status(plugin, st):
    ans = importlib.import_module(plugin.__name__ + ".answers")
    ans.propose("phone_number", "555-1234")
    ans.confirm(["phone_number"])  # confirmed
    ans.propose("linkedin_url", "https://linkedin.com/in/x")  # left a draft

    row = st.track_application(company="Acme", role="ML Engineer", status="preparing")
    snap = {a["key"]: a for a in st.mark_applied(row["id"])["applied_with"]["answers"]}
    assert snap["phone_number"]["status"] == "confirmed"
    assert snap["linkedin_url"]["status"] == "draft"


def test_mark_applied_answers_used_overrides_the_source(st):
    row = st.track_application(company="Acme", role="ML Engineer", status="preparing")
    st.set_kit(row["id"], answers=[{"key": "ignored", "label": "ignored", "value": "x", "status": "draft"}])
    used = [{"key": "q1", "value": "a1", "status": "confirmed"}]
    assert st.mark_applied(row["id"], answers_used=used)["applied_with"]["answers"] == used


def test_mark_applied_unknown_id_raises_keyerror(st):
    with pytest.raises(KeyError):
        st.mark_applied("does-not-exist")


def test_mark_applied_refuses_an_unreadable_store(st):
    app_id = st.track_application(company="Acme", role="ML Engineer")["id"]
    st._path().write_text("{not json", encoding="utf-8")
    with pytest.raises(st.StoreUnreadable):
        st.mark_applied(app_id)


# ── set_notes ─────────────────────────────────────────────────────────────────────
def test_set_notes_persists_without_a_history_entry(st):
    row = st.track_application(company="Acme", role="ML Engineer", status="considering")
    st.set_notes(row["id"], "ping the recruiter")
    saved = st.load_applications()[0]
    assert saved["notes"] == "ping the recruiter"
    assert [h["status"] for h in saved["history"]] == ["considering"]  # notes never move the row
    with pytest.raises(KeyError):
        st.set_notes("does-not-exist", "x")


# ── the careercoach_save_apply_kit tool ─────────────────────────────────────────────
def test_save_apply_kit_stores_everything_and_moves_saved_to_preparing(tools, st, resume_pdf):
    st.track_application(company="Acme", role="ML Engineer", status="considering")
    out = tools["careercoach_save_apply_kit"].invoke(
        {
            "company": "Acme",
            "role": "ML Engineer",
            "resume_path": resume_pdf,
            "resume_version": "v2",
            "cover_letter": "Dear team",
            "cover_letter_needed": True,
            "answers_json": '{"Why Acme?": "Mission"}',
        }
    )
    assert "Saved the apply kit" in out and "Acme" in out and "preparing" in out

    r = st.load_applications()[0]
    assert r["status"] == "preparing"
    assert [h["status"] for h in r["history"]] == ["considering", "preparing"]  # moved with a history entry
    assert r["kit"]["resume"] == {"path": resume_pdf, "version": "v2", "built": st._today()}
    assert r["kit"]["cover_letter"] == {"text": "Dear team", "needed": True}
    # a one-off answer is stored as a DRAFT, keyed and labelled by the question
    assert r["kit"]["answers"] == [{"key": "Why Acme?", "label": "Why Acme?", "value": "Mission", "status": "draft"}]
    assert r["kit"]["status"] == "complete" and r["kit"]["missing"] == []


@pytest.mark.parametrize(
    "update, resume_changes, letter_changes",
    [
        ({"resume_version": " v3 "}, {"version": "v3"}, {}),
        ({"resume_path": " /tmp/new.pdf "}, {"path": "/tmp/new.pdf"}, {}),
        ({"cover_letter": "Updated letter"}, {}, {"text": "Updated letter"}),
        ({"cover_letter_needed": True}, {}, {"needed": True}),
        ({"cover_letter_needed": False}, {}, {"needed": False}),
        ({"resume_path": ""}, {"path": ""}, {}),
        ({"resume_version": ""}, {"version": ""}, {}),
        ({"cover_letter": ""}, {}, {"text": ""}),
        ({}, {}, {}),
        (
            {"resume_path": None, "resume_version": None, "cover_letter": None, "cover_letter_needed": None},
            {},
            {},
        ),
    ],
)
def test_save_apply_kit_partial_updates_preserve_omitted_fields(tools, st, update, resume_changes, letter_changes):
    save = tools["careercoach_save_apply_kit"]
    identity = {"company": "Acme", "role": "ML Engineer"}
    save.invoke(
        {
            **identity,
            "resume_path": "/tmp/acme.pdf",
            "resume_version": "v2",
            "cover_letter": "Dear team",
            "cover_letter_needed": True,
            "answers_json": '{"Why Acme?": "Mission"}',
        }
    )
    before = st.load_applications()[0]

    out = save.invoke({**identity, **update})

    # (the reply's saved/DRAFT wording depends on kit completeness — asserted in the draft/complete
    # tests below; this test owns the field-merge behaviour, which is unchanged by validation)
    assert out
    after = st.load_applications()[0]
    assert after["kit"]["resume"] == {**before["kit"]["resume"], **resume_changes}
    assert after["kit"]["cover_letter"] == {**before["kit"]["cover_letter"], **letter_changes}
    assert after["kit"]["answers"] == before["kit"]["answers"]
    assert after["history"] == before["history"]
    assert len(after["kit"]["events"]) == len(before["kit"]["events"]) + 1
    assert after["kit"]["status"] in ("complete", "draft") and isinstance(after["kit"]["missing"], list)


def test_save_apply_kit_tracks_an_absent_role_as_preparing(tools, st):
    out = tools["careercoach_save_apply_kit"].invoke(
        {"company": "Globex", "role": "Staff Engineer", "resume_path": "/tmp/g.pdf", "resume_version": "v1"}
    )
    assert "Globex" in out
    rows = st.load_applications()
    assert len(rows) == 1
    assert rows[0]["status"] == "preparing"
    assert rows[0]["kit"]["resume"]["path"] == "/tmp/g.pdf"


def test_save_apply_kit_leaves_a_non_saved_row_in_place(tools, st):
    st.track_application(company="Acme", role="ML Engineer", status="interviewing")
    tools["careercoach_save_apply_kit"].invoke({"company": "Acme", "role": "ML Engineer", "resume_version": "v9"})
    r = st.load_applications()[0]
    assert r["status"] == "interviewing"  # only a Saved (considering) row is moved to preparing
    assert r["kit"]["resume"]["version"] == "v9"


def test_save_apply_kit_rejects_bad_answers_json_and_writes_nothing(tools, st):
    st.track_application(company="Acme", role="ML Engineer", status="considering")
    out = tools["careercoach_save_apply_kit"].invoke(
        {"company": "Acme", "role": "ML Engineer", "answers_json": "{not json"}
    )
    assert "isn't valid JSON" in out
    r = st.load_applications()[0]
    assert r["status"] == "considering" and "kit" not in r  # nothing moved, nothing stored


# ── validation: a partial/empty kit saves as a DRAFT with `missing` (#66) ───────────────
@pytest.mark.parametrize(
    "use_resume, extra, expected_missing",
    [
        (False, {}, ["résumé (no path)", "cover letter", "answers"]),
        (
            False,
            {"cover_letter": "Dear team", "cover_letter_needed": True, "answers_json": '{"Q": "A"}'},
            ["résumé (no path)"],
        ),
        (True, {"answers_json": '{"Q": "A"}'}, ["cover letter"]),  # cover letter, needed unset
        (True, {"cover_letter_needed": True, "answers_json": '{"Q": "A"}'}, ["cover letter"]),  # needed true, no text
        (True, {"cover_letter": "Dear team", "cover_letter_needed": True}, ["answers"]),  # no answers, no flag
    ],
    ids=["empty", "no-resume", "no-cover-needed-unset", "no-cover-needed-true", "no-answers"],
)
def test_save_apply_kit_partial_kits_save_as_draft(tools, st, resume_pdf, use_resume, extra, expected_missing):
    args = {"company": "Acme", "role": "ML Engineer", **extra}
    if use_resume:
        args["resume_path"], args["resume_version"] = resume_pdf, "v1"
    out = tools["careercoach_save_apply_kit"].invoke(args)

    assert out.startswith("Apply kit saved as a DRAFT — not ready:")
    assert "Saved the apply kit" not in out
    kit = st.load_applications()[0]["kit"]
    assert kit["status"] == "draft"
    assert kit["missing"] == expected_missing
    assert ("careercoach_render_resume" in out) == ("résumé (no path)" in expected_missing)
    assert ("cover_letter_needed=false" in out) == ("cover letter" in expected_missing)
    assert ("careercoach_answers" in out) == ("answers" in expected_missing)
    assert ("no_form_questions=true" in out) == ("answers" in expected_missing)
    if "résumé (no path)" in expected_missing:
        assert "pass its PDF path" in out


def test_save_apply_kit_resume_path_to_a_missing_file_is_a_draft(tools, st, tmp_path):
    missing = tmp_path / "nope.pdf"  # a path that does not exist
    out = tools["careercoach_save_apply_kit"].invoke(
        {
            "company": "Acme",
            "role": "ML Engineer",
            "resume_path": str(missing),
            "cover_letter": "Dear team",
            "cover_letter_needed": True,
            "answers_json": '{"Q": "A"}',
        }
    )
    assert out.startswith("Apply kit saved as a DRAFT — not ready:")
    assert "Saved the apply kit" not in out
    kit = st.load_applications()[0]["kit"]
    assert kit["status"] == "draft"
    assert kit["missing"] == [f"résumé (file not found: {missing})"]
    assert str(missing) in out
    assert "no file exists" in out
    assert "re-render with careercoach_render_resume" in out
    assert "pass the real PDF path" in out
    assert "cover_letter_needed" not in out
    assert "careercoach_answers" not in out


@pytest.mark.parametrize("letter", [None, "", " \n\t"])
def test_cover_letter_needed_false_removes_the_cover_letter_from_missing(tools, st, resume_pdf, letter):
    out = tools["careercoach_save_apply_kit"].invoke(
        {
            "company": "Acme",
            "role": "ML Engineer",
            "resume_path": resume_pdf,
            "resume_version": "v1",
            "cover_letter": letter,
            "cover_letter_needed": False,  # posting asks for no letter
            "answers_json": '{"Q": "A"}',
        }
    )
    assert "Saved the apply kit" in out
    assert "no cover letter (not requested)" in out
    assert "(needed)" not in out
    kit = st.load_applications()[0]["kit"]
    assert kit["status"] == "complete" and kit["missing"] == []


def test_no_form_questions_true_removes_answers_from_missing(tools, st, resume_pdf):
    out = tools["careercoach_save_apply_kit"].invoke(
        {
            "company": "Acme",
            "role": "ML Engineer",
            "resume_path": resume_pdf,
            "resume_version": "v1",
            "cover_letter": "Dear team",
            "cover_letter_needed": True,
            "no_form_questions": True,  # posting has no form questions
        }
    )
    assert "Saved the apply kit" in out
    assert "no form questions" in out
    kit = st.load_applications()[0]["kit"]
    assert kit["status"] == "complete" and kit["missing"] == []
    assert kit["no_form_questions"] is True


def test_partial_update_on_a_complete_kit_stays_complete(tools, st, resume_pdf):
    save = tools["careercoach_save_apply_kit"]
    ident = {"company": "Acme", "role": "ML Engineer"}
    save.invoke(
        {
            **ident,
            "resume_path": resume_pdf,
            "resume_version": "v1",
            "cover_letter": "Dear team",
            "cover_letter_needed": True,
            "no_form_questions": True,
            "answers_json": '{"Q": "A"}',
        }
    )
    assert st.load_applications()[0]["kit"]["status"] == "complete"

    out = save.invoke({**ident, "resume_version": "v2"})  # a later partial update — bump the version only
    assert "Saved the apply kit" in out
    kit = st.load_applications()[0]["kit"]
    assert kit["status"] == "complete" and kit["missing"] == []
    assert kit["resume"] == {"path": resume_pdf, "version": "v2", "built": st._today()}
    assert "résumé v2, cover letter, 1 draft answer(s)" in out
    assert kit["no_form_questions"] is True  # preserved across the omitting update


@pytest.mark.parametrize("letter", ["", "Dear team"])
def test_complete_reply_preserves_saved_parts_when_only_needed_changes(tools, resume_pdf, letter):
    save = tools["careercoach_save_apply_kit"]
    ident = {"company": "Acme", "role": "ML Engineer"}
    save.invoke(
        {
            **ident,
            "resume_path": resume_pdf,
            "resume_version": "v1",
            "cover_letter": letter,
            "cover_letter_needed": False,
            "no_form_questions": True,
        }
    )

    out = save.invoke({**ident, "cover_letter_needed": False})

    cover = "cover letter" if letter else "no cover letter (not requested)"
    assert f"résumé v1, {cover}, no form questions" in out
    assert "Saved the apply kit" in out
    assert "(needed)" not in out


def test_save_apply_kit_accepts_answers_json_as_a_dict(tools, st, resume_pdf):
    out = tools["careercoach_save_apply_kit"].invoke(
        {
            "company": "Acme",
            "role": "ML Engineer",
            "resume_path": resume_pdf,
            "resume_version": "v1",
            "cover_letter": "Dear team",
            "cover_letter_needed": True,
            "answers_json": {"Why Acme?": "Mission"},  # a dict/object, not a JSON string (the S5c shape)
        }
    )
    assert "Saved the apply kit" in out
    kit = st.load_applications()[0]["kit"]
    assert kit["answers"] == [{"key": "Why Acme?", "label": "Why Acme?", "value": "Mission", "status": "draft"}]
    assert kit["status"] == "complete"


def test_save_apply_kit_accepts_the_answers_alias(tools, st):
    tools["careercoach_save_apply_kit"].invoke(
        {"company": "Acme", "role": "ML Engineer", "answers": {"Why Acme?": "Mission"}}
    )
    kit = st.load_applications()[0]["kit"]  # a draft (no résumé), but the aliased answers are stored
    assert kit["answers"] == [{"key": "Why Acme?", "label": "Why Acme?", "value": "Mission", "status": "draft"}]


# ── GET /applications/{id} ──────────────────────────────────────────────────────────
def test_application_detail_shape_and_404(plugin, client, monkeypatch):
    tc, _reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    monkeypatch.setattr(st, "_today", lambda: "2026-10-01")
    app_id = st.track_application(company="Acme", role="ML Engineer", status="preparing")["id"]
    monkeypatch.setattr(st, "_today", lambda: "2026-10-05")
    st.set_kit(app_id, resume={"path": "/tmp/r.pdf", "version": "v1", "built": "2026-10-05"})

    body = tc.get(f"/api/plugins/careercoach/applications/{app_id}").json()
    assert set(body) == {"application", "kit", "answers", "contacts", "timeline"}
    assert body["application"]["id"] == app_id
    assert body["kit"]["resume"]["version"] == "v1"
    assert isinstance(body["answers"], list)
    assert body["contacts"] == []  # no contacts store yet
    # timeline merges history (kind=status) + kit events (kind=kit), date-sorted ascending
    assert {e["kind"] for e in body["timeline"]} == {"status", "kit"}
    dates = [e["date"] for e in body["timeline"]]
    assert dates == sorted(dates) == ["2026-10-01", "2026-10-05"]

    assert tc.get("/api/plugins/careercoach/applications/nope").status_code == 404


def test_application_detail_kit_defaults_to_empty_skeleton(plugin, client):
    tc, _reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    app_id = st.track_application(company="Acme", role="ML Engineer")["id"]
    body = tc.get(f"/api/plugins/careercoach/applications/{app_id}").json()
    assert body["kit"] == st._empty_kit()  # a row without a kit reads as the empty shape


def test_application_detail_round_trips_a_complete_kit(plugin, client, resume_pdf):
    tc, reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    save = {t.name: t for t in reg.tools}["careercoach_save_apply_kit"]
    save.invoke(
        {
            "company": "Acme",
            "role": "ML Engineer",
            "resume_path": resume_pdf,
            "resume_version": "v2",
            "cover_letter": "Dear team",
            "cover_letter_needed": True,
            "answers_json": {"Why Acme?": "Mission"},
        }
    )
    app_id = st.load_applications()[0]["id"]

    kit = tc.get(f"/api/plugins/careercoach/applications/{app_id}").json()["kit"]
    assert kit["status"] == "complete" and kit["missing"] == []
    assert kit["resume"] == {"path": resume_pdf, "version": "v2", "built": st._today()}
    assert kit["cover_letter"] == {"text": "Dear team", "needed": True}
    assert kit["answers"] == [{"key": "Why Acme?", "label": "Why Acme?", "value": "Mission", "status": "draft"}]


def test_application_detail_answers_preserve_draft_confirmed(plugin, client):
    tc, _reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    ans = importlib.import_module(plugin.__name__ + ".answers")
    app_id = st.track_application(company="Acme", role="ML Engineer")["id"]
    ans.propose("phone_number", "555")
    ans.confirm(["phone_number"])
    ans.propose("linkedin_url", "https://linkedin.com/in/x")

    by_key = {a["key"]: a for a in tc.get(f"/api/plugins/careercoach/applications/{app_id}").json()["answers"]}
    assert by_key["phone_number"]["status"] == "confirmed"
    assert by_key["linkedin_url"]["status"] == "draft"


def test_application_detail_returns_contacts_at_company(plugin, client):
    tc, _reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    contacts = importlib.import_module(plugin.__name__ + ".contacts")
    app_id = st.track_application(company="Acme", role="ML Engineer")["id"]
    contacts.merge_contacts([{"name": "Dana", "company": "Acme", "url": "https://linkedin.com/in/dana"}])
    body = tc.get(f"/api/plugins/careercoach/applications/{app_id}").json()
    assert [c["name"] for c in body["contacts"]] == ["Dana"]


def test_application_detail_tolerates_an_unreadable_contacts_store(plugin, client):
    tc, _reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    contacts = importlib.import_module(plugin.__name__ + ".contacts")
    app_id = st.track_application(company="Acme", role="ML Engineer")["id"]
    contacts._path().write_text("{not json", encoding="utf-8")
    assert tc.get(f"/api/plugins/careercoach/applications/{app_id}").json()["contacts"] == []


# ── POST /applications/{id}/applied ─────────────────────────────────────────────────
def test_applied_route_snapshots_and_emits(plugin, client):
    tc, reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    app_id = st.track_application(company="Acme", role="ML Engineer", status="preparing")["id"]
    st.set_kit(
        app_id,
        resume={"path": "/tmp/r.pdf", "version": "v5", "built": st._today()},
        answers=[{"key": "Why?", "label": "Why?", "value": "Mission", "status": "draft"}],
    )
    r = tc.post(f"/api/plugins/careercoach/applications/{app_id}/applied")
    assert r.status_code == 200
    row = r.json()["application"]
    assert row["status"] == "applied"
    assert row["history"][-1]["status"] == "applied"
    assert row["applied_with"]["resume_version"] == "v5"
    assert row["applied_with"]["answers"] == [{"key": "Why?", "value": "Mission", "status": "draft"}]
    assert "careercoach.application_tracked" in [t for t, _ in reg.emitted]

    assert tc.post("/api/plugins/careercoach/applications/nope/applied").status_code == 404


# ── PATCH /applications/{id}/notes ──────────────────────────────────────────────────
def test_notes_route_persists_and_404(plugin, client):
    tc, reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    app_id = st.track_application(company="Acme", role="ML Engineer")["id"]
    r = tc.patch(f"/api/plugins/careercoach/applications/{app_id}/notes", json={"notes": "ping recruiter"})
    assert r.status_code == 200
    assert st.load_applications()[0]["notes"] == "ping recruiter"
    assert "careercoach.application_tracked" in [t for t, _ in reg.emitted]

    bad = tc.patch("/api/plugins/careercoach/applications/nope/notes", json={"notes": "x"})
    assert bad.status_code == 404
