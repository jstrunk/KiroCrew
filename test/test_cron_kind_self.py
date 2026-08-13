"""Tests for perpetual agents: CronSchedule kind='self' + agent_sleep.

RFC rev 3 Phase 1 floor items 1/4/5: the code-owned §7 contract preamble with
the ranking step, agent_sleep's wake-sooner half, and the §9 inheritance
decisions. Each §9 row pinned here is a path that would otherwise silently
kill or distort an agent nobody is watching.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.cron import (
    _AUTO_PAUSE_THRESHOLD,
    _AUTO_PAUSE_THRESHOLD_SELF,
    _MIN_INTERVAL_SECS,
    _SELF_CONTRACT_PREAMBLE,
    CronJob,
    CronSchedule,
    CronService,
    build_cron_session_context,
    compute_next_run_ts,
    format_schedule,
)


def _svc(tmp_path: Path) -> CronService:
    svc = CronService(base_dir=tmp_path)
    svc._load()
    return svc


def _add_self(svc: CronService, name: str = "warden", every: int = 3600) -> CronJob:
    return svc.add_job(name=name, message="pursue the goal", every_secs=every, perpetual=True)


class TestSelfScheduleCreation:
    def test_perpetual_creates_kind_self(self, tmp_path: Path) -> None:
        job = _add_self(_svc(tmp_path))
        assert job.schedule.kind == "self"
        assert job.schedule.every_secs == 3600
        assert job.next_wake_ts is None

    def test_perpetual_requires_every(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="every_secs"):
            _svc(tmp_path).add_job(name="w", message="m", at_ts=time.time() + 60, perpetual=True)

    def test_perpetual_refuses_delete_after_run(self, tmp_path: Path) -> None:
        """§9: one-shot semantics are refused at validation."""
        with pytest.raises(ValueError, match="delete_after_run"):
            _svc(tmp_path).add_job(
                name="w", message="m", every_secs=3600, perpetual=True, delete_after_run=True
            )

    def test_perpetual_forces_strict_schedule_and_persistence(self, tmp_path: Path) -> None:
        """§9: jitter off (strict_schedule), continuity on (persistent_session)."""
        job = _svc(tmp_path).add_job(
            name="w", message="m", every_secs=3600, perpetual=True,
            strict_schedule=False, persistent_session=False,
        )
        assert job.strict_schedule is True
        assert job.persistent_session is True

    def test_round_trips_through_store(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did a thing", "next thing")
        svc2 = _svc(tmp_path)
        got = svc2.list_jobs()[0]
        assert got.schedule.kind == "self"
        assert got.next_wake_ts is not None
        assert "did: did a thing" in got.last_sleep_record

    def test_format_schedule(self) -> None:
        assert "self-scheduled" in format_schedule(CronSchedule(kind="self", every_secs=3600))


class TestSelfNextRun:
    def test_fallback_is_operator_ceiling(self, tmp_path: Path) -> None:
        """No agent choice -> behaves like 'every' (the ceiling)."""
        job = _add_self(_svc(tmp_path))
        job.last_run_ts = 1000.0
        assert compute_next_run_ts(job, now=1100.0) == 1000.0 + 3600

    def test_agent_choice_wins(self, tmp_path: Path) -> None:
        job = _add_self(_svc(tmp_path))
        job.last_run_ts = 1000.0
        job.next_wake_ts = 1600.0
        assert compute_next_run_ts(job, now=1100.0) == 1600.0

    def test_missed_wake_fires_on_recovery(self, tmp_path: Path) -> None:
        """A deadline that passed while the host was down fires NOW, not one
        interval later — the property that made cron the host."""
        job = _add_self(_svc(tmp_path))
        job.next_wake_ts = 1000.0
        assert compute_next_run_ts(job, now=5000.0) == 5000.0


class TestAgentSleep:
    def test_records_wake_and_result(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        t0 = time.time()
        got = svc.record_agent_sleep(job.id, 600, "fixed the flake", "verify at population level")
        assert got.next_wake_ts is not None
        assert t0 + 595 <= got.next_wake_ts <= t0 + 605
        assert "did: fixed the flake" in got.last_sleep_record
        assert "next: verify at population level" in got.last_sleep_record

    def test_wake_sooner_allowed_sleep_longer_clamped(self, tmp_path: Path) -> None:
        """The wake-sooner half: earlier than the ceiling is allowed, past it
        is clamped TO the ceiling (RFC rev 3: sleep-longer is §4 config, not
        agent-chosen distant deadlines)."""
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        t0 = time.time()
        got = svc.record_agent_sleep(job.id, 86400, "idle", "")
        assert got.next_wake_ts is not None
        assert got.next_wake_ts <= t0 + 3600 + 5

    def test_floor_clamped(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        t0 = time.time()
        got = svc.record_agent_sleep(job.id, 1, "quick continue", "")
        assert got.next_wake_ts is not None
        assert got.next_wake_ts >= t0 + _MIN_INTERVAL_SECS - 5

    def test_rejected_for_non_self_jobs(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = svc.add_job(name="plain", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="kind='self'"):
            svc.record_agent_sleep(job.id, 600, "did", "")

    def test_unknown_job(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            _svc(tmp_path).record_agent_sleep("deadbeef", 600, "did", "")


class TestConsumeOnFire:
    def test_consume_clears_matching_deadline(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did", "")
        target = svc.list_jobs()[0]
        assert target.next_wake_ts is not None
        svc._consume_self_wake_locked(target)
        assert target.next_wake_ts is None
        svc2 = _svc(tmp_path)
        assert svc2.list_jobs()[0].next_wake_ts is None

    def test_consume_preserves_newer_choice(self, tmp_path: Path) -> None:
        """An agent_sleep landing during the run must survive the clear."""
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did", "")
        fired = svc.list_jobs()[0]
        stale = CronJob(id=fired.id, name=fired.name, message=fired.message,
                        schedule=fired.schedule)
        stale.next_wake_ts = (fired.next_wake_ts or 0) - 100  # a DIFFERENT value
        svc._consume_self_wake_locked(stale)
        # Disk value differed from what this fire consumed -> preserved.
        assert svc.list_jobs()[0].next_wake_ts is not None


class TestSelfPromptAssembly:
    def test_contract_preamble_prepended(self, tmp_path: Path) -> None:
        job = _add_self(_svc(tmp_path))
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            key, prompt = build_cron_session_context(job)
        assert key == f"cron:{job.id}"
        assert prompt.startswith("[Perpetual agent contract]")
        assert "RANK FIRST" in prompt
        assert prompt.rstrip().endswith("pursue the goal")

    def test_life_and_journal_included_when_present(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        base = tmp_path / "agents" / job.id
        base.mkdir(parents=True)
        (base / "LIFE.md").write_text("## 1. The goal\nKeep CI trustworthy.\n", encoding="utf-8")
        (base / "JOURNAL.md").write_text(
            "\n".join(f"line {i}" for i in range(50)) + "\n", encoding="utf-8"
        )
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "Keep CI trustworthy." in prompt
        assert "line 49" in prompt
        assert "line 0" not in prompt  # tail only

    def test_missing_life_dir_degrades_gracefully(self, tmp_path: Path) -> None:
        job = _add_self(_svc(tmp_path))
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "nope"):
            _, prompt = build_cron_session_context(job)
        assert "[Perpetual agent contract]" in prompt

    def test_life_md_capped(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        base = tmp_path / "agents" / job.id
        base.mkdir(parents=True)
        (base / "LIFE.md").write_text("x" * 50_000, encoding="utf-8")
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "[truncated at cap]" in prompt
        assert len(prompt) < 40_000

    def test_no_idle_punishment_language(self) -> None:
        """§7: the preamble must never claim idle will be refused/punished —
        that instruction is what produces invented work."""
        low = _SELF_CONTRACT_PREAMBLE.lower()
        assert "honest idle is a legitimate outcome" in low
        for banned in ("will be refused", "punish", "must produce"):
            assert banned not in low.replace("never punished", "")

    def test_plain_jobs_unchanged(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = svc.add_job(name="plain", message="hello", every_secs=3600)
        _, prompt = build_cron_session_context(job)
        assert "[Perpetual agent contract]" not in prompt


class TestSelfAutoPause:
    def test_higher_threshold_for_self(self, tmp_path: Path) -> None:
        """§9: a self job survives the ordinary threshold and pauses only at
        the raised one — it must never die quietly on an ordinary bad day."""
        job = _add_self(_svc(tmp_path))
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is False  # survived the ordinary threshold
        for _ in range(_AUTO_PAUSE_THRESHOLD_SELF - _AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is True

    def test_plain_jobs_keep_ordinary_threshold(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = svc.add_job(name="plain", message="m", every_secs=3600)
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is True


class TestSelfIsDue:
    """GPT round-1 F5: _is_due must support kind='self' or nothing ever fires."""

    def test_due_when_agent_deadline_passed(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        job.next_wake_ts = 1000.0
        assert CronService._is_due(job, now=1001.0) is True
        assert CronService._is_due(job, now=999.0) is False

    def test_fallback_ceiling_when_no_choice(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        job.last_run_ts = 1000.0
        assert CronService._is_due(job, now=1000.0 + 3599) is False
        assert CronService._is_due(job, now=1000.0 + 3601) is True


class TestLifeContextSafety:
    """GPT round-1 F1: the job name must not escape the agents directory."""

    @pytest.mark.parametrize("bad", ["/tmp/private", "../outside", "a/../../b"])
    def test_hostile_names_cannot_select_files(self, tmp_path: Path, bad: str) -> None:
        """GPT round-4: the life dir is keyed by generated job.id, so a name
        — hostile or colliding with an existing agent — selects nothing."""
        svc = _svc(tmp_path)
        job = svc.add_job(name=bad, message="m", every_secs=3600, perpetual=True)
        outside = tmp_path / "private"
        outside.mkdir()
        (outside / "LIFE.md").write_text("SECRET", encoding="utf-8")
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "SECRET" not in prompt
        assert "[Perpetual agent contract]" in prompt

    def test_name_collision_cannot_steal_another_agents_life(self, tmp_path: Path) -> None:
        """A job named after an existing agent must NOT read that agent's
        LIFE.md — directories are keyed by id, not name."""
        svc = _svc(tmp_path)
        victim = _add_self(svc, name="warden")
        base = tmp_path / "agents" / victim.id
        base.mkdir(parents=True)
        (base / "LIFE.md").write_text("VICTIM GOAL", encoding="utf-8")
        impostor = _add_self(svc, name="warden")  # same NAME, different id
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(impostor)
        assert "VICTIM GOAL" not in prompt

    def test_reads_are_byte_bounded(self, tmp_path: Path) -> None:
        """A huge journal must not be read whole — only the tail cap."""
        from kiro_crew.cron import _JOURNAL_TAIL_CAP_BYTES, _read_tail_bytes

        big = tmp_path / "JOURNAL.md"
        big.write_text("x" * 5_000_000 + "\nlast line", encoding="utf-8")
        tail = _read_tail_bytes(big, _JOURNAL_TAIL_CAP_BYTES, tmp_path)
        assert tail is not None
        assert len(tail.encode("utf-8")) <= _JOURNAL_TAIL_CAP_BYTES
        assert tail.endswith("last line")


class TestSleepRecordSurvivesTurnMerge:
    """GPT round-1 F4: the sleep record must not be clobbered by the
    turn-completion merge (last_result belongs to the turn)."""

    def test_merge_preserves_sleep_record(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "the real record", "next step")
        # Simulate the gateway's turn-completion merge with a stale in-memory
        # snapshot whose last_result is the turn's own text.
        snapshot = svc.list_jobs()[0]
        snapshot.set_run_result("turn output text")
        svc._merge_job_result(snapshot)
        svc2 = _svc(tmp_path)
        got = svc2.list_jobs()[0]
        assert "the real record" in got.last_sleep_record
        assert got.last_result == "turn output text"

    def test_sleep_record_reaches_next_prompt(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did the thing", "verify it")
        target = svc.list_jobs()[0]
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(target)
        assert "did: did the thing" in prompt
        assert "next: verify it" in prompt


class TestLifeContextSymlinkGuard:
    """GPT round-2: a symlinked LIFE.md must never pull outside bytes into
    the prompt — O_NOFOLLOW at the open plus a realpath containment check."""

    def test_symlinked_life_md_refused(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY BYTES", encoding="utf-8")
        base = tmp_path / "agents" / "warden"
        base.mkdir(parents=True)
        (base / "LIFE.md").symlink_to(secret)
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "PRIVATE KEY BYTES" not in prompt
        assert "[Perpetual agent contract]" in prompt

    def test_symlinked_parent_dir_refused(self, tmp_path: Path) -> None:
        """A symlinked intermediate directory is refused because the walk opens
        every component with O_NOFOLLOW — it is never traversed, rather than
        being traversed and then compared (GPT round-15)."""
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "LIFE.md").write_text("OUTSIDE BYTES", encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / job.id).symlink_to(outside, target_is_directory=True)
        with patch("kiro_crew.cron._agents_dir", return_value=agents):
            _, prompt = build_cron_session_context(job)
        assert "OUTSIDE BYTES" not in prompt

    def test_intermediate_symlink_inside_root_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        """The case a containment COMPARISON cannot catch: the link target is
        itself inside the agents root, so realpath containment passes — only
        refusing to traverse the link at all rejects it. This is what makes the
        guard structural instead of a check that can go stale.

        Requires ``dir_fd`` support: the per-component ``openat`` walk is what
        refuses the traversal, and on a platform without it
        (``_open_inside_nofollow_no_dirfd``, i.e. Windows) this exact case is the
        documented residual rather than a regression — the fallback can only
        compare the resolved path, which lands inside the root here. Confirmed
        on CI: this assertion is the one that fails on the Windows shard.
        """
        if os.open not in os.supports_dir_fd:
            pytest.skip("per-component openat walk requires dir_fd support")
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        agents = tmp_path / "agents"
        victim = agents / "victim"
        victim.mkdir(parents=True)
        (victim / "LIFE.md").write_text("VICTIM GOAL BYTES", encoding="utf-8")
        (agents / job.id).symlink_to(victim, target_is_directory=True)
        with patch("kiro_crew.cron._agents_dir", return_value=agents):
            _, prompt = build_cron_session_context(job)
        assert "VICTIM GOAL BYTES" not in prompt

    def test_no_dirfd_fallback_leaks_inside_root_links_by_design(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin the PLATFORM RESIDUAL so it cannot become an unnoticed hole.

        Without ``dir_fd`` the guard can only compare the resolved path, and an
        inside-root link resolves to an allowed location — so it reads. This
        asserts the limit explicitly rather than leaving the Windows behaviour
        undescribed: if someone later closes it (or a refactor makes POSIX
        silently take this path), this test fails and says so.
        """
        from kiro_crew.cron import _open_inside_nofollow

        monkeypatch.setattr(os, "supports_dir_fd", set())
        agents = tmp_path / "agents"
        victim = agents / "victim"
        victim.mkdir(parents=True)
        (victim / "LIFE.md").write_text("VICTIM", encoding="utf-8")
        (agents / "abc123").symlink_to(victim, target_is_directory=True)
        fd = _open_inside_nofollow(agents / "abc123" / "LIFE.md", agents)
        assert fd is not None, "documented residual: fallback cannot refuse this"
        os.close(fd)

    def test_dotdot_segment_in_the_life_path_is_refused(self, tmp_path: Path) -> None:
        from kiro_crew.cron import _open_inside_nofollow

        agents = tmp_path / "agents"
        (agents / "warden").mkdir(parents=True)
        (agents / "warden" / "LIFE.md").write_text("x", encoding="utf-8")
        assert _open_inside_nofollow(agents / "warden" / ".." / "warden" / "LIFE.md", agents) is None
        assert _open_inside_nofollow(tmp_path / "elsewhere" / "LIFE.md", agents) is None

    def test_regular_files_still_read(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        base = tmp_path / "agents" / job.id
        base.mkdir(parents=True)
        (base / "LIFE.md").write_text("normal goal text", encoding="utf-8")
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "normal goal text" in prompt


class TestRound3Fixes:
    """GPT round-3: FIFO refusal, contention propagation, last_run advance."""

    def test_fifo_life_md_refused(self, tmp_path: Path) -> None:
        """A FIFO at LIFE.md must not hang the open — refused via O_NONBLOCK
        + regular-file check."""
        import os as _os
        import sys

        if sys.platform == "win32":
            pytest.skip("mkfifo is POSIX-only")
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        base = tmp_path / "agents" / job.id
        base.mkdir(parents=True)
        _os.mkfifo(base / "LIFE.md")
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)  # must return, not hang
        assert "[Perpetual agent contract]" in prompt
        assert "[LIFE.md" not in prompt.replace("[LIFE.md truncated", "")

    def test_consume_propagates_store_busy(self, tmp_path: Path) -> None:
        """Contention must propagate — an in-memory-only clear would leave the
        past-due deadline live on disk and refire every tick."""
        from kiro_crew.cron import CronStoreBusy

        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did", "")
        target = svc.list_jobs()[0]
        fired_value = target.next_wake_ts

        class _Busy:
            def __enter__(self):
                raise CronStoreBusy("contended")

            def __exit__(self, *a):
                return False

        with patch.object(svc, "_file_lock", return_value=_Busy()):
            with pytest.raises(CronStoreBusy):
                svc._consume_self_wake_locked(target)
        # In-memory value untouched on the failure path too.
        assert target.next_wake_ts == fired_value

    def test_self_last_run_advances_like_every(self, tmp_path: Path) -> None:
        """A completed self wake must advance last_run_ts so the fallback
        deadline moves forward even when agent_sleep was never called."""
        import asyncio as _aio

        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        job.last_run_ts = None

        async def _noop(_j: CronJob) -> None:
            return None

        svc._on_job = _noop
        svc._job_run_meta[job.id] = (1234.5, "scheduled")
        svc._executing.add(job.id)
        _aio.run(svc._run_job_isolated(job))
        assert job.last_run_ts == 1234.5


