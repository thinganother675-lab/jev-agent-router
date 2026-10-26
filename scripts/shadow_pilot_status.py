"""Read pilot health and telemetry without printing prompts or credentials."""
import json
from pathlib import Path
from sidecar_runtime import health

ROOT = Path(__file__).resolve().parent.parent
result = {"health": health(), "disabled": (ROOT / "telemetry/pilot/DISABLED").exists(), "consumers": {}}
for consumer in ("claude", "codex"):
    rows = []
    for path in (ROOT / "telemetry/pilot" / consumer).glob("*.jsonl"):
        if path.name.endswith("events.jsonl"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row.get("type") in ("shadow_skill", "shadow_agent_decision"):
                    rows.append(row)
            except ValueError:
                pass
    result["consumers"][consumer] = {
        "records": len(rows), "successful": sum(r.get("ok") is True for r in rows),
        "global_records": sum(r.get("scope") == "user" for r in rows),
        "global_successful": sum(r.get("scope") == "user" and r.get("ok") is True for r in rows),
        "failed": sum(r.get("ok") is not True for r in rows),
        "sessions": sorted({str(r.get("session_id")) for r in rows}),
        "last_timestamp": max((r["ts"] for r in rows), default=None),
        "models_from_hook": sorted({str(r["model"]) for r in rows if r.get("model")}),
    }
print(json.dumps(result, ensure_ascii=False, indent=2))
