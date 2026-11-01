"""Undo of a board move (spec §7): it restores the row exactly, which a second move cannot do."""

from __future__ import annotations

import importlib
import json

import pytest

from _plugin_testkit import FakeRegistry


@pytest.fixture
def st(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".state")


@pytest.fixture
def client(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    reg = FakeRegistry({"followup_days": 10}, plugin_id="careercoach")
    plugin.register(reg)
    prefix, router = next((p, r) for p, r in reg.routers if p == "/api/plugins/careercoach")
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return TestClient(app), reg


def _seed(st, status="applied", date="2026-09-01"):
    """One row last moved long ago. Written directly: a legacy status can't get in any other way."""
    row = {
        "id": "a1",
        "company": "Acme",
        "role": "AI Engineer",
        "status": status,
        "updated": date,
        "history": [{"status": status, "date": date}],
    }
    st._path().parent.mkdir(parents=True, exist_ok=True)
    st._path().write_text(json.dumps([row]))
    return row


def test_undo_restores_the_row_exactly(st):
    before = _seed(st)
    st.move("a1", "interviewing")
    st.undo_move("a1")
    assert st.load_applications()[0] == before


def test_undo_keeps_the_follow_up_clock(st):
    """The reason a second move won't do: its fresh history date would clear the flag for days."""
    _seed(st)
    st.move("a1", "interviewing")
    st.undo_move("a1")
    assert st.needs_followup(st.load_applications()[0], 10, "2026-10-31")


def test_undo_restores_a_legacy_status_a_move_cannot(st):
    before = _seed(st, status="ghosted")
    st.move("a1", "passed")
    with pytest.raises(ValueError):
        st.move("a1", "ghosted")
    st.undo_move("a1")
    assert st.load_applications()[0] == before


def test_undo_restores_a_row_that_never_had_updated(st):
    row = _seed(st)
    del row["updated"]
    st._path().write_text(json.dumps([row]))
    st.move("a1", "interviewing")
    assert "updated" not in st.undo_move("a1")


def test_undo_takes_back_one_move_only(st):
    _seed(st)
    st.move("a1", "interviewing")
    st.move("a1", "offer")
    assert st.undo_move("a1")["status"] == "interviewing"
    with pytest.raises(ValueError):
        st.undo_move("a1")


def test_nothing_to_undo_writes_nothing(st):
    before = _seed(st)
    with pytest.raises(ValueError):
        st.undo_move("a1")
    assert st.load_applications()[0] == before
    with pytest.raises(KeyError):
        st.undo_move("missing")


def test_marking_applied_is_not_undoable(st):
    _seed(st, status="preparing")
    st.move("a1", "preparing")
    st.mark_applied("a1")
    with pytest.raises(ValueError):
        st.undo_move("a1")
    assert st.load_applications()[0]["status"] == "applied"


def test_undo_route_restores_and_emits(st, client):
    http, reg = client
    before = _seed(st)
    assert http.post("/api/plugins/careercoach/applications/a1/move", json={"status": "offer"}).status_code == 200
    got = http.post("/api/plugins/careercoach/applications/a1/undo-move")
    assert got.status_code == 200 and got.json()["application"]["status"] == "applied"
    assert st.load_applications()[0] == before
    assert reg.emitted[-1][1]["status"] == "applied"


def test_undo_route_errors(st, client):
    http, _ = client
    _seed(st)
    assert http.post("/api/plugins/careercoach/applications/a1/undo-move").status_code == 409
    assert http.post("/api/plugins/careercoach/applications/nope/undo-move").status_code == 400
