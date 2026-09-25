"""Jev vs Mercury 2.5 on the same routing cases, same catalog, blind to labels.

    python benchmarks/run_comparison.py                      # the new 50-case blind set
    python benchmarks/run_comparison.py --cases routing_cases.json   # the original 15

This does not touch `run_benchmark.py` or the Jev router. It drives both arms through
their own public `decide()` and scores them with the shared scorer in `scoring.py`,
which is the only module allowed to see the labels — and only after inference is done.

Each arm pays one warm-up call outside the measurements so the latency columns compare
warm connections, which is the regime any real integration would run in.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scoring  # noqa: E402
from jev_router import Router  # noqa: E402
from mercury_router import MercuryRouter  # noqa: E402


def run_jev(prompts: list[dict]) -> dict[str, dict]:
    out = {}
    with Router() as r:
        r.decide("warm up the connection")
        for i, p in enumerate(prompts, 1):
            d = r.decide(p["prompt"]).as_dict()
            out[p["id"]] = {**d, "output_tokens": 0}
            print(f"  jev     {i:>3}/{len(prompts)} {p['id']:<26} "
                  f"{str(d['skill']):<26} conf={d['skill_confidence']:.2f} "
                  f"{d['complexity']:<8} {d['latency_ms']:6.0f}ms")
    return out


def run_mercury(prompts: list[dict], effort: str) -> dict[str, dict]:
    out = {}
    with MercuryRouter(reasoning_effort=effort) as r:
        r.decide("warm up the connection")
        for i, p in enumerate(prompts, 1):
            d = r.decide(p["prompt"]).as_dict()
            out[p["id"]] = d
            print(f"  mercury {i:>3}/{len(prompts)} {p['id']:<26} "
                  f"{str(d['skill']):<26} conf={d['skill_confidence']:.2f} "
                  f"{d['complexity']:<8} {d['latency_ms']:6.0f}ms"
                  + (f"  ERR {d['error']}" if d.get("error") else ""))
    return out


def disagreements(cases: list[dict], jev: dict, merc: dict) -> list[dict]:
    rows = []
    for c in cases:
        j, m = jev[c["id"]], merc[c["id"]]
        if j.get("skill") != m.get("skill") or j.get("complexity") != m.get("complexity"):
            rows.append({"id": c["id"], "prompt": c["prompt"][:80],
                         "expected_skill": c["expected_skill"],
                         "expected_complexity": c["expected_complexity"],
                         "jev": {"skill": j.get("skill"), "complexity": j.get("complexity")},
                         "mercury": {"skill": m.get("skill"), "complexity": m.get("complexity")}})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="blind_cases_50.json")
    ap.add_argument("--effort", default="instant", help="Mercury reasoning_effort")
    ap.add_argument("--arm", choices=["both", "jev", "mercury"], default="both")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    prompts = scoring.blind_prompts(args.cases)   # id + prompt only — no labels reach inference
    print(f"{len(prompts)} cases from {args.cases}, Mercury reasoning_effort={args.effort}\n")

    results, scores = {}, {}
    if args.arm in ("both", "jev"):
        results["jev"] = run_jev(prompts)
        print()
    if args.arm in ("both", "mercury"):
        results["mercury"] = run_mercury(prompts, args.effort)

    for name, decisions in results.items():
        scores[name] = scoring.score(args.cases, decisions)
        scoring.print_summary(f"{name} ({args.cases})", scores[name])

    cases = scoring.load_cases(args.cases)
    diff = disagreements(cases, results["jev"], results["mercury"]) if len(results) == 2 else []
    if diff:
        print(f"\n--- disagreements ({len(diff)}/{len(cases)}) " + "-" * 30)
        for d in diff:
            what = "skill" if d['jev']['skill'] != d['mercury']['skill'] else "complexity"
            if what == "skill":
                print(f"  {d['id']:<6} skill       want {str(d['expected_skill']):<34} "
                      f"jev={str(d['jev']['skill']):<34} mercury={str(d['mercury']['skill'])}")
            else:
                print(f"  {d['id']:<6} complexity  want {d['expected_complexity']:<34} "
                      f"jev={d['jev']['complexity']:<34} mercury={d['mercury']['complexity']}")

    out = Path(args.out) if args.out else \
        Path(__file__).parent / f"results_{Path(args.cases).stem}.json"
    out.write_text(json.dumps({"cases_file": args.cases, "mercury_effort": args.effort,
                               "scores": scores, "disagreements": diff}, indent=2),
                   encoding="utf-8")
    print(f"\nWritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
