"""Browser fill is an explicit per-ATS beta; preparation defaults to the apply kit."""

import asyncio
import importlib
import json
import re

import pytest
import yaml

from _plugin_testkit import FakeRegistry
from conftest import ROOT

UUID = "12345678-abcd-1234-abcd-123456789abc"
URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/acme/jobs/123",
    "ashby": f"https://jobs.ashbyhq.com/acme/{UUID}",
    "lever": f"https://jobs.lever.co/acme/{UUID}",
    "workable": "https://apply.workable.com/acme/j/ABC123",
    "": "https://example.com/careers/123",
}
SCHEMA = {
    "questions": [
        {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text"}]},
        {"label": "Phone", "required": True, "fields": [{"name": "phone", "type": "input_text"}]},
        {"label": "Why this role?", "required": True, "fields": [{"name": "why", "type": "textarea"}]},
    ]
}


@pytest.fixture
def setup(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "cc"))
    ats = importlib.import_module(plugin.__name__ + ".ats")
    answers = importlib.import_module(plugin.__name__ + ".answers")
    formfill = importlib.import_module(plugin.__name__ + ".formfill")
    calls = []

    async def fetch(board, job_id):
        calls.append((board, job_id))
        return SCHEMA, ""

    monkeypatch.setattr(ats, "fetch_schema", fetch)
    answers.propose("email", "ada@example.com")
    answers.confirm(["email"])
    answers.propose("phone_number", "DRAFT-PHONE")
    reg = FakeRegistry({"packet_root": str(iso / "ws")})
    plugin.register(reg)
    return ats, formfill, reg.config, {t.name: t for t in reg.tools}, calls


def assert_kit(out):
    for text in (
        "KIT HANDOFF",
        "browser fill is off",
        "careercoach_save_apply_kit",
        "browser_fill",
        "careercoach_render_resume",
        "browser_pdf",
        "cover letter only if the posting asks",
        "careercoach_get_answers",
        "Career Coach view",
        "copy buttons",
        "Never auto-submit",
    ):
        assert text in out
    assert "session_id:" not in out


@pytest.mark.parametrize("name", URLS)
def test_default_prepare_and_plan_handoff_without_session(setup, name):
    _, formfill, _, tools, calls = setup
    out = asyncio.run(tools["careercoach_prepare_application"].ainvoke({"url": URLS[name]}))
    assert_kit(out)
    assert f"off for {name or 'unknown ATS'}" in out
    if name == "greenhouse":
        assert calls == [("acme", "123")]
        assert "ada@example.com" in out and "Why this role?" in out
        assert "DRAFT-PHONE" not in out
    else:
        assert calls == []
    assert_kit(tools["careercoach_plan_fill"].invoke({"ats": name, "form_json": "[]", "company": "A", "role": "R"}))
    assert not formfill._path().exists()


def test_greenhouse_optin_round_trip_and_other_ats_refused(setup):
    ats, formfill, cfg, tools, _ = setup
    cfg["browser_fill"] = "greenhouse"
    out = asyncio.run(tools["careercoach_prepare_application"].ainvoke({"url": URLS["greenhouse"]}))
    assert "Prepared from the Greenhouse public schema" in out
    assert "ada@example.com" in out
    fields = ats.greenhouse_fields(SCHEMA)[:1]
    plan = tools["careercoach_plan_fill"].invoke(
        {"ats": "greenhouse", "form_json": json.dumps(fields), "company": "A", "role": "R"}
    )
    sid = re.search(r"session_id:\s*(\S+)", plan).group(1)
    fields[0]["value"] = "ada@example.com"
    assert (
        tools["careercoach_verify_fill"]
        .invoke({"session_id": sid, "form_json": json.dumps(fields)})
        .startswith("VERIFIED")
    )
    before = formfill._path().read_bytes()
    for name in ("ashby", "", "unknown"):
        assert_kit(
            tools["careercoach_plan_fill"].invoke({"ats": name, "form_json": "not JSON", "company": "A", "role": "R"})
        )
    assert formfill._path().read_bytes() == before


@pytest.mark.parametrize("result", [(None, "HTTP 404"), ({}, "")])
def test_schema_failure_still_hands_off(setup, monkeypatch, result):
    ats, formfill, _, tools, _ = setup

    async def fetch(*args):
        return result

    monkeypatch.setattr(ats, "fetch_schema", fetch)
    assert_kit(asyncio.run(tools["careercoach_prepare_application"].ainvoke({"url": URLS["greenhouse"]})))
    assert not formfill._path().exists()


def test_embedded_greenhouse_and_unknown_stay_off(setup):
    _, formfill, cfg, tools, calls = setup
    cfg["browser_fill"] = "ashby,lever,workable,unknown"
    for url in ("https://example.com/?gh_jid=123", URLS[""]):
        assert_kit(asyncio.run(tools["careercoach_prepare_application"].ainvoke({"url": url})))
    assert calls == []
    assert not formfill._path().exists()


@pytest.mark.parametrize("suffix", ["", "/", "/apply", "/apply/", "/apply?source=test"])
def test_lever_detection(setup, suffix):
    ats, *_ = setup
    assert ats.detect(URLS["lever"] + suffix) == {
        "ats": "lever",
        "company": "acme",
        "job_id": UUID,
        "apply_url": URLS["lever"] + "/apply",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/acme",
        "https://jobs.lever.co/acme/not-a-uuid",
        f"https://jobs.lever.co.evil/acme/{UUID}",
        URLS["lever"] + "/extra",
    ],
)
def test_lever_rejects_non_job_urls(setup, url):
    ats, *_ = setup
    assert ats.detect(url) is None


@pytest.mark.parametrize(
    "name,cfg,expected",
    [
        ("greenhouse", "", False),
        ("greenhouse", "greenhouse", True),
        (" Lever ", " GREENHOUSE, lever , ,", True),
        ("ashby", "greenhouse", False),
        ("workable", "workable", True),
        ("ashby", "ashby", True),
        ("", "greenhouse,", False),
        ("unknown", "unknown", False),
        ("greenhouse", "*", False),
        ("greenhouse", "greenhouseish", False),
        ("greenhouse", None, False),
        ("greenhouse", True, False),
    ],
)
def test_fill_enabled(setup, name, cfg, expected):
    ats, *_ = setup
    assert ats.fill_enabled(name, cfg) is expected


def test_manifest_default_and_beta_setting():
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert manifest["config"]["browser_fill"] == ""
    setting = next(row for row in manifest["settings"] if row["key"] == "browser_fill")
    assert "beta" in setting["description"].lower()
