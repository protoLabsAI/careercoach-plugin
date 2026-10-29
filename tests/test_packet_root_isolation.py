"""Blank workspace settings must never share another agent's career history."""

import importlib
import logging
from pathlib import Path

import pytest
from _plugin_testkit import FakeRegistry


@pytest.fixture
def packet(plugin, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "agent"))
    return importlib.import_module(plugin.__name__ + ".packet")


def _tools(plugin, configured=""):
    reg = FakeRegistry({"packet_root": configured, "extended_tools": True})
    plugin.register(reg)
    return {t.name: t for t in reg.tools}


@pytest.mark.parametrize("configured", ["", "  "])
def test_blank_root_uses_agent_data_dir(packet, profile, configured):
    root = packet.resolve_root(configured)
    assert root == profile._dir() / "workspace"
    assert root.is_dir()
    assert not root.is_relative_to(Path.home() / "CareerCoach")


def test_two_agents_share_home_but_not_experience(plugin, packet, iso, monkeypatch):
    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "agent-a"))
    a_tools = _tools(plugin)
    a_root = packet.resolve_root("")
    a_tools["careercoach_init_workspace"].invoke({})
    experience = a_root / "Resume" / "Experience.md"
    experience.write_text("# Experience\n\n## Roles\n### Engineer — AGENT A ONLY\n", encoding="utf-8")
    original = experience.read_text()

    monkeypatch.setenv("CAREERCOACH_DIR", str(iso / "agent-b"))
    b_tools = _tools(plugin)
    b_root = packet.resolve_root("")
    assert b_root == iso / "agent-b" / "workspace"
    assert b_root != a_root
    out = b_tools["careercoach_import_experience"].invoke({})
    assert "No Resume/Experience.md" in out and str(b_root) in out
    assert "AGENT A ONLY" not in out
    assert not (iso / "agent-b" / "profile.json").exists()
    out = b_tools["careercoach_init_workspace"].invoke({})
    assert "Seeded your workspace" in out and str(b_root) in out
    assert (b_root / "Resume" / "Experience.md").is_file()
    assert "AGENT A ONLY" not in (b_root / "Resume" / "Experience.md").read_text()
    assert experience.read_text() == original


@pytest.mark.parametrize("marker", ["Resume/Experience.md", "Companies"])
def test_legacy_workspace_warns_once_without_importing(plugin, packet, caplog, marker):
    legacy = Path.home() / "CareerCoach"
    target = legacy / marker
    if marker == "Companies":
        target.mkdir(parents=True)
    else:
        target.parent.mkdir(parents=True)
        target.write_text("# Experience\n\n## Roles\n### Engineer — LEGACY ONLY\n", encoding="utf-8")
    tools = _tools(plugin)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.careercoach"):
        root = packet.resolve_root("")
        assert len(caplog.records) == 1
        packet.resolve_root("")
        out = tools["careercoach_import_experience"].invoke({})
        assert "No Resume/Experience.md" in out and str(root) in out
        assert "LEGACY ONLY" not in out
        assert len(caplog.records) == 1
    assert str(legacy) in caplog.text
    assert "careercoach no longer reads ~/CareerCoach by default; set packet_root: ~/CareerCoach" in caplog.text
    assert not (root / "Resume" / "Experience.md").exists()
    assert not (root.parent / "profile.json").exists()


@pytest.mark.parametrize("kind", ["absolute", "tilde", "relative"])
def test_explicit_root_still_wins_without_warning(packet, iso, caplog, kind):
    (Path.home() / "CareerCoach" / "Companies").mkdir(parents=True)
    configured, expected = {
        "absolute": (str(iso / "custom"), iso / "custom"),
        "tilde": ("~/custom", Path.home() / "custom"),
        "relative": ("custom", Path.home() / "custom"),
    }[kind]
    assert packet.resolve_root(configured) == expected
    assert expected.is_dir()
    assert not caplog.records


@pytest.mark.parametrize("configured", ["", "~/configured", "relative", "/configured"])
@pytest.mark.parametrize("override", ["absolute", "~/override", "override"])
def test_env_overrides_config(packet, iso, monkeypatch, caplog, configured, override):
    (Path.home() / "CareerCoach" / "Companies").mkdir(parents=True)
    value = str(iso / "override") if override == "absolute" else override
    monkeypatch.setenv("CAREERCOACH_PACKET_DIR", value)
    expected = iso / "override" if override == "absolute" else Path.home() / "override"
    assert packet.resolve_root(configured) == expected
    assert not caplog.records


