"""Pipeline DATA + gated ROUTES (S1b): stable ids, status history, `preparing`, move,
follow-up flagging, and the gated /jobs · keep · dismiss · move routes.

Host-free: the state helpers are exercised directly, and the routes run through a FastAPI
TestClient fed the plugin's own gated router (the same router register() hands the host)."""

from __future__ import annotations

import importlib
import json

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
    assert row["fit_score"] == 92  # score kept
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
