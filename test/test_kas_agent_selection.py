"""Tests for carrying a Kiro Crew agent onto a KAS session.

KAS has no ``--agent`` flag, so the agent has to be named AND defined in
``session/new``. The failure this guards is silent: KAS binds ``modeId`` only to
an agent already in its registry and ignores an unresolvable name rather than
rejecting it, so selecting without defining yields a completely successful
``session/new`` that runs KAS's own default mode -- with none of the agent's
prompt, tool grants or MCP servers in effect and every log line looking healthy.

Measured against the real KAS that kiro-cli extracts: ``modeId`` alone came back
with ``configOptions.currentValue == "vibe"``; sending the definition alongside it
came back ``"kirocrew"``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp import kas_agent
from kiro_crew.acp._dispatch import build_session_new_params


class TestBackendStaysHiddenWhileUnderTest:
    """The wiring lands; the exposure does not.

    Worth pinning separately because the two are independent: with the agent
    wiring in place it is tempting to widen the selectable set in the same change,
    and that is the line between "ready to test" and "shipped".
    """

    def test_kas_is_not_offered_by_default(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_KAS, selectable_backends

        assert ACP_BACKEND_KAS not in selectable_backends()

    def test_kas_is_listed_in_the_schema_domain(self) -> None:
        """The enum is the JSON-Schema domain, NOT the exposure surface.

        ``validate_config_data`` REMOVES a value outside this enum before the
        loader sees the key, so listing 'kas' is what lets the preview opt-in take
        effect at all — without it the value is stripped, not degraded, and the
        degrade log never even fires. Exposure is controlled by
        ``selectable_backends()`` above, and no settings surface reads this field.
        """
        from kiro_crew.config.loader import AgentConfig

        meta = AgentConfig.__dataclass_fields__["acp_backend"].metadata
        assert "kas" in (meta.get("enum") or [])

    def test_no_settings_surface_reads_the_field(self) -> None:
        """Pins the reason listing 'kas' in the enum exposes nothing.

        If a frontend panel ever starts rendering this field, the enum stops being
        purely a schema domain and this test is the reminder to gate it there too.
        """
        import subprocess

        repo = Path(__file__).resolve().parents[1]
        hits = subprocess.run(
            ["grep", "-rl", "acp_backend", str(repo / "website" / "src")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert hits.stdout.strip() == "", f"frontend now reads acp_backend: {hits.stdout}"

    def test_preview_env_opens_the_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ENV_KAS_PREVIEW, selectable_backends

        monkeypatch.setenv(ENV_KAS_PREVIEW, "1")

        assert ACP_BACKEND_KAS in selectable_backends()

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_preview_value_does_not_open_the_set(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exported-but-empty variable is the shell's idea of unset."""
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ENV_KAS_PREVIEW, selectable_backends

        monkeypatch.setenv(ENV_KAS_PREVIEW, value)

        assert ACP_BACKEND_KAS not in selectable_backends()

    def test_claude_stays_unselectable_either_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate opens KAS specifically, not the dormant claude seam."""
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ENV_KAS_PREVIEW, selectable_backends

        monkeypatch.setenv(ENV_KAS_PREVIEW, "1")

        assert ACP_BACKEND_CLAUDE not in selectable_backends()


class TestSessionNewParams:
    def test_kas_agent_is_selected_and_defined_together(self) -> None:
        """Both keys, one namespace: the definition registers, modeId selects."""
        definition = {"id": "kirocrew", "prompt": "p"}

        params = build_session_new_params("/w", mcp_servers=[], kas_agent=definition)

        assert params["_meta"] == {
            "kiro": {"modeId": "kirocrew", "customAgents": [definition]}
        }

    def test_mode_id_is_taken_from_the_definition_not_a_second_argument(self) -> None:
        """One source for the name, so selection cannot disagree with definition."""
        params = build_session_new_params(
            "/w", mcp_servers=[], kas_agent={"id": "other", "prompt": "p"}
        )

        assert params["_meta"]["kiro"]["modeId"] == "other"

    def test_absent_kas_agent_sends_no_meta(self) -> None:
        """kiro-cli gets its agent from --agent; an empty _meta would be noise."""
        params = build_session_new_params("/w", mcp_servers=[])

        assert "_meta" not in params

    def test_cwd_and_mcp_servers_are_still_always_present(self) -> None:
        """kiro-cli treats a missing mcpServers as malformed and exits rc=0."""
        params = build_session_new_params("/w", kas_agent={"id": "a", "prompt": "p"})

        assert params["cwd"] == "/w"
        assert params["mcpServers"] == []

    def test_claude_meta_wins_over_kas_agent(self) -> None:
        """The two backends are mutually exclusive; one _meta slot, no merging."""
        params = build_session_new_params(
            "/w", mcp_servers=[], claude_meta=True, kas_agent={"id": "a", "prompt": "p"}
        )

        assert params["_meta"] == {"claudeCode": {"options": {}}}


class TestClientCustomAgent:
    def test_id_and_prompt_are_the_required_core(self) -> None:
        out = kas_agent.client_custom_agent("kirocrew", {"prompt": "be helpful"})

        assert out == {"id": "kirocrew", "prompt": "be helpful"}

    def test_star_tools_stay_the_wildcard(self) -> None:
        """KAS accepts "*" or a list; the wildcard must not become ["*"]."""
        out = kas_agent.client_custom_agent("a", {"prompt": "p", "tools": "*"})

        assert out["tools"] == "*"

    def test_tool_list_is_passed_through(self) -> None:
        out = kas_agent.client_custom_agent("a", {"prompt": "p", "tools": ["fs_read"]})

        assert out["tools"] == ["fs_read"]

    def test_empty_tool_list_is_omitted_rather_than_sent(self) -> None:
        """An empty list means "no tools" to KAS, which is not what absence means."""
        out = kas_agent.client_custom_agent("a", {"prompt": "p", "tools": []})

        assert "tools" not in out

    def test_allowed_tools_is_never_forwarded(self) -> None:
        """allowedTools is an approval concern Kiro Crew's own gate owns.

        Forwarding it as tool ACCESS would widen what the agent can reach beyond
        what its `tools` grant says.
        """
        out = kas_agent.client_custom_agent(
            "a", {"prompt": "p", "tools": ["fs_read"], "allowedTools": ["execute_bash"]}
        )

        assert out["tools"] == ["fs_read"]
        assert "allowedTools" not in out

    @pytest.mark.parametrize("key", ["description", "model", "mcpServers", "resources"])
    def test_shared_keys_pass_through(self, key: str) -> None:
        out = kas_agent.client_custom_agent("a", {"prompt": "p", key: {"x": 1}})

        assert out[key] == {"x": 1}

    def test_falsy_optional_values_are_omitted(self) -> None:
        """KAS reads its own defaults for an absent key, not for an empty one."""
        out = kas_agent.client_custom_agent("a", {"prompt": "p", "model": "", "resources": []})

        assert set(out) == {"id", "prompt"}


class TestLoadClientCustomAgent:
    """Reading the config is best-effort: an unusable one degrades, never raises.

    Raising would fail session/new outright and take down a surface over a
    configuration problem the operator can fix without a restart.
    """

    @pytest.fixture(autouse=True)
    def _agents_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        d = tmp_path / "agents"
        d.mkdir()
        monkeypatch.setattr(kas_agent, "kiro_agents_dir", lambda: d)
        return d

    def test_reads_and_shapes_a_config(self, _agents_dir: Path) -> None:
        (_agents_dir / "kirocrew.json").write_text(
            json.dumps({"prompt": "hello", "tools": "*", "description": "d"})
        )

        out = kas_agent.load_client_custom_agent("kirocrew")

        assert out == {"id": "kirocrew", "prompt": "hello", "tools": "*", "description": "d"}

    def test_resolves_a_file_uri_prompt(self, _agents_dir: Path, tmp_path: Path) -> None:
        """KAS rejects a file:// URI here and makes resolution the client's job."""
        prompt_file = tmp_path / "p.md"
        prompt_file.write_text("from a file")
        (_agents_dir / "a.json").write_text(
            json.dumps({"prompt": f"file://{prompt_file}"})
        )

        out = kas_agent.load_client_custom_agent("a")

        assert out is not None
        assert out["prompt"] == "from a file"

    def test_unresolvable_file_uri_degrades(self, _agents_dir: Path) -> None:
        (_agents_dir / "a.json").write_text(json.dumps({"prompt": "file:///nope/x.md"}))

        assert kas_agent.load_client_custom_agent("a") is None

    def test_windows_drive_uri_keeps_its_drive_letter(self, tmp_path: Path) -> None:
        """``file://C:\\...`` puts the drive in urlparse's NETLOC, not the path.

        Reading only ``path`` silently drops ``C:`` so the file is never found —
        which passes on POSIX and fails only on a Windows runner. Asserted on the
        RECONSTRUCTED path rather than a real read, so it exercises the Windows
        spelling from any host.
        """
        from urllib.parse import urlparse

        parsed = urlparse("file://C:/Users/runneradmin/p.md")

        assert parsed.netloc == "C:", "precondition: the drive lands in netloc"
        assert kas_agent._DRIVE_NETLOC_RE.fullmatch(parsed.netloc)
        # path alone loses the drive; the reconstruction must restore it.
        assert not parsed.path.startswith("/C:")
        assert f"/{parsed.netloc}{parsed.path}" == "/C:/Users/runneradmin/p.md"

    def test_a_real_host_is_not_mistaken_for_a_drive(self) -> None:
        """``file://server/share`` is a UNC reference, not a drive letter."""
        from urllib.parse import urlparse

        assert not kas_agent._DRIVE_NETLOC_RE.fullmatch(urlparse("file://server/s").netloc)

    def test_posix_file_uri_is_read(self, tmp_path: Path) -> None:
        target = tmp_path / "p.md"
        target.write_text("posix-form", encoding="utf-8")

        assert kas_agent._read_file_uri(f"file://{target}") == "posix-form"

    def test_missing_config_degrades(self) -> None:
        assert kas_agent.load_client_custom_agent("absent") is None

    def test_malformed_json_degrades(self, _agents_dir: Path) -> None:
        (_agents_dir / "a.json").write_text("{not json")

        assert kas_agent.load_client_custom_agent("a") is None

    def test_non_object_config_degrades(self, _agents_dir: Path) -> None:
        (_agents_dir / "a.json").write_text("[1, 2]")

        assert kas_agent.load_client_custom_agent("a") is None

    @pytest.mark.parametrize("prompt", ["", "   ", None])
    def test_promptless_config_degrades(self, _agents_dir: Path, prompt: object) -> None:
        """KAS requires prompt; sending an empty one would define a mute agent."""
        (_agents_dir / "a.json").write_text(json.dumps({"prompt": prompt, "tools": "*"}))

        assert kas_agent.load_client_custom_agent("a") is None

    @pytest.mark.parametrize("name", ["", "   "])
    def test_blank_agent_name_needs_no_filesystem_hit(self, name: str) -> None:
        assert kas_agent.load_client_custom_agent(name) is None
