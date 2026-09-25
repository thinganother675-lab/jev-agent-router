"""jevd — a tiny loopback sidecar that holds one warm HTTPS connection to Jev.

    python src/jev_sidecar.py              # foreground, Ctrl+C to stop
    python src/jev_sidecar.py --port 8787

Why it exists: a Claude Code hook is a fresh process on every prompt, and a fresh process
pays the full TLS handshake. Measured on this machine, that is ~1,300 ms cold against
~310 ms on a warm connection. The sidecar pays the handshake once and answers over
loopback, which costs about a millisecond.

    hook (new process) ──▶ 127.0.0.1:8787 ──▶ jevd (one warm TLS session) ──▶ api.typesafe.ai
                              ~1 ms                                              ~310 ms

Deliberately small. It holds a connection, answers JSON, and does nothing else — no
framework, no dependencies beyond the standard library, no persistence, no background work.

Endpoints
    GET  /health         liveness plus counters; never calls the API, so it is free
    POST /decide         {"prompt": "..."} -> the Decision dict from jev_router (SIMPLE, frozen)
    POST /agent_decide   {"prompt": "..."} -> the AgentDecision dict from agent_decider
                         (shadow agent-shape layer; own connection and lock, so it runs
                         concurrently with /decide and cannot disturb it)
    POST /quit           clean shutdown (loopback only, same header requirement)

Security posture
  * Binds 127.0.0.1 only. There is no configuration that binds a routable interface.
  * Requires the header `X-Jev-Sidecar: 1` and rejects any request carrying an `Origin`
    header. Loopback alone is not sufficient: a web page in a browser can POST to
    127.0.0.1, and that would spend API credit. A browser cannot send a custom header
    cross-origin without a preflight, and the preflight is refused.
  * The API key is read once from .env at startup, kept in memory, and never logged,
    echoed, or returned. Request logging records a prompt *hash*, never prompt text.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_router import Router  # noqa: E402
from agent_decider import AgentRouter  # noqa: E402

GUARD_HEADER = "X-Jev-Sidecar"
MAX_BODY = 64 * 1024          # a prompt, not a payload
DEFAULT_PORT = int(os.environ.get("JEV_SIDECAR_PORT", "8787"))


class State:
    """Shared mutable state. `lock` serialises access to the one HTTPS connection."""

    def __init__(self, router: Router, agent_router: AgentRouter):
        self.router = router
        self.lock = threading.Lock()
        # The agent layer has its own connection and lock: it never queues behind, or
        # shares a socket with, the frozen SIMPLE router.
        self.agent_router = agent_router
        self.agent_lock = threading.Lock()
        self.agent_decisions = 0
        self.agent_failures = 0
        self.agent_cost_usd = 0.0
        self.started = time.time()
        self.decisions = 0
        self.failures = 0
        self.total_latency_ms = 0.0
        self.total_cost_usd = 0.0
        self.last_error: str | None = None

    def snapshot(self) -> dict:
        n = self.decisions
        return {
            "ok": True,
            "uptime_s": round(time.time() - self.started, 1),
            "decisions": n,
            "failures": self.failures,
            "mean_latency_ms": round(self.total_latency_ms / n, 1) if n else None,
            "spend_usd": round(self.total_cost_usd, 6),
            "last_error": self.last_error,
            "agent_decisions": self.agent_decisions,
            "agent_failures": self.agent_failures,
            "agent_spend_usd": round(self.agent_cost_usd, 6),
            "model": self.router.model,
            "pid": os.getpid(),
        }


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "jevd"
        sys_version = ""

        def log_message(self, fmt, *args):
            """Silence the default access log: it would print request lines, and a prompt
            must never reach a log. Failures are surfaced through /health instead."""

        # -- helpers ------------------------------------------------------
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> bool:
            """Reject anything a browser could have sent, and anything unlabelled."""
            if self.headers.get("Origin") is not None:
                self._send(403, {"error": "cross-origin requests are refused"})
                return False
            if self.headers.get(GUARD_HEADER) != "1":
                self._send(403, {"error": f"missing {GUARD_HEADER}: 1 header"})
                return False
            return True

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._send(400, {"error": "missing or oversized body"})
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": "body is not valid JSON"})
                return None

        # -- routes -------------------------------------------------------
        def do_OPTIONS(self):  # noqa: N802 — refuse CORS preflight outright
            self._send(403, {"error": "preflight refused"})

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] != "/health":
                self._send(404, {"error": "not found"})
                return
            self._send(200, state.snapshot())

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path not in ("/decide", "/agent_decide", "/quit"):
                self._send(404, {"error": "not found"})
                return
            if not self._guard():
                return

            if path == "/quit":
                self._send(200, {"ok": True, "stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            data = self._read_json()
            if data is None:
                return
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                self._send(400, {"error": "prompt is required"})
                return

            if path == "/agent_decide":
                self._agent_decide(prompt)
                return

            started = time.perf_counter()
            # One connection, one caller at a time. Decisions take ~300-800 ms, and a
            # hook issues one per prompt, so serialising costs nothing in practice and
            # removes a whole class of connection-reuse bugs.
            with state.lock:
                decision = state.router.decide(prompt)
            elapsed = (time.perf_counter() - started) * 1000

            state.decisions += 1
            state.total_latency_ms += elapsed
            state.total_cost_usd += decision.cost_usd
            if decision.error:
                state.failures += 1
                state.last_error = decision.error

            payload = decision.as_dict()
            payload["sidecar_latency_ms"] = round(elapsed, 1)
            payload["prompt_sha256_8"] = sha256(prompt.encode("utf-8")).hexdigest()[:8]
            self._send(200, payload)

        def _agent_decide(self, prompt: str) -> None:
            started = time.perf_counter()
            with state.agent_lock:
                decision = state.agent_router.decide(prompt)
            elapsed = (time.perf_counter() - started) * 1000
            state.agent_decisions += 1
            state.agent_cost_usd += decision.cost_usd
            if not decision.ok:
                state.agent_failures += 1
            payload = decision.as_dict()
            payload["sidecar_latency_ms"] = round(elapsed, 1)
            self._send(200, payload)

    return Handler


def serve(port: int, timeout: float) -> int:
    router = Router(timeout=timeout)   # reads the key from .env here, once
    state = State(router, AgentRouter(timeout=timeout))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    httpd.daemon_threads = True

    def stop(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"jevd listening on http://127.0.0.1:{port}  (pid {os.getpid()}, model {router.model})")
    print(f"  health : curl http://127.0.0.1:{port}/health")
    print(f"  decide : curl -H '{GUARD_HEADER}: 1' -d '{{\"prompt\":\"...\"}}' "
          f"http://127.0.0.1:{port}/decide")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        router.close()
        state.agent_router.close()
        snap = state.snapshot()
        print(f"\nstopped after {snap['uptime_s']}s — {snap['decisions']} decisions, "
              f"{snap['failures']} failures, ${snap['spend_usd']:.6f} spent")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Loopback Jev sidecar.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--timeout", type=float, default=5.0, help="upstream Jev timeout, seconds")
    args = ap.parse_args()
    try:
        return serve(args.port, args.timeout)
    except OSError as exc:
        # Most often: the port is already taken, which usually means jevd is already up.
        print(f"could not start on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
