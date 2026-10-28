"""Retained inbox evidence, durable decisions, and unchanged tool rendering."""

import asyncio
import datetime
import json

import pytest

import test_hunt

hunt_tools = test_hunt.hunt_tools
mods = test_hunt.mods
net = test_hunt.net
watch_env = test_hunt.watch_env


def job(jid="one", **overrides):
    return {
        "id": jid,
        "company": "Example",
        "title": "AI Engineer",
        "location": "Remote",
        "url": f"https://example.com/jobs/{jid}",
        "posted": "2026-10-26T00:00:00+00:00",
        "first_seen": "2026-10-27T00:00:00+00:00",
        "hash": f"hash-{jid}",
        "score": 100,
        "reason": 'title matches "AI Engineer" (2/2 words)',
        "snippet": "Build useful AI systems.",
        **overrides,
    }


def test_reason_is_pure_and_uses_best_role(mods):
    j = job()
    before = dict(j)
    assert mods.watchlist.match_reason(j, "Design Engineer; AI Engineer") == (
        'title matches "AI Engineer" (2/2 words) · remote · posted 2026-10-26'
    )
    assert j == before
    assert mods.watchlist.match_reason({}, "") == "No target title words matched"
    assert "\n" not in mods.watchlist.match_reason(j, "AI\nEngineer")


def test_retains_evidence_and_digest_marks(mods):
    wl = mods.watchlist
    wl.add_to_inbox([job(), job(), job("two", posted="2026-10-27")])
    assert [j["id"] for j in wl.peek_inbox()] == ["two", "one"]
    wl.drop_from_inbox(["one"])
    saved = next(j for j in wl.inbox_for_view() if j["id"] == "one")
    for key in ("score", "reason", "snippet", "hash"):
        assert saved[key] == job()[key]
    datetime.datetime.fromisoformat(saved["digested"])
    wl.drop_from_inbox(["one"])
    assert next(j for j in wl.inbox_for_view() if j["id"] == "one") == saved
    assert [j["id"] for j in wl.take_inbox()] == ["two"]
    assert wl.take_inbox() == []
    assert len(wl.inbox_for_view()) == 2
    wl.add_to_inbox([job()])
    assert wl.peek_inbox() == []


def test_dismissal_survives_eviction_and_blocks_id_or_hash(mods, monkeypatch):
    wl = mods.watchlist
    wl.add_to_inbox([job(), job("alias", hash="hash-one"), job("hashless", hash="")])
    wl.dismiss(["one", "hashless", "unknown"])
    assert wl.inbox_for_view() == []
    assert wl.peek_inbox() == []
    monkeypatch.setattr(wl, "INBOX_CAP", 1)
    wl.add_to_inbox([job("replacement")])
    wl.add_to_inbox(
        [
            job("one", hash="changed"),
            job("repost", hash="hash-one"),
            job("unknown"),
            job("hashless", hash="changed"),
        ]
    )
    assert [j["id"] for j in wl.inbox_for_view()] == ["replacement"]
    wl.add_to_inbox([job("other-hashless", hash="")])
    assert [j["id"] for j in wl.inbox_for_view()] == ["other-hashless"]
    assert json.loads(wl._path(wl.DISMISSED).read_text())["one"] == ["hash-one"]


def test_kept_and_digested_evict_before_pending(mods, monkeypatch):
    wl = mods.watchlist
    monkeypatch.setattr(wl, "INBOX_CAP", 3)
    wl.add_to_inbox([job("pending"), job("digested"), job("kept")])
    wl.drop_from_inbox(["digested"])
    wl.mark_kept("kept")
    assert {j["id"] for j in wl.inbox_for_view()} == {"pending", "digested"}
    assert [j["id"] for j in wl.peek_inbox()] == ["pending"]
    wl.add_to_inbox([job("new1"), job("new2")])
    assert {j["id"] for j in wl.inbox_for_view()} == {"pending", "new1", "new2"}
    wl.add_to_inbox([job("new3")])
    assert {j["id"] for j in wl.peek_inbox()} == {"new1", "new2", "new3"}


