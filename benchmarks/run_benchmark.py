"""Benchmark the Jev routing layer against 15 labelled cases.

    python benchmarks/run_benchmark.py

Measures what actually matters for the decision to adopt or drop this:
  * skill routing accuracy, split into hits, misroutes and misses
  * false-positive rate on the cases where NO skill should fire
  * complexity classification accuracy
  * latency (warm connection) and cost per decision
  * total cost of the run

Cost: ~15 x 1700 input tokens ~= $0.0011 per full run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jev_router import Router, Decision  # noqa: E402

CASES = json.loads((Path(__file__).with_name("routing_cases.json")).read_text(encoding="utf-8"))["cases"]


def classify(case: dict, d: Decision) -> str:
    """How the router did on this case's skill routing."""
    want, got = case["expected_skill"], d.skill
    if want is None:
        return "correct_abstain" if got is None else "false_positive"
    if got is None:
        return "missed"
    return "correct_skill" if got == want else "misrouted"


def main() -> int:
    rows: list[dict] = []
    with Router() as router:
        router.decide("warm up the connection")  # pay the TLS cost once, outside the measurements
        for case in CASES:
            d = router.decide(case["prompt"])
            verdict = classify(case, d)
            cx_ok = d.complexity == case["expected_complexity"]
            rows.append({**case, "verdict": verdict, "complexity_ok": cx_ok, **d.as_dict()})
            mark = {"correct_skill": "OK ", "correct_abstain": "OK ",
                    "false_positive": "FP ", "misrouted": "WRONG", "missed": "MISS"}[verdict]
            print(f"{mark} {case['id']:22} {str(d.skill):28} conf={d.skill_confidence:.2f} "
                  f"none={d.none_probability:.2f} cx={d.complexity:<8}{'ok' if cx_ok else 'X ('+case['expected_complexity']+')':10} "
                  f"{d.latency_ms:6.0f}ms")

    n = len(rows)
    counts = {k: sum(1 for r in rows if r["verdict"] == k)
              for k in ("correct_skill", "correct_abstain", "false_positive", "misrouted", "missed")}
    should_fire = [r for r in rows if r["expected_skill"] is not None]
    should_not = [r for r in rows if r["expected_skill"] is None]
    cx_ok = sum(1 for r in rows if r["complexity_ok"])
    lat = sorted(r["latency_ms"] for r in rows)
    tokens = sum(r["input_tokens"] for r in rows)
    cost = sum(r["cost_usd"] for r in rows)

    print("\n" + "=" * 72)
    print(f"Routing correct      : {counts['correct_skill'] + counts['correct_abstain']}/{n}")
    print(f"  right skill picked : {counts['correct_skill']}/{len(should_fire)}")
    print(f"  right to abstain   : {counts['correct_abstain']}/{len(should_not)}")
    print(f"  misrouted          : {counts['misrouted']}")
    print(f"  missed a skill     : {counts['missed']}")
    print(f"  false positives    : {counts['false_positive']}/{len(should_not)}")
    print(f"Complexity correct   : {cx_ok}/{n}")
    print(f"Latency (warm)       : min={lat[0]:.0f} med={lat[n // 2]:.0f} max={lat[-1]:.0f} ms")
    print(f"Cost                 : {tokens} input tokens, ${cost:.6f} total, ${cost / n:.6f} per decision")
    print(f"Projected on $5      : ~{int(5 / (cost / n)):,} decisions")

    out = Path(__file__).parent / "results.json"
    out.write_text(json.dumps({"rows": rows, "counts": counts, "complexity_correct": cx_ok,
                               "n": n, "total_cost_usd": cost, "total_input_tokens": tokens,
                               "latency_ms": {"min": lat[0], "median": lat[n // 2], "max": lat[-1]}},
                              indent=2), encoding="utf-8")
    print(f"\nWritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