class TestSkippedWakeNotFinalized:
    """GPT round-4: a wake skipped on consumption contention must not be
    finalized — no last_run_ts advance, no phantom history row."""

    def test_contention_skip_leaves_run_state_untouched(self, tmp_path: Path) -> None:
        import asyncio as _aio

        from kiro_crew.cron import CronStoreBusy

        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did", "")
        target = svc.list_jobs()[0]
        target.last_run_ts = None
        ran = []

        async def _mark(_j: CronJob) -> None:
            ran.append(True)

        svc._on_job = _mark
        svc._job_run_meta[target.id] = (999.0, "scheduled")
        svc._executing.add(target.id)
        with patch.object(
            svc, "_consume_self_wake_locked", side_effect=CronStoreBusy("busy")
        ):
            _aio.run(svc._run_job_isolated(target))
        assert ran == []  # never executed
        assert target.last_run_ts is None  # not finalized as a run


class TestLifeMdWriteDeny:
    """GPT round-5: LIFE.md is agent-read-only — both tool gates hard-deny
    writes so a perpetual agent cannot self-modify its goal."""

    def test_edit_gate_denies_agents_life_md(self, tmp_path: Path) -> None:
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_write_path

        home = _P.home()
        life = home / ".kiro" / "crew" / "agents" / "abcd1234" / "LIFE.md"
        assert is_sensitive_write_path(str(life)) is True

    def test_edit_gate_allows_journal_md(self) -> None:
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_write_path

        home = _P.home()
        journal = home / ".kiro" / "crew" / "agents" / "abcd1234" / "JOURNAL.md"
        assert is_sensitive_write_path(str(journal)) is False

    def test_bash_gate_catches_life_md_write(self) -> None:
        from kiro_crew.security import _build_sensitive_regex

        rx = _build_sensitive_regex()
        assert rx.search('echo hacked > ~/.kiro/crew/agents/ab12cd34/LIFE.md')
        assert rx.search("tee $HOME/.kiro/crew/agents/x/LIFE.md")
        # JOURNAL.md writes stay allowed.
        assert not rx.search("echo entry >> ~/.kiro/crew/agents/ab12cd34/JOURNAL.md")

    # ── GPT round-6 ──────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # single-dot segment right before the leaf (the reported bypass)
            "echo hacked > ~/.kiro/crew/agents/ab12cd34/./LIFE.md",
            # dot segment inside the crew prefix
            "echo hacked > ~/.kiro/./crew/agents/ab12cd34/LIFE.md",
            # same-level down-up excursion re-entering the id segment
            "echo hacked > ~/.kiro/crew/agents/x/../x/LIFE.md",
            # excursion through the agents dir itself
            "tee $HOME/.kiro/crew/agents/./ab12cd34/LIFE.md",
        ],
    )
    def test_bash_gate_catches_dot_segment_spellings(self, cmd: str) -> None:
        from kiro_crew.security import _build_sensitive_regex

        assert _build_sensitive_regex().search(cmd), cmd

    def test_bash_gate_dot_segments_leave_journal_alone(self) -> None:
        from kiro_crew.security import _build_sensitive_regex

        rx = _build_sensitive_regex()
        assert not rx.search("echo e >> ~/.kiro/crew/agents/ab12cd34/./JOURNAL.md")

    def test_edit_gate_denies_life_md_under_kirocrew_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import is_sensitive_write_path

        crew = tmp_path / "crew-home"
        life = crew / "agents" / "ab12cd34" / "LIFE.md"
        life.parent.mkdir(parents=True)
        life.write_text("goal", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        assert is_sensitive_write_path(str(life)) is True
        # JOURNAL.md in the same env-anchored dir stays writable.
        journal = life.parent / "JOURNAL.md"
        assert is_sensitive_write_path(str(journal)) is False

    def test_bash_gate_catches_life_md_under_kirocrew_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import _build_sensitive_regex

        crew = tmp_path / "crew-home"
        (crew / "agents" / "ab12cd34").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        rx = _build_sensitive_regex()
        assert rx.search(f"echo hacked > {crew}/agents/ab12cd34/LIFE.md")
        assert rx.search(f"echo hacked > {crew}/agents/ab12cd34/./LIFE.md")
        assert not rx.search(f"echo e >> {crew}/agents/ab12cd34/JOURNAL.md")

    def test_full_bash_gate_denies_env_anchored_write_via_normalizer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live gate (regex pass may be process-cached from before the env
        was set) still denies via the normalizer second-pass, because
        ``_is_agent_life_md`` consults ``KIROCREW_HOME`` at call time."""
        from kiro_crew.security import is_sensitive_write_path

        crew = tmp_path / "crew-home"
        life = crew / "agents" / "ab12cd34" / "LIFE.md"
        life.parent.mkdir(parents=True)
        life.write_text("goal", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        # dot-segment spelling resolves to the same guarded file
        dotted = crew / "agents" / "ab12cd34" / "." / "LIFE.md"
        assert is_sensitive_write_path(str(dotted)) is True

    # ── GPT round-7 ──────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # the reported bypass: cd into the agents dir, write relatively
            "cd ~/.kiro/crew/agents/ab12cd34 && echo hacked > LIFE.md",
            # other chained-relative verbs
            "cd $HOME/.kiro/crew/agents/x; cp /tmp/evil LIFE.md",
            "cd ~/.kirocrew/agents/ab12cd34 && tee LIFE.md < /tmp/evil",
            # dot-segment spelling of the cd target
            "cd ~/.kiro/crew/agents/./ab12cd34 && echo hacked > LIFE.md",
        ],
    )
    def test_bash_gate_catches_chained_relative_write(self, cmd: str) -> None:
        # The chain check lives in the gate function, not the compiled
        # alternation (kept out to avoid two whole-command scans per call).
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            r"cmd /c echo hacked > C:\Users\u\.kiro\crew\agents\ab12\LIFE.md",
            r"Set-Content $env:USERPROFILE\.kirocrew\agents\x\LIFE.md evil",
            r"echo hacked > %USERPROFILE%\.kiro\crew\agents\ab12\LIFE.md",
            # native cd + relative write chain
            r"cd C:\Users\u\.kiro\crew\agents\ab12 && echo hacked > LIFE.md",
        ],
    )
    def test_bash_gate_catches_windows_native_spellings(self, cmd: str) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    def test_chained_journal_work_stays_allowed(self) -> None:
        from kiro_crew.security import _build_sensitive_regex

        rx = _build_sensitive_regex()
        assert not rx.search(
            "cd ~/.kiro/crew/agents/ab12cd34 && echo entry >> JOURNAL.md"
        )

    def test_env_anchored_chain_caught_when_regex_built_with_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import (
            _build_sensitive_regex,
            is_sensitive_bash_command,
        )

        crew = tmp_path / "crew-home"
        crew.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        # Rebuild so the env-anchored halves are recomputed for this HOME.
        _build_sensitive_regex()
        cmd = f"cd {crew}/agents/ab12cd34 && echo hacked > LIFE.md"
        assert is_sensitive_bash_command(cmd) is not None

    # ── GPT round-8 ──────────────────────────────────────────────────────

    @pytest.mark.parametrize("leaf", ["life.md", "Life.md", "LIFE.MD"])
    def test_edit_gate_denies_recased_life_md(self, leaf: str) -> None:
        """On case-insensitive filesystems (macOS/Windows) ``life.md`` names
        the same goal file — the guard compares casefolded."""
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_write_path

        home = _P.home()
        life = home / ".kiro" / "crew" / "agents" / "abcd1234" / leaf
        assert is_sensitive_write_path(str(life)) is True

    def test_edit_gate_recased_journal_stays_writable(self) -> None:
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_write_path

        home = _P.home()
        journal = home / ".kiro" / "crew" / "agents" / "abcd1234" / "journal.md"
        assert is_sensitive_write_path(str(journal)) is False

    def test_bash_gate_recased_spelling_matches(self) -> None:
        from kiro_crew.security import _build_sensitive_regex

        # the bash regex already compiles with re.IGNORECASE — pin it
        rx = _build_sensitive_regex()
        assert rx.search("echo hacked > ~/.kiro/crew/agents/ab12cd34/life.md")

    # ── GPT round-9 ──────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # quote-splice inside the leaf name (the reported bypass)
            "echo hacked > ~/.kiro/crew/agents/ab12cd34/LI''FE.md",
            'echo hacked > ~/.kiro/crew/agents/ab12cd34/"LIFE".md',
            'echo hacked > ~/.kiro/crew/agents/ab12cd34/L"IF"E.md',
            "tee ~/.kiro/crew/agents/x1/LI''FE.md < /tmp/x",
            # quote-splice on the chained-relative form
            "cd ~/.kiro/crew/agents/ab12cd34 && echo hacked > LI''FE.md",
            # unquoted forms must keep working
            "echo hacked > ~/.kiro/crew/agents/ab12cd34/LIFE.md",
        ],
    )
    def test_full_bash_gate_denies_quote_spliced_life_md(self, cmd: str) -> None:
        """The FULL gate (regex pass 1 + normalizer pass 2) must deny every
        shell-quoting spelling — the regex cannot see through quotes, so the
        normalizer pass is the layer that has to check the write-protected
        goal file, not only the read-blocked credential set."""
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo entry >> ~/.kiro/crew/agents/ab12cd34/JOURNAL.md",
            "echo entry >> ~/.kiro/crew/agents/ab12cd34/JOUR''NAL.md",
            "ls ~/.kiro/crew/agents",
            "cat README.md",
        ],
    )
    def test_full_bash_gate_leaves_journal_and_reads_alone(self, cmd: str) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is None, cmd

    # ── GPT round-10 ─────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # variable indirection removes the literal entirely
            'cd ~/.kiro/crew/agents/ab12cd34 && n=LIFE; echo hacked > "$n.md"',
            "cd ~/.kiro/crew/agents/ab12cd34 && f=LIFE.md; echo hacked > $f",
            'cd ~/.kiro/crew/agents/ab12cd34 && printf x > "${n}.md"',
            "cd ~/.kiro/crew/agents/ab12cd34 && tee $f < /tmp/evil",
            'echo hacked > ~/.kiro/crew/agents/ab12cd34/"$n".md',
            # command substitution is the same class
            "cd ~/.kiro/crew/agents/ab12cd34 && echo x > `basename LIFE.md`",
        ],
    )
    def test_variable_derived_write_in_agents_dir_is_refused(self, cmd: str) -> None:
        """No static layer can resolve a variable-derived target, so a write
        whose destination is variable-derived is refused when the command
        already names a crew agents directory. Fail closed: the caller can
        always spell the path literally."""
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # variable write targets ELSEWHERE stay allowed — the refusal is
            # scoped to the agents-directory conjunction
            "cd /tmp && f=out.txt; echo x > $f",
            "cd ~/Repos/proj && echo x > $OUT",
            "echo $HOME",
            # literal JOURNAL.md work in the agents dir stays allowed
            "cd ~/.kiro/crew/agents/ab12cd34 && echo entry >> JOURNAL.md",
        ],
    )
    def test_variable_write_scope_is_narrow(self, cmd: str) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is None, cmd


class TestSelfInvariantsSurviveUpdate:
    """GPT round-11: the generic update path must not un-make a perpetual job.

    Each field here silently BREAKS the agent rather than erroring, which is
    why refusal beats coercion: a coerced update would report success for a
    change that did not happen.
    """

    def test_interval_change_keeps_kind_self(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        got = svc.update_job(job.id, every_secs=7200)
        assert got is not None
        assert got.schedule.kind == "self"
        assert got.schedule.every_secs == 7200

    def test_interval_change_leaves_agent_sleep_usable(self, tmp_path: Path) -> None:
        """The regression this guards: after an interval change the job used to
        become kind='every', and agent_sleep then refused it outright."""
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        svc.update_job(job.id, every_secs=7200)
        got = svc.record_agent_sleep(job.id, 600, "did", "next")
        assert got.next_wake_ts is not None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"persistent_session": False},
            {"strict_schedule": False},
            {"delete_after_run": True},
        ],
    )
    def test_invariant_breaking_updates_are_refused(
        self, tmp_path: Path, kwargs: dict
    ) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        with pytest.raises(ValueError):
            svc.update_job(job.id, **kwargs)

    def test_refused_update_leaves_the_job_untouched(self, tmp_path: Path) -> None:
        """Validation runs before ANY field assignment, so a rejected update
        must not have applied the fields that came with it."""
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        with pytest.raises(ValueError):
            svc.update_job(job.id, name="renamed", persistent_session=False)
        got = svc.get_job(job.id)
        assert got is not None
        assert got.name == job.name
        assert got.persistent_session is True

    def test_enabling_the_invariants_is_still_allowed(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        got = svc.update_job(job.id, persistent_session=True, strict_schedule=True)
        assert got is not None
        assert got.persistent_session is True
        assert got.strict_schedule is True

    def test_plain_jobs_keep_generic_update_semantics(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = svc.add_job(name="plain", message="m", every_secs=3600)
        got = svc.update_job(job.id, every_secs=7200, persistent_session=False)
        assert got is not None
        assert got.schedule.kind == "every"
        assert got.persistent_session is False

    # ── GPT round-12 ─────────────────────────────────────────────────────

    def test_cron_expr_update_is_refused_on_self_job(self, tmp_path: Path) -> None:
        """The other route that converted the job away from kind='self'."""
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        with pytest.raises(ValueError):
            svc.update_job(job.id, cron_expr="0 9 * * MON-FRI")
        got = svc.get_job(job.id)
        assert got is not None
        assert got.schedule.kind == "self"

    def test_at_ts_update_is_refused_on_self_job(self, tmp_path: Path) -> None:
        """Refused LOUDLY rather than left as a silent no-op: this path ignores
        at_ts (no branch applies it), so update_job reported success while
        changing nothing — the same "operator believes the cadence moved"
        failure shape as a silent conversion."""
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        with pytest.raises(ValueError):
            svc.update_job(job.id, at_ts=time.time() + 9999)
        got = svc.get_job(job.id)
        assert got is not None
        assert got.schedule.kind == "self"
        assert got.schedule.every_secs == 3600

    def test_at_ts_still_works_on_plain_jobs(self, tmp_path: Path) -> None:
        """The refusal is scoped to perpetual jobs — a plain job keeps whatever
        generic semantics this path already had for at_ts."""
        svc = _svc(tmp_path)
        job = svc.add_job(name="plain", message="m", every_secs=3600)
        got = svc.update_job(job.id, at_ts=time.time() + 9999)
        assert got is not None

    def test_lowering_the_ceiling_clamps_a_stale_deadline(self, tmp_path: Path) -> None:
        """_is_due honours next_wake_ts first, so a deadline recorded under the
        OLD ceiling would keep the lowered ceiling from taking effect."""
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        svc.record_agent_sleep(job.id, 3500, "did", "next")
        before = svc.get_job(job.id)
        assert before is not None and before.next_wake_ts is not None
        base = before.last_run_ts or before.created_ts

        got = svc.update_job(job.id, every_secs=600)
        assert got is not None
        assert got.next_wake_ts is not None
        assert got.next_wake_ts <= base + 600 + 1

    def test_lowering_the_ceiling_keeps_an_earlier_deadline(self, tmp_path: Path) -> None:
        """The wake-sooner half must survive an unrelated ceiling change: a
        deadline already EARLIER than the new ceiling is left alone."""
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        svc.record_agent_sleep(job.id, 120, "did", "next")
        before = svc.get_job(job.id)
        assert before is not None and before.next_wake_ts is not None
        chosen = before.next_wake_ts

        got = svc.update_job(job.id, every_secs=600)
        assert got is not None
        assert got.next_wake_ts == chosen

    def test_raising_the_ceiling_leaves_the_deadline_alone(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        svc.record_agent_sleep(job.id, 1800, "did", "next")
        before = svc.get_job(job.id)
        assert before is not None
        chosen = before.next_wake_ts

        got = svc.update_job(job.id, every_secs=7200)
        assert got is not None
        assert got.next_wake_ts == chosen


class TestCronsStoreWriteProtected:
    """GPT round-11: the scheduler store is an input to an authorization
    decision — every scheduling invariant is enforced at the tool boundary and
    then persisted there, so an agent tool that could rewrite it authors a job
    the tools would have refused."""

    def test_edit_gate_denies_store_write(self) -> None:
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path(str(_P.home() / ".kiro" / "crew" / "crons.json")) is True

    def test_store_stays_readable(self) -> None:
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_path

        # write-protected, NOT read-blocked: `cron list` and the dashboard
        # render it constantly and it holds no secret.
        assert is_sensitive_path(str(_P.home() / ".kiro" / "crew" / "crons.json")) is False

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo x > ~/.kiro/crew/crons.json",
            "tee $HOME/.kiro/crew/crons.json < /tmp/evil",
            "cp /tmp/evil ~/.kirocrew/crons.json",
        ],
    )
    def test_bash_gate_denies_store_write(self, cmd: str) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    # ── GPT round-12 ─────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # dot segment: the write-protected-leaf regex branch carries no
            # dot-segment tolerance, so the normalizer pass is what has to
            # catch these
            "echo x > ~/.kiro/crew/./crons.json",
            "echo x > ~/.kiro/./crew/crons.json",
            "tee ~/.kiro/crew/x/../crons.json < /tmp/evil",
            # quote splice
            "echo x > ~/.kiro/crew/crons''.json",
            # the same normalized coverage must hold for the OTHER
            # write-protected leaves, not just the store
            "echo x > ~/.kiro/crew/./config.json",
            "echo x > ~/.kiro/crew/apps/ops-mission-control/data/./rotation.yaml",
        ],
    )
    def test_normalized_spellings_of_write_protected_paths_are_denied(
        self, cmd: str
    ) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # unrelated writes elsewhere are untouched
            "echo x > /tmp/out.json",
            "cd /tmp && f=out.json; echo x > $f",
        ],
    )
    def test_write_protection_does_not_block_unrelated_writes(self, cmd: str) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is None, cmd

    def test_shell_reads_of_the_store_are_blocked_like_the_other_leaves(self) -> None:
        """Documented posture of _WRITE_PROTECTED_BASH_LEAVES: that branch is
        matched verb-INDEPENDENTLY so no write form can bypass it, which blocks
        shell READS of those leaves too. Harmless for this file (it holds no
        secret) and consistent with .data-home-ready / rotation.yaml: legitimate
        readers use the CLI or the read tool, not shell cat."""
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command("cat ~/.kiro/crew/crons.json") is not None

    def test_python_api_reads_are_unaffected(self) -> None:
        """is_sensitive_path is the READ gate — the store stays outside it, so
        the CLI, the dashboard and the read tool are unaffected."""
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_path

        assert is_sensitive_path(str(_P.home() / ".kiro" / "crew" / "crons.json")) is False

    # ── GPT round-13 ─────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            "cd ~/.kiro/crew && printf '{}' > crons.json",
            "cd ~/.kiro/crew; echo x >> crons.json",
            "cd ~/.kirocrew && tee crons.json < /tmp/evil",
            "cd $HOME/.kiro/crew && cp /tmp/evil crons.json",
            "cd ~/.kiro/crew/apps/ops-mission-control/data && echo x > rotation.yaml",
            "cd ~/.kiro/crew && echo x > .data-home-ready",
        ],
    )
    def test_chained_relative_write_to_fenced_leaf_is_denied(self, cmd: str) -> None:
        """After a chained ``cd`` the leaf is a bare relative token, so neither
        a path-anchored branch nor the normalizer's resolved-path check can bind
        it to the crew home — the normalizer resolves against the agent's cwd,
        not against a ``cd`` earlier on the same line."""
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # reads in the crew home keep the posture their own branch defines
            "cd ~/.kiro/crew && cat config.json",
            "cd ~/.kiro/crew && ls -la",
            "cat ~/.kiro/crew/config.json",
            "sqlite3 ~/.kiro/crew/sessions.db .tables",
            # a same-named file elsewhere, and non-fenced writes in the crew home
            "cd /tmp && echo x > crons.json",
            "cd ~/.kiro/crew && echo x > /tmp/out.txt",
            "cd ~/Repos/proj && echo x > notes.md",
        ],
    )
    def test_chained_relative_check_stays_narrow(self, cmd: str) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is None, cmd

    def test_write_context_ignores_fd_duplication_and_input_redirects(self) -> None:
        """``2>&1`` redirects no file and ``<`` is a read, so neither may count
        as write context and turn a read into a refusal."""
        from kiro_crew.security import _command_has_write_context

        assert _command_has_write_context("cd ~/.kiro/crew && cat crons.json 2>&1") is False
        assert _command_has_write_context("wc -l < crons.json") is False
        assert _command_has_write_context("echo x > crons.json") is True
        assert _command_has_write_context("tee crons.json") is True

    # ── GPT round-14 ─────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # shell terminators immediately after the leaf
            'echo "{}" > ~/.kiro/crew/crons.json;',
            'echo "{}" > ~/.kiro/crew/crons.json|tee /tmp/x',
            '(echo "{}" > ~/.kiro/crew/crons.json)',
            "echo x > ~/.kiro/crew/crons.json&",
            # the PRE-EXISTING fences carried the same gap
            "echo x > ~/.kiro/crew/.data-home-ready;",
            "echo x > ~/.kiro/crew/agents/ab12cd34/LIFE.md;",
            # and the chained-relative half
            "cd ~/.kiro/crew && printf '{}' > crons.json;",
            "cd ~/.kiro/crew && printf '{}' > crons.json)",
        ],
    )
    def test_shell_terminators_do_not_end_the_fence(self, cmd: str) -> None:
        """A path token can END at a shell control character, so the trailing
        boundary has to admit them — otherwise a single ``;`` walks past every
        branch that anchors on the leaf."""
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    def test_credential_paths_get_the_same_boundary(self) -> None:
        """The boundary is shared, so the credential fences gain the fix too."""
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command("cat ~/.aws/credentials;") is not None
        assert is_sensitive_bash_command("cat ~/.ssh/id_rsa|base64") is not None

    # ── GPT round-15 ─────────────────────────────────────────────────────

    @pytest.mark.parametrize("ws", ["\t", "\n", "\x0b", "  "])
    def test_write_verbs_are_recognised_after_any_shell_whitespace(self, ws: str) -> None:
        """A shell word separator is not just U+0020, so the write-context test
        must not key on the space character."""
        from kiro_crew.security import _command_has_write_context

        assert _command_has_write_context(f"tee{ws}crons.json") is True

    def test_tab_separated_write_reaches_the_store_fence(self) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command("cd ~/.kiro/crew && tee\tcrons.json") is not None

    # ── GPT round-16 ─────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # a redirect attached with no space ends the path token too
            "echo x > ~/.kiro/crew/crons.json>/tmp/out",
            "echo x > ~/.kiro/crew/crons.json<in",
            "echo x > ~/.kiro/crew/agents/ab12/LIFE.md>/tmp/out",
            # shared boundary, so the credential fences get it as well
            "cat ~/.aws/credentials>/tmp/leak",
        ],
    )
    def test_attached_redirect_does_not_end_the_fence(self, cmd: str) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            'cd ~/.kiro/crew && f=crons; : > "$f.json"',
            'cd ~/.kiro/crew && n=data-home-ready; echo x > ".$n"',
            "cd ~/.kirocrew && tee $f",
        ],
    )
    def test_variable_composed_leaf_in_crew_home_is_refused(self, cmd: str) -> None:
        """Same answer as the agents-directory case, through the same matcher:
        a variable-derived write target cannot be resolved statically, so inside
        a command that already names the crew home it is refused."""
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "cd /tmp && f=out; echo x > $f.json",
            "echo x > /tmp/a.json>/tmp/b",
            "cd ~/.kiro/crew && cat config.json",
        ],
    )
    def test_round16_additions_stay_narrow(self, cmd: str) -> None:
        from kiro_crew.security import is_sensitive_bash_command

        assert is_sensitive_bash_command(cmd) is None, cmd


class TestAgentSleepGovernance:
    """GPT round-10: writing next_wake_ts IS a cron mutation, so the
    capabilities.cron gate applies to agent_sleep as it does to cron_add.

    These go through the real ``_call_tool_inner``, which builds its OWN
    ``CronService(base_dir=config_dir())`` — so the job has to be created in
    that same store (conftest pins ``KIROCREW_HOME`` per test), not in a
    separately-constructed service.
    """

    @staticmethod
    def _handler_svc() -> Any:
        from kiro_crew.config.loader import config_dir
        from kiro_crew.cron import CronService

        return CronService(base_dir=config_dir())

    def test_denied_capability_refuses_and_leaves_deadline_untouched(
        self, tmp_path: Path
    ) -> None:
        import kiro_crew.mcp_cron as mc

        svc = self._handler_svc()
        job = _add_self(svc, every=3600)
        assert job.next_wake_ts is None

        with (
            patch.object(mc, "_resolve_session_key_strict", return_value=f"cron:{job.id}"),
            patch.object(
                mc,
                "_vet_cron_capability_governance",
                return_value="Error: cron scheduling blocked by governance policy: off",
            ),
        ):
            out = mc._call_tool_inner(
                "agent_sleep", {"next_wake_secs": 600, "did": "x", "next_intent": "y"}
            )

        assert "governance policy" in out, out
        # The refused call must not have persisted a deadline.
        assert self._handler_svc().get_job(job.id).next_wake_ts is None

    def test_permitted_capability_records_normally(self, tmp_path: Path) -> None:
        import kiro_crew.mcp_cron as mc

        svc = self._handler_svc()
        job = _add_self(svc, every=3600)

        with (
            patch.object(mc, "_resolve_session_key_strict", return_value=f"cron:{job.id}"),
            patch.object(mc, "_vet_cron_capability_governance", return_value=None),
        ):
            out = mc._call_tool_inner(
                "agent_sleep", {"next_wake_secs": 600, "did": "x", "next_intent": "y"}
            )

        assert "Recorded" in out, out
        assert self._handler_svc().get_job(job.id).next_wake_ts is not None

    def test_gate_is_keyed_to_the_calling_job(self, tmp_path: Path) -> None:
        """The SEL deny trail must name the job, not the generic vetting key."""
        import kiro_crew.mcp_cron as mc

        svc = self._handler_svc()
        job = _add_self(svc, every=3600)
        seen: list[str] = []

        def _spy(session_key: str | None = None) -> str | None:
            seen.append(session_key or "")
            return None

        with (
            patch.object(mc, "_resolve_session_key_strict", return_value=f"cron:{job.id}"),
            patch.object(mc, "_vet_cron_capability_governance", _spy),
        ):
            mc._call_tool_inner("agent_sleep", {"next_wake_secs": 600})

        assert seen == [f"cron:{job.id}"], seen


class TestPerpetualCreationAllowlist:
    """GPT round-5: perpetual creation is a POSITIVE operator allowlist —
    empty and automation identities are refused, not just cron:."""

    @pytest.mark.parametrize(
        "caller,allowed",
        [
            ("dashboard:abc123", True),
            ("slack:C1:169.1", True),
            # GPT round-7: every human messaging channel is an operator
            # surface, via the shared is_channel_session_key predicate.
            ("webex:room1", True),
            ("wecom:u1", True),
            ("teams:conv1", True),
            ("weixin:u1", True),
            ("whatsapp:u1", True),
            ("unified:kirocrew:dm:u1", True),
            ("discord:guild1:chan1", True),
            ("telegram:chat1", True),
            # legacy un-namespaced Slack thread_ts
            ("1785370133.085469", True),
            # automation identities stay refused
            ("cron:ab12cd34", False),
            ("subagent:xyz", False),
            ("webhook:h1", False),
            ("heartbeat:h1", False),
            ("taskrunner:t1", False),
            ("", False),
        ],
    )
    def test_caller_gating(self, tmp_path: Path, caller: str, allowed: bool) -> None:
        import kiro_crew.mcp_cron as mc

        svc = _svc(tmp_path)
        args = {
            "name": "w",
            "message": "goal",
            "every": 3600,
            "perpetual": True,
        }
        with (
            patch.object(mc, "_resolve_session_key_strict", return_value=caller),
            patch.object(mc, "_resolve_session_key", return_value=caller or "x"),
            patch.object(mc, "get_service", return_value=svc, create=True),
        ):
            # Route through the real handler if its service accessor matches;
            # otherwise call the inner tool with the svc patched in place.
            with patch.object(mc, "svc", svc, create=True):
                out = mc._call_tool_inner("cron_add", dict(args))
        if allowed:
            assert "Added job" in out, out
        else:
            assert "operator session" in out, out
