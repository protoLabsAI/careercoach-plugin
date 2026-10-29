"""Fixed benchmark contracts and offline runner integration."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from evals.tool_selection import run_eval as evaluation


@pytest.mark.parametrize(
    ("expect", "pick", "aliases", "correct"),
    [
        (["old", "alternate"], "old", {}, True),
        (["old", "alternate"], "alternate", {}, True),
        (["old"], "new", {"find": ["new"]}, True),
        (["old"], "new", {"other": ["new"]}, False),
        (["old"], "web_search", {}, False),
        (["old"], None, {}, False),
        ("none", None, {}, True),
        ("none", "none", {}, True),
        ("none", "old", {}, False),
        ("none", "new", {"find": ["new"]}, False),
    ],
)
def test_score_acceptance(expect, pick, aliases, correct):
    prompts = [{"id": "one", "prompt": "Example", "expect": expect, "capability": "find"}]
    before = copy.deepcopy((prompts, aliases))
    result = evaluation.score({"one": pick}, prompts, aliases)
    assert result["picks"][0]["correct"] is correct
    assert result["overall"] == {"correct": int(correct), "total": 1, "accuracy": float(correct)}
    assert result["per_capability"]["find"] == result["overall"]
    assert (prompts, aliases) == before


def test_score_aggregates_and_rejects_incomplete_observations():
    prompts = [
        {"id": "a", "expect": ["old"], "capability": "find"},
        {"id": "b", "expect": ["old"], "capability": "find"},
        {"id": "c", "expect": "none", "capability": "chat"},
    ]
    result = evaluation.score({"a": "old", "b": "web_search", "c": None}, prompts, {})
    assert result["overall"] == {"correct": 2, "total": 3, "accuracy": 2 / 3}
    assert result["per_capability"]["find"]["accuracy"] == 0.5
    assert result["per_capability"]["chat"]["accuracy"] == 1
    with pytest.raises(ValueError, match="exactly"):
        evaluation.score({"a": "old", "b": "old"}, prompts, {})
    with pytest.raises(ValueError, match="exactly"):
        evaluation.score({"a": "old"}, [prompts[0], prompts[0]], {})
    with pytest.raises(ValueError, match="exactly"):
        evaluation.score({"extra": None}, [], {})
    assert evaluation.score({}, [], {})["overall"] == {"correct": 0, "total": 0, "accuracy": 0}


def test_committed_prompts_match_current_registration(plugin, iso):
    from _plugin_testkit import FakeRegistry

    # The `expect` lists keep the pre-consolidation names so the same corpus scores both surfaces;
    # they resolve on the EXTENDED surface. The `aliases` name the merged default tools; they resolve
    # on the lean DEFAULT surface a shipped plugin exposes (S4b).
    default_reg = FakeRegistry()
    plugin.register(default_reg)
    default_names = {tool.name for tool in default_reg.tools}
    extended_reg = FakeRegistry({"extended_tools": True, "browser_fill": "greenhouse,lever,ashby,workable"})
    plugin.register(extended_reg)
    extended_names = {tool.name for tool in extended_reg.tools}

    prompts, aliases = evaluation.load_prompts()
    assert 40 <= len(prompts) <= 50
    assert len({p["id"] for p in prompts}) == len(prompts)
    capabilities = {p["capability"] for p in prompts}
    assert capabilities == {"find", "evaluate", "apply_kit", "track", "interview", "profile", "digest", "coaching"}
    assert set(aliases) == capabilities
    for prompt in prompts:
        assert prompt["capability"].strip()
        if prompt["expect"] != "none":
            assert set(prompt["expect"]) <= extended_names
    # Every capability maps to a non-empty list of real default-surface tools (S4b acceptance).
    for capability, names in aliases.items():
        assert names, f"{capability} has an empty alias list"
        assert set(names) <= default_names, (capability, set(names) - default_names)
    text = evaluation.PROMPTS_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("Josh", "jobCoach"):
        assert forbidden.lower() not in text


def test_collects_actual_schemas_and_passes_config(monkeypatch, iso):
    from _plugin_testkit import FakeRegistry

    received = []
    original_init = FakeRegistry.__init__

    def capture(self, *args, **kwargs):
        received.append(kwargs["config"])
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(FakeRegistry, "__init__", capture)
    tools, placeholders = evaluation.collect_tools({"jobs_provider": "himalayas"})
    assert received == [{"jobs_provider": "himalayas"}]
    functions = {t["function"]["name"]: t["function"] for t in tools}
    assert len(functions) == len(tools)
    assert {"web_search", "fetch_url", "memory_recall", "browser_open"} <= functions.keys()
    # The lean default surface exposes the merged profile façade, not the legacy get/update tools (S4b).
    assert functions["careercoach_profile"]["parameters"]["properties"]["action"]
    assert functions["careercoach_profile"]["description"]
    assert set(placeholders) == {"careercoach_knobs", "careercoach_tune", "careercoach_preset"}
    for fn in functions.values():
        assert fn["parameters"]["type"] == "object"
    assert evaluation.fake_pick("hello", tools) == evaluation.fake_pick("hello", tools)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"content": "No tool needed"}, None),
        ({"tool_calls": []}, None),
        ({"tool_calls": [{"function": {"name": "first"}}, {"function": {"name": "second"}}]}, "first"),
        ({"function_call": {"name": "legacy"}}, "legacy"),
    ],
)
def test_chat_request_and_first_call(message, expected):
    tools = [evaluation.function_tool("first", "Example", {"type": "object", "properties": {}})]

    def respond(request):
        assert str(request.url) == "https://example.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["temperature"] == 0
        assert payload["model"] == "test-model"
        assert payload["tool_choice"] == "auto"
        assert payload["tools"] == tools
        assert payload["messages"][-1] == {"role": "user", "content": "Example prompt"}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert (
            evaluation.model_pick(client, "https://example.com/v1/", "test-key", "test-model", "Example prompt", tools)
            == expected
        )


def test_endpoint_failures_are_not_scored_as_abstentions():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            evaluation.model_pick(client, "https://example.com/v1", "key", "model", "prompt", [])


@pytest.mark.parametrize(
    "base_url",
    ["https://api.openai.com/v1", "https://unlisted.example/v1", "http://localhost:8000/v1"],
)
def test_eval_endpoint_is_independent_of_plugin_manifest(monkeypatch, tmp_path, base_url):
    manifest = yaml.safe_load((evaluation.ROOT / "protoagent.plugin.yaml").read_text())
    assert "api.openai.com" not in manifest["capabilities"]["network"]
    # No manifest exists here: endpoint selection must not read runtime permissions.
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    monkeypatch.setenv("EVAL_API_KEY", "test-secret")
    monkeypatch.setenv("EVAL_MODEL", "test-model")
    monkeypatch.setenv("EVAL_BASE_URL", base_url)
    assert evaluation.endpoint_from_env() == (base_url, "test-secret", "test-model")


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp/model",
        "https:///v1",
        "https://user:secret@example.com/v1",
        "https://example.com/v1?key=secret",
        "https://example.com/v1#fragment",
    ],
)
def test_eval_endpoint_rejects_invalid_urls(monkeypatch, base_url):
    monkeypatch.setenv("EVAL_API_KEY", "test-secret")
    monkeypatch.setenv("EVAL_MODEL", "test-model")
    monkeypatch.setenv("EVAL_BASE_URL", base_url)
    with pytest.raises(ValueError, match="EVAL_BASE_URL"):
        evaluation.endpoint_from_env()


@pytest.mark.parametrize("missing", ["EVAL_BASE_URL", "EVAL_API_KEY", "EVAL_MODEL"])
def test_eval_endpoint_requires_explicit_configuration(monkeypatch, missing):
    monkeypatch.setenv("EVAL_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("EVAL_API_KEY", "test-secret")
    monkeypatch.setenv("EVAL_MODEL", "test-model")
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match="require"):
        evaluation.endpoint_from_env()


def test_dry_run_cli_is_offline_and_writes_reproducible_results(tmp_path):
    # A real CLI subprocess with socket connections AND HTTP requests forbidden.
    # No model env vars, operator store or workspace is needed.
    script = """
