"""Opt-in local collection of sanitised prompts, for building real-world benchmark cases.

OFF by default. It turns on only when one of these is true:

  * the environment has JEV_SHADOW_COLLECT=1, or
  * the file telemetry/private/COLLECT_ENABLED exists (so it can be switched on without
    editing the hook command in .claude/settings.json).

What it writes, and where:

  * telemetry/private/collected_prompts.jsonl — git-ignored, local only, never sent anywhere
    by this module. One line per prompt: the redacted text, its hash (the same 12-char hash
    shadow.jsonl uses, so the two join), length, session id, timestamp.

What it refuses to keep:

  * any prompt that looks like it carries a credential — API-key shapes, bearer tokens,
    private-key blocks, `password=` / `secret:` assignments, JWTs, connection strings with
    a password. Such a prompt is recorded as `{"excluded": "secret_suspected"}` with its
    hash and length only; the text is never written.
  * pasted file bodies beyond MAX_CHARS — the prompt is truncated, because a router decides
    on the ask, and a long paste is exactly where sensitive file content hides.

Redaction applied to what is kept: e-mail addresses, URL query strings, long digit runs
(phones, card-like numbers) and the Windows user-profile segment of paths.

This is a measurement aid for a benchmark, not a transcript. Turn it off by deleting the
flag file; delete telemetry/private/ to discard everything it collected.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DIR = ROOT / "telemetry" / "private"
FLAG = PRIVATE_DIR / "COLLECT_ENABLED"
OUT = PRIVATE_DIR / "collected_prompts.jsonl"
MAX_CHARS = 2000

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(sk|pk|rk)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bapik_[A-Za-z0-9]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}\b"),                       # telegram bot token
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token|client[_-]?secret)\s*[:=]\s*\S{6,}"),
    re.compile(r"(?i)\b[a-z]+://[^\s:/@]+:[^\s@/]{3,}@"),                     # user:pass@host
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),                                        # long hex blob
]

REDACTIONS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"(https?://[^\s?#]+)\?[^\s#]*"), r"\1?<query>"),
    (re.compile(r"\b\d{9,}\b"), "<number>"),
    (re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s]+"), r"\1<user>"),
]


def enabled() -> bool:
    return os.environ.get("JEV_SHADOW_COLLECT") == "1" or FLAG.exists()


def looks_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def redact(text: str) -> str:
    for pat, repl in REDACTIONS:
        text = pat.sub(repl, text)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f" …[truncated, {len(text) - MAX_CHARS} more chars]"
    return text


def collect(prompt: str, prompt_hash: str, ts: str, session_id: str | None) -> None:
    """Append one sanitised record. Never raises."""
    if not enabled():
        return
    try:
        rec = {"ts": ts, "session_id": session_id, "prompt_sha256_12": prompt_hash,
               "prompt_chars": len(prompt)}
        if looks_secret(prompt):
            rec["excluded"] = "secret_suspected"
        else:
            rec["prompt_redacted"] = redact(prompt)
        PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — collection must never be why anything fails
        pass