def test_empty_legacy_folder_does_not_warn(packet, caplog):
    (Path.home() / "CareerCoach").mkdir()
    packet.resolve_root("")
    assert not caplog.records


@pytest.mark.parametrize("override", [False, True])
def test_relative_connections_path_uses_packet_resolver(plugin, packet, iso, monkeypatch, override):
    if override:
        monkeypatch.setenv("CAREERCOACH_PACKET_DIR", str(iso / "override"))
    tools = _tools(plugin)
    root = packet.resolve_root("")
    out = tools["careercoach_import_connections"].invoke({"path": "Connections.csv"})
    assert out.startswith(f"No file at {root / 'Connections.csv'}.")
    (root / "Connections.csv").write_text(
        "First Name,Last Name,Email Address,Company,Position,Connected On\n"
        "Ada,Lovelace,ada@example.com,Acme,Engineer,01 Jan 2026\n",
        encoding="utf-8",
    )
    out = tools["careercoach_import_connections"].invoke({"path": "Connections.csv"})
    assert out.startswith("Imported 1 connection(s) across 1 companies (1 new, 0 updated).")


@pytest.mark.parametrize("marker", ["Resume/Experience.md", "Companies"])
def test_upgrade_notice_reaches_tools_after_log_warning(plugin, packet, marker):
    legacy = Path.home() / "CareerCoach"
    target = legacy / marker
    if marker == "Companies":
        target.mkdir(parents=True)
    else:
        target.parent.mkdir(parents=True)
        target.write_text("# Experience\n\n## Roles\n### Engineer — LEGACY ONLY\n", encoding="utf-8")
    root = packet.resolve_root("")  # The process-wide warning has already fired.
    tools = _tools(plugin)
    for name in (
        "careercoach_import_experience",
        "careercoach_list_roles",
        "careercoach_read_profile",
        "careercoach_init_workspace",
        "careercoach_init_workspace",  # Already seeded must still explain the upgrade.
        "careercoach_import_experience",  # Untouched template must also explain it.
    ):
        out = tools[name].invoke({})
        assert "Workspace upgrade notice:" in out
        assert "set packet_root: ~/CareerCoach in Settings → Career Coach" in out
        assert str(legacy) in out
        assert "have not been moved or imported" in out
        assert "LEGACY ONLY" not in out
    assert not (root.parent / "profile.json").exists()
    assert "LEGACY ONLY" not in (root / "Resume" / "Experience.md").read_text()


def test_explicit_legacy_opt_in_restores_access(plugin, packet):
    legacy = Path.home() / "CareerCoach"
    experience = legacy / "Resume" / "Experience.md"
    experience.parent.mkdir(parents=True)
    text = "# Experience\n\n## Roles\n### Engineer — LEGACY ONLY\n"
    experience.write_text(text, encoding="utf-8")
    packet.scaffold_role(legacy, "Acme", "Engineer")
    tools = _tools(plugin, "~/CareerCoach")
    preview = tools["careercoach_import_experience"].invoke({})
    assert "Import preview" in preview and "LEGACY ONLY" in preview
    roles = tools["careercoach_list_roles"].invoke({})
    assert "Engineer @ Acme" in roles
    assert "Workspace upgrade notice:" not in preview + roles
    assert experience.read_text() == text


@pytest.mark.parametrize("explicit", ["config", "env", "none"])
def test_notice_absent_without_legacy_or_with_override(plugin, packet, monkeypatch, iso, explicit):
    if explicit != "none":
        (Path.home() / "CareerCoach" / "Companies").mkdir(parents=True)
    if explicit == "env":
        monkeypatch.setenv("CAREERCOACH_PACKET_DIR", str(iso / "override"))
    tools = _tools(plugin, "~/chosen" if explicit == "config" else "")
    for name in ("careercoach_init_workspace", "careercoach_import_experience", "careercoach_list_roles"):
        assert "Workspace upgrade notice:" not in tools[name].invoke({})
