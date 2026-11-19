"""The triage log: every keep and dismiss is recorded with the card and, for a dismissal, why.

The operator asked for it on 2026-11-18, to tune the search from what they actually reject:
"a reason for dismissal so that we can track and adjust… a log and then periodic sweeps".
"""

from __future__ import annotations

import importlib

import pytest

from _plugin_testkit import FakeRegistry
from test_pipeline import client  # noqa: F401 — fixture


def card(jid, **over):
    base = {
        "id": jid,
        "company": "Acme",
        "title": "Frontend Engineer",
        "source": "himalayas",
        "location": "Remote (United States)",
        "comp": "USD 140,000–180,000",
        "url": f"https://example.com/jobs/{jid}",
        "posted": "2026-11-17T00:00:00+00:00",
        "hash": f"hash-{jid}",
        "score": 84,
        "reason": 'stack 9/10 · mid/senior · title ~ "Frontend Engineer" · pay $140K+',
    }
    return {**base, **over}


@pytest.fixture
def mods(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))

    class M:
        pass

    m = M()
    for name in ("triage", "watchlist"):
        setattr(m, name, importlib.import_module(f"{plugin.__name__}.search.{name}"))
    return m


@pytest.fixture
def track(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    reg = FakeRegistry({}, plugin_id="careercoach")
    plugin.register(reg)
    return next(t for t in reg.tools if t.name == "careercoach_track")


# ── the log ───────────────────────────────────────────────────────────────────
def test_a_dismissal_is_logged_with_the_card_and_the_reason(mods):
    wl, tr = mods.watchlist, mods.triage
    wl.add_to_inbox([card("a"), card("b", company="Jobgether", source="jsearch", score=70)])
    wl.dismiss(["a"], reason="location", note="  hybrid in Austin  ", via="view")
    wl.dismiss(["b"])
    a, b = tr.entries()
    assert a["action"] == "dismiss" and a["reason"] == "location" and a["note"] == "hybrid in Austin"
    assert (a["company"], a["title"], a["source"], a["score"], a["via"]) == (
        "Acme", "Frontend Engineer", "himalayas", 84, "view",
    )  # fmt: skip
    assert a["why_scored"].startswith("stack 9/10") and a["location"] == "Remote (United States)"
    assert "reason" not in b and b["company"] == "Jobgether"  # no reason given: still logged
    wl.dismiss(["a"], reason="pay")  # already dismissed: not logged twice
    assert len(tr.entries()) == 2 and tr.entries()[0]["reason"] == "location"


def test_a_reason_can_be_a_code_or_its_label_and_nothing_else(mods):
    tr = mods.triage
    assert tr.normalize_reason("wrong_role") == tr.normalize_reason("Wrong Role") == "wrong_role"
    assert tr.normalize_reason("Location doesn't work") == "location" and tr.normalize_reason("") == ""
    with pytest.raises(ValueError, match="wrong_role, level, stack"):
        tr.normalize_reason("vibes")
    assert set(tr.REASONS) == {
        "wrong_role", "level", "stack", "location", "pay", "company", "recruiter", "stale", "duplicate", "other",
    }  # fmt: skip


def test_a_reason_given_afterwards_lands_on_the_latest_dismissal(mods):
    """The view dismisses first and asks second."""
    wl, tr = mods.watchlist, mods.triage
    wl.add_to_inbox([card("a"), card("b"), card("c")])
    wl.dismiss(["a", "b", "c"])
    assert tr.set_reason(["a", "b"], "recruiter", "reposted by an agency") == 2
    by_id = {e["id"]: e for e in tr.entries()}
    assert by_id["a"]["reason"] == by_id["b"]["reason"] == "recruiter" and "reason" not in by_id["c"]
    assert by_id["a"]["note"] == "reposted by an agency"
    assert tr.set_reason(["nope"], "pay") == 0
    with pytest.raises(ValueError):
        tr.set_reason(["a"], "")
    with pytest.raises(ValueError):
        tr.set_reason(["a"], "vibes")


def test_the_log_never_blocks_a_decision_and_is_capped(mods, monkeypatch):
    wl, tr = mods.watchlist, mods.triage
    wl.add_to_inbox([card("a")])
    wl._path(tr.LOG).parent.mkdir(parents=True, exist_ok=True)
    wl._path(tr.LOG).write_text("{broken")
    wl.dismiss(["a"], reason="pay")  # an unreadable log: the dismissal still happens
    assert not [c for c in wl.inbox_for_view() if c["id"] == "a"] and tr.entries() == []
    wl._path(tr.LOG).unlink()
    monkeypatch.setattr(tr, "LOG_CAP", 3)
    tr.record("dismiss", [card(f"n{i}") for i in range(5)])
    assert [e["id"] for e in tr.entries()] == ["n2", "n3", "n4"]  # oldest dropped first
    assert tr.record("pass", [card("x")]) == 0 and tr.record("keep", []) == 0


# ── adding it up ──────────────────────────────────────────────────────────────
def _log(tr):
    rows = []

    def add(n, action, source, role, score, reason="", company="Acme"):
        for i in range(n):
            why = f'stack 3/3 · title ~ "{role}"'
            c = card(f"{source}-{role}-{action}-{reason}-{i}", source=source, score=score, reason=why, company=company)
            rows.append(tr._entry(action, c, reason, "", "view", "2026-11-18T10:00:00+00:00"))

    add(9, "keep", "himalayas", "Frontend Engineer", 92)
    add(6, "dismiss", "himalayas", "Frontend Engineer", 88, "location")
    add(1, "keep", "jsearch", "AI Engineer", 72)
    add(11, "dismiss", "jsearch", "AI Engineer", 70, "recruiter", company="Jobgether")
    add(4, "dismiss", "jsearch", "Design Engineer", 68)
    return rows


def test_the_report_adds_up_reasons_sources_roles_and_scores(mods):
    tr = mods.triage
    rep = tr.report(_log(tr))
    assert (rep["decisions"], rep["kept"], rep["dismissed"], rep["with_reason"]) == (31, 10, 21, 17)
    assert rep["by_source"]["jsearch"] == {"kept": 1, "dismissed": 15, "total": 16, "keep_rate": 0.06}
    assert rep["by_source"]["himalayas"]["keep_rate"] == 0.6
    assert rep["by_role"]["AI Engineer"]["total"] == 12 and rep["by_role"]["Frontend Engineer"]["kept"] == 9
    assert rep["by_score"]["65-79"]["total"] == 16 and rep["by_score"]["90-100"]["keep_rate"] == 1.0
    assert list(rep["reasons"]) == ["recruiter", "location"]
    assert rep["reasons"]["recruiter"]["sources"] == {"jsearch": 11}
    assert rep["reasons"]["recruiter"]["companies"] == {"Jobgether": 11}
    assert tr.report([])["decisions"] == 0 and tr.report([])["keep_rate"] == 0.0


def test_the_report_says_what_to_change_only_when_the_numbers_are_there(mods):
    tr = mods.triage
    tips = tr.suggestions(tr.report(_log(tr)))
    text = "\n".join(tips)
    assert "jsearch: you kept 1 of 16." in text and "himalayas" not in text  # 60% kept is fine
    assert 'Target role "AI Engineer": you kept 1 of 12.' in text
    assert "Roles scoring 65-79: you kept 1 of 16." in text
    assert "Recruiter or reposted listing was the reason 11 times." in text and "Jobgether" in text
    assert "Location doesn't work was the reason 6 times. Check the Locations setting" in text
    assert tr.suggestions(tr.report(_log(tr)[:6])) == []  # six decisions say nothing yet
    out = tr.format_report(tr.report(_log(tr)), days=30)
    assert out.startswith("Triage over the last 30 days: 31 decisions, 10 kept, 21 dismissed (32% kept).")
    assert "nothing has been changed" in out and "- jsearch: 1 of 16 (6%)" in out
    assert "No triage decisions are logged yet" in tr.format_report(tr.report([]))


# ── the routes and the tool ───────────────────────────────────────────────────
def test_the_dismiss_route_takes_a_reason_and_refuses_an_unknown_one(plugin, client):  # noqa: F811
    tc, _ = client
    wl = importlib.import_module(plugin.__name__ + ".search.watchlist")
    tr = importlib.import_module(plugin.__name__ + ".search.triage")
    wl.add_to_inbox([card("a"), card("b"), card("c")])
    bad = tc.post("/api/plugins/careercoach/inbox/dismiss", json={"id": "a", "reason": "vibes"})
    assert bad.status_code == 400 and "wrong_role" in bad.json()["error"]
    assert [c["id"] for c in wl.inbox_for_view()] and tr.entries() == []  # nothing dismissed
    assert tc.post("/api/plugins/careercoach/inbox/dismiss", json={"id": "a", "reason": "pay"}).json() == {"ok": True}
    assert tc.post("/api/plugins/careercoach/inbox/dismiss", json={"id": "b"}).status_code == 200  # reason optional
    later = tc.post(
        "/api/plugins/careercoach/inbox/dismiss-reason", json={"ids": ["b"], "reason": "Too junior or too senior"}
    )
    assert later.json() == {"ok": True, "updated": 1}
    assert (
        tc.post("/api/plugins/careercoach/inbox/dismiss-reason", json={"ids": [], "reason": "pay"}).status_code == 400
    )
    assert tc.post("/api/plugins/careercoach/inbox/dismiss-reason", json={"id": "b", "reason": "x"}).status_code == 400
    assert tc.post("/api/plugins/careercoach/inbox/keep", json={"id": "c"}).status_code == 200
    assert [(e["id"], e["action"], e.get("reason")) for e in tr.entries()] == [
        ("a", "dismiss", "pay"), ("b", "dismiss", "level"), ("c", "keep", None),
    ]  # fmt: skip
    body = tc.get("/api/plugins/careercoach/triage?days=30").json()
    assert body["report"]["decisions"] == 3 and body["reasons"]["pay"] == "Pay too low" and body["suggestions"] == []


def test_the_coach_can_log_a_reason_and_read_the_report(plugin, track):
    wl = importlib.import_module(plugin.__name__ + ".search.watchlist")
    tr = importlib.import_module(plugin.__name__ + ".search.triage")
    wl.add_to_inbox([card("a"), card("b", title="AI Engineer")])
    refused = track.invoke({"action": "dismiss", "inbox_id": "a", "reason": "vibes"})
    assert "unknown reason" in refused and "Nothing was dismissed" in refused and tr.entries() == []
    out = track.invoke({"action": "dismiss", "inbox_id": "a", "reason": "stack", "notes": "all Java"})
    assert "Dismissed Frontend Engineer @ Acme" in out and "Reason logged." in out
    (entry,) = tr.entries()
    assert (entry["reason"], entry["note"], entry["via"]) == ("stack", "all Java", "chat")
    track.invoke({"action": "keep", "inbox_id": "b"})
    report = track.invoke({"action": "triage_report"})
    assert "2 decisions, 1 kept, 1 dismissed" in report and "Wrong tech stack: 1" in report
    assert "triage_report" in track.invoke({"action": "nonsense"})
