"""Pipeline DATA + gated ROUTES (S1b): stable ids, status history, `preparing`, move,
follow-up flagging, and the gated /jobs · keep · dismiss · move routes.

Host-free: the state helpers are exercised directly, and the routes run through a FastAPI
TestClient fed the plugin's own gated router (the same router register() hands the host)."""

from __future__ import annotations

import importlib
import json
from urllib.parse import quote

import pytest

from _plugin_testkit import FakeRegistry


@pytest.fixture
def st(plugin, iso, monkeypatch):
    """The ``state`` module with an isolated store."""
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".state")


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


def _inbox_job(wl, jid="j1"):
    """Queue one watch-inbox item carrying the evidence the keep route promotes."""
    wl.add_to_inbox(
        [
            {
                "id": jid,
                "company": "Acme",
                "title": "AI Engineer",
                "location": "Remote",
                "url": f"https://acme.example/jobs/{jid}",
                "posted": "2026-10-20",
                "comp": "$200k",
                "source": "ashby",
                "first_seen": "2026-10-21",
                "ghost": [],
                "reason": 'title matches "AI Engineer" (2/2 words)',
                "snippet": "Build useful AI systems.",
                "hash": f"hash-{jid}",
                "score": 92,
            }
        ]
    )


# ── r1: stable ids + history backfill ─────────────────────────────────────────
def test_legacy_rows_get_stable_ids_and_history_without_a_write(st):
    raw = [{"date": "2026-10-01", "company": "Acme", "role": "ML Engineer", "fit_score": 80, "status": "applied"}]
    p = st._path()  # resolving the path creates the store dir
    p.write_text(json.dumps(raw), encoding="utf-8")

    rows1 = st.load_applications()
    rows2 = st.load_applications()
    assert rows1[0]["id"] and rows1[0]["id"] == rows2[0]["id"]  # same id across loads
    # derived from status + (updated or date); no `updated` here → the date
    assert rows1[0]["history"] == [{"status": "applied", "date": "2026-10-01"}]
    # a plain load wrote nothing back — the id is stable without needing persistence
    assert "id" not in json.loads(p.read_text())[0]


def test_preparing_is_a_valid_status_between_considering_and_applied(st):
    assert "preparing" in st.STATUSES
    assert (
        list(st.STATUSES).index("considering")
        < list(st.STATUSES).index("preparing")
        < list(st.STATUSES).index("applied")
    )


# ── r2: move() ────────────────────────────────────────────────────────────────
def test_move_updates_status_updated_and_appends_history(st):
    row = st.track_application(company="Acme", role="ML Engineer", status="considering")
    moved = st.move(row["id"], "applied")
    assert moved["status"] == "applied" and moved["updated"]
    assert [h["status"] for h in moved["history"]] == ["considering", "applied"]
    assert moved["history"][-1]["date"] == st._today()


def test_move_rejects_bad_id_and_bad_status_without_writing(st):
    row = st.track_application(company="Acme", role="ML Engineer", status="considering")
    with pytest.raises(KeyError):
        st.move("does-not-exist", "applied")
    with pytest.raises(ValueError):
        st.move(row["id"], "bogus")
    # nothing changed: still a single considering row with a single history entry
    rows = st.load_applications()
    assert len(rows) == 1 and rows[0]["status"] == "considering"
    assert len(rows[0]["history"]) == 1


def test_move_refuses_an_unreadable_pipeline(st):
    st.track_application(company="Acme", role="ML Engineer", status="considering")
    app_id = st.load_applications()[0]["id"]
    st._path().write_text("{not json", encoding="utf-8")
    with pytest.raises(st.StoreUnreadable):
        st.move(app_id, "applied")


# ── r3: needs_followup() ───────────────────────────────────────────────────────
def _applied(date):
    return {"status": "applied", "history": [{"status": "applied", "date": date}]}


def test_needs_followup_edges(st):
    assert st.needs_followup(_applied("2026-10-18"), 10, "2026-10-28") is True  # exactly 10d
    assert st.needs_followup(_applied("2026-10-19"), 10, "2026-10-28") is False  # 9d
    # any non-applied status is never a follow-up, however old
    considering = {"status": "considering", "history": [{"status": "considering", "date": "2026-01-01"}]}
    assert st.needs_followup(considering, 10, "2026-10-28") is False


