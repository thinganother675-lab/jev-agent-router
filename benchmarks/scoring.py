"""Shared scoring for the routing arms — and the thing that keeps the comparison honest.

`blind_prompts()` is the only way the benchmark drivers are allowed to reach the cases.
It returns prompts and ids and *nothing else*: no expected skill, no expected complexity,
no other arm's answers. Labels are re-attached by `score()` after every inference has
already happened, so no inference code path can read them.

That is not ceremony. The old 15-case set was used while the Jev router's prompt was being
written, so it is a development set, not a test set; `blind_cases_50.json` is the replacement
and must stay untouched by tuning. See BENCHMARK.md.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

LABEL_KEYS = ("expected_skill", "expected_complexity", "rationale", "trap")


def _read(filename: str) -> dict:
    return json.loads((HERE / filename).read_text(encoding="utf-8"))


def load_cases(filename: str) -> list[dict]:
    """Cases with labels attached. Only `score()` may call this.

    Two layouts are supported. The original 15-case file carries its labels inline.
    `blind_cases_50.json` keeps prompts and labels in two files on purpose, so that
    reading the prompts cannot accidentally surface an answer; the sibling
    `*_labels.json` is merged in here and nowhere else.
    """
    data = _read(filename)
    cases = data["cases"]
    sidecar = HERE / f"{Path(filename).stem}_labels.json"
    if sidecar.is_file():
        labels = _read(sidecar.name)["labels"]
        cases = [{**c, **labels[c["id"]]} for c in cases]
    return cases


def blind_prompts(filename: str) -> list[dict]:
    """Inference input: id and prompt only. Reads the prompt file, never the labels."""
    return [{"id": c["id"], "prompt": c["prompt"]} for c in _read(filename)["cases"]]


def verdict(expected_skill, got_skill) -> str:
    if expected_skill is None:
        return "correct_abstain" if got_skill is None else "false_positive"
    if got_skill is None:
        return "missed"
    return "correct_skill" if got_skill == expected_skill else "misrouted"


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. n is 15-50 here, so interpolation would be false precision."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))
    return round(s[k], 1)


def score(filename: str, decisions: dict[str, dict]) -> dict:
    """Attach labels *after* inference and compute the comparison metrics.

    `decisions` maps case id -> a dict with at least: skill, complexity, latency_ms,
    input_tokens, output_tokens, cost_usd, error.
    """
    cases = load_cases(filename)
    rows, counts = [], {k: 0 for k in
                        ("correct_skill", "correct_abstain", "false_positive", "misrouted", "missed")}
    for case in cases:
        d = decisions[case["id"]]
        v = verdict(case["expected_skill"], d.get("skill"))
        counts[v] += 1
        rows.append({**case, **d, "verdict": v,
                     "complexity_ok": d.get("complexity") == case["expected_complexity"]})

    should_fire = [r for r in rows if r["expected_skill"] is not None]
    should_not = [r for r in rows if r["expected_skill"] is None]

    # Precision/recall on the binary "fire a skill at all" decision, which is what a
    # false positive actually costs: wasted context and a misled model.
    tp = counts["correct_skill"] + counts["misrouted"]   # fired when it should have
    fp = counts["false_positive"]                        # fired when it should not have
    fn = counts["missed"]                                # stayed silent when it should have fired
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    lat = [r["latency_ms"] for r in rows if r.get("latency_ms")]
    n = len(rows)
    return {
        "n": n,
        "counts": counts,
        "routing_correct": counts["correct_skill"] + counts["correct_abstain"],
        "routing_accuracy": round((counts["correct_skill"] + counts["correct_abstain"]) / n, 4),
        "skill_hits": f"{counts['correct_skill']}/{len(should_fire)}",
        "abstain_hits": f"{counts['correct_abstain']}/{len(should_not)}",
        "false_positive_rate": round(fp / len(should_not), 4) if should_not else 0.0,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "complexity_correct": sum(1 for r in rows if r["complexity_ok"]),
        "complexity_accuracy": round(sum(1 for r in rows if r["complexity_ok"]) / n, 4),
        "latency_ms": {"min": round(min(lat), 1) if lat else 0,
                       "median": percentile(lat, 50),
                       "p90": percentile(lat, 90),
                       "p95": percentile(lat, 95),
                       "max": round(max(lat), 1) if lat else 0},
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "total_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in rows), 8),
        "cost_per_decision_usd": round(sum(r.get("cost_usd", 0.0) for r in rows) / n, 8),
        "errors": sum(1 for r in rows if r.get("error")),
        "rows": rows,
    }


def print_summary(label: str, s: dict) -> None:
    print(f"\n--- {label} " + "-" * max(0, 60 - len(label)))
    print(f"  routing accuracy   : {s['routing_correct']}/{s['n']}  ({s['routing_accuracy']:.1%})")
    print(f"    right skill      : {s['skill_hits']}")
    print(f"    right to abstain : {s['abstain_hits']}")
    print(f"    misrouted        : {s['counts']['misrouted']}   missed: {s['counts']['missed']}")
    print(f"    false positives  : {s['counts']['false_positive']}  (FPR {s['false_positive_rate']:.1%})")
    print(f"  precision / recall : {s['precision']:.3f} / {s['recall']:.3f}   F1 {s['f1']:.3f}")
    print(f"  complexity         : {s['complexity_correct']}/{s['n']}  ({s['complexity_accuracy']:.1%})")
    L = s["latency_ms"]
    print(f"  latency ms         : med {L['median']}  p90 {L['p90']}  p95 {L['p95']}  max {L['max']}")
    print(f"  tokens             : {s['input_tokens']} in / {s['output_tokens']} out")
    print(f"  cost               : ${s['total_cost_usd']:.6f} total, "
          f"${s['cost_per_decision_usd']:.6f} per decision")
    if s["errors"]:
        print(f"  ERRORS             : {s['errors']}")
