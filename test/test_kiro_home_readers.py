"""``KIRO_HOME`` moves every reader and writer of kiro-cli's user directory.

The suite's floor pins each formerly import-time ``~/.kiro`` binding to a tmp path
(``conftest_floor_home._SHARED_KIRO_PATHS``). These tests clear those pins back to
their live ``None`` so the accessors fall through to ``kiro_home()``, set
``KIRO_HOME`` to a tmp directory, and plant a decoy under ``Path.home()/.kiro``
that must never be read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent
from kiro_crew import member_essential_context as mec
from kiro_crew.agent_materialization import mcp_sources
from kiro_crew.apps import bridges_mcp
from kiro_crew.config import paths
from kiro_crew.dashboard.handlers import _shared_skill_roots
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.dashboard.handlers import steering


@pytest.fixture
def kiro_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "alt-kiro"
    (home / "settings").mkdir(parents=True)
    monkeypatch.setenv("KIRO_HOME", str(home))
    # Back to live: None is "resolve from kiro_home() at call time".
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", None)
    monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", None)
    monkeypatch.setattr(bridges_mcp, "_LEGACY_SHARED_MCP_PATH", None)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", None)
    monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", None)
    return home.resolve()


@pytest.fixture
def decoy(monkeypatch, tmp_path) -> Path:
    """A host-shaped ``~/.kiro`` whose contents must stay unread and untouched."""
    fake_home = tmp_path / "host-home"
    host_kiro = fake_home / ".kiro"
    (host_kiro / "settings").mkdir(parents=True)
    (host_kiro / "steering").mkdir()
    (host_kiro / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"host-only": {"command": "host"}}})
    )
    (host_kiro / "steering" / "host.md").write_text("HOST STEERING")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return host_kiro


def test_mcp_settings_path_is_resolved_per_call(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "a"))
    assert paths.kiro_mcp_settings_path() == (tmp_path / "a").resolve() / "settings" / "mcp.json"
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "b"))
    assert paths.kiro_mcp_settings_path() == (tmp_path / "b").resolve() / "settings" / "mcp.json"


def test_default_agent_merges_only_the_kiro_home_registry(kiro_home, decoy, monkeypatch):
    (kiro_home / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"alt-only": {"command": "alt"}}})
    )
    monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", lambda: {})
    config: dict = {}
    mcp_sources.merge_mcp_sources(config)
    servers = config.get("mcpServers", {})
    assert "alt-only" in servers
    assert "host-only" not in servers


def test_dashboard_mcp_write_and_lock_land_under_kiro_home(kiro_home, decoy):
    before = (decoy / "settings" / "mcp.json").read_bytes()
    assert mcp_mod._global_mcp_json() == kiro_home / "settings" / "mcp.json"
    assert mcp_mod._mcp_lock_path() == kiro_home / "settings" / "mcp.lock"
    mcp_mod._write_mcp_json({"mcpServers": {"added": {"command": "x"}}})
    written = json.loads((kiro_home / "settings" / "mcp.json").read_text())
    assert written["mcpServers"] == {"added": {"command": "x"}}
    assert (decoy / "settings" / "mcp.json").read_bytes() == before


def test_test_pin_still_wins_over_kiro_home(kiro_home, monkeypatch, tmp_path):
    pinned = tmp_path / "pinned.json"
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", pinned)
    assert mcp_mod._global_mcp_json() == pinned
    assert agent._kiro_mcp_json() == kiro_home / "settings" / "mcp.json"


def test_hooks_skills_and_legacy_scrub_follow_kiro_home(kiro_home, decoy):
    assert agent._kiro_hooks_dir() == kiro_home / "hooks"
    assert bridges_mcp._legacy_shared_mcp_path() == kiro_home / "settings" / "mcp.json"
    assert _shared_skill_roots._canonical_skill_roots()[0] == kiro_home / "skills"


def test_steering_roots_follow_kiro_home(kiro_home, decoy):
    roots = dict(steering.steering_roots())
    assert roots["user"] == kiro_home / "steering"
    assert mec._global_steering_scan() == (kiro_home, "steering/**/*.md")


def test_default_steering_scan_is_unchanged(monkeypatch, decoy):
    monkeypatch.delenv("KIRO_HOME", raising=False)
    assert mec._global_steering_scan() == (Path.home(), ".kiro/steering/**/*.md")


def test_no_project_declared_steering_follows_kiro_home(kiro_home, decoy, monkeypatch, tmp_path):
    (kiro_home / "steering").mkdir()
    (kiro_home / "steering" / "alt.md").write_text("ALT STEERING")
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "kirocrew.json").write_text(
        json.dumps({"name": "kirocrew", "resources": ["file://.kiro/steering/**/*.md"]})
    )
    monkeypatch.setattr(paths, "_agents_dir_override", lambda: agents)
    sources = [source for source, _ in mec.kiro_launch_documents("kirocrew", None)]
    assert str(kiro_home / "steering" / "alt.md") in sources
    assert not any(str(decoy) in source for source in sources)


def test_session_mcp_reads_the_kiro_home_registry(kiro_home, decoy):
    # Reads the override hook through its accessor: the bare hook is None when live.
    from kiro_crew.acp import session_mcp

    (kiro_home / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"alt-only": {"command": "alt"}}})
    )
    assert set(session_mcp._global_settings().get("mcpServers", {})) == {"alt-only"}
    sources = session_mcp.native_settings_sources(None)
    assert [s.path for s in sources][:1] == [kiro_home / "settings" / "mcp.json"]


def test_kas_init_timeout_reads_cli_json_beside_the_kiro_home_registry(kiro_home, decoy):
    # The bare ``agent._KIRO_MCP_JSON`` hook is None when live; the KAS start
    # reads ``cli.json`` beside the accessor's registry, not off the hook.
    from kiro_crew.acp import kas_settings_mcp

    (kiro_home / "settings" / "cli.json").write_text(json.dumps({"mcp.initTimeout": 4321}))
    (decoy / "settings" / "cli.json").write_text(json.dumps({"mcp.initTimeout": 9}))
    assert kas_settings_mcp._init_timeout_ms(None) == 4321
