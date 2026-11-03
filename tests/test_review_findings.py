"""Regressions for the QA panel's findings on #102: atomic packet writes, naive board times, year ranges."""

from __future__ import annotations

import importlib


def test_naive_board_timestamps_are_utc(plugin):
    boards = importlib.import_module(f"{plugin.__name__}.search.boards")
    assert boards._iso("2026-11-02T09:00:00") == "2026-11-02T09:00:00+00:00"
    assert boards._iso("2026-11-02T09:00:00Z") == "2026-11-02T09:00:00+00:00"


def test_year_ranges_are_not_flagged_but_other_ranges_are(plugin):
    grounding = importlib.import_module(f"{plugin.__name__}.record.grounding")
    sources = "Product Engineer at Example Co."
    assert grounding.check_draft("I was a Product Engineer, 2021-2026.", sources) == []
    assert grounding.check_draft("I was a Product Engineer, 2021–2026.", sources) == []
    flagged = grounding.check_draft("We cut review time 30-40 hours a week.", sources)
    assert flagged, "a range that isn't years is still checked"


def test_packet_writes_leave_no_temp_files_and_replace_content(plugin, tmp_path):
    packet = importlib.import_module(f"{plugin.__name__}.applying.packet")
    target = tmp_path / "letter.md"
    packet._write(target, "first\n")
    packet._write(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert [p.name for p in tmp_path.iterdir()] == ["letter.md"]
