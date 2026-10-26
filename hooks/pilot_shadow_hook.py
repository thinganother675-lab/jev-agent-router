"""Claude/Codex shadow observation; explicit project or user scope, no control."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parent.parent
os.environ["JEV_SHADOW_TIMEOUT"] = "8"
os.environ["JEV_SHADOW_LOG_PROMPTS"] = "0"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks"))
from sidecar_runtime import ensure_running
from jev_shadow_hook import ask_sidecar, agent_record, write_line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--consumer", required=True, choices=("claude", "codex"))
    ap.add_argument("--scope", choices=("project", "user"), default="project")
    args = ap.parse_args()
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
        return 0
    # User scope is explicit in the globally installed handlers, not inferred.
    cwd = payload.get("cwd")
    if not cwd or (args.scope == "project" and Path(cwd).resolve() != ROOT.resolve()):
        return 0
    if (ROOT / "telemetry" / "pilot" / "DISABLED").exists():
        return 0
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    prompt = prompt.strip()
    session = str(payload.get("session_id") or "unknown")
    # One file per consumer/session avoids cross-client append collisions.
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", session)[:96]
    logs = ROOT / "telemetry" / "pilot" / args.consumer
    write_line({"ts": datetime.now(timezone.utc).isoformat(), "type": "hook_received",
                "consumer": args.consumer, "session_id": session, "cwd": cwd,
                "hook_event_name": payload.get("hook_event_name"), "input_fields": sorted(payload)},
               logs / f"{name}.events.jsonl")
    base = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "session_id": session, "turn_id": payload.get("turn_id"),
        "model": payload.get("model"), "consumer": args.consumer, "scope": args.scope,
        "run_id": str(uuid.uuid4()), "cwd": cwd,
        "prompt_sha256_12": sha256(prompt.encode("utf-8")).hexdigest()[:12],
        "prompt_chars": len(prompt), "prompt_words": len(prompt.split()),
        "mode": "shadow",
    }
    simple, agent = {}, {}
    ready = ensure_running()
    if ready:
        # The existing helper has an 8-second timeout, no retry and no API-key copy.
        threads = [
            threading.Thread(target=ask_sidecar, args=("http://127.0.0.1:8787/decide", prompt, simple)),
            threading.Thread(target=ask_sidecar, args=("http://127.0.0.1:8787/agent_decide", prompt, agent)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    else:
        for result in (simple, agent):
            result.update(source="unreachable", error="sidecar not ready")
    data = simple.get("data")
    record = dict(base, type="shadow_skill", source=simple.get("source", "sidecar"))
    if data is not None:
        record.update(ok=data.get("error") is None, decision=data)
    else:
        record.update(ok=False, api_error=simple.get("error"))
    write_line(record, logs / f"{name}.skill.jsonl")
    second = agent_record(base, data if data and not data.get("error") else None, agent)
    second.update({k: base[k] for k in ("consumer", "scope", "model", "turn_id", "run_id")})
    write_line(second, logs / f"{name}.agent.jsonl")
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        write_line({"ts": datetime.now(timezone.utc).isoformat(), "type": "hook_failure",
                    "error_type": type(exc).__name__},
                   ROOT / "telemetry" / "pilot" / "hook_failures.jsonl")
    # Never return context, a recommendation, a blocking decision or stderr.
    raise SystemExit(0)
