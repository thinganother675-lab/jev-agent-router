"""UserPromptSubmit hook — SHADOW MODE ONLY.

Reads the prompt, asks Jev what it would have routed to, writes one telemetry line,
and returns nothing at all. It is a measurement instrument, not a router.

What it deliberately does NOT do, and must not be made to do without an explicit
decision to leave shadow mode:

  * it prints nothing on stdout, so no context is injected
  * it never emits `hookSpecificOutput`, `additionalContext`, `decision` or `continue`
  * it does not hide, select, rank or pre-load any skill
  * it does not block, delay or alter the prompt — it is registered with `async: true`,
    so Claude Code does not wait for it at all
  * every failure path exits 0 silently

Privacy: the prompt text is NOT logged by default. Each line carries a truncated
SHA-256 of the prompt, its length and word count — enough to correlate and to spot
repeats, not enough to reconstruct. Set JEV_SHADOW_LOG_PROMPTS=1 to opt in to storing
prompt text, which is off precisely because it is the kind of thing that silently
accumulates a transcript of everything you type.

Telemetry goes to <project>/telemetry/shadow.jsonl (override with JEV_SHADOW_LOG).

Two shadow layers, two namespaces, one hook invocation:

  * `shadow_skill` — the frozen SIMPLE router (`/decide`), one line per prompt in
    shadow.jsonl, exactly as before plus a `type` field.
  * `shadow_agent_decision` — the agent-shape layer (`/agent_decide`, src/agent_decider.py):
    tool / subagent / research / compaction / Mercury-candidate / escalation predictions, one
    line per prompt in telemetry/agent_shadow.jsonl (override with JEV_AGENT_SHADOW_LOG).
    Its skill and complexity fields are copied from the SIMPLE answer of the same prompt.
    Turn it off with JEV_AGENT_SHADOW=0 or by creating telemetry/AGENT_SHADOW_DISABLED.

Both requests run concurrently. Neither result is ever printed, injected or acted on.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JEV_SIDECAR_PORT", "8787"))
SIDECAR = f"http://127.0.0.1:{PORT}/decide"
LOG_PATH = Path(os.environ.get("JEV_SHADOW_LOG", ROOT / "telemetry" / "shadow.jsonl"))
LOG_PROMPTS = os.environ.get("JEV_SHADOW_LOG_PROMPTS") == "1"
AGENT_URL = f"http://127.0.0.1:{PORT}/agent_decide"
AGENT_LOG_PATH = Path(os.environ.get("JEV_AGENT_SHADOW_LOG", ROOT / "telemetry" / "agent_shadow.jsonl"))
AGENT_OFF_FLAG = ROOT / "telemetry" / "AGENT_SHADOW_DISABLED"

# Generous, because the hook is async and delays nothing. If the sidecar is down this
# fails fast on connection refused rather than waiting this out.
TIMEOUT_S = float(os.environ.get("JEV_SHADOW_TIMEOUT", "8"))


def write_line(record: dict, path: Path = LOG_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # telemetry must never be the reason anything fails


def agent_enabled() -> bool:
    return os.environ.get("JEV_AGENT_SHADOW", "1") != "0" and not AGENT_OFF_FLAG.exists()


def ask_sidecar(url: str, prompt: str, out: dict) -> None:
    """POST one prompt to the sidecar; put the parsed JSON, or the failure, into `out`.
    Runs in a thread, so it must never raise."""
    try:
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps({"prompt": prompt}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Jev-Sidecar": "1"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            out["data"] = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The sidecar answered but refused — most likely an old jevd without /agent_decide.
        out["source"], out["error"] = "sidecar", f"HTTP {exc.code} from sidecar"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        out["source"], out["error"] = "unreachable", f"{type(exc).__name__}: {exc}"[:160]
    except Exception as exc:  # noqa: BLE001
        out["source"], out["error"] = "hook_error", type(exc).__name__


def agent_record(base: dict, simple: dict | None, res: dict) -> dict:
    """Compose the shadow_agent_decision line: the user-facing schema in `decision`, raw
    probabilities in `detail`, cost and latency beside it. No prompt text, ever."""
    rec = {k: base[k] for k in ("ts", "session_id", "cwd", "prompt_sha256_12",
                                "prompt_chars", "prompt_words")}
    rec.update({"type": "shadow_agent_decision", "mode": "shadow"})
    d = res.get("data")
    if d is None:
        rec.update({"ok": False, "source": res.get("source", "unreachable"),
                    "api_error": res.get("error")})
        return rec
    # Skill and complexity come from the frozen SIMPLE call on the same prompt, not re-asked.
    # If that call failed they are null (not "none"), so the report cannot score a gap as
    # an abstention.
    s = simple or {}
    decision = {
        "skill": (s.get("skill") or "none") if simple else None,
        "skill_confidence": s.get("skill_confidence"),
        "skill_source": "simple_v1" if simple else "simple_v1_unavailable",
        "complexity": s.get("complexity"),
    }
    decision.update(d.get("decision") or {})
    rec.update({
        "ok": bool(d.get("ok")), "source": "sidecar", "schema": d.get("schema"),
        "decision": decision, "detail": d.get("detail"),
        "consistency_flags": d.get("consistency_flags"),
        "api_latency_ms": d.get("latency_ms"), "sidecar_latency_ms": d.get("sidecar_latency_ms"),
        "input_tokens": d.get("input_tokens"), "output_tokens": d.get("output_tokens"),
        "cost_usd": d.get("cost_usd"), "api_error": d.get("error"),
    })
    return rec


def main() -> int:
    started = time.perf_counter()
    try:
        # Bytes, decoded as UTF-8 explicitly. `json.load(sys.stdin)` decodes with the
        # Windows locale codec (cp1251 here): every Cyrillic prompt reached Jev as mojibake,
        # and any prompt containing a byte cp1251 leaves undefined (0x98, e.g. the second
        # byte of "И") raised UnicodeDecodeError and was dropped without a trace. That is
        # why shadow mode collected nothing from real sessions until 2026-09-23.
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return 0

    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "prompt_sha256_12": sha256(prompt.encode("utf-8")).hexdigest()[:12],
        "prompt_chars": len(prompt),
        "prompt_words": len(prompt.split()),
        "mode": "shadow",
    }
    if LOG_PROMPTS:
        record["prompt"] = prompt
    # Separate, explicit opt-in (off by default): sanitised prompts to a private local file
    # for building real-world benchmark cases. See hooks/prompt_collector.py.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import prompt_collector
        prompt_collector.collect(prompt, record["prompt_sha256_12"], record["ts"],
                                 record["session_id"])
    except Exception:  # noqa: BLE001
        pass

    simple_res: dict = {}
    agent_res: dict = {}
    threads = [threading.Thread(target=ask_sidecar, args=(SIDECAR, prompt, simple_res), daemon=True)]
    if agent_enabled():
        threads.append(threading.Thread(target=ask_sidecar, args=(AGENT_URL, prompt, agent_res),
                                        daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT_S + 2)

    d = simple_res.get("data")
    record["type"] = "shadow_skill"
    if d is not None:
        record.update({
            "ok": d.get("error") is None,
            "source": "sidecar",
            "action": d.get("action"),
            "skill": d.get("skill"),
            "confidence": d.get("skill_confidence"),
            "none_probability": d.get("none_probability"),
            "complexity": d.get("complexity"),
            "complexity_confidence": d.get("complexity_confidence"),
            "api_latency_ms": d.get("latency_ms"),
            "sidecar_latency_ms": d.get("sidecar_latency_ms"),
            "input_tokens": d.get("input_tokens"),
            "cost_usd": d.get("cost_usd"),
            "api_error": d.get("error"),
        })
    else:
        # Sidecar down, refused or slow. Shadow mode simply records the gap; there is
        # deliberately no direct-to-API fallback here, because that would put a ~1.2 s
        # cold call on a path whose whole point is to cost nothing.
        record.update({"ok": False, "source": simple_res.get("source", "unreachable"),
                       "api_error": simple_res.get("error")})

    record["hook_total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    write_line(record)
    if len(threads) > 1:
        simple = d if (d is not None and d.get("error") is None) else None
        arec = agent_record(record, simple, agent_res)
        arec["hook_total_ms"] = record["hook_total_ms"]
        write_line(arec, AGENT_LOG_PATH)
    return 0  # never anything on stdout


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a hook must not be able to break the session
        sys.exit(0)
