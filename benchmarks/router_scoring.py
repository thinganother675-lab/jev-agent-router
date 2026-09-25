"""Scoring for the router-architecture benchmark. The only module that reads labels.

`inference_inputs(split)` is what the runner gets: id + prompt, nothing else. Labels are
joined in `score_run()` after every decision has been made and written to disk.

Definitions (ambiguous cases are excluded from all of these and reported on their own):

  exact        pred == expected
  acceptable   pred == expected, or pred in `acceptable` (None there means abstaining is fine)
  fired        pred is not None
  precision    fired and acceptable / fired              — how often an injected skill is right
  recall       fired and acceptable / cases whose expected is a skill
  FP           fired on a case whose expected is None, and the pick is not acceptable
  FN           abstained on a case whose expected is a skill, and None is not acceptable
  misroute     fired the wrong skill on a case that needed a skill
  FPR / FNR    FP / expected-None cases, FN / expected-skill cases

Weighted cost (shown next to, never instead of, the plain metrics): a wrongly injected skill
misleads the model and spends context, which is worse than silence. FP = 3, misroute = 2,
FN = 1, correct = 0. Reported as cost per 100 decisions (lower is better).
"""

from __future__ import annotations

import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRIVATE = HERE / "private"
WEIGHTS = {"fp": 3, "misroute": 2, "fn": 1, "ok": 0}
CATEGORIES = ["normal", "keyword_trap", "explanation_vs_action", "similar_skills", "compound",
              "adversarial", "real_world"]


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def inference_inputs(split: str) -> list[dict]:
    """id + prompt only. Never opens a labels file."""
    rows = [{"id": r["id"], "prompt": r["prompt"]} for r in _jsonl(HERE / f"router_{split}_prompts.jsonl")]
    real = PRIVATE / "router_real_prompts.jsonl"
    if real.is_file():
        rows += [{"id": r["id"], "prompt": r["prompt"]} for r in _jsonl(real) if r["split"] == split]
    return sorted(rows, key=lambda r: r["id"])


def load_labels(split: str) -> dict[str, dict]:
    return {r["id"]: r for r in _jsonl(HERE / f"router_{split}_labels.jsonl")}


def outcome(label: dict, pred) -> str:
    """ok | fp | fn | misroute, on the acceptable standard."""
    exp, acc = label["expected"], label["acceptable"]
    if pred == exp or pred in acc:
        return "ok"
    if exp is None:
        return "fp"
    if pred is None:
        return "fn"
    return "misroute"


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))], 1)


