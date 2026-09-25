"""Aggregate router-benchmark results: metrics, stability, bootstrap CIs, error lists.

    python benchmarks/router_report.py --split dev --suffix _probe
    python benchmarks/router_report.py --split test          # after the final runs

Writes results/summary_<split><suffix>.json and results/errors_<split><suffix>.json.
Error codes are a mechanical first pass (see `code_error`); ROUTER_ERROR_ANALYSIS.md is the
reviewed version.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import router_scoring as rs  # noqa: E402

FAMILIES = [
    {"schedule", "anthropic-skills:schedule", "loop"},
    {"code-review", "simplify", "security-review"},
    {"artifact-design", "artifact-diagramming", "artifact-capabilities", "dataviz"},
    {"anthropic-skills:docs", "anthropic-skills:docx"},
    {"update-config", "fewer-permission-prompts", "keybindings-help"},
    {"anthropic-skills:consolidate-memory", "anthropic-skills:import-memory"},
    {"anthropic-skills:skill-creator", "workflow-authoring"},
]
CODES = {"A": "bad catalog description", "B": "keyword attraction", "C": "none competition",
         "D": "similar skills", "E": "explanation-vs-action", "F": "insufficient SKILL excerpt",
         "G": "gate failure", "H": "rerank failure", "I": "genuine ambiguity", "J": "API/runtime failure"}


def family(a, b) -> bool:
    return any(a in f and b in f for f in FAMILIES) or (str(a).startswith("figma:") and str(b).startswith("figma:"))


def code_error(r: dict, oc: str) -> str:
    t = r.get("trace") or {}
    tags = set(r.get("tags", []))
    if r.get("error"):
        return "J"
    if r["ambiguous"]:
        return "I"
    reason = t.get("reason", "") or ""
    if oc == "fn" and reason.startswith("gate"):
        return "G"
    if reason.startswith("nothing fits") and oc == "fn":
        return "H"
    if oc in ("fn", "misroute") and t.get("rerank_winner") is not None:
        sl = t.get("shortlist") or (t.get("ranked") or [])[:3]
        if r["expected"] in sl:
            return "H"
    if oc == "fp" and (tags & {"explain_vs_do", "tell_how_vs_do"} or r["category"] == "explanation_vs_action"):
        return "E"
    if oc == "fp" and (tags & {"context_only_keyword", "debug_vs_author", "multi_domain", "negation"}
                       or r["category"] == "keyword_trap"):
        return "B"
    if oc == "misroute" and family(r["pred"], r["expected"]):
        return "D"
    none_p = t.get("none_p")
    if none_p is not None and 0.2 <= none_p <= 0.8:
        return "C"
    return "A"


def load_runs(variant: str, split: str, suffix: str) -> list[list[dict]]:
    d = ROOT / "results" / variant
    files = sorted(d.glob(f"{split}{suffix}_run*.json")) if d.exists() else []
    return [json.loads(f.read_text(encoding="utf-8"))["records"] for f in files]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", required=True)
    ap.add_argument("--suffix", default="")
    ap.add_argument("--variants", default="simple,official_default,official_tuned,hybrid")
    args = ap.parse_args()
    variants = [v for v in args.variants.split(",") if load_runs(v, args.split, args.suffix)]

    summary, errors = {}, {}
    labels = rs.load_labels(args.split)
    prompts = {c["id"]: c["prompt"] for c in rs.inference_inputs(args.split)}
    for v in variants:
        runs = load_runs(v, args.split, args.suffix)
        summary[v] = {"run1": rs.score_run(args.split, runs[0]),
                      "per_run_acc": [rs.score_run(args.split, r)["overall"]["acc"] for r in runs],
                      "stability": rs.stability(runs)}
        errs = []
        for r in rs.join(args.split, runs[0]):
            oc = rs.outcome(r, r["pred"])
            if oc == "ok" and not r["ambiguous"]:
                continue
            if oc == "ok":
                continue
            real = r["source"] == "real_world"
            errs.append({"id": r["id"], "variant": v, "outcome": oc, "code": code_error(r, oc),
                         "category": r["category"], "tags": r["tags"],
                         "prompt": ("<real-world prompt, private>" if real else prompts[r["id"]]),
                         "expected": r["expected"], "acceptable": r["acceptable"], "pred": r["pred"],
                         "trace": r.get("trace")})
        errors[v] = errs

    pairs = {}
    ref = "simple"
    for v in variants:
        if v != ref and ref in variants:
            pairs[f"{v}_minus_{ref}"] = rs.paired_bootstrap(
                args.split, load_runs(v, args.split, args.suffix)[0], load_runs(ref, args.split, args.suffix)[0])
    if "hybrid" in variants and "official_tuned" in variants:
        pairs["hybrid_minus_official_tuned"] = rs.paired_bootstrap(
            args.split, load_runs("hybrid", args.split, args.suffix)[0],
            load_runs("official_tuned", args.split, args.suffix)[0])

    out = ROOT / "results"
    (out / f"summary_{args.split}{args.suffix}.json").write_text(
        json.dumps({"variants": summary, "paired_bootstrap": pairs}, indent=1), encoding="utf-8")
    (out / f"errors_{args.split}{args.suffix}.json").write_text(
        json.dumps(errors, indent=1, ensure_ascii=False), encoding="utf-8")

    hdr = f"{'':26s}" + "".join(f"{v:>18s}" for v in variants)
    print(hdr)

    def row(name, fn):
        print(f"{name:26s}" + "".join(f"{str(fn(summary[v])):>18s}" for v in variants))
    o = lambda s: s["run1"]["overall"]  # noqa: E731
    row("n (non-ambiguous)", lambda s: o(s)["n"])
    row("acceptable acc", lambda s: o(s)["acc"])
    row("exact acc", lambda s: o(s)["exact_acc"])
    row("precision", lambda s: o(s)["precision"])
    row("recall", lambda s: o(s)["recall"])
    row("F1", lambda s: o(s)["f1"])
    row("FP / FN / misroute", lambda s: f"{o(s)['fp']}/{o(s)['fn']}/{o(s)['misroute']}")
    row("FPR", lambda s: o(s)["fpr"])
    row("FNR", lambda s: o(s)["fnr"])
    row("abstention", lambda s: o(s)["abstention_rate"])
    row("weighted cost/100", lambda s: o(s)["weighted_cost_per_100"])
    row("adversarial acc", lambda s: s["run1"]["adversarial_flag"]["acc"])
    row("real-world acc", lambda s: s["run1"]["by_category"]["real_world"].get("acc"))
    row("russian acc", lambda s: s["run1"]["lang_ru"]["acc"])
    for c in rs.CATEGORIES[:-1]:
        row(f"  {c}", lambda s, c=c: f"{s['run1']['by_category'][c].get('acc')} (n={s['run1']['by_category'][c].get('n')})")
    row("calls/decision", lambda s: s["run1"]["calls_per_decision"])
    row("second-pass rate", lambda s: s["run1"]["second_pass_rate"])
    row("tokens/decision", lambda s: s["run1"]["input_tokens_per_decision"])
    row("cost/decision $", lambda s: f"{s['run1']['cost_per_decision_usd']:.7f}")
    row("latency med", lambda s: s["run1"]["latency_ms"]["median"])
    row("latency p95", lambda s: s["run1"]["latency_ms"]["p95"])
    row("api failures", lambda s: s["run1"]["api_failures"])
    row("runs / acc per run", lambda s: " ".join(f"{a:.3f}" for a in s["per_run_acc"]))
    row("winner disagreement", lambda s: s["stability"]["winner_disagreement_cases"])
    row("top-prob jitter max", lambda s: s["stability"]["top_prob_jitter_max"])
    print()
    for k, b in pairs.items():
        print(f"{k:34s} acc {b['acc_diff']:+.3f} CI{b['acc_diff_ci95']}  fpr {b['fpr_diff']:+.3f} "
              f"CI{b['fpr_diff_ci95']}  cost {b['cost_diff_per_100']:+.1f} CI{b['cost_diff_ci95']}  "
              f"discordant {b['discordant_cases']} (A {b['a_only_right']} / B {b['b_only_right']})")
    print()
    for v in variants:
        from collections import Counter
        print(v, dict(Counter(e["code"] for e in errors[v])), "errors:", len(errors[v]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
