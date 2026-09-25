"""Tune OFFICIAL-TUNED and HYBRID thresholds on development-set probe traces only.

    python benchmarks/tune_dev.py            # prints the grids' best regions
    python benchmarks/tune_dev.py --write    # writes benchmarks/tuned_params.json

Reads results/<variant>/dev_probe_run1.json (every second pass forced, so any threshold can
be replayed offline through the same pure decision function the live path uses).

Objective: weighted cost (FP 3, misroute 2, FN 1) — the stated priority is routing quality,
then false positives. On 79 cases a single best cell is mostly noise, so the selection uses
the cost **averaged over each config's grid neighbourhood** (±1 step on every axis), which
prefers the middle of a good plateau over its edge. Ties on that are broken towards the
defaults / starting hypothesis, then towards fewer second passes.

Revision note (2026-09-23, before any test run): the first version ranked by raw cost and
broke hybrid ties by second-pass rate first. With 792 of 2,304 hybrid configs tied at zero
cost that drove the thresholds into a degenerate corner, and for OFFICIAL it picked a single
knife-edge cell. Both were visible on dev only; the test split had not been run.

It never opens a test-split file.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import router_scoring as rs  # noqa: E402
from router_variants import (HYBRID_START, OFFICIAL_DEFAULTS, hybrid_decide, hybrid_stage1,  # noqa: E402
                             official_decide)


def load(variant: str) -> list[dict]:
    p = ROOT / "results" / variant / "dev_probe_run1.json"
    return json.loads(p.read_text(encoding="utf-8"))["records"]


def evaluate(records, labels, decide, params):
    rows = []
    for r in records:
        pred, _ = decide(r["trace"], params)
        rows.append({**labels[r["id"]], "pred": pred})
    rows = [x for x in rows if not x["ambiguous"]]
    return rs.metrics(rows)


def distance(p: dict, ref: dict) -> float:
    return sum(abs(p[k] - ref[k]) for k in p if isinstance(p[k], (int, float)) and k in ref)


def select(grid: dict, axes: dict, ref: dict) -> list:
    """Rank by neighbourhood-averaged cost, then closeness to `ref`, then second passes.

    Returns [(smoothed_cost, params, metrics)] best first."""
    ranked = []
    for idx, (p, m) in grid.items():
        neigh = [grid[j][1]["weighted_cost_per_100"]
                 for j in itertools.product(*(range(max(0, i - 1), min(len(v), i + 2))
                                              for i, v in zip(idx, axes.values())))]
        smooth = round(sum(neigh) / len(neigh), 2)
        ranked.append((smooth, p, m))
    ranked.sort(key=lambda x: (x[0], x[2]["weighted_cost_per_100"], distance(x[1], ref),
                               x[2].get("second_pass_rate", 0)))
    return ranked


def tune_official(labels):
    recs = load("official_default")
    axes = {"gate_threshold": [x / 20 for x in range(0, 17)], "fits_threshold": [x / 20 for x in range(0, 19)]}
    grid = {}
    for idx in itertools.product(*(range(len(v)) for v in axes.values())):
        p = {**OFFICIAL_DEFAULTS, **{k: axes[k][i] for k, i in zip(axes, idx)}}
        grid[idx] = (p, evaluate(recs, labels, official_decide, p))
    near = select(grid, axes, OFFICIAL_DEFAULTS)
    default = evaluate(recs, labels, official_decide, OFFICIAL_DEFAULTS)
    return near, default, len(grid)


def tune_hybrid(labels):
    recs = load("hybrid")
    vals = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
    axes = {"none_accept": vals, "win_accept": vals, "gap": [0.0, 0.2, 0.3, 0.4, 0.5, 0.6],
            "final_min": [0.3, 0.45, 0.5, 0.6, 0.7, 0.8]}
    grid = {}
    for idx in itertools.product(*(range(len(v)) for v in axes.values())):
        p = {**HYBRID_START, **{k: axes[k][i] for k, i in zip(axes, idx)}}
        m = evaluate(recs, labels, hybrid_decide, p)
        second = sum(1 for r in recs if hybrid_stage1(r["trace"]["probs_full"], p)[0] == "rerank") / len(recs)
        m["second_pass_rate"] = round(second, 3)
        grid[idx] = (p, m)
    near = select(grid, axes, HYBRID_START)
    start = evaluate(recs, labels, hybrid_decide, HYBRID_START)
    return near, start, len(grid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    labels = rs.load_labels("dev")

    near_o, default_o, n_o = tune_official(labels)
    print(f"OFFICIAL  default {default_o}")
    print(f"          {n_o} configs; top 8 by neighbourhood-averaged cost:")
    for c, p, m in near_o[:8]:
        print(f"   smooth {c:5.1f} raw {m['weighted_cost_per_100']:5.1f} gate {p['gate_threshold']:.2f} fits {p['fits_threshold']:.2f}  "
              f"acc {m['acc']:.3f} fp {m['fp']} fn {m['fn']} mis {m['misroute']}")

    near_h, start_h, n_h = tune_hybrid(labels)
    print(f"\nHYBRID    start {start_h}")
    print(f"          {n_h} configs; top 8 by neighbourhood-averaged cost:")
    for c, p, m in near_h[:8]:
        print(f"   smooth {c:5.1f} raw {m['weighted_cost_per_100']:5.1f} none {p['none_accept']:.2f} win {p['win_accept']:.2f} gap {p['gap']:.2f} "
              f"final {p['final_min']:.2f}  acc {m['acc']:.3f} fp {m['fp']} fn {m['fn']} mis {m['misroute']} "
              f"2nd {m['second_pass_rate']:.2f}")

    if args.write:
        out = {"official_tuned": {k: near_o[0][1][k] for k in ("shortlist", "gate_threshold", "fits_threshold", "excerpt_chars")},
               "hybrid": {k: near_h[0][1][k] for k in ("none_accept", "win_accept", "gap", "top_n", "final_min", "excerpt_chars")},
               "_tuned_on": "dev split only, probe traces results/*/dev_probe_run1.json",
               "_objective": "weighted cost FP3/mis2/FN1, averaged over the +-1 grid neighbourhood; ties -> raw cost, closest to default/start, fewer second passes"}
        (HERE / "tuned_params.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        print("\nwrote benchmarks/tuned_params.json:", json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
