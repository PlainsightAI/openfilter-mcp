"""Tests for the sensitive value redaction system."""

import logging

import pytest

from openfilter_mcp.redact import (
    RedactingFilter,
    clear,
    redact,
    register_sensitive,
    unregister_sensitive,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure each test starts with a clean registry."""
    clear()
    yield
    clear()


class TestRegistry:
    def test_register_returns_stable_stub(self):
        stub = register_sensitive("ps_secret_token_abc123", label="scoped-token")
        assert stub.startswith("<REDACTED scoped-token:")
        assert stub.endswith(">")
        # Same value → same stub (idempotent)
        assert register_sensitive("ps_secret_token_abc123", label="scoped-token") == stub

    def test_different_values_get_different_stubs(self):
        s1 = register_sensitive("token_aaa", label="a")
        s2 = register_sensitive("token_bbb", label="b")
        assert s1 != s2

    def test_short_values_ignored(self):
        assert register_sensitive("abc", label="x") == "abc"
        assert redact("abc") == "abc"  # not scrubbed

    def test_unregister(self):
        register_sensitive("ps_to_remove", label="tmp")
        assert "ps_to_remove" not in redact("ps_to_remove")
        unregister_sensitive("ps_to_remove")
        assert redact("ps_to_remove") == "ps_to_remove"

    def test_clear(self):
        register_sensitive("secret1", label="a")
        register_sensitive("secret2", label="b")
        clear()
        assert redact("secret1 secret2") == "secret1 secret2"


class TestRedact:
    def test_replaces_known_value(self):
        stub = register_sensitive("ps_my_secret_token", label="scoped-token")
        result = redact("Authorization: Bearer ps_my_secret_token")
        assert "ps_my_secret_token" not in result
        assert stub in result

    def test_replaces_multiple_occurrences(self):
        register_sensitive("ps_tok_repeat", label="tok")
        result = redact("first: ps_tok_repeat, second: ps_tok_repeat")
        assert "ps_tok_repeat" not in result

    def test_replaces_multiple_different_values(self):
        register_sensitive("secret_aaa", label="a")
        register_sensitive("secret_bbb", label="b")
        result = redact("secret_aaa and secret_bbb")
        assert "secret_aaa" not in result
        assert "secret_bbb" not in result

    def test_leaves_non_sensitive_text_alone(self):
        register_sensitive("ps_only_this", label="tok")
        result = redact("some normal text here")
        assert result == "some normal text here"

    def test_empty_registry_is_identity(self):
        assert redact("anything at all") == "anything at all"


class TestRedactingFilter:
    def _make_record(self, msg, args=None, exc_text=None):
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )
        if exc_text:
            record.exc_text = exc_text
        return record

    def test_scrubs_msg(self):
        stub = register_sensitive("ps_in_msg", label="tok")
        f = RedactingFilter()
        record = self._make_record("error with ps_in_msg")
        f.filter(record)
        assert "ps_in_msg" not in record.msg
        assert stub in record.msg

    def test_scrubs_args_tuple(self):
        register_sensitive("ps_in_args", label="tok")
        f = RedactingFilter()
        record = self._make_record("token=%s", args=("ps_in_args",))
        f.filter(record)
        assert "ps_in_args" not in record.args[0]

    def test_scrubs_args_dict(self):
        """Dict args are passed as a tuple wrapping the dict by Python logging."""
        register_sensitive("ps_in_dict", label="tok")
        f = RedactingFilter()
        # In real usage: logger.info("%(token)s", {"token": "ps_in_dict"})
        # LogRecord stores this as args=({"token": "ps_in_dict"},)
        record = self._make_record("%(token)s", args=({"token": "ps_in_dict"},))
        f.filter(record)
        # The dict is inside a tuple, so the tuple branch scrubs it
        assert "ps_in_dict" not in record.getMessage()

    def test_scrubs_exc_text(self):
        stub = register_sensitive("ps_in_traceback", label="tok")
        f = RedactingFilter()
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "entity_tools.py", line 625\n'
            "    response = await self.client.post(...)\n"
            "httpx.HTTPError: 401 - Bearer ps_in_traceback\n"
        )
        record = self._make_record("renewal failed", exc_text=traceback)
        f.filter(record)
        assert "ps_in_traceback" not in record.exc_text
        assert stub in record.exc_text

    def test_noop_when_registry_empty(self):
        f = RedactingFilter()
        record = self._make_record("nothing to scrub", args=("safe",))
        f.filter(record)
        assert record.msg == "nothing to scrub"
        assert record.args == ("safe",)

    def test_non_string_args_untouched(self):
        register_sensitive("ps_secret", label="tok")
        f = RedactingFilter()
        record = self._make_record("code=%s count=%s", args=(42, None))
        f.filter(record)
        assert record.args == (42, None)


class TestIntegrationWithLogger:
    """Verify the filter works end-to-end with a real logger."""

    def test_logger_exception_scrubs_token(self, capfd):
        token = "ps_scoped_integration_test_token_xyz"
        stub = register_sensitive(token, label="scoped-token")

        log = logging.getLogger("openfilter_mcp.test_integration")
        log.addFilter(RedactingFilter())
        handler = logging.StreamHandler()
        handler.setLevel(logging.ERROR)
        log.addHandler(handler)
        log.setLevel(logging.ERROR)

        try:
            # Simulate an exception whose message contains the token
            raise RuntimeError(f"HTTP 401: Bearer {token} is invalid")
        except RuntimeError:
            log.exception("Token renewal failed")

        log.removeHandler(handler)
        log.removeFilter(log.filters[-1])

        captured = capfd.readouterr()
        assert token not in captured.err
        assert stub in captured.err
