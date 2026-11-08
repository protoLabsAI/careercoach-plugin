"""The plugin names only the host memory tools that exist on its core floor.

protoAgent 0.231.0 (ADR 0125) split memory from knowledge and renamed the memory tools with no
alias: ``memory_recall`` -> ``memory_search``, ``memory_ingest`` -> ``memory_save``,
``forget_memory`` -> ``memory_forget``, ``memory_stats`` -> ``memory_list``. A skill, prompt or
subagent that still says ``memory_recall`` tells the model to call a tool that isn't there (the
host's subagent shim rewrites tool *lists* at delegation, never skill or prompt text). So the
removed names stay out of every file the plugin ships, and the manifest floor stays at or above
the release that introduced the new names.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REMOVED = ("memory_recall", "memory_ingest", "forget_memory", "memory_stats")
PATTERN = re.compile(r"\b(" + "|".join(REMOVED) + r")\b")
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".html", ".js", ".css", ".txt", ".toml"}
# Directories that aren't shipped plugin text: tests may name the removed tools (this file does),
# and the rest are tooling or caches.
SKIP_DIRS = {".git", "tests", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
FLOOR = (0, 231, 0)


def _shipped_text_files():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.suffix in TEXT_SUFFIXES:
            yield rel, path


def test_the_scan_covers_the_skills_soul_and_plugin_code():
    scanned = {str(rel) for rel, _ in _shipped_text_files()}
    for expected in ("SOUL.md", "__init__.py", "protoagent.plugin.yaml", "skills/job-hunt/SKILL.md"):
        assert expected in scanned


def test_no_shipped_file_names_a_removed_memory_tool():
    hits = []
    for rel, path in _shipped_text_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for match in PATTERN.finditer(line):
                hits.append(f"{rel}:{lineno}: {match.group(1)}")
    assert not hits, "removed host memory tools are still named:\n" + "\n".join(hits)


def test_core_floor_has_the_renamed_memory_tools():
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))
    floor = tuple(int(part) for part in str(manifest["min_protoagent_version"]).split("."))
    assert floor >= FLOOR, f"min_protoagent_version {floor} predates memory_search (0.231.0)"
