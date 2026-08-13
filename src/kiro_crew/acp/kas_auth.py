"""Host side of KAS's ``_kiro/auth/getAccessToken`` callback.

A KAS spawn cannot resolve Kiro credentials on its own, and pointing it at a token
file does not work in general: credential resolution is stateful logic that lives
in kiro-cli, not a file KAS could read. Its hidden ``chat _ get-kas-token``
subcommand

* takes a CROSS-PROCESS refresh lock, so concurrent hosts cannot race,
* picks the highest-priority cached identity (External -> Builder -> Social),
* refreshes it over OIDC when it has crossed expiry,
* reports which auth method and sign-in provider produced it.

None of that is reproducible by reading a path, and the default cache location
holds only the Social variant — so on a host signed in any other way KAS's own
``FileAuthProvider`` correctly finds nothing. The absent file is the right answer,
not a misconfiguration.

Kiro Crew therefore does what kiro-cli's own ACP host does: shell out for an
ACCESS token each time KAS asks. The refresh token stays with kiro-cli and is
never seen here, so this module moves a short-lived credential and stores none.

Two forwarded fields are easy to drop and both change behaviour when missing:

``authMethod``
    Selects KAS's upstream ``TokenType`` header. Absent for Builder ID / IdC /
    Social, where the subcommand deliberately sends nothing.
``provider``
    Lets KAS's governance service decide enterprise status. Without it KAS infers
    from ``profileArn`` presence and misclassifies Builder ID and social sign-ins
    as enterprise, which FAIL-CLOSES governance for them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Agent -> client extension method KAS calls. Must match the name KAS sends.
GET_ACCESS_TOKEN_METHOD = "_kiro/auth/getAccessToken"

#: Hidden kiro-cli subcommand that owns credential resolution and refresh.
GET_KAS_TOKEN_ARGV: tuple[str, ...] = ("chat", "_", "get-kas-token")

#: Envelope kind the subcommand emits on success.
_OK_KIND = "getKasToken"

#: One user-facing string for every failure path. A token failure has many causes,
#: none of them actionable in a chat surface, and the detail can name paths and
#: account identifiers. Diagnostics go to the log instead.
AUTH_ERROR_USER_FACING = "Failed to verify authentication. Please log in again to continue."

#: Matches kiro-cli's own budget for this subcommand. It can perform an OIDC round
#: trip, so it is not instant, and KAS holds a turn open while it waits.
GET_TOKEN_TIMEOUT_SECS = 30.0

#: Forwarded verbatim when present, omitted when not: KAS distinguishes an absent
#: key from an empty one for both of these.
_OPTIONAL_FIELDS: tuple[str, ...] = ("authMethod", "provider")


class KasTokenError(Exception):
    """No access token could be obtained. Carries only the user-facing string."""


def parse_last_json_line(stdout: str) -> dict[str, Any]:
    """Return the LAST JSON object printed on stdout.

    The subcommand may emit progress lines before its result, so the envelope is
    the last parseable line rather than the whole stream. Mirrors kiro-cli's own
    ``parseLastJsonLine`` so both hosts tolerate the same output shape.
    """
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise KasTokenError(AUTH_ERROR_USER_FACING)


def response_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Convert the subcommand's envelope into KAS's GetAccessTokenResponse.

    Validated here rather than forwarded partially: KAS rejects a response without
    ``accessToken`` or with an unparseable ``expiresAt``, and its refusal surfaces
    as an opaque protocol error far from this cause.
    """
    kind = envelope.get("kind")
    if kind == "error":
        logger.warning("KAS auth: error envelope from kiro-cli: %s", envelope.get("data"))
        raise KasTokenError(AUTH_ERROR_USER_FACING)
    if kind != _OK_KIND:
        logger.error("KAS auth: unexpected envelope kind %r", kind)
        raise KasTokenError(AUTH_ERROR_USER_FACING)

    data = envelope.get("data")
    if not isinstance(data, dict):
        logger.error("KAS auth: envelope carried no data object")
        raise KasTokenError(AUTH_ERROR_USER_FACING)

    access_token = data.get("accessToken")
    expires_at = data.get("expiresAt")
    if not access_token or not expires_at:
        # Presence booleans only, and named so no credential word appears in a log
        # line: the SAST logger-credential-disclosure rule matches on the field
        # NAME, and the presence check is the whole diagnosis needed anyway.
        logger.error(
            "KAS auth: payload incomplete (has_secret=%s, has_expiry=%s)",
            bool(access_token),
            bool(expires_at),
        )
        raise KasTokenError(AUTH_ERROR_USER_FACING)

    response: dict[str, Any] = {
        "accessToken": access_token,
        "expiresAt": expires_at,
        "profileArn": data.get("profileArn"),
    }
    for field in _OPTIONAL_FIELDS:
        value = data.get(field)
        if value:
            response[field] = value
    logger.debug(
        "KAS auth: credential acquired (expiry=%s, has_profile=%s)",
        expires_at,
        bool(response["profileArn"]),
    )
    return response


async def fetch_access_token(kiro_bin: str) -> dict[str, Any]:
    """Ask kiro-cli for an access token and shape it for KAS.

    Raises ``KasTokenError`` for every failure, so callers have one thing to catch.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            kiro_bin,
            *GET_KAS_TOKEN_ARGV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.warning("KAS auth: could not spawn %s", kiro_bin, exc_info=True)
        raise KasTokenError(AUTH_ERROR_USER_FACING) from None

    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=GET_TOKEN_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        # Leaving the child running would hold the cross-process refresh lock and
        # wedge every other host waiting on it.
        proc.kill()
        await proc.wait()
        logger.warning("KAS auth: token subcommand timed out")
        raise KasTokenError(AUTH_ERROR_USER_FACING) from None

    return response_from_envelope(
        parse_last_json_line(stdout.decode("utf-8", errors="replace"))
    )
