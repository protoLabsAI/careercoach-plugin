"""Workable navigation and live-form read-back regressions (no host or network)."""

import asyncio
import importlib
import json

import pytest


@pytest.fixture
def ats(plugin):
    return importlib.import_module(plugin.__name__ + ".ats")


@pytest.fixture
def formfill(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    return importlib.import_module(plugin.__name__ + ".formfill")


@pytest.mark.parametrize(
    "url",
    [
        "https://apply.workable.com/acme/j/ABC123/",
        "https://acme.workable.com/j/ABC123",
        "https://apply.workable.com/acme/j/ABC123/apply/",
        "https://acme.workable.com/j/ABC123/apply/?source=test",
        "apply.workable.com/acme/j/ABC123/?source=test#details",
    ],
)
def test_detect_workable(ats, url):
    assert ats.detect(url) == {
        "ats": "workable",
        "company": "acme",
        "job_id": "ABC123",
        "apply_url": "https://apply.workable.com/acme/j/ABC123/apply/",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://apply.workable.com/acme/",
        "https://acme.workable.com/j/",
        "https://apply.workable.com.evil.test/acme/j/ABC123/",
        "https://example.com/acme/j/ABC123/",
    ],
)
def test_non_job_urls_are_not_workable(ats, url):
    assert ats.detect(url) is None


@pytest.mark.parametrize("host", ["boards.greenhouse.io", "job-boards.greenhouse.io"])
def test_greenhouse_preserved(ats, host):
    assert ats.detect(f"https://{host}/acme/jobs/123") == {
        "ats": "greenhouse",
        "board": "acme",
        "job_id": "123",
    }


def test_prepare_navigates_directly_without_fetch(tools, ats, monkeypatch):
    async def forbidden(*args):
        pytest.fail("Workable detection must not fetch a schema")

    monkeypatch.setattr(ats, "fetch_schema", forbidden)
    out = asyncio.run(tools["careercoach_prepare_application"].ainvoke({"url": "https://acme.workable.com/j/ABC123"}))
    assert "https://apply.workable.com/acme/j/ABC123/apply/" in out
    assert "directly" in out and "instead of clicking Apply" in out
    assert "careercoach_plan_fill" in out and "careercoach_verify_fill" in out


def radio(kind):
    return {"id": "auth", "label": "Authorized to work", "kind": kind, "options": ["Yes", "No"]}


@pytest.mark.parametrize("kind", ["radio", "radio-group"])
@pytest.mark.parametrize("state", [{}, {"checked": False}, {"checked": "false"}, {"aria-checked": "false"}])
def test_radio_without_checked_evidence_is_unfilled(formfill, kind, state):
    field = radio(kind)
    plan = formfill.build_plan([field], {"authorized_us": "Yes"})
    mismatches = formfill.diff(plan, [{**field, "value": "Yes", **state}])
    assert len(mismatches) == 1
    assert "unfilled" in mismatches[0]["reason"]


@pytest.mark.parametrize("kind", ["radio", "radio-group"])
@pytest.mark.parametrize("state", [{"checked": True}, {"aria-checked": "true"}])
def test_checked_radio_must_also_match_value(formfill, kind, state):
    field = radio(kind)
    plan = formfill.build_plan([field], {"authorized_us": "Yes"})
    assert formfill.diff(plan, [{**field, "value": "Yes", **state}]) == []
    assert formfill.diff(plan, [{**field, "value": "No", **state}])


def test_verify_tool_rejects_unchecked_radio(tools, formfill):
    field = radio("radio")
    plan = formfill.build_plan([field], {"authorized_us": "Yes"})
    out = tools["careercoach_verify_fill"].invoke(
        {"session_id": plan["session_id"], "form_json": json.dumps([{**field, "value": "Yes", "checked": False}])}
    )
    assert "NOT verified" in out
    assert not formfill.load_session(plan["session_id"]).get("verified")
    assert "checked=true" in tools["careercoach_plan_fill"].invoke(
        {"form_json": json.dumps([field]), "company": "Acme", "role": "Engineer", "extra_answers": '{"auth":"Yes"}'}
    )


def test_split_location_and_hidden_fields(formfill):
    fields = [
        {"label": label, "id": ident, "kind": "text", **attrs}
        for label, ident, attrs in [
            ("City", "city", {}),
            ("Location", "location", {}),
            ("Where are you based", "based", {}),
            ("Location (city, region, country)", "full", {}),
            ("City", "hidden-city", {"type": "hidden", "required": True}),
            ("City", "invisible-city", {"visible": False, "required": True}),
            ("City", "kind-hidden", {"kind": "hidden"}),
            ("State", "location-state", {}),
            ("Country", "location-country", {}),
        ]
    ]
    plan = formfill.build_plan(fields, {"current_location": "Portland, Oregon, USA"})
    rows = {r["id"]: r for r in plan["rows"]}
    assert rows["city"]["value"] == "Portland"
    assert rows["location"]["value"] == rows["based"]["value"] == rows["full"]["value"] == "Portland, Oregon, USA"
    assert rows["location-state"]["action"] == rows["location-country"]["action"] == "skip"
    assert not ({"hidden-city", "invisible-city", "kind-hidden"} & rows.keys())
    assert plan["unmapped"] == []


def test_city_does_not_use_drafts_or_override_explicit_answer(formfill, plugin):
    answers = importlib.import_module(plugin.__name__ + ".answers")
    answers.propose("current_location", "Portland, Oregon, USA")
    field = {"label": "City", "id": "city", "kind": "text", "required": True}
    plan = formfill.build_plan([field], answers.confirmed())
    assert not plan["rows"]
    plan = formfill.build_plan([field], {"current_location": "Portland, Oregon, USA"}, {"city": "Salem"})
    assert plan["rows"][0]["value"] == "Salem"
