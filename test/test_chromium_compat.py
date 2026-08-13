"""Tests for the Chromium CUPS compatibility probe (#2894).

On systems with old libcups (e.g. AL2 with CUPS 1.6.3), newer Chromium
revisions (>=1234) crash at load time due to a hard (U) symbol reference to
``ippValidateAttributes`` (added in CUPS 2.5). Older revisions use a weak (w)
reference that gracefully resolves to NULL. The probe finds a compatible binary.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from kiro_crew.browser.setup import (
    _chromium_needs_cups_symbol,
    _find_compatible_chromium,
)


class TestChromiumNeedsCupsSymbol:
    """Unit tests for _chromium_needs_cups_symbol."""

    def test_undefined_symbol_returns_true(self, tmp_path: Path):
        """A binary with U ippValidateAttributes is incompatible."""
        binary = tmp_path / "chrome"
        binary.write_text("")

        nm_output = (
            "                 U ippValidateAttributes\n"
            "                 T main\n"
        )
        with patch(
            "kiro_crew.browser.setup.platform_compat.trusted_system_bin",
            return_value="/usr/bin/nm",
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=nm_output, stderr=""
                )
                assert _chromium_needs_cups_symbol(binary) is True

    def test_weak_symbol_returns_false(self, tmp_path: Path):
        """A binary with w ippValidateAttributes is compatible."""
        binary = tmp_path / "chrome"
        binary.write_text("")

        nm_output = (
            "                 w ippValidateAttributes\n"
            "                 T main\n"
        )
        with patch(
            "kiro_crew.browser.setup.platform_compat.trusted_system_bin",
            return_value="/usr/bin/nm",
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=nm_output, stderr=""
                )
                assert _chromium_needs_cups_symbol(binary) is False

    def test_no_symbol_returns_false(self, tmp_path: Path):
        """A binary without ippValidateAttributes at all is compatible."""
        binary = tmp_path / "chrome"
        binary.write_text("")

        nm_output = "                 T main\n"
        with patch(
            "kiro_crew.browser.setup.platform_compat.trusted_system_bin",
            return_value="/usr/bin/nm",
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=nm_output, stderr=""
                )
                assert _chromium_needs_cups_symbol(binary) is False

    def test_nm_unavailable_returns_false(self, tmp_path: Path):
        """If nm is not in trusted system dirs, assume the binary is fine."""
        binary = tmp_path / "chrome"
        binary.write_text("")

        with patch(
            "kiro_crew.browser.setup.platform_compat.trusted_system_bin",
            return_value=None,
        ):
            assert _chromium_needs_cups_symbol(binary) is False

    def test_nm_timeout_returns_false(self, tmp_path: Path):
        """If nm times out, assume the binary is fine."""
        binary = tmp_path / "chrome"
        binary.write_text("")

        with patch(
            "kiro_crew.browser.setup.platform_compat.trusted_system_bin",
            return_value="/usr/bin/nm",
        ):
            with patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired("nm", 10),
            ):
                assert _chromium_needs_cups_symbol(binary) is False


class TestFindCompatibleChromium:
    """Unit tests for _find_compatible_chromium."""

    def test_returns_none_on_non_linux(self):
        """Non-Linux platforms skip the probe entirely."""
        with patch(
            "kiro_crew.browser.setup.platform.system", return_value="Darwin"
        ):
            assert _find_compatible_chromium() is None

    def test_returns_none_when_no_browser_cache(self, tmp_path: Path):
        """Missing browser cache directory returns None."""
        with patch(
            "kiro_crew.browser.setup.platform.system", return_value="Linux"
        ):
            with patch.dict(
                os.environ,
                {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "nope")},
            ):
                assert _find_compatible_chromium() is None

    def test_returns_none_when_newest_is_compatible(self, tmp_path: Path):
        """When the newest revision is compatible, no fallback needed."""
        rev_dir = tmp_path / "chromium-1237" / "chrome-linux64"
        rev_dir.mkdir(parents=True)
        (rev_dir / "chrome").write_text("")

        with patch(
            "kiro_crew.browser.setup.platform.system", return_value="Linux"
        ):
            with patch.dict(
                os.environ,
                {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path)},
            ):
                with patch(
                    "kiro_crew.browser.setup._chromium_needs_cups_symbol",
                    return_value=False,
                ):
                    assert _find_compatible_chromium() is None

    def test_returns_older_compatible_revision(self, tmp_path: Path):
        """Falls back to older revision when newest is incompatible."""
        new_dir = tmp_path / "chromium-1237" / "chrome-linux64"
        new_dir.mkdir(parents=True)
        (new_dir / "chrome").write_text("")

        old_dir = tmp_path / "chromium-1208" / "chrome-linux64"
        old_dir.mkdir(parents=True)
        (old_dir / "chrome").write_text("")

        def mock_needs_symbol(binary: Path) -> bool:
            return "1237" in str(binary)

        with patch(
            "kiro_crew.browser.setup.platform.system", return_value="Linux"
        ):
            with patch.dict(
                os.environ,
                {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path)},
            ):
                with patch(
                    "kiro_crew.browser.setup._chromium_needs_cups_symbol",
                    side_effect=mock_needs_symbol,
                ):
                    result = _find_compatible_chromium()
                    assert result is not None
                    assert "1208" in result

    def test_returns_none_when_all_incompatible(self, tmp_path: Path):
        """No compatible revision means None (caller uses default channel)."""
        rev_dir = tmp_path / "chromium-1237" / "chrome-linux64"
        rev_dir.mkdir(parents=True)
        (rev_dir / "chrome").write_text("")

        with patch(
            "kiro_crew.browser.setup.platform.system", return_value="Linux"
        ):
            with patch.dict(
                os.environ,
                {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path)},
            ):
                with patch(
                    "kiro_crew.browser.setup._chromium_needs_cups_symbol",
                    return_value=True,
                ):
                    assert _find_compatible_chromium() is None

    def test_uses_home_cache_when_env_unset(self, tmp_path: Path, monkeypatch):
        """Uses ~/.cache/ms-playwright when PLAYWRIGHT_BROWSERS_PATH unset."""
        browsers_dir = tmp_path / ".cache" / "ms-playwright"
        rev_dir = browsers_dir / "chromium-1208" / "chrome-linux64"
        rev_dir.mkdir(parents=True)
        (rev_dir / "chrome").write_text("")

        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        with patch(
            "kiro_crew.browser.setup.platform.system", return_value="Linux"
        ):
            with patch(
                "kiro_crew.browser.setup._chromium_needs_cups_symbol",
                return_value=False,
            ):
                # Newest (only) revision is compatible — no fallback needed
                assert _find_compatible_chromium() is None
