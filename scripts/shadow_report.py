"""Aggregate shadow-mode telemetry into the numbers that decide whether to go active.

    python scripts/shadow_report.py
    python scripts/shadow_report.py --since 2026-09-22 --json

Reads telemetry/shadow.jsonl (one JSON object per prompt, written by hooks/jev_shadow_hook.py).

The question this report exists to answer is not "does Jev work" — the benchmark answers
that on labelled cases. It is: **on the prompts this person actually writes, how often
would routing have fired, how confident was it, what would it have cost, and how often was
it unavailable?** A high `none` rate is a good result, not a bad one: it means the layer
stays out of the way.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "telemetry" / "shadow.jsonl"


def load(path: Path, since: str | None) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and (rec.get("ts") or "") < since:
            continue
        rows.append(rec)
    return rows


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))], 1)


def histogram(values: list[float], width: int = 34) -> list[str]:
    """Confidence in ten buckets. The shape matters more than the numbers: a bimodal
    distribution means the thresholds separate cleanly, a flat one means they do not."""
    buckets = Counter(min(int(v * 10), 9) for v in values)
    top = max(buckets.values(), default=1)
    out = []
    for b in range(10):
        n = buckets.get(b, 0)
        bar = "#" * int(round(n / top * width)) if n else ""
        out.append(f"    {b / 10:.1f}-{(b + 1) / 10:.1f}  {n:>4}  {bar}")
    return out


def build(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    unreachable = [r for r in rows if r.get("source") == "unreachable"]
    api_fail = [r for r in rows if r.get("source") == "sidecar" and not r.get("ok")]
    fired = [r for r in ok if r.get("skill")]
    abstained = [r for r in ok if not r.get("skill")]
    lat = [r["sidecar_latency_ms"] for r in ok if r.get("sidecar_latency_ms")]
    hook_lat = [r["hook_total_ms"] for r in rows if r.get("hook_total_ms")]
    conf = [r["confidence"] for r in fired if r.get("confidence") is not None]

    return {
        "decisions": len(rows),
        "sessions": len({r.get("session_id") for r in rows if r.get("session_id")}),
        "unique_prompts": len({r.get("prompt_sha256_12") for r in rows}),
        "succeeded": len(ok),
        "sidecar_unreachable": len(unreachable),
        "api_failures": len(api_fail),
        "availability": round(len(ok) / len(rows), 4) if rows else 0.0,
        "abstained": len(abstained),
        "none_rate": round(len(abstained) / len(ok), 4) if ok else 0.0,
        "fired": len(fired),
        "actions": dict(Counter(r.get("action") for r in ok)),
        "skills": dict(Counter(r["skill"] for r in fired).most_common()),
        "complexity": dict(Counter(r.get("complexity") for r in ok)),
        "confidence_values": conf,
        "confidence_mean": round(statistics.mean(conf), 3) if conf else None,
        "latency_ms": {"median": pct(lat, 50), "p90": pct(lat, 90), "p95": pct(lat, 95),
                       "max": round(max(lat), 1) if lat else 0},
        "hook_total_ms": {"median": pct(hook_lat, 50), "p95": pct(hook_lat, 95)},
        "spend_usd": round(sum(r.get("cost_usd") or 0 for r in rows), 6),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in rows),
        "prompts_stored": sum(1 for r in rows if "prompt" in r),
    }


def render(s: dict) -> None:
    n = s["decisions"]
    if not n:
        print("No telemetry yet.\n"
              "Start the sidecar (python src/jev_sidecar.py), then use Claude Code in this\n"
              "project. Every prompt appends one line to telemetry/shadow.jsonl.")
        return

    print(f"Shadow-mode telemetry — {n} decisions across {s['sessions']} session(s), "
          f"{s['unique_prompts']} unique prompts\n")

    print("Availability")
    print(f"  succeeded            : {s['succeeded']}/{n}  ({s['availability']:.1%})")
    print(f"  sidecar unreachable  : {s['sidecar_unreachable']}")
    print(f"  API failures         : {s['api_failures']}")

    print("\nWhat it would have done")
    print(f"  abstained (none)     : {s['abstained']}/{s['succeeded']}  "
          f"({s['none_rate']:.1%}) — higher is quieter, not worse")
    print(f"  would have fired     : {s['fired']}")
    for action, count in sorted(s["actions"].items(), key=lambda kv: -kv[1]):
        print(f"    {str(action):<18} {count}")

    if s["skills"]:
        print("\nSkill distribution")
        for skill, count in s["skills"].items():
            print(f"  {skill:<38} {count:>4}  {'#' * min(count, 30)}")

    if s["complexity"]:
        print("\nComplexity")
        for level in ("trivial", "normal", "complex", "unknown"):
            if level in s["complexity"]:
                print(f"  {level:<10} {s['complexity'][level]:>4}")

    if s["confidence_values"]:
        print(f"\nConfidence on fired decisions (mean {s['confidence_mean']})")
        for line in histogram(s["confidence_values"]):
            print(line)

    L, H = s["latency_ms"], s["hook_total_ms"]
    print("\nLatency")
    print(f"  jev via sidecar      : median {L['median']}ms  p90 {L['p90']}ms  "
          f"p95 {L['p95']}ms  max {L['max']}ms")
    print(f"  whole hook           : median {H['median']}ms  p95 {H['p95']}ms  "
          f"(async — not added to your prompt)")

    print("\nSpend")
    print(f"  total                : ${s['spend_usd']:.6f} over {n} decisions "
          f"({s['input_tokens']} input tokens)")
    if n:
        print(f"  per decision         : ${s['spend_usd'] / n:.6f}")
        print(f"  $5 credit would last : ~{int(5 / (s['spend_usd'] / n)):,} decisions"
              if s["spend_usd"] else "")

    if s["prompts_stored"]:
        print(f"\n  NOTE: {s['prompts_stored']} lines contain full prompt text "
              f"(JEV_SHADOW_LOG_PROMPTS=1 was set).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--since", default=None, help="ISO timestamp prefix, e.g. 2026-09-22")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load(Path(args.log), args.since)
    summary = build(rows)
    if args.json:
        summary.pop("confidence_values", None)
        print(json.dumps(summary, indent=2))
    else:
        render(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
