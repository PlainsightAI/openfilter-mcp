"""AST-enforced security invariants for the OAuth bootstrap exception.

The PR that introduced OAuth-mode bootstrap (DT-133) carved out a
single sanctioned consumer of the request-bound OAuth Bearer for
outbound auth: `openfilter_mcp.server._resolve_bootstrap_auth`. Every
other code path — entity ops in particular — must derive outbound
Authorization from the session-scoped token (set by user-approved
`request_scoped_token`) or the static psctl/OPENFILTER_TOKEN.
Collapsing these layers silently bypasses the elicitation gate: an
agent operating under a broad OAuth token would skip per-session
scope approval and reach API surfaces the user never consented to.

These tests scan the Python AST of `src/openfilter_mcp/` for the
anti-patterns the invariant forbids. The previous implementation
documented the rule in a 30-line comment in `entity_tools.py`; this
file enforces it in CI so a future contributor can't unknowingly
regress it.

Forbidden patterns (in any function except `_resolve_bootstrap_auth`):

  1. `ctx.request_context.access_token` — a deep attribute chain that
     pulls the FastMCP context's authenticated token.
  2. Subscripting `request.headers` for an authorization key — reading
     the raw inbound Authorization header off the request object.
  3. Calling `get_access_token()` imported from any `fastmcp` module —
     the FastMCP dependency that returns the request's verified bearer.

Allowlist: only `_resolve_bootstrap_auth` (and any function nested
inside it) may use these. The allowlist is enforced by name, not by
file, so moving the function to another module without updating the
allowlist will NOT silently grant blanket permission to that module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "openfilter_mcp"

# Functions allowed to consume the request OAuth bearer for outbound auth.
# Any function lexically nested inside one of these names is also exempt.
_ALLOWLISTED_FUNCTIONS = frozenset(
    {
        "_resolve_bootstrap_auth",
    }
)

# Authorization-header key variants that count as "reading the inbound auth".
_AUTH_HEADER_KEYS = frozenset({"authorization", "Authorization", "AUTHORIZATION"})


def _iter_python_files() -> list[Path]:
    """Walk src/openfilter_mcp/ and return every .py file."""
    return sorted(p for p in SRC_ROOT.rglob("*.py") if p.is_file())


def _attribute_chain(node: ast.AST) -> list[str] | None:
    """Flatten an `a.b.c` Attribute chain to ['a','b','c']; None if not pure."""
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts
    return None


def _function_violates(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return human-readable descriptions of any anti-patterns inside `func`.

    The walk descends into nested expressions but treats nested function
    definitions whose name is allowlisted as a transparent boundary: their
    body is not scanned (the allowlist applies recursively).
    """
    violations: list[str] = []

    def visit(node: ast.AST) -> None:
        # Don't descend into a nested function whose name is itself allowlisted —
        # the recursion guarantees nested functions stay on the same allowlist.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _ALLOWLISTED_FUNCTIONS:
                return

        # Pattern 1: <anything>.request_context.access_token
        if isinstance(node, ast.Attribute) and node.attr == "access_token":
            chain = _attribute_chain(node)
            if chain and len(chain) >= 2 and chain[-2] == "request_context":
                violations.append(
                    f"line {node.lineno}: reads `{'.'.join(chain)}` "
                    "(forbidden — collapses ingress OAuth bearer onto outbound auth)"
                )

        # Pattern 2: request.headers[...] subscripted by an auth key
        if isinstance(node, ast.Subscript):
            value_chain = _attribute_chain(node.value)
            if value_chain and len(value_chain) >= 2 and value_chain[-1] == "headers":
                key = node.slice
                if isinstance(key, ast.Constant) and key.value in _AUTH_HEADER_KEYS:
                    violations.append(
                        f"line {node.lineno}: reads "
                        f"`{'.'.join(value_chain)}[{key.value!r}]` "
                        "(forbidden — collapses raw inbound Authorization header onto outbound auth)"
                    )

        # Pattern 3: calling get_access_token() — the FastMCP request-bearer accessor
        if isinstance(node, ast.Call):
            callee = node.func
            chain = _attribute_chain(callee) if isinstance(callee, ast.Attribute) else None
            if isinstance(callee, ast.Name) and callee.id == "get_access_token":
                violations.append(
                    f"line {node.lineno}: calls `get_access_token()` "
                    "(forbidden — only `_resolve_bootstrap_auth` may consume the request OAuth bearer)"
                )
            elif chain and chain[-1] == "get_access_token":
                violations.append(
                    f"line {node.lineno}: calls `{'.'.join(chain)}()` "
                    "(forbidden — only `_resolve_bootstrap_auth` may consume the request OAuth bearer)"
                )

        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in func.body:
        visit(stmt)

    return violations


