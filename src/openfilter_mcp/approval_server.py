"""Lightweight local web server for token approval when MCP elicitation is unavailable.

When an MCP client (e.g., Claude Code) does not support the elicitation protocol,
this module spins up a temporary HTTP server on localhost that presents an approval
dialog to the user. The server shuts down immediately after the user responds or
a timeout expires.

Usage::

    result = await request_approval_via_browser(
        title="Scoped Token Request",
        message="The AI agent is requesting a scoped API token.",
        details={"Token name": "my-token", "Scopes": ["project:read"]},
        timeout_seconds=120,
    )
    # result is "approve" | "deny" | "timeout"
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import socket
from typing import Any
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand colors from Plainsight brand guidelines (May 2024)
# ---------------------------------------------------------------------------
_MIDNIGHT = "#242444"
_TURQUOISE = "#6399AE"
_GRAPE = "#6D2077"
_PURPLE = "#615E9B"
_LIGHT_SKY = "#B6CFD0"
_DUSK = "#E5E2E7"
_NOON = "#73BDC5"
_WHITE = "#FFFFFF"

# Bulma CDN (pure CSS, no JS build step)
_BULMA_CDN = "https://cdn.jsdelivr.net/npm/bulma@1.0.4/css/bulma.min.css"
_LATO_CDN = "https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&family=Open+Sans:wght@400;600&display=swap"


def _render_approval_page(
    title: str,
    message: str,
    details: dict[str, Any],
    nonce: str,
) -> str:
    """Render the approval HTML page with Plainsight branding + Bulma CSS."""

    # Build the details table rows
    detail_rows = []
    for key, value in details.items():
        if isinstance(value, list):
            escaped_val = "<br>".join(html.escape(str(v)) for v in value)
        else:
            escaped_val = html.escape(str(value))
        detail_rows.append(
            f'<tr><th class="has-text-left" style="white-space:nowrap; '
            f'padding-right:1.5rem; color:{_PURPLE};">{html.escape(key)}</th>'
            f'<td><code>{escaped_val}</code></td></tr>'
        )
    details_html = "\n".join(detail_rows)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} — Plainsight</title>
  <link rel="stylesheet" href="{_BULMA_CDN}">
  <link rel="stylesheet" href="{_LATO_CDN}">
  <style>
    :root {{
      --midnight: {_MIDNIGHT};
      --turquoise: {_TURQUOISE};
      --grape: {_GRAPE};
      --purple: {_PURPLE};
      --light-sky: {_LIGHT_SKY};
      --dusk: {_DUSK};
      --noon: {_NOON};
    }}
    body {{
      font-family: 'Lato', 'Open Sans', sans-serif;
      background: var(--dusk);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .approval-card {{
      max-width: 540px;
      width: 100%;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 8px 30px rgba(36, 36, 68, 0.15);
    }}
    .card-header-banner {{
      background: linear-gradient(135deg, var(--midnight) 0%, var(--purple) 50%, var(--grape) 100%);
      padding: 1.5rem 2rem;
    }}
    .card-header-banner h1 {{
      color: {_WHITE};
      font-size: 1.35rem;
      font-weight: 700;
      margin: 0;
    }}
    .card-header-banner .subtitle {{
      color: var(--light-sky);
      font-size: 0.85rem;
      margin-top: 0.25rem;
      font-weight: 300;
    }}
    .card-body {{
      background: {_WHITE};
      padding: 2rem;
    }}
    .card-body .message-text {{
      color: var(--midnight);
      font-size: 0.95rem;
      line-height: 1.6;
      margin-bottom: 1.5rem;
    }}
    .detail-table {{
      width: 100%;
      margin-bottom: 1.5rem;
    }}
    .detail-table th {{
      font-size: 0.85rem;
      font-weight: 600;
      padding: 0.4rem 0;
      vertical-align: top;
    }}
    .detail-table td {{
      font-size: 0.85rem;
      padding: 0.4rem 0;
    }}
    .detail-table code {{
      background: var(--dusk);
      padding: 0.15rem 0.4rem;
      border-radius: 4px;
      font-size: 0.8rem;
      color: var(--midnight);
    }}
    .btn-approve {{
      background: var(--turquoise);
      border: none;
      color: {_WHITE};
      font-weight: 700;
      font-family: 'Lato', sans-serif;
      transition: background 0.2s;
    }}
    .btn-approve:hover {{
      background: var(--noon);
      color: {_WHITE};
    }}
    .btn-deny {{
      background: transparent;
      border: 2px solid var(--purple);
      color: var(--purple);
      font-weight: 600;
      font-family: 'Lato', sans-serif;
      transition: all 0.2s;
    }}
    .btn-deny:hover {{
      background: var(--purple);
      color: {_WHITE};
    }}
    .card-footer-bar {{
      background: {_WHITE};
      padding: 0 2rem 1.5rem;
      display: flex;
      gap: 0.75rem;
      justify-content: flex-end;
    }}
    .security-note {{
      color: #888B8D;
      font-size: 0.72rem;
      text-align: center;
      padding: 0.75rem 2rem;
      background: var(--dusk);
      border-top: 1px solid #d5d5d5;
    }}
    .responded {{
      text-align: center;
      padding: 3rem 2rem;
    }}
    .responded h2 {{
      color: var(--midnight);
      font-weight: 700;
    }}
    .responded p {{
      color: #888B8D;
      margin-top: 0.5rem;
    }}
  </style>
</head>
<body>
  <div class="approval-card" id="card">
    <div class="card-header-banner">
      <h1>{html.escape(title)}</h1>
      <div class="subtitle">OpenFilter MCP &middot; Token Approval</div>
    </div>
    <div class="card-body">
      <p class="message-text">{html.escape(message)}</p>
      <table class="detail-table">
        {details_html}
      </table>
    </div>
    <div class="card-footer-bar">
      <form method="POST" action="/respond" style="display:inline">
        <input type="hidden" name="nonce" value="{html.escape(nonce)}">
        <input type="hidden" name="action" value="deny">
        <button type="submit" class="button is-medium btn-deny">Deny</button>
      </form>
      <form method="POST" action="/respond" style="display:inline">
        <input type="hidden" name="nonce" value="{html.escape(nonce)}">
        <input type="hidden" name="action" value="approve">
        <button type="submit" class="button is-medium btn-approve">Approve</button>
      </form>
    </div>
    <div class="security-note">
      This request originated from an AI agent via the OpenFilter MCP server running on localhost.
      <br>The token value will never be shown to the agent.
    </div>
  </div>
</body>
</html>"""


