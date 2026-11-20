"""Keep the dashboard on this machine, even when the server is behind a public tunnel.

ElevenLabs and the Grok Bots reach the server from the internet, so the server is exposed
through a tunnel. Without this, the tunnel would expose everything else too: the route that
shows the one-time challenge word, the freeze switch, the dry-run stand-in for the call. That
would undo the whole approval design, because the word is only a secret if a caller on the
phone cannot fetch it.

So only a short list of prefixes is served to the outside, and each of those checks its own
credential (a bearer token or an HMAC signature). Every other path is served only when the
request was addressed to localhost and did not arrive through a proxy. The Host check also
defeats DNS rebinding from a web page.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

# Reachable from anywhere. Each one authenticates the caller itself.
PUBLIC_PREFIXES = (
    "/api/voice/",  # bearer VOICE_TOKEN; the post-call webhook is HMAC-verified
    "/api/bots/",  # bearer BOT_TOKEN
    # One bearer token per connected assistant. The trailing slash matters: the routes that
    # create and revoke those tokens are under /api/connectors and stay on this machine.
    "/api/connect/",
    "/api/hooks/",  # P1's Stream webhook, HMAC-verified
    "/api/health",
)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
# Any of these means the request came through a proxy or tunnel, not straight from a browser
# on this machine.
FORWARDED_HEADERS = frozenset(
    {
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"forwarded",
        b"x-real-ip",
        b"cf-connecting-ip",
        b"cf-ray",
        b"ngrok-skip-browser-warning",
    }
)
REFUSAL = json.dumps(
    {"detail": "The dashboard and its routes are only served on the machine running Switchboard."}
).encode()


def host_name(host_header: str) -> str:
    """`localhost:8787` -> `localhost`; `[::1]:8787` -> `[::1]`."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def is_local(scope: Scope) -> bool:
    headers = dict(scope.get("headers") or [])
    if FORWARDED_HEADERS & headers.keys():
        return False
    return host_name(headers.get(b"host", b"").decode("latin-1")) in LOCAL_HOSTS


class LocalOnly:
    """ASGI middleware. Written at this level so it does not buffer the SSE stream."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path.startswith(PUBLIC_PREFIXES) or is_local(scope):
            await self._app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(REFUSAL)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": REFUSAL})
