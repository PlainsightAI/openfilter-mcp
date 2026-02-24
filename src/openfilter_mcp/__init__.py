from openfilter_mcp.redact import install as _install_redaction

# Scrub registered sensitive values (tokens, keys) from all log output
# across the package.  Must run before any logger in the package is used.
_install_redaction()


def main() -> None:
    print("Hello from openfilter-mcp!")
