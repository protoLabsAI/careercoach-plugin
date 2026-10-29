"""Fixed first-tool selection benchmark (never executes selected tools).

Install requirements-dev.txt, then run:
    python evals/tool_selection/run_eval.py --label before
    python evals/tool_selection/run_eval.py --label smoke --dry-run

Live runs use EVAL_BASE_URL (a /v1-style base), EVAL_API_KEY and EVAL_MODEL.
EVAL_BASE_URL explicitly selects the endpoint for this standalone developer CLI.
It is independent of the shipped plugin runtime capabilities.network permissions.
Use --config KEY=VALUE for existing plugin settings; the current full tool surface
is registered by default.
Dry-run picks are synthetic plumbing checks, not model accuracy measurements.
The testkit's name-only host knob stubs have permissive placeholder schemas;
results identify these explicitly. All plugin-owned schemas come from registration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROMPTS_PATH = Path(__file__).with_name("prompts.yaml")
SYSTEM_PROMPT = (
    "You are a career coach. Select the first tool needed for the user's request, "
    "or answer directly when no tool is needed. Read confirmed profile facts before "
    "personalized evaluation or drafting. Never invent career facts."
)


def score(picks, prompts, aliases):
    """Score an id -> tool-name/None mapping without mutating inputs.

    Every prompt must have exactly one recorded pick. Missing observations are errors,
    not successful abstentions. Only the literal expectation 'none' accepts no call;
    aliases extend tool expectations, never general-chat expectations.
    """
    ids = [p["id"] for p in prompts]
    if len(set(ids)) != len(ids) or set(picks) != set(ids):
        raise ValueError("picks must match unique prompt ids exactly")
    rows = []
    groups = {}
    for prompt in prompts:
        pick = picks[prompt["id"]]
        pick = None if pick == "none" else pick
        expect = prompt["expect"]
        correct = (
            pick is None
            if expect == "none"
            else pick is not None and pick in [*expect, *aliases.get(prompt["capability"], [])]
        )
        rows.append({**prompt, "pick": pick, "correct": correct})
        group = groups.setdefault(prompt["capability"], {"correct": 0, "total": 0})
        group["correct"] += int(correct)
        group["total"] += 1
    for group in groups.values():
        group["accuracy"] = group["correct"] / group["total"]
    total = len(rows)
    correct = sum(row["correct"] for row in rows)
    return {
        "picks": rows,
        "overall": {"correct": correct, "total": total, "accuracy": correct / total if total else 0.0},
        "per_capability": groups,
    }


def load_prompts(path=PROMPTS_PATH):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    prompts, aliases = data["prompts"], data["aliases"]
    if not isinstance(aliases, dict) or any(
        not isinstance(names, list) or any(not isinstance(n, str) or not n.strip() for n in names)
        for names in aliases.values()
    ):
        raise ValueError("aliases must map capabilities to lists of tool names")
    for p in prompts:
        if any(not isinstance(p[k], str) or not p[k].strip() for k in ("id", "prompt", "capability")):
            raise ValueError("prompt ids, text and capabilities must be non-empty strings")
        expect = p["expect"]
        if expect != "none" and (
            not isinstance(expect, list) or not expect or any(not isinstance(n, str) or not n.strip() for n in expect)
        ):
            raise ValueError("expect must be 'none' or a non-empty tool-name list")
    if len({p["id"] for p in prompts}) != len(prompts):
        raise ValueError("prompt ids must be unique")
    return prompts, aliases


def function_tool(name, description, parameters):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def collect_tools(config):
    """Register without starting host surfaces or executing any tool."""
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from _plugin_testkit import FakeRegistry, install_host_stubs, load_plugin
    finally:
        sys.path.pop(0)

    install_host_stubs()
    plugin = load_plugin(ROOT, "careercoach")
    registry = FakeRegistry(config=config, plugin_id="careercoach", plugin_dir=ROOT)
    plugin.register(registry)
    tools, placeholders = [], []
    for tool in registry.tools:
        if hasattr(tool, "tool_call_schema"):
            schema = tool.tool_call_schema
            schema = schema if isinstance(schema, dict) else schema.model_json_schema()
            description = tool.description
        else:
            # Vendored make_knob_tools records names only. Do not pretend these
            # schemas were obtained from the host, or silently drop the tools.
            if tool.name not in {"careercoach_knobs", "careercoach_tune", "careercoach_preset"}:
                raise ValueError(f"registered tool has no schema: {tool.name}")
            placeholders.append(tool.name)
            schema = {"type": "object", "properties": {}, "additionalProperties": True}
            description = f"Host rubric control: {tool.name}. Schema unavailable in the host-free testkit."
        tools.append(function_tool(tool.name, description, schema))
    for name, argument, description in (
        ("web_search", "query", "Search the public web for information."),
        ("fetch_url", "url", "Fetch the text of a web page."),
        ("memory_recall", "query", "Recall information from previous conversations."),
        ("browser_open", "url", "Open a URL in the browser."),
    ):
        tools.append(
            function_tool(
                name,
                description,
                {
                    "type": "object",
                    "properties": {argument: {"type": "string"}},
                    "required": [argument],
                },
            )
        )
    return sorted(tools, key=lambda t: t["function"]["name"]), sorted(placeholders)


def fake_pick(prompt, tools):
    """Deterministic and independent of expectations/aliases; deliberately imperfect."""
    choices = [None, *(t["function"]["name"] for t in tools)]
    digest = hashlib.sha256(prompt.encode("utf-8")).digest()
    return choices[int.from_bytes(digest[:8], "big") % len(choices)]


def model_pick(client, base_url, api_key, model, prompt, tools):
    response = client.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "temperature": 0,
            "tool_choice": "auto",
            "tools": tools,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        },
    )
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    if calls:
        return calls[0]["function"]["name"]
    # Older OpenAI-compatible servers may still return function_call.
    if message.get("function_call"):
        return message["function_call"]["name"]
    return None


def endpoint_from_env():
    base_url, api_key, model = (os.environ.get(k, "") for k in ("EVAL_BASE_URL", "EVAL_API_KEY", "EVAL_MODEL"))
    if not all((base_url, api_key, model)):
        raise ValueError("live runs require EVAL_BASE_URL, EVAL_API_KEY and EVAL_MODEL")
    url = urlsplit(base_url)
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise ValueError("EVAL_BASE_URL must be an HTTP(S) URL with a hostname")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("EVAL_BASE_URL must not contain credentials, a query or a fragment")
    return base_url, api_key, model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="dry-run")
    parser.add_argument("--config", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=PROMPTS_PATH.parent / "results")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.label):
        parser.error("label must be a filename-safe identifier")
    config = {}
    for item in args.config:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            parser.error("config must be KEY=VALUE (repeat --config for multiple keys)")
        config[key] = yaml.safe_load(value)
    prompts, aliases = load_prompts()
    tools, placeholders = collect_tools(config)
    if args.dry_run:
        picks = {p["id"]: fake_pick(p["prompt"], tools) for p in prompts}
        model = "deterministic-fake"
    else:
        try:
            base_url, api_key, model = endpoint_from_env()
        except ValueError as exc:
            parser.error(str(exc))
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            picks = {p["id"]: model_pick(client, base_url, api_key, model, p["prompt"], tools) for p in prompts}
    result = score(picks, prompts, aliases)
    result.update(
        {
            "label": args.label,
            "dry_run": args.dry_run,
            "model": model,
            "tools": tools,
            "placeholder_schema_tools": placeholders,
            "aliases": aliases,
            "prompt_set_sha256": hashlib.sha256(PROMPTS_PATH.read_bytes()).hexdigest(),
            "system_prompt": SYSTEM_PROMPT,
        }
    )
    # Config can contain plugin secrets: record only its keys, never values.
    result["config_keys"] = sorted(config)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"{args.label}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"{'Capability':<20} {'Correct':>9} {'Accuracy':>10}")
    for capability, stats in [("overall", result["overall"]), *sorted(result["per_capability"].items())]:
        print(f"{capability:<20} {stats['correct']:>3}/{stats['total']:<5} {stats['accuracy']:>9.1%}")
    print(f"{'Synthetic dry-run' if args.dry_run else 'Model evaluation'} results: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