def _walk_functions(tree: ast.AST):
    """Yield every (top-level or nested) FunctionDef / AsyncFunctionDef in `tree`,
    skipping ones (and their descendants) whose name is allowlisted."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _ALLOWLISTED_FUNCTIONS:
                # Allowlisted: don't yield this function and don't recurse inside.
                continue
            yield node
            # Don't recurse into the function body; _function_violates handles it.
            continue
        # Recurse into every other compound node — class bodies, control-flow
        # blocks, exception handlers, match cases, etc. — so a function
        # defined in any of them is still reached.
        yield from _walk_functions(node)


def test_src_root_exists():
    """Sanity check: the test wouldn't catch anything if the path were wrong."""
    assert SRC_ROOT.is_dir(), f"expected source tree at {SRC_ROOT}"
    assert any(SRC_ROOT.rglob("*.py")), "no Python files found under src/openfilter_mcp"


@pytest.mark.parametrize(
    "src_path",
    _iter_python_files(),
    ids=lambda p: p.relative_to(SRC_ROOT).as_posix(),
)
def test_no_forbidden_oauth_bearer_consumption(src_path: Path):
    """Every function in src/openfilter_mcp/ except `_resolve_bootstrap_auth`
    must avoid the documented anti-patterns. If you have a legitimate need
    to consume the request OAuth bearer for outbound auth, add the helper
    function to `_ALLOWLISTED_FUNCTIONS` here AND extend the security
    invariant doc in `_resolve_bootstrap_auth`'s docstring — drift between
    the two is exactly what this test exists to catch."""
    tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))

    failures: list[str] = []
    for func in _walk_functions(tree):
        problems = _function_violates(func)
        if problems:
            qualname = f"{src_path.relative_to(SRC_ROOT)}::{func.name}"
            for p in problems:
                failures.append(f"{qualname} — {p}")

    assert not failures, (
        "Security invariant violation — entity-op / non-bootstrap code is "
        "consuming the request OAuth bearer for outbound auth:\n  - "
        + "\n  - ".join(failures)
    )


def test_resolve_bootstrap_auth_is_the_only_allowlisted_function():
    """Pin the allowlist so adding a new exemption requires touching this
    test (and therefore the security invariant doc in tandem)."""
    assert _ALLOWLISTED_FUNCTIONS == frozenset({"_resolve_bootstrap_auth"})


def test_allowlisted_function_actually_uses_get_access_token():
    """Lock in that `_resolve_bootstrap_auth` is the function that pulls
    the request OAuth bearer — if it stops doing so the allowlist entry
    is dead and should be removed.

    This is the *positive* form of the invariant: not just 'no one else
    does this' but 'precisely one place does, and we know which.'
    """
    server_path = SRC_ROOT / "server.py"
    tree = ast.parse(server_path.read_text(encoding="utf-8"))

    found_function = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_resolve_bootstrap_auth"
        ):
            found_function = node
            break

    assert found_function is not None, (
        "_resolve_bootstrap_auth not found in server.py — either it was "
        "renamed (update _ALLOWLISTED_FUNCTIONS) or removed (drop the "
        "allowlist entry entirely)."
    )

    uses_get_access_token = False
    for node in ast.walk(found_function):
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id == "get_access_token":
                uses_get_access_token = True
                break
            chain = _attribute_chain(callee) if isinstance(callee, ast.Attribute) else None
            if chain and chain[-1] == "get_access_token":
                uses_get_access_token = True
                break

    assert uses_get_access_token, (
        "_resolve_bootstrap_auth no longer calls get_access_token(). The "
        "allowlist entry is now dead — drop it from _ALLOWLISTED_FUNCTIONS."
    )
