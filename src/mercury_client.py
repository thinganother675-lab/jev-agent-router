"""Minimal Inception Mercury client — stdlib only, OpenAI-compatible.

Contract verified against https://api.inceptionlabs.ai/openapi.json (Inception API 1.0.0)
and the live `/v1/models` endpoint on 2026-09-22.

    POST /v1/chat/completions   OpenAI-shaped; extra params: reasoning_effort, diffusing,
                                realtime, reasoning_summary
    GET  /v1/models             {"data": [ModelObject]} with pricing and context_length

Two ways to call it:
    chat(...)                   one-shot, new connection (urllib) — measures cold cost
    MercuryClient()             keeps one HTTPS connection warm — what the sidecar pattern needs

The API key is read from the environment only. It is never logged, never written to
disk, and never included in any returned structure or exception message.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from net import connect, proxy_for

HOST = os.environ.get("MERCURY_HOST", "api.inceptionlabs.ai")
BASE_URL = f"https://{HOST}"
DEFAULT_MODEL = os.environ.get("MERCURY_MODEL", "mercury-2.5")


# $ per million tokens, mercury-2.5, as reported by GET /v1/models on 2026-09-22.
# These are the 80%-launch-discount rates (list: 0.20 / 0.75). Re-read /v1/models
# rather than trusting these constants if the number matters.
PRICE_IN_PER_MTOK = 0.04
PRICE_OUT_PER_MTOK = 0.15
PRICE_CACHED_IN_PER_MTOK = 0.004

# Accepted env var names, in priority order. `mercury_api` is what this machine's
# .env already uses; INCEPTION_API_KEY is what the official SDKs expect.
KEY_NAMES = ("mercury_api", "INCEPTION_API_KEY", "MERCURY_API_KEY")

REASONING_EFFORTS = ("instant", "low", "medium", "high")


class MercuryError(RuntimeError):
    """Any Mercury call that did not return a usable answer."""


def load_dotenv(path: Path | str | None = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ without overwriting."""
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_api_key() -> str:
    """Return the key from the first env var that has one. Never print this."""
    load_dotenv()
    for name in KEY_NAMES:
        val = os.environ.get(name)
        if val:
            return val
    raise MercuryError(f"No API key found. Set one of: {', '.join(KEY_NAMES)}")


def cost_usd(usage: dict) -> float:
    """Price a usage block. Cached prompt tokens bill at a tenth of the input rate."""
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    fresh = max(usage.get("prompt_tokens", 0) - cached, 0)
    return round(
        fresh / 1e6 * PRICE_IN_PER_MTOK
        + cached / 1e6 * PRICE_CACHED_IN_PER_MTOK
        + usage.get("completion_tokens", 0) / 1e6 * PRICE_OUT_PER_MTOK,
        8,
    )


def list_models() -> tuple[list[dict], float]:
    req = urllib.request.Request(
        f"{BASE_URL}/v1/models",
        headers={"Authorization": f"Bearer {get_api_key()}", "Accept": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise MercuryError(f"HTTP {exc.code} on GET /v1/models: "
                           f"{exc.read().decode(errors='replace')[:300]}") from None
    return body["data"], (time.perf_counter() - started) * 1000.0


def build_payload(messages, *, model: str = DEFAULT_MODEL, **kw) -> dict:
    """Assemble a chat-completions body, dropping unset optional params."""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    payload = {"model": model, "messages": messages}
    for key in ("max_tokens", "temperature", "stop", "tools", "tool_choice", "stream",
                "stream_options", "response_format", "reasoning_effort", "diffusing"):
        if kw.get(key) is not None:
            payload[key] = kw[key]
    return payload


class MercuryClient:
    """Holds one warm HTTPS connection, the way the Jev Router does.

    Non-streaming calls return the parsed body with `latency_ms` and `cost_usd` added.
    Streaming calls are driven through `stream_chat`, which yields raw SSE data objects.
    """

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = 120.0, host: str = HOST):
        self.model = model
        self.timeout = timeout
        self.host = host
        self._conn: http.client.HTTPSConnection | None = None
        self._headers = {
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _connection(self) -> http.client.HTTPSConnection:
        if self._conn is None:
            # CONNECT-tunnelled when a proxy is configured. Mercury is geo-blocked
            # on the direct route from here, so this is required, not an optimisation.
            self._conn = connect(self.host, self.timeout, disable_env="MERCURY_NO_PROXY")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close(); return False

    def _raw_post(self, body: str):
        """POST with one retry — a kept-alive connection can be closed by the far end."""
        for attempt in (1, 2):
            conn = self._connection()
            try:
                conn.request("POST", "/v1/chat/completions", body, self._headers)
                resp = conn.getresponse()
                if resp.status != 200:
                    detail = resp.read()[:300].decode(errors="replace")
                    self.close()
                    raise MercuryError(f"HTTP {resp.status}: {detail}")
            except (http.client.HTTPException, OSError, TimeoutError):
                self.close()
                if attempt == 2:
                    raise
                continue
            return resp
        raise AssertionError("unreachable")

    def chat(self, messages, **kw) -> dict:
        payload = build_payload(messages, model=kw.pop("model", self.model), **kw)
        payload.pop("stream", None)
        started = time.perf_counter()
        resp = self._raw_post(json.dumps(payload))
        data = json.loads(resp.read().decode())
        data["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        data["cost_usd"] = cost_usd(data.get("usage", {}))
        return data

    def stream_chat(self, messages, **kw):
        """Yield (elapsed_ms, chunk_dict) for each SSE event. Final usage chunk included
        when stream_options={"include_usage": True} is passed."""
        kw.setdefault("stream_options", {"include_usage": True})
        payload = build_payload(messages, model=kw.pop("model", self.model), stream=True, **kw)
        started = time.perf_counter()
        resp = self._raw_post(json.dumps(payload))
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            yield (time.perf_counter() - started) * 1000, json.loads(data)


def text_of(response: dict) -> str:
    """The assistant message content of a non-streaming response, or ''."""
    choices = response.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content") or ""