def _render_response_page(action: str) -> str:
    """Render the post-response confirmation page."""
    if action == "approve":
        icon = "&#10003;"
        heading = "Approved"
        detail = "The scoped token has been created. You can close this tab."
        color = _TURQUOISE
    elif action == "timeout":
        icon = "&#9201;"
        heading = "Timed Out"
        detail = "The approval window expired. The agent will proceed without a scoped token."
        color = _PURPLE
    else:
        icon = "&#10007;"
        heading = "Denied"
        detail = "The token request was denied. You can close this tab."
        color = _GRAPE

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{heading} — Plainsight</title>
  <link rel="stylesheet" href="{_BULMA_CDN}">
  <link rel="stylesheet" href="{_LATO_CDN}">
  <style>
    body {{
      font-family: 'Lato', 'Open Sans', sans-serif;
      background: {_DUSK};
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .result-card {{
      max-width: 420px;
      width: 100%;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 8px 30px rgba(36, 36, 68, 0.15);
      background: {_WHITE};
      text-align: center;
      padding: 3rem 2rem;
    }}
    .result-icon {{
      font-size: 3rem;
      color: {color};
      margin-bottom: 1rem;
    }}
    .result-card h2 {{
      color: {_MIDNIGHT};
      font-weight: 700;
      font-size: 1.5rem;
    }}
    .result-card p {{
      color: #888B8D;
      margin-top: 0.5rem;
      font-size: 0.9rem;
    }}
  </style>
</head>
<body>
  <div class="result-card">
    <div class="result-icon">{icon}</div>
    <h2>{heading}</h2>
    <p>{detail}</p>
  </div>
</body>
</html>"""


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApprovalSession:
    """Handle for a running approval server.

    Attributes:
        url: The localhost URL to present to the user.
    """

    def __init__(self, url: str, future: asyncio.Future[str], server: asyncio.AbstractServer, timeout: int):
        self.url = url
        self._future = future
        self._server = server
        self._timeout = timeout
        # Start a background timeout that auto-resolves the future,
        # so callers using the non-blocking pattern (_future.done()) also
        # see timeout without needing to call wait().
        self._timeout_task = asyncio.create_task(self._auto_timeout())

    async def _auto_timeout(self):
        """Resolve the future with "timeout" after the deadline."""
        try:
            await asyncio.sleep(self._timeout)
            if not self._future.done():
                self._future.set_result("timeout")
                self._server.close()
                await self._server.wait_closed()
                logger.info("Approval server timed out and shut down")
        except asyncio.CancelledError:
            pass

    async def wait(self) -> str:
        """Block until the user responds or the timeout expires.

        Returns:
            ``"approve"``, ``"deny"``, or ``"timeout"``.
        """
        try:
            result = await self._future
        finally:
            self._timeout_task.cancel()
            self._server.close()
            await self._server.wait_closed()
        logger.info("Approval server shut down (result: %s)", result)
        return result


async def start_approval_server(
    title: str,
    message: str,
    details: dict[str, Any],
    timeout_seconds: int = 120,
) -> ApprovalSession:
    """Spin up a temporary local web server for user approval.

    Returns an :class:`ApprovalSession` whose ``.url`` can be shown to the
    user (via ``ctx.info`` or tool result) and whose ``.wait()`` coroutine
    blocks until the user clicks Approve/Deny or the timeout expires.

    Args:
        title: Dialog title shown in the banner.
        message: Explanatory text shown to the user.
        details: Key-value pairs displayed in a table (e.g., scopes, token name).
        timeout_seconds: How long to wait before auto-denying.

    Returns:
        An :class:`ApprovalSession` with ``.url`` and ``.wait()``.
    """
    import secrets

    nonce = secrets.token_urlsafe(16)
    port = _find_free_port()
    result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    approval_html = _render_approval_page(title, message, details, nonce)
    approval_bytes = approval_html.encode()

    async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # Read the HTTP request
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="replace")
            method, path, *_ = request_str.split()

            # Read headers
            content_length = 0
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=10)
                if header_line in (b"\r\n", b"\n", b""):
                    break
                if header_line.lower().startswith(b"content-length:"):
                    content_length = int(header_line.split(b":")[1].strip())

            if method == "GET" and path == "/":
                # Serve the approval page
                _send_response(writer, 200, "text/html", approval_bytes)

            elif method == "POST" and path == "/respond":
                # Read POST body
                body = b""
                if content_length > 0:
                    body = await asyncio.wait_for(reader.read(content_length), timeout=10)
                params = parse_qs(body.decode("utf-8", errors="replace"))

                submitted_nonce = params.get("nonce", [""])[0]
                action = params.get("action", ["deny"])[0]

                if submitted_nonce != nonce:
                    _send_response(writer, 403, "text/plain", b"Invalid nonce")
                elif action not in ("approve", "deny"):
                    _send_response(writer, 400, "text/plain", b"Invalid action")
                else:
                    response_html = _render_response_page(action).encode()
                    _send_response(writer, 200, "text/html", response_html)
                    if not result_future.done():
                        result_future.set_result(action)
            else:
                _send_response(writer, 404, "text/plain", b"Not found")

            await writer.drain()
        except Exception:
            logger.debug("Error handling approval HTTP connection", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle_connection, "127.0.0.1", port)
    url = f"http://127.0.0.1:{port}/"
    logger.info("Approval server listening on %s", url)

    return ApprovalSession(url, result_future, server, timeout_seconds)


def _send_response(writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes):
    """Write a minimal HTTP/1.1 response."""
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"}.get(status, "OK")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    writer.write(headers.encode() + body)
