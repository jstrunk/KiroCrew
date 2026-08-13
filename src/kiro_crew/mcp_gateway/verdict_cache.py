"""Remember, per machine, what a shareability pre-flight concluded.

Nothing about which MCP servers a machine runs ships with Kiro Crew. Each host
derives its own verdicts and keeps them here, which is why there is no curated
list of server names in the repository and none has to leave the host.

What the key is, and why it is not the server name
--------------------------------------------------
A server's name is chosen by whoever wrote the config, and the same name can be
pointed at a different binary tomorrow. Keying on the name alone would keep
serving a verdict for a server that no longer exists. The key is therefore the
same set of inputs that decides whether two backends are interchangeable —
command+args, effective env, resolved binary version — reusing the very hashes
``PoolKey`` is built from, so "the MCP was upgraded" invalidates the verdict for
free.

``SCHEMA`` is part of the key material as well: a smarter pre-flight must not
inherit conclusions the older one reached with less evidence, so bumping it
invalidates every stored verdict at once.

What is deliberately NOT cached
-------------------------------
Observed hazards. Those live in ``hazards`` and outrank anything here: a cached
"safe" must never be able to rescue a server the gateway has watched misbehave.
Keeping the two stores separate is what makes that ordering impossible to get
wrong by editing one file.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.mcp_gateway.record_json import finite_float

logger = logging.getLogger(__name__)

VERDICT_CACHE_FILENAME = "shareability-verdicts.json"

#: Bump when the pre-flight learns a new check, so older verdicts are re-derived
#: rather than trusted. Part of the entry key, not a top-level file version, so
#: a mixed-version rollout degrades to "re-evaluate" instead of "wipe".
SCHEMA = 1


@dataclass(frozen=True)
class CacheKey:
    """Execution identity of one configured server."""

    server_name: str
    command_args_hash: str
    env_hash: str
    binary_version: str
    schema: int = SCHEMA

    def as_str(self) -> str:
        return "\u0000".join(
            (
                self.server_name,
                self.command_args_hash,
                self.env_hash,
                self.binary_version,
                str(self.schema),
            )
        )


@dataclass(frozen=True)
class CachedPreflight:
    """A stored pre-flight MEASUREMENT — not a final verdict.

    Only the expensive part is cached. The cheap evidence (declared env names,
    advertised capabilities, observed hazards) is re-read on every request and
    recombined by ``shareability.assess``, so a hazard observed a minute ago
    changes the answer immediately without invalidating anything here. Caching
    the composite instead would go stale the moment the ledger grew an entry.

    ``ran`` distinguishes "provoked and found nothing" from "could not provoke
    it", and callers must branch on it before trusting ``caller_sensitive``.
    """

    ran: bool
    caller_sensitive: bool
    reasons: tuple[str, ...]
    evaluated_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "callerSensitive": self.caller_sensitive,
            "reasons": list(self.reasons),
            "evaluatedAt": self.evaluated_at,
        }


class VerdictCache:
    """Verdicts for this host, one JSON object keyed by execution identity."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, CachedPreflight] = {}
        # Servers whose recommendation has already been written into config.
        # Keyed by NAME, deliberately NOT by execution identity: an operator who
        # switches a server off must stay switched off across an upgrade of that
        # server, and an identity-keyed marker would re-flip it the moment the
        # binary version changed.
        self._applied: set[str] = set()
        self._dirty = False

    def load(self) -> None:
        """Read the file. Absence, corruption and a future shape all read empty.

        Reading empty means "nothing evaluated yet", which costs a re-evaluation
        and never a wrong answer. Guessing at an unreadable file's meaning is the
        only outcome that could produce one.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("shareability cache: ignoring unreadable %s: %s", self._path, exc)
            return
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return
        applied = raw.get("applied") if isinstance(raw, dict) else None
        if isinstance(applied, list):
            self._applied = {a for a in applied if isinstance(a, str) and a}
        for key, value in entries.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            ran = value.get("ran")
            if not isinstance(ran, bool):
                # An entry that cannot say whether the pre-flight ran is
                # unusable: treating it as "ran" would fabricate evidence.
                continue
            reasons = value.get("reasons")
            when = value.get("evaluatedAt")
            self._entries[key] = CachedPreflight(
                ran=ran,
                caller_sensitive=value.get("callerSensitive") is True,
                reasons=tuple(r for r in reasons if isinstance(r, str))
                if isinstance(reasons, list)
                else (),
                evaluated_at=finite_float(when),
            )

    def get(self, key: CacheKey) -> CachedPreflight | None:
        return self._entries.get(key.as_str())

    def put(self, key: CacheKey, verdict: CachedPreflight) -> None:
        self._entries[key.as_str()] = verdict
        self._dirty = True

    def prune_to(self, live: set[str]) -> int:
        """Drop entries whose key is not in *live*. Returns how many went.

        Called with the keys of the currently configured servers, so a removed or
        upgraded MCP does not leave its verdict behind for ever. Without this the
        file grows once per config edit and never shrinks.

        Applied markers are deliberately NOT pruned: a server removed and later
        re-added should not be seeded a second time, because the operator may
        have removed it precisely because they did not want it stubbed.
        """
        stale = [k for k in self._entries if k not in live]
        for key in stale:
            del self._entries[key]
        if stale:
            self._dirty = True
        return len(stale)

    def was_applied(self, server_name: str) -> bool:
        """True when this server's recommendation was already written to config.

        The gate that stops a later start from undoing an operator's choice: a
        server is seeded once, and after that the config is theirs.
        """
        return server_name in self._applied

    def mark_applied(self, server_name: str) -> None:
        if server_name and server_name not in self._applied:
            self._applied.add(server_name)
            self._dirty = True

    def server_names(self) -> set[str]:
        """Every server name with at least one stored measurement.

        The key encoding (name first, NUL-joined) is an implementation detail of
        this module, so callers ask here rather than splitting keys themselves.
        """
        return {key.split("\u0000", 1)[0] for key in self._entries}

    def entries_by_name(self, server_name: str) -> list[tuple[str, CachedPreflight]]:
        """Entries whose key names *server_name*, newest measurement first.

        The key is NUL-joined with the name first, so this is a prefix match.
        Exposed because the dashboard row builder knows a name but not the
        command hash; a stale identity is self-correcting on the next probe.
        """
        prefix = server_name + "\u0000"
        found = [(k, v) for k, v in self._entries.items() if k.startswith(prefix)]
        found.sort(key=lambda kv: kv[1].evaluated_at, reverse=True)
        return found

    def flush(self) -> None:
        """Persist when dirty. Blocking IO — keep it off the event loop."""
        if not self._dirty:
            return
        payload = {
            "entries": {k: v.to_json() for k, v in sorted(self._entries.items())},
            "applied": sorted(self._applied),
        }
        try:
            atomic_write(self._path, json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            # A cache we cannot persist costs a re-evaluation next start, so
            # this is a warning and never fatal.
            logger.warning("shareability cache: could not write %s: %s", self._path, exc)
            return
        self._dirty = False

    def __len__(self) -> int:
        return len(self._entries)


def cache_path(runtime_dir: Path) -> Path:
    """Sibling of ``hot-keys.json`` and ``observed-hazards.json``."""
    return runtime_dir / VERDICT_CACHE_FILENAME


def load_cache(runtime_dir: Path) -> VerdictCache:
    cache = VerdictCache(cache_path(runtime_dir))
    cache.load()
    return cache


def now() -> float:
    """Wall clock for ``evaluated_at``. Seam so tests need no monkeypatching."""
    return time.time()
