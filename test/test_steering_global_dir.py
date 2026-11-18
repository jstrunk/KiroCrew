"""``steering.global_dir``: where global steering is read from.

Unset keeps ``~/.kiro/steering``; a path reads that directory; an empty string
disables global steering. The session loader and the folder-steering dedup read
the same resolved directory, and project steering plus ``AGENTS.md`` still reach
a project-bound chat whatever the global directory is (kirodotdev/KiroCrew#30097).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import folder_steering
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture
def home(tmp_path, monkeypatch):
    from kiro_crew.config import paths as config_paths

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / AGENT_FILENAME).write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    monkeypatch.setattr(config_paths, "_agents_dir_override", lambda: agents)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _configure(monkeypatch, global_dir, home=None):
    # Imported here, not at module level: with the feature reverted the module still
    # collects, so its tests report the missing behaviour instead of an import error.
    from kiro_crew.config.sections import SteeringConfig

    if global_dir == "moved":
        global_dir = str(home / "dotfiles" / "steering")
    cfg = KiroCrewConfig()
    cfg.steering = SteeringConfig(global_dir=global_dir)
    monkeypatch.setattr(folder_steering.KiroCrewConfig, "load", staticmethod(lambda: cfg))


def _doc(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load(project=None) -> str:
    from kiro_crew.context import _load_steering_resources

    return _load_steering_resources(str(project) if project else None)


def test_unset_keeps_the_default_directory(home, monkeypatch):
    _configure(monkeypatch, None)
    _doc(home / ".kiro" / "steering" / "g.md", "DEFAULT-GLOBAL")

    assert folder_steering.global_steering_dir() == home / ".kiro" / "steering"
    assert "DEFAULT-GLOBAL" in _load()


def test_a_path_replaces_the_default_directory(home, monkeypatch):
    moved = home / "dotfiles" / "steering"
    _configure(monkeypatch, "moved", home)
    _doc(home / ".kiro" / "steering" / "g.md", "OLD-GLOBAL")
    _doc(moved / "g.md", "MOVED-GLOBAL")

    assert folder_steering.global_steering_dir() == moved
    loaded = _load()
    assert "MOVED-GLOBAL" in loaded
    assert "OLD-GLOBAL" not in loaded


def test_an_empty_string_disables_global_steering(home, monkeypatch):
    _configure(monkeypatch, "")
    _doc(home / ".kiro" / "steering" / "g.md", "OLD-GLOBAL")

    assert folder_steering.global_steering_dir() is None
    assert _load() == ""


@pytest.mark.parametrize("configured", [None, "moved", ""])
def test_dedup_skips_the_same_directory_the_loader_reads(home, monkeypatch, configured):
    _configure(monkeypatch, configured, home)
    resolved = folder_steering.global_steering_dir()
    roots = folder_steering._double_load_roots(None, home)

    if resolved is None:
        assert roots == ()
    else:
        assert roots == (resolved.resolve(),)


@pytest.mark.parametrize("configured", [None, "moved", ""])
def test_claude_code_still_gets_project_steering_and_agents_md(home, monkeypatch, configured):
    """kirodotdev/KiroCrew#30097: the project half must not depend on the global dir."""
    _configure(monkeypatch, configured, home)
    project = home / "proj"
    _doc(project / ".kiro" / "steering" / "p.md", "PROJECT-STEERING")
    _doc(project / "AGENTS.md", "PROJECT-AGENTS")
    _doc(home / "dotfiles" / "steering" / "g.md", "MOVED-GLOBAL")

    loaded = _load(project)

    assert "PROJECT-STEERING" in loaded
    assert "PROJECT-AGENTS" in loaded
    assert ("MOVED-GLOBAL" in loaded) is (configured == "moved")


def test_kiro_home_does_not_move_global_steering(home, monkeypatch, tmp_path):
    """Unset under a moved ``KIRO_HOME``: the tab, the loaders and the dedup share one tree."""
    from kiro_crew import member_essential_context as mec
    from kiro_crew.dashboard.handlers import steering as steering_tab

    kiro = tmp_path / "moved-kiro"
    _doc(kiro / "steering" / "decoy.md", "KIRO-HOME-DECOY")
    monkeypatch.setenv("KIRO_HOME", str(kiro))
    _configure(monkeypatch, None)
    host = home / ".kiro" / "steering"
    _doc(host / "g.md", "HOST-GLOBAL")

    assert folder_steering.global_steering_dir() == host
    assert dict(steering_tab.steering_roots())["user"] == host
    assert folder_steering._double_load_roots(None, home) == (host.resolve(),)
    assert mec._global_steering_scan() == (home, ".kiro/steering/**/*.md")
    assert list(mec.global_always_steering_documents()) == [str(host / "g.md")]
    loaded = _load()
    assert "HOST-GLOBAL" in loaded
    assert "KIRO-HOME-DECOY" not in loaded


def test_an_empty_global_dir_leaves_the_tab_and_member_scan_empty(home, monkeypatch):
    from kiro_crew import member_essential_context as mec
    from kiro_crew.dashboard.handlers import steering as steering_tab

    _configure(monkeypatch, "")
    _doc(home / ".kiro" / "steering" / "g.md", "OLD-GLOBAL")

    assert "user" not in dict(steering_tab.steering_roots())
    assert mec._global_steering_scan() is None
    assert mec.global_always_steering_documents() == {}
