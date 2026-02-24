"""Sensitive value redaction for logging and exception tracebacks.

Provides a registry of known sensitive values (tokens, keys) and a logging
filter that scrubs them from all log output across the openfilter_mcp package.

Usage:
    from openfilter_mcp.redact import register_sensitive, redact

    # At the point where a token is obtained:
    register_sensitive(token, label="scoped-token")

    # The RedactingFilter (installed on the package logger) handles log
    # output automatically.  For manual scrubbing of arbitrary strings:
    safe_text = redact(some_text)
"""

from __future__ import annotations

import hashlib
import logging


# Registry: maps raw sensitive value -> display stub
_sensitive: dict[str, str] = {}


def register_sensitive(value: str, label: str = "secret") -> str:
    """Mark *value* as sensitive and return its display stub.

    The stub has the form ``<REDACTED label:a1b2c3d4>`` where the hex suffix
    is the first 8 characters of the SHA-256 of the value.  The stub is
    stable for a given value, so a human reading logs can correlate multiple
    occurrences without seeing the real secret.  The actual value can be
    recovered only by someone who already has it (by recomputing the hash).

    Calling this multiple times with the same value is a no-op (idempotent).

    Args:
        value: The sensitive string (token, key, etc.).
        label: A short human-readable category (e.g. "scoped-token", "jwt").

    Returns:
        The display stub that will replace *value* in logs.
    """
    if not value or len(value) < 4:
        return value  # too short to be a real secret
    if value in _sensitive:
        return _sensitive[value]
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    stub = f"<REDACTED {label}:{digest}>"
    _sensitive[value] = stub
    return stub


def unregister_sensitive(value: str) -> None:
    """Remove *value* from the sensitive registry."""
    _sensitive.pop(value, None)


def redact(text: str) -> str:
    """Replace all registered sensitive values in *text* with their stubs."""
    for secret, stub in _sensitive.items():
        text = text.replace(secret, stub)
    return text


def clear() -> None:
    """Remove all registered sensitive values.  Intended for tests."""
    _sensitive.clear()


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs registered sensitive values from records.

    Handles:
    - ``record.msg`` — the format string (scrubbed *after* %-formatting)
    - ``record.args`` — format arguments (scrubbed individually so that
      ``logger.info("token=%s", token)`` is safe)
    - ``record.exc_text`` — the cached formatted traceback (populated by
      ``logger.exception``).  This is the main vector for token leakage.
      We eagerly format exception info during filtering so we can scrub
      it before any handler sees it.
    """

    @staticmethod
    def _redact_arg(arg):
        if isinstance(arg, str):
            return redact(arg)
        if isinstance(arg, dict):
            return {
                k: redact(v) if isinstance(v, str) else v
                for k, v in arg.items()
            }
        return arg

    def filter(self, record: logging.LogRecord) -> bool:
        if not _sensitive:
            return True  # fast path

        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(self._redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }

        if isinstance(record.msg, str):
            record.msg = redact(record.msg)

        # Eagerly format exception text so we can scrub it.  Normally
        # exc_text is only populated during Formatter.format(), which runs
        # *after* filters.  By formatting it here and caching it on the
        # record, the Formatter will reuse our scrubbed version.
        if record.exc_info and not record.exc_text:
            import traceback

            record.exc_text = "".join(traceback.format_exception(*record.exc_info))

        if record.exc_text:
            record.exc_text = redact(record.exc_text)

        return True


def install(logger_name: str = "openfilter_mcp") -> None:
    """Install the RedactingFilter on the named logger (and all children)."""
    target = logging.getLogger(logger_name)
    # Avoid double-install
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(RedactingFilter())