def metrics(rows: list[dict]) -> dict:
    """rows: each has label fields + pred. Ambiguous rows must already be filtered out."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    oc = Counter(outcome(r, r["pred"]) for r in rows)
    exact = sum(1 for r in rows if r["pred"] == r["expected"])
    fired = [r for r in rows if r["pred"] is not None]
    fired_ok = sum(1 for r in fired if outcome(r, r["pred"]) == "ok")
    need = [r for r in rows if r["expected"] is not None]
    need_ok = sum(1 for r in need if r["pred"] is not None and outcome(r, r["pred"]) == "ok")
    none_cases = [r for r in rows if r["expected"] is None]
    precision = fired_ok / len(fired) if fired else 0.0
    recall = need_ok / len(need) if need else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    cost = sum(WEIGHTS[outcome(r, r["pred"])] for r in rows)
    return {
        "n": n,
        "exact_acc": round(exact / n, 4),
        "acc": round(oc["ok"] / n, 4),
        "correct": oc["ok"],
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "fp": oc["fp"], "fn": oc["fn"], "misroute": oc["misroute"],
        "fpr": round(oc["fp"] / len(none_cases), 4) if none_cases else None,
        "fnr": round(oc["fn"] / len(need), 4) if need else None,
        "abstention_rate": round(sum(1 for r in rows if r["pred"] is None) / n, 4),
        "weighted_cost_per_100": round(100 * cost / n, 1),
    }


def join(split: str, records: list[dict]) -> list[dict]:
    labels = load_labels(split)
    return [{**labels[r["id"]], **{k: r[k] for k in ("pred", "calls", "input_tokens", "latency_ms",
                                                       "second_pass", "error", "trace") if k in r}}
            for r in records]


def score_run(split: str, records: list[dict]) -> dict:
    rows = join(split, records)
    main = [r for r in rows if not r["ambiguous"]]
    amb = [r for r in rows if r["ambiguous"]]
    by_cat = {c: metrics([r for r in main if r["category"] == c]) for c in CATEGORIES}
    by_group = {g: metrics([r for r in main if r["group"] == g]) for g in sorted({r["group"] for r in main})}
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms")]
    tok = [r["input_tokens"] for r in rows]
    return {
        "overall": metrics(main),
        "adversarial_flag": metrics([r for r in main if r["adversarial"]]),
        "lang_ru": metrics([r for r in main if r["lang"] == "ru"]),
        "by_category": by_cat,
        "by_group": by_group,
        "ambiguous": [{"id": r["id"], "expected": r["expected"], "acceptable": r["acceptable"],
                       "pred": r["pred"]} for r in amb],
        "latency_ms": {"mean": round(statistics.mean(lat), 1) if lat else 0, "median": pct(lat, 50),
                       "p90": pct(lat, 90), "p95": pct(lat, 95), "max": round(max(lat), 1) if lat else 0},
        "calls_per_decision": round(sum(r["calls"] for r in rows) / len(rows), 3),
        "second_pass_rate": round(sum(1 for r in rows if r["second_pass"]) / len(rows), 4),
        "input_tokens_per_decision": round(sum(tok) / len(rows), 1),
        "cost_per_decision_usd": round(sum(tok) / len(rows) / 1e6 * 0.042, 8),
        "api_failures": sum(1 for r in rows if r.get("error")),
    }


def stability(runs: list[list[dict]]) -> dict:
    """Run-to-run agreement across repeated runs of one variant on one split."""
    by_id = defaultdict(list)
    for run in runs:
        for r in run:
            by_id[r["id"]].append(r)
    n = len(by_id)
    winner_diff = sum(1 for rs in by_id.values() if len({r["pred"] for r in rs}) > 1)
    abst_diff = sum(1 for rs in by_id.values() if len({r["pred"] is None for r in rs}) > 1)
    path_diff = sum(1 for rs in by_id.values() if len({r["second_pass"] for r in rs}) > 1)
    jitter = []
    for rs in by_id.values():
        tops = []
        for r in rs:
            t = r.get("trace", {})
            probs = t.get("probs") or t.get("top_probs") or {}
            if probs:
                tops.append(max(probs.values()))
        if len(tops) > 1:
            jitter.append(max(tops) - min(tops))
    return {
        "runs": len(runs), "cases": n,
        "winner_disagreement_cases": winner_diff,
        "winner_disagreement_rate": round(winner_diff / n, 4) if n else 0,
        "abstention_disagreement_cases": abst_diff,
        "path_disagreement_cases": path_diff,
        "top_prob_jitter_max": round(max(jitter), 4) if jitter else 0.0,
        "top_prob_jitter_mean": round(statistics.mean(jitter), 5) if jitter else 0.0,
        "hard_failures": sum(1 for run in runs for r in run if r.get("error")),
        "unstable_ids": sorted(i for i, rs in by_id.items() if len({r["pred"] for r in rs}) > 1),
    }


def paired_bootstrap(split: str, a: list[dict], b: list[dict], reps: int = 10000, seed: int = 7) -> dict:
    """95% CI of (A - B) for acceptable accuracy, FP count rate and weighted cost, over cases."""
    labels = load_labels(split)
    pa = {r["id"]: r["pred"] for r in a}
    pb = {r["id"]: r["pred"] for r in b}
    ids = [i for i in sorted(pa) if not labels[i]["ambiguous"]]
    none_ids = [i for i in ids if labels[i]["expected"] is None]

    def stats(sample):
        acc_a = sum(outcome(labels[i], pa[i]) == "ok" for i in sample)
        acc_b = sum(outcome(labels[i], pb[i]) == "ok" for i in sample)
        cost_a = sum(WEIGHTS[outcome(labels[i], pa[i])] for i in sample)
        cost_b = sum(WEIGHTS[outcome(labels[i], pb[i])] for i in sample)
        return (acc_a - acc_b) / len(sample), 100 * (cost_a - cost_b) / len(sample)

    def fpr_diff(sample):
        if not sample:
            return 0.0
        fa = sum(outcome(labels[i], pa[i]) == "fp" for i in sample)
        fb = sum(outcome(labels[i], pb[i]) == "fp" for i in sample)
        return (fa - fb) / len(sample)

    rng = random.Random(seed)
    accs, costs, fprs = [], [], []
    for _ in range(reps):
        s = [rng.choice(ids) for _ in ids]
        d_acc, d_cost = stats(s)
        accs.append(d_acc)
        costs.append(d_cost)
        fprs.append(fpr_diff([rng.choice(none_ids) for _ in none_ids]))

    def ci(v):
        v = sorted(v)
        return [round(v[int(0.025 * len(v))], 4), round(v[int(0.975 * len(v)) - 1], 4)]

    point_acc, point_cost = stats(ids)
    disc = [i for i in ids if (outcome(labels[i], pa[i]) == "ok") != (outcome(labels[i], pb[i]) == "ok")]
    return {"n": len(ids),
            "acc_diff": round(point_acc, 4), "acc_diff_ci95": ci(accs),
            "fpr_diff": round(fpr_diff(none_ids), 4), "fpr_diff_ci95": ci(fprs),
            "cost_diff_per_100": round(point_cost, 1), "cost_diff_ci95": ci(costs),
            "discordant_cases": len(disc),
            "a_only_right": sum(1 for i in disc if outcome(labels[i], pa[i]) == "ok"),
            "b_only_right": sum(1 for i in disc if outcome(labels[i], pb[i]) == "ok")}