import runpy, socket, sys
import httpx
def forbidden(*args, **kwargs):
    raise AssertionError('dry-run attempted network access')
socket.socket.connect = forbidden
socket.create_connection = forbidden
httpx.Client.send = forbidden
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("EVAL_")}
    env["CAREERCOACH_DIR"] = str(tmp_path / "store")
    command = [
        sys.executable,
        "-c",
        script,
        str(Path(evaluation.__file__).resolve()),
        "--dry-run",
        "--label",
        "offline",
        "--config",
        "jobs_provider=himalayas",
        "--config",
        "jobs_api_key=do-not-record",
        "--output-dir",
        str(tmp_path / "results"),
    ]
    first = subprocess.run(command, cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60)
    assert first.returncode == 0, first.stderr
    path = tmp_path / "results" / "offline.json"
    saved = path.read_text()
    result = json.loads(saved)
    assert result["dry_run"] is True
    assert result["model"] == "deterministic-fake"
    assert 40 <= result["overall"]["total"] <= 50
    assert len(result["picks"]) == result["overall"]["total"]
    assert len(result["per_capability"]) == 8
    assert "Accuracy" in first.stdout and "Synthetic dry-run" in first.stdout
    assert "do-not-record" not in saved
    prompts, aliases = evaluation.load_prompts()
    rescored = evaluation.score({p["id"]: p["pick"] for p in result["picks"]}, prompts, aliases)
    assert result["overall"] == rescored["overall"]
    assert result["per_capability"] == rescored["per_capability"]
    second = subprocess.run(command, cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60)
    assert second.returncode == 0, second.stderr
    assert path.read_text() == saved
    assert not (tmp_path / "store").exists()