def test_legacy_rows_and_unreadable_dismissals(mods):
    wl = mods.watchlist
    wl._save(wl.INBOX, [{"id": "legacy", "title": "Engineer"}])
    assert wl.take_inbox()[0]["id"] == "legacy"
    assert wl.inbox_for_view()[0]["digested"]
    wl._path(wl.DISMISSED).write_text("broken")
    before = wl._path(wl.INBOX).read_bytes()
    with pytest.raises(wl.StoreUnreadable):
        wl.add_to_inbox([job()])
    assert wl._path(wl.INBOX).read_bytes() == before


def test_watch_populates_all_evidence(watch_env, mods):
    plugin, reg, cfg = watch_env
    matches = asyncio.run(plugin._watch_tick(reg, cfg, 75))
    assert matches
    saved = {j["id"]: j for j in mods.watchlist.inbox_for_view()}
    for j in matches:
        assert saved[j["id"]]["score"] == j["score"]
        assert saved[j["id"]]["reason"] == mods.watchlist.match_reason(j, cfg["target_roles"])
        assert saved[j["id"]]["snippet"] and saved[j["id"]]["hash"]


def test_scan_feeds_untracked_matches_without_changing_text(hunt_tools, mods, monkeypatch):
    from types import SimpleNamespace

    wl = mods.watchlist
    wl.add_company("Example", "ashby", "example")
    jobs = [job(tracked=""), job("tracked", tracked="applied")]

    async def scan(*args, **kwargs):
        return SimpleNamespace(matches=jobs, new=jobs, scanned=1, total=2, errors=[])

    monkeypatch.setattr(mods.scan, "scan_watchlist", scan)
    monkeypatch.setattr(mods.boards, "age_days", lambda *args: 2)
    out = asyncio.run(hunt_tools["careercoach_scan_boards"].ainvoke({"roles": "AI Engineer"}))
    assert out == (
        "Scanned 1 board(s), 2 open roles → 2 match (roles: AI Engineer; locations: remote, portland; "
        "≤ 30 days old); 2 new since the last scan.\n\n"
        "1. AI Engineer — Example · Remote · posted 2d ago\n   https://example.com/jobs/one\n"
        "2. AI Engineer — Example · Remote · posted 2d ago  [already tracked: applied]\n"
        "   https://example.com/jobs/tracked"
    )
    saved = wl.inbox_for_view()
    assert len(saved) == 1
    assert saved[0]["score"] == 100
    assert saved[0]["reason"] == wl.match_reason(jobs[0], "AI Engineer")
    assert saved[0]["snippet"] == jobs[0]["snippet"] and saved[0]["hash"] == jobs[0]["hash"]


def test_digest_prechange_golden_and_no_repeat(hunt_tools, mods, monkeypatch):
    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 28, tzinfo=tz)

    monkeypatch.setattr(datetime, "datetime", FixedDatetime)
    monkeypatch.setattr(mods.boards, "age_days", lambda *args: 2)
    mods.watchlist.add_to_inbox([job()])
    out = asyncio.run(hunt_tools["careercoach_digest"].ainvoke({}))
    assert out == (
        "# Job-hunt digest — 2026-10-28\n\n"
        "## New roles\nNo watchlist or target roles yet — add companies with careercoach_watchlist and set "
        "Target roles in Settings.\n\n"
        "## From the background watch (1 queued)\n"
        "1. AI Engineer — Example · Remote · posted 2d ago\n   https://example.com/jobs/one\n\n"
        "## Pipeline\nempty"
    )
    again = asyncio.run(hunt_tools["careercoach_digest"].ainvoke({}))
    assert "Example" not in again and "background watch" not in again
    assert mods.watchlist.inbox_for_view()[0]["digested"]
