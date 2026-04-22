"""Role-filtered RBAC scope introspection via plainsight-api GET /rbac/scopes.

Used by request_scoped_token to validate elicitation against the authoritative
scope set rather than a hand-rolled list that drifts from policies.csv.
"""

from __future__ import annotations

import difflib
from typing import Any

import httpx
from fastmcp.server.context import Context

GRANTABLE_SCOPES_KEY = "rbac_grantable_scopes"


class ScopesUnavailable(RuntimeError):
    """Raised when GET /rbac/scopes fails. Elicitation refuses to proceed
    rather than silently falling back to stale data (DT-134 acceptance)."""

    def __init__(self, status: int | None, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"/rbac/scopes unavailable (status={status}): {detail}")


async def fetch_grantable_scopes(client: httpx.AsyncClient) -> list[str]:
    """GET /rbac/scopes; return the caller's grantable scope-value list.

    Raises ScopesUnavailable on any non-2xx response, transport error, or
    malformed body.
    """
    try:
        resp = await client.get("/rbac/scopes")
    except httpx.HTTPError as e:
        raise ScopesUnavailable(None, f"transport error: {e}") from e

    if resp.status_code // 100 != 2:
        detail = resp.text[:500] if resp.text else "(empty body)"
        raise ScopesUnavailable(resp.status_code, detail)

    try:
        body = resp.json()
    except ValueError as e:
        raise ScopesUnavailable(resp.status_code, f"non-JSON body: {e}") from e

    scopes = body.get("scopes") if isinstance(body, dict) else None
    if not isinstance(scopes, list):
        raise ScopesUnavailable(resp.status_code, f"missing 'scopes' list in body: {body!r}")

    values: list[str] = []
    for entry in scopes:
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            raise ScopesUnavailable(resp.status_code, f"malformed scope entry: {entry!r}")
        values.append(entry["value"])
    return values


def is_scope_granted(requested: str, grantable: set[str]) -> bool:
    """Does `grantable` cover `requested`?

    - '*:*' in grantable covers any well-formed request.
    - '<res>:*' in grantable covers '<res>:<any_action>'.
    - Exact match always succeeds.
    - A wildcard request ('*:*', '<res>:*') is only granted when that exact
      wildcard (or a broader one) is in grantable — concrete tuples never
      compose up to a wildcard.
    """
    if requested in grantable:
        return True
    parts = requested.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    res, act = parts
    if "*:*" in grantable:
        return True
    if res == "*" or act == "*":
        return False
    return f"{res}:*" in grantable


def suggest_grantable(requested: str, grantable: set[str]) -> str | None:
    """Closest match for a rejected scope (difflib, cutoff 0.6). None if
    nothing is close enough."""
    if not grantable:
        return None
    matches = difflib.get_close_matches(requested, grantable, n=1, cutoff=0.6)
    return matches[0] if matches else None


async def get_or_fetch_grantable(ctx: Context, client: httpx.AsyncClient) -> set[str]:
    """Return the caller's grantable scopes, fetching once per MCP session.

    Failures are not cached — subsequent calls retry the fetch.
    """
    cached: Any = await ctx.get_state(GRANTABLE_SCOPES_KEY)
    if cached is not None:
        return set(cached)
    scopes = await fetch_grantable_scopes(client)
    await ctx.set_state(GRANTABLE_SCOPES_KEY, scopes)
    return set(scopes)
