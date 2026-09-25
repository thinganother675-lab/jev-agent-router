"""Jev determinism, measured two ways on the final-test runs.

    python benchmarks/stability_extra.py

1. Across runs: the same variant, same prompt, three runs (router_scoring.stability does the
   winner/abstention part; this adds the probability spread of SIMPLE's `none`).
2. Within a run: SIMPLE's request and HYBRID's first request are byte-identical and were sent
   seconds apart (round-robin). Any difference between them is pure API non-determinism.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NONE = "__none__"


def load(variant: str) -> list[dict]:
    files = sorted((ROOT / "results" / variant).glob("test_run*.json"))
    return [{r["id"]: r for r in json.loads(f.read_text(encoding="utf-8"))["records"]} for f in files]


def top(probs: dict) -> tuple[str, float]:
    k = max(probs, key=probs.get)
    return k, probs[k]


def main() -> int:
    simple, hybrid = load("simple"), load("hybrid")
    out = {}

    # 1. across runs, SIMPLE none-probability spread
    spreads, top_flips = [], 0
    for cid in simple[0]:
        nones = [run[cid]["trace"].get("none_p", 0.0) for run in simple]
        spreads.append(max(nones) - min(nones))
        if len({top(run[cid]["trace"]["probs"])[0] for run in simple}) > 1:
            top_flips += 1
    out["simple_across_runs"] = {
        "runs": len(simple), "cases": len(spreads),
        "none_p_spread_max": round(max(spreads), 4), "none_p_spread_mean": round(statistics.mean(spreads), 4),
        "cases_spread_over_0.05": sum(1 for s in spreads if s > 0.05),
        "top_option_flips": top_flips}

    # 2. within a run, identical request: SIMPLE vs HYBRID call 1
    diffs, flips, pred_flips = [], 0, 0
    for s_run, h_run in zip(simple, hybrid):
        for cid, s in s_run.items():
            h = h_run[cid]
            sp, hp = s["trace"].get("probs", {}), h["trace"].get("probs", {})
            if not sp or not hp:
                continue
            diffs.append(abs(s["trace"].get("none_p", 0) - h["trace"].get("none_p", 0)))
            if top(sp)[0] != top(hp)[0]:
                flips += 1
    out["identical_request_within_run"] = {
        "pairs": len(diffs), "none_p_absdiff_max": round(max(diffs), 4),
        "none_p_absdiff_mean": round(statistics.mean(diffs), 4),
        "pairs_diff_over_0.05": sum(1 for d in diffs if d > 0.05),
        "top_option_flips": flips}
    print(json.dumps(out, indent=2))
    (ROOT / "results" / "stability_extra_test.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
