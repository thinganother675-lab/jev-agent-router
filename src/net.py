"""One shared rule for opening a keep-alive HTTPS connection from this machine.

Both API clients need the same thing and for the same measured reasons, so the rule lives
in one place rather than being reinvented per client.

Two findings, both verified on 2026-09-22, both of which make the naive
`http.client.HTTPSConnection(host)` the wrong call here:

1. **Mercury is unreachable directly.** Inception's CloudFront distribution geo-blocks this
   country. TCP and TLS complete, and then every HTTP request returns
   `403 ... configured to block access from your country`. `urllib` reads the proxy
   environment automatically and therefore works; a bare `http.client` connection does not,
   and fails in exactly that confusing way.

2. **Jev's keep-alive only survives on the proxied route.** On a direct connection to
   `api.typesafe.ai`, the third request on a connection is never answered — the socket is
   not closed, so the client blocks until its own timeout and then retries, costing a full
   timeout every other call. Measured, same payload, same minute:

       direct           : 2 consecutive successes, then TimeoutError
       through proxy    : 6/6 successes, ~400 ms each, stable

   That is the difference between a sidecar that is worth running and one that is slower
   than not having it.

So: use the proxy when the environment defines one. Set `<PREFIX>_NO_PROXY=1` to force the
direct route for comparison.
"""

from __future__ import annotations

import http.client
import os
import urllib.request


def proxy_for(disable_env: str | None = None) -> tuple[str, int] | None:
    """(host, port) of the HTTPS proxy to use, or None to connect directly."""
    if disable_env and os.environ.get(disable_env):
        return None
    url = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
    if not url:
        return None
    netloc = url.split("://", 1)[-1].rstrip("/")
    host, _, port = netloc.partition(":")
    return (host, int(port or 8080))


def connect(host: str, timeout: float, disable_env: str | None = None
            ) -> http.client.HTTPSConnection:
    """A keep-alive HTTPS connection to `host`, tunnelled through the proxy if there is one.

    With a proxy this issues CONNECT, so the TLS session still terminates at `host` and the
    connection is reusable exactly like a direct one.
    """
    proxy = proxy_for(disable_env)
    if proxy:
        conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout)
        conn.set_tunnel(host, 443)
        return conn
    return http.client.HTTPSConnection(host, timeout=timeout)


def route_description(disable_env: str | None = None) -> str:
    proxy = proxy_for(disable_env)
    return f"via proxy {proxy[0]}:{proxy[1]}" if proxy else "direct"