# ── r4: gated routes ────────────────────────────────────────────────────────────
def test_jobs_route_shape(plugin, client):
    tc, _reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    st.track_application(company="Acme", role="ML Engineer", status="applied")
    body = tc.get("/api/plugins/careercoach/jobs").json()
    assert set(body) == {"inbox", "applications", "columns", "followup_days"}
    assert body["followup_days"] == 10
    assert body["columns"]["Saved"] == ["considering"]
    assert body["columns"]["Closed"] == ["rejected", "passed"]
    assert all("followup" in a for a in body["applications"])  # every row carries the flag


def test_keep_promotes_inbox_item_and_removes_it(plugin, client):
    tc, reg = client
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    st = importlib.import_module(plugin.__name__ + ".state")
    _inbox_job(wl, "j1")

    r = tc.post("/api/plugins/careercoach/inbox/j1/keep")
    assert r.status_code == 200

    rows = st.load_applications()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "considering"
    assert row["fit_score"] == 0  # a title match is not a fit evaluation
    assert row["source"] == "https://acme.example/jobs/j1"  # url kept as source
    assert row["posting"]["title"] == "AI Engineer"  # posting evidence stored
    assert row["posting"]["snippet"] == "Build useful AI systems."

    assert wl.inbox_for_view() == []  # gone from the inbox
    assert tc.get("/api/plugins/careercoach/jobs").json()["inbox"] == []
    assert "careercoach.application_tracked" in [t for t, _ in reg.emitted]


def test_keep_unknown_item_is_4xx(client):
    tc, _reg = client
    r = tc.post("/api/plugins/careercoach/inbox/nope/keep")
    assert 400 <= r.status_code < 500 and "error" in r.json()


@pytest.mark.parametrize("body_route", [False, True])
@pytest.mark.parametrize("status", ["considering", "preparing", "applied", "interviewing", "offer"])
@pytest.mark.parametrize("match", ["url", "company_role"])
def test_keep_preserves_existing_application(plugin, client, monkeypatch, status, match, body_route):
    tc, reg = client
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    st = importlib.import_module(plugin.__name__ + ".state")
    _inbox_job(wl, "123456")
    monkeypatch.setattr(st, "_today", lambda: "2026-10-01")
    existing = st.track_application(
        company="Acme",
        role="Retyped title" if match == "url" else "AI Engineer",
        source="https://acme.example/jobs/123456" if match == "url" else "",
        status=status,
        fit_score=81,
        notes="Operator notes",
        posting={"snippet": "Original evidence"},
    )
    before = st._path().read_bytes()
    monkeypatch.setattr(st, "_today", lambda: "2026-10-28")
    response = (
        tc.post("/api/plugins/careercoach/inbox/keep", json={"id": "123456"})
        if body_route
        else tc.post("/api/plugins/careercoach/inbox/123456/keep")
    )
    assert response.status_code == 200
    assert response.json()["application"] == existing
    assert st._path().read_bytes() == before
    assert wl.inbox_for_view() == []
    assert reg.emitted[-1][1]["status"] == status


@pytest.mark.parametrize("action", ["keep", "dismiss"])
@pytest.mark.parametrize("encoded", [False, True])
def test_inbox_routes_accept_market_search_ids(plugin, client, action, encoded):
    tc, _reg = client
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    item_id = "market:https://jobs.example/companies/acme/jobs/123456"
    _inbox_job(wl, item_id)
    path_id = quote(item_id, safe="") if encoded else item_id
    response = tc.post(f"/api/plugins/careercoach/inbox/{path_id}/{action}")
    assert response.status_code == 200
    assert wl.inbox_for_view() == []
    _inbox_job(wl, item_id)
    assert wl.inbox_for_view() == []


@pytest.mark.parametrize("body_route", [False, True])
@pytest.mark.parametrize("operation", ["inbox_item", "mark_kept", "track_application", "dismiss"])
def test_inbox_routes_unreadable_store_returns_conflict(plugin, client, monkeypatch, operation, body_route):
    tc, _reg = client
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    _inbox_job(wl)

    st = importlib.import_module(plugin.__name__ + ".state")
    module = st if operation == "track_application" else wl

    def unreadable(*args, **kwargs):
        raise module.StoreUnreadable(st._path() if module is st else wl._path("inbox"), "invalid JSON")

    monkeypatch.setattr(module, operation, unreadable)
    action = "dismiss" if operation == "dismiss" else "keep"
    response = (
        tc.post(f"/api/plugins/careercoach/inbox/{action}", json={"id": "j1"})
        if body_route
        else tc.post(f"/api/plugins/careercoach/inbox/j1/{action}")
    )
    assert response.status_code == 409
    assert "error" in response.json()


