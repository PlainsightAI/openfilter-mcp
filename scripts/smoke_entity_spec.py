#!/usr/bin/env python3
"""Smoke test: entity-spec parsing against a live plainsight-api.

Fetches /openapi.json and /entity-spec from a running API server, builds
EntityRegistry instances via both the entity-spec (primary) and OpenAPI-only
(fallback) paths, and prints a comparison table for visual inspection.

Validates invariants and exits non-zero on failure.

Usage:
    PS_API_URL=http://localhost:8080 python scripts/smoke_entity_spec.py
    # or via Makefile:
    make smoke
    make smoke PS_API_URL=http://staging:8080
"""

from __future__ import annotations

import os
import sys

import httpx

# ── Fetch from live server ──────────────────────────────────────────────────

API_URL = os.environ.get("PS_API_URL", "http://localhost:8080")


def fetch_specs() -> tuple[dict, dict | None]:
    """Fetch OpenAPI spec and entity-spec from the running API."""
    print(f"Connecting to {API_URL} ...")

    openapi = httpx.get(f"{API_URL}/openapi.json", timeout=15.0)
    openapi.raise_for_status()
    openapi_spec = openapi.json()

    entity_spec = None
    try:
        resp = httpx.get(f"{API_URL}/entity-spec", timeout=15.0)
        resp.raise_for_status()
        entity_spec = resp.json()
    except httpx.HTTPStatusError as e:
        print(f"  /entity-spec returned {e.response.status_code} — skipping")

    return openapi_spec, entity_spec


# ── Reuse project code ─────────────────────────────────────────────────────

from openfilter_mcp.server import sanitize_openapi_spec  # noqa: E402
from openfilter_mcp.entity_tools import EntityRegistry  # noqa: E402


# ── Display helpers ─────────────────────────────────────────────────────────

CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _col(text: str, width: int) -> str:
    """Left-pad/truncate text to fixed width."""
    return text[:width].ljust(width)


def print_entity_table(title: str, registry: EntityRegistry) -> None:
    """Print a formatted table of entities and their operations."""
    entities = sorted(registry.entities.items())
    if not entities:
        print(f"\n{BOLD}{title}{RESET}: (no entities)\n")
        return

    # Column widths
    W_NAME, W_SCOPE, W_RBAC, W_CNT = 28, 16, 22, 5

    print(f"\n{BOLD}{title}{RESET}  ({len(entities)} entities)")
    header = (
        f"  {CYAN}{_col('Entity', W_NAME)}"
        f"{_col('Scope', W_SCOPE)}"
        f"{_col('RBAC Domain', W_RBAC)}"
        f"{_col('#Ops', W_CNT)}"
        f"Actions{RESET}"
    )
    print(header)
    print(f"  {'─' * 100}")

    for name, entity in entities:
        actions = sorted(entity.operations.keys())
        action_str = ", ".join(actions)
        scope_colored = entity.scope
        if entity.scope == "unknown":
            scope_colored = f"{DIM}{entity.scope}{RESET}"
        elif entity.scope == "global":
            scope_colored = f"{YELLOW}{entity.scope}{RESET}"

        print(
            f"  {_col(name, W_NAME)}"
            f"{_col(scope_colored, W_SCOPE + (len(scope_colored) - len(entity.scope)))}"
            f"{_col(entity.rbac_domain or '—', W_RBAC)}"
            f"{_col(str(len(actions)), W_CNT)}"
            f"{action_str}"
        )
    print()


