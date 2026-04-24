"""Role-filtered RBAC scope introspection via plainsight-api GET /rbac/scopes.

Used by request_scoped_token to validate elicitation against the authoritative
scope set rather than a hand-rolled list that drifts from policies.csv.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from typing import Any

import httpx
from fastmcp.server.context import Context

logger = logging.getLogger(__name__)

GRANTABLE_SCOPES_KEY = "rbac_grantable_scopes"
GRANTABLE_SCOPES_LOCK_KEY = "rbac_grantable_scopes_lock"


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
    except httpx.RequestError as e:
        # Only narrow this to transport-level errors. The authenticated client
        # doesn't configure raise_for_status, so httpx.HTTPStatusError won't
        # fire here today — but if someone wires up a response hook later, we
        # want a real HTTP status error to propagate rather than be swallowed
        # as a "transport error" with status=None.
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

    # Per-entry malformation is log-and-skip rather than fatal: a single bad
    # row upstream shouldn't disable scope validation wholesale. Structural
    # failures (missing 'scopes' key, HTTP error, non-JSON body) still raise
    # ScopesUnavailable above.
    values: list[str] = []
    skipped = 0
    for idx, entry in enumerate(scopes):
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            logger.warning(
                "Skipping malformed /rbac/scopes entry at index %d: %r", idx, entry
            )
            skipped += 1
            continue
        values.append(entry["value"])
    if skipped:
        logger.warning(
            "/rbac/scopes: %d of %d entries skipped due to malformed shape",
            skipped,
            len(scopes),
        )
    return values


def parse_scope(s: str) -> tuple[str, str] | None:
    """Split a scope into (resource, action), or None if malformed.

    Shape rule: exactly one ':' separating non-empty halves; stray colons
    in the action half (e.g. 'a:b:c') are malformed. Single source of truth
    for scope shape — a wildcard grant must not paper over a bad shape, so
    this check runs before any wildcard/exact-match logic in callers.
    """
    res, sep, act = s.partition(":")
    if sep != ":" or not res or not act or ":" in act:
        return None
    return res, act


def is_scope_granted(requested: str, grantable: set[str]) -> bool:
    """Does `grantable` cover `requested`?

    - '*:*' in grantable covers any well-formed request.
    - '<res>:*' in grantable covers '<res>:<any_action>'.
    - Exact match always succeeds.
    - A wildcard request ('*:*', '<res>:*') is only granted when that exact
      wildcard (or a broader one) is in grantable — concrete tuples never
      compose up to a wildcard.
    - Malformed requests are rejected regardless of what's in `grantable`;
      see `parse_scope` for the shape rule.
    """
    parsed = parse_scope(requested)
    if parsed is None:
        return False
    res, act = parsed
    if requested in grantable:
        return True
    if "*:*" in grantable:
        return True
    if res == "*" or act == "*":
        return False
    return f"{res}:*" in grantable


def classify_rejection(requested: str, grantable: set[str]) -> str:
    """Explain *why* a rejected scope failed validation.

    Disambiguates the four failure modes an agent typically hits — malformed
    shape, unknown resource, unknown action under a known resource, or a
    wildcard request that needs a wildcard grant — so the error message points
    at the half that's actually wrong instead of one generic "not granted"
    blob. Caller is responsible for first checking is_scope_granted; this
    function assumes the request was already rejected.
    """
    parsed = parse_scope(requested)
    if parsed is None:
        return f"expected 'resource:action'; got {requested!r}"
    res, act = parsed

    # Decompose the grantable set once. parse_scope filters out any malformed
    # entries defensively (fetch_grantable_scopes already validates shape, but
    # this keeps the classifier honest if it's ever called with an ad-hoc set).
    pairs = [p for p in (parse_scope(g) for g in grantable) if p is not None]
    granted_resources = {r for r, _ in pairs}

    # Wildcard requests fail for a different reason than concrete typos: the
    # user is asking to escalate to a wildcard grant they don't hold. Surface
    # that directly rather than reporting "unknown action '*'".
    if res == "*" and act == "*":
        return "admin scope; requires '*:*' in your grantable set"
    if act == "*":
        return f"wildcard action; requires {res!r}:* or '*:*' in your grantable set"
    if res == "*":
        return "cross-resource wildcard; requires '*:*' in your grantable set"

    if res not in granted_resources:
        known = sorted(granted_resources)
        return f"unknown resource {res!r} (known resources: {known})"

    actions = sorted({a for r, a in pairs if r == res})
    return f"unknown action {act!r} for resource {res!r} (granted actions: {actions})"


def suggest_grantable(requested: str, grantable: set[str]) -> str | None:
    """Closest match for a rejected scope (difflib, cutoff 0.6). None if
    nothing is close enough."""
    if not grantable:
        return None
    # sorted() for deterministic suggestions: set iteration order is
    # hash-randomized, so when two candidates are equidistant, difflib would
    # otherwise pick different winners across runs.
    matches = difflib.get_close_matches(requested, sorted(grantable), n=1, cutoff=0.6)
    return matches[0] if matches else None


async def get_or_fetch_grantable(ctx: Context, client: httpx.AsyncClient) -> set[str]:
    """Return the caller's grantable scopes, fetching once per MCP session.

    Successes are stored via ctx.set_state with the default serializable=True,
    which routes them through fastmcp's pydantic-backed session store — so the
    cache lives for the session, not just the current request. Failures are
    not cached; subsequent calls retry the fetch.
    """
    cached: Any = await ctx.get_state(GRANTABLE_SCOPES_KEY)
    if cached is not None:
        return set(cached)

    # Serialize concurrent first-fetches within a request (e.g.
    # list_grantable_scopes and request_scoped_token racing on a cold cache)
    # so at most one GET /rbac/scopes is in flight per request. Double-check
    # the cache after acquiring the lock so losers of the race don't refetch.
    # The lock is stored with serializable=False because asyncio.Lock cannot
    # be round-tripped through fastmcp's pydantic-backed session store; this
    # routes it into the request-scoped dict instead, which is the correct
    # scope for intra-request dedup anyway.
    lock: Any = await ctx.get_state(GRANTABLE_SCOPES_LOCK_KEY)
    if lock is None:
        lock = asyncio.Lock()
        await ctx.set_state(GRANTABLE_SCOPES_LOCK_KEY, lock, serializable=False)

    async with lock:
        cached = await ctx.get_state(GRANTABLE_SCOPES_KEY)
        if cached is not None:
            return set(cached)
        scopes = await fetch_grantable_scopes(client)
        await ctx.set_state(GRANTABLE_SCOPES_KEY, scopes)
        return set(scopes)