def test_track_application_history_only_records_status_changes(st, monkeypatch):
    monkeypatch.setattr(st, "_today", lambda: "2026-10-01")
    st.track_application(company="Acme", role="Engineer")
    monkeypatch.setattr(st, "_today", lambda: "2026-10-02")
    changed = st.track_application(company="Acme", role="Engineer", status="preparing")
    assert changed["history"] == [
        {"status": "considering", "date": "2026-10-01"},
        {"status": "preparing", "date": "2026-10-02"},
    ]
    unchanged = st.track_application(company="Acme", role="Engineer", status="preparing", notes="Updated")
    assert unchanged["history"] == changed["history"]


def test_dismiss_removes_and_sticks(plugin, client):
    tc, _reg = client
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    _inbox_job(wl, "j1")

    assert tc.post("/api/plugins/careercoach/inbox/j1/dismiss").status_code == 200
    assert wl.inbox_for_view() == []

    _inbox_job(wl, "j1")  # re-queuing the same posting must not resurrect it
    assert wl.inbox_for_view() == []
    assert tc.get("/api/plugins/careercoach/jobs").json()["inbox"] == []


def test_move_route_bad_status_is_4xx_and_good_move_works(plugin, client):
    tc, reg = client
    st = importlib.import_module(plugin.__name__ + ".state")
    app_id = st.track_application(company="Acme", role="ML Engineer", status="considering")["id"]

    bad = tc.post(f"/api/plugins/careercoach/applications/{app_id}/move", json={"status": "bogus"})
    assert bad.status_code == 400 and "error" in bad.json()
    assert st.load_applications()[0]["status"] == "considering"  # wrote nothing

    ok = tc.post(f"/api/plugins/careercoach/applications/{app_id}/move", json={"status": "preparing"})
    assert ok.status_code == 200
    assert st.load_applications()[0]["status"] == "preparing"
    assert "careercoach.application_tracked" in [t for t, _ in reg.emitted]

    missing = tc.post("/api/plugins/careercoach/applications/nope/move", json={"status": "applied"})
    assert missing.status_code == 400 and "error" in missing.json()


@pytest.mark.parametrize("action", ["keep", "dismiss"])
def test_inbox_body_routes_accept_url_ids(plugin, client, action):
    tc, reg = client
    wl = importlib.import_module(plugin.__name__ + ".watchlist")
    st = importlib.import_module(plugin.__name__ + ".state")
    item_id = "himalayas:https://himalayas.app/companies/acme/jobs/123?ref=x&y=1"
    _inbox_job(wl, item_id)
    response = tc.post(f"/api/plugins/careercoach/inbox/{action}", json={"id": item_id})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    rows = st.load_applications()
    if action == "keep":
        assert len(rows) == 1
        assert response.json()["application"] == rows[0]
        assert rows[0]["status"] == "considering"
        assert "careercoach.application_tracked" in [t for t, _ in reg.emitted]
    else:
        assert response.json() == {"ok": True}
        assert rows == []
    assert tc.get("/api/plugins/careercoach/jobs").json()["inbox"] == []
    _inbox_job(wl, item_id)
    assert tc.get("/api/plugins/careercoach/jobs").json()["inbox"] == []


@pytest.mark.parametrize("action", ["keep", "dismiss"])
@pytest.mark.parametrize("payload", [None, {}, {"id": ""}, {"id": None}, {"id": 123}])
def test_inbox_body_routes_require_id(client, action, payload):
    tc, _reg = client
    response = tc.post(f"/api/plugins/careercoach/inbox/{action}", json=payload)
    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.parametrize("action, expected", [("keep", 404), ("dismiss", 200)])
def test_inbox_body_routes_unknown_id(client, action, expected):
    tc, _reg = client
    response = tc.post(f"/api/plugins/careercoach/inbox/{action}", json={"id": "unknown"})
    assert response.status_code == expected
    assert "error" in response.json() if expected == 404 else response.json() == {"ok": True}