def print_diff(primary: EntityRegistry, fallback: EntityRegistry) -> None:
    """Print differences between entity-spec and OpenAPI-fallback registries."""
    p_names = set(primary.entities.keys())
    f_names = set(fallback.entities.keys())

    only_primary = sorted(p_names - f_names)
    only_fallback = sorted(f_names - p_names)
    common = sorted(p_names & f_names)

    print(f"{BOLD}Comparison: entity-spec vs OpenAPI fallback{RESET}")
    print(f"  Entity-spec entities:  {len(p_names)}")
    print(f"  OpenAPI-fallback entities: {len(f_names)}")
    print(f"  Common:                {len(common)}")

    if only_primary:
        print(f"\n  {GREEN}Only in entity-spec ({len(only_primary)}):{RESET}")
        for name in only_primary:
            e = primary.entities[name]
            actions = sorted(e.operations.keys())
            print(f"    + {name}  [{e.scope}]  {', '.join(actions)}")

    if only_fallback:
        print(f"\n  {YELLOW}Only in OpenAPI fallback ({len(only_fallback)}):{RESET}")
        for name in only_fallback:
            e = fallback.entities[name]
            actions = sorted(e.operations.keys())
            print(f"    - {name}  {', '.join(actions)}")

    # Action-level diff for common entities
    mismatches = []
    for name in common:
        p_actions = set(primary.entities[name].operations.keys())
        f_actions = set(fallback.entities[name].operations.keys())
        if p_actions != f_actions:
            mismatches.append((name, p_actions, f_actions))

    if mismatches:
        print(f"\n  {YELLOW}Action mismatches ({len(mismatches)}):{RESET}")
        for name, p_acts, f_acts in mismatches:
            only_p = sorted(p_acts - f_acts)
            only_f = sorted(f_acts - p_acts)
            parts = []
            if only_p:
                parts.append(f"{GREEN}+entity-spec: {', '.join(only_p)}{RESET}")
            if only_f:
                parts.append(f"{YELLOW}+fallback: {', '.join(only_f)}{RESET}")
            print(f"    {name}: {' | '.join(parts)}")

    if not only_primary and not only_fallback and not mismatches:
        print(f"\n  {GREEN}Registries are identical.{RESET}")

    print()


# ── Validation ──────────────────────────────────────────────────────────────


def validate(primary: EntityRegistry, entity_spec: dict | None) -> list[str]:
    """Run invariant checks. Returns list of failure messages."""
    failures: list[str] = []

    if entity_spec is None:
        failures.append("/entity-spec endpoint not available — cannot validate primary path")
        return failures

    # 1. Every entity-spec entity has at least one operation
    for name, entity in primary.entities.items():
        if not entity.operations:
            failures.append(f"Entity '{name}' has zero operations")

    # 2. No duplicate actions within any entity (by construction, but verify)
    for name, entity in primary.entities.items():
        actions = list(entity.operations.keys())
        if len(actions) != len(set(actions)):
            dupes = [a for a in actions if actions.count(a) > 1]
            failures.append(f"Entity '{name}' has duplicate actions: {dupes}")

    # 3. Scopes are populated in primary path
    for name, entity in primary.entities.items():
        if not entity.scope or entity.scope == "unknown":
            failures.append(f"Entity '{name}' has no scope in entity-spec path")

    # 4. All entity-spec response entities ended up in the registry
    spec_names = {e["name"] for e in entity_spec.get("entities", [])}
    registry_names = set(primary.entities.keys())
    missing = sorted(spec_names - registry_names)
    if missing:
        failures.append(f"Entity-spec entities missing from registry: {missing}")

    return failures


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    openapi_raw, entity_spec = fetch_specs()
    openapi_spec = sanitize_openapi_spec(openapi_raw)

    path_count = len(openapi_spec.get("paths", {}))
    entity_count = len(entity_spec.get("entities", [])) if entity_spec else 0
    print(f"  OpenAPI paths: {path_count}")
    print(f"  Entity-spec entities: {entity_count}")

    # Build registries
    primary = EntityRegistry(openapi_spec, entity_spec=entity_spec)
    fallback = EntityRegistry(openapi_spec)  # OpenAPI-only

    # Display
    print_entity_table("Entity-spec (primary path)", primary)
    print_entity_table("OpenAPI fallback path", fallback)
    print_diff(primary, fallback)

    # Validate
    failures = validate(primary, entity_spec)
    if failures:
        print(f"{RED}{BOLD}VALIDATION FAILURES ({len(failures)}):{RESET}")
        for f in failures:
            print(f"  {RED}✗{RESET} {f}")
        return 1

    print(f"{GREEN}{BOLD}All validations passed.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
