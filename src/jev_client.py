"""Minimal TypeSafe Jev (System One) client — stdlib only.

Contract (from https://api.typesafe.ai/openapi.json, spec version 0.2.0):
  GET  /v1/models    -> {"models": [{name, description, release_date}]}
  POST /v1/systemone -> {"model", "answers": {name: Answer}, "usage": {input_tokens, output_tokens}}

Answer types:
  noul   -> {"type": "noul",   "noul": p_yes}
  choice -> {"type": "choice", "choice": name, "confidence": c, "probabilities": {...}}
  score  -> {"type": "score",  "score": s, "confidence": c, "legend": {...}, "probabilities": {...}}

The API key is read from the environment only. It is never logged, never written
to disk, and never included in any returned structure.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai")
DEFAULT_MODEL = os.environ.get("JEV_MODEL", "jev-latest")

# $ per million input tokens. Output tokens are free of charge.
INPUT_PRICE_PER_MTOK = 0.042

# Accepted env var names, in priority order. `jev_api` is what this machine's
# .env already uses; the other two are what third-party integrations expect.
KEY_NAMES = ("jev_api", "TYPESAFE_API_KEY", "JEV_API_KEY")


class JevError(RuntimeError):
    """Any Jev call that did not return a usable answer."""


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
    raise JevError(f"No API key found. Set one of: {', '.join(KEY_NAMES)}")


def _request(method: str, path: str, payload: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # Read the body for the reason, but never echo request headers.
        detail = exc.read().decode(errors="replace")[:500]
        raise JevError(f"HTTP {exc.code} on {method} {path}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise JevError(f"{type(exc).__name__} on {method} {path}: {exc}") from None
    return body, (time.perf_counter() - started) * 1000.0


def list_models() -> tuple[list[dict], float]:
    body, ms = _request("GET", "/v1/models")
    return body["models"], ms


def ask(state, questions: dict, model: str = DEFAULT_MODEL, timeout: float = 30.0) -> dict:
    """POST /v1/systemone. Returns the response with `latency_ms` and `cost_usd` added."""
    body, ms = _request(
        "POST", "/v1/systemone",
        {"state": state, "model": model, "questions": questions},
        timeout=timeout,
    )
    body["latency_ms"] = round(ms, 1)
    body["cost_usd"] = cost_usd(body["usage"]["input_tokens"])
    return body


def cost_usd(input_tokens: int) -> float:
    return round(input_tokens / 1_000_000 * INPUT_PRICE_PER_MTOK, 8)


# --- question builders -------------------------------------------------------

def noul(instructions, criteria: dict | None = None) -> dict:
    q = {"type": "noul", "instructions": instructions}
    if criteria:
        q["criteria"] = criteria
    return q


def choice(criteria: dict, instructions=None) -> dict:
    q = {"type": "choice", "criteria": criteria}
    if instructions is not None:
        q["instructions"] = instructions
    return q


def score(criteria: list, instructions=None) -> dict:
    q = {"type": "score", "criteria": criteria}
    if instructions is not None:
        q["instructions"] = instructions
    return q
