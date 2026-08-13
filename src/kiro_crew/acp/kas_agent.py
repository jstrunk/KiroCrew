"""Translate a Kiro Crew agent config into KAS's injected-agent shape.

The KAS backend has no ``--agent`` flag, so what that flag carries for kiro-cli
has to travel in ``session/new`` instead. KAS accepts client-supplied agents in
``_meta.kiro.customAgents``, and those are the HIGHEST-precedence source in its
registry (above ``~/.kiro/agents``, ``.kiro/agents``, bundled and cloud profiles),
so the config is translated on the way out rather than migrated on disk — nothing
is written anywhere and a stale file cannot shadow the live config.

Sending the definition is not optional. KAS binds ``modeId`` only to an agent
already in its registry and **ignores an unresolvable name rather than rejecting
it**, so selecting without defining produces a completely successful
``session/new`` that runs KAS's own default mode. Measured: sending
``modeId: "kirocrew"`` alone came back with ``configOptions.currentValue ==
"vibe"``; sending it together with the definition came back ``"kirocrew"``. Every
log line looks healthy either way, which is what makes the failure worth guarding
in code rather than documenting.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from kiro_crew.config.paths import kiro_agents_dir

logger = logging.getLogger(__name__)

#: Prompt references Kiro Crew may write into an agent config. KAS rejects these
#: in its ``prompt`` field and documents resolution as the client's job, so they
#: are read here rather than forwarded and silently dropped.
_FILE_URI_SCHEME = "file://"

#: A Windows drive letter that ``urlparse`` mistook for a host, e.g. the ``C:`` in
#: ``file://C:\\Users\\...``. Matched rather than assumed so a genuine host in a
#: ``file://host/share`` UNC reference is left alone.
_DRIVE_NETLOC_RE = re.compile(r"[A-Za-z]:")

#: Keys the two formats share, copied when present and non-empty. Absent differs
#: from empty to KAS, so a falsy value is omitted rather than sent.
_PASSTHROUGH_KEYS: tuple[str, ...] = ("description", "model", "mcpServers", "resources")


def _read_file_uri(uri: str) -> str | None:
    """Resolve a ``file://`` prompt reference to its text, or None on failure.

    Handles the Windows spelling as well as the POSIX one. ``urlparse`` reads the
    segment after ``//`` as a HOST, so ``file://C:\\Users\\...`` puts the drive
    letter in ``netloc`` and leaves ``path`` pointing at the rest — dropping the
    drive entirely. Rebuilding from ``netloc + path`` when a drive-shaped netloc is
    present keeps both spellings working; POSIX ``file:///home/...`` has an empty
    netloc and is unaffected.
    """
    parsed = urlparse(uri)
    raw = parsed.path
    if parsed.netloc and _DRIVE_NETLOC_RE.fullmatch(parsed.netloc):
        raw = f"/{parsed.netloc}{parsed.path}"
    try:
        return Path(url2pathname(raw)).read_text(encoding="utf-8")
    except (OSError, ValueError):
        logger.warning("KAS agent: prompt unreadable at %s", uri, exc_info=True)
        return None


def client_custom_agent(agent_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Shape one agent config as a KAS ClientCustomAgent.

    Two fields need real translation rather than a copy:

    ``prompt``
        Must be resolved content; KAS rejects a ``file://`` URI here.
    ``tools``
        ``"*"``-or-list in KAS, while kiro-cli splits the grant across ``tools``
        and ``allowedTools``. Only ``tools`` maps — ``allowedTools`` is an
        approval concern Kiro Crew's own PreToolUse gate owns, and forwarding it
        as tool ACCESS would widen what the agent can actually reach.
    """
    out: dict[str, Any] = {"id": agent_id, "prompt": str(config.get("prompt") or "")}
    tools = config.get("tools")
    if tools == "*" or (isinstance(tools, list) and tools):
        out["tools"] = tools
    for key in _PASSTHROUGH_KEYS:
        value = config.get(key)
        if value:
            out[key] = value
    return out


def load_client_custom_agent(agent: str) -> dict[str, Any] | None:
    """Read *agent*'s config and shape it for KAS, or None when unusable.

    Blocking (filesystem + JSON), so callers run it off the event loop.

    Best-effort by design: returning None leaves KAS on its own default mode,
    which is a degraded session but a working one. Raising would fail
    ``session/new`` outright and take down a surface over a configuration problem
    the operator can fix without a restart.
    """
    name = (agent or "").strip()
    if not name:
        return None
    try:
        raw = json.loads((kiro_agents_dir() / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("KAS agent: config unreadable for %r", name, exc_info=True)
        return None
    if not isinstance(raw, dict):
        logger.warning("KAS agent: config for %r is not an object", name)
        return None
    prompt = raw.get("prompt")
    if isinstance(prompt, str) and prompt.startswith(_FILE_URI_SCHEME):
        resolved = _read_file_uri(prompt)
        if resolved is None:
            return None
        raw["prompt"] = resolved
    if not str(raw.get("prompt") or "").strip():
        logger.warning("KAS agent: config for %r carries no prompt", name)
        return None
    return client_custom_agent(name, raw)
