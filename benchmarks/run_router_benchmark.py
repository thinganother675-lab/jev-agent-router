"""Run the routing architectures on the dev or final-test split.

    python benchmarks/run_router_benchmark.py --split dev --variants simple,official_default,hybrid
    python benchmarks/run_router_benchmark.py --split dev --probe            # tuning traces
    python benchmarks/run_router_benchmark.py --split dev --variants simple --catalog benchmarks/catalog_trigger_v1.json --label trigger
    python benchmarks/run_router_benchmark.py --freeze-final                 # once, after tuning
    python benchmarks/run_router_benchmark.py --split test --runs 3          # only after the freeze

Per case, every variant runs back to back (round-robin), so drift in API latency over the
run hits all variants alike. Each variant keeps its own warm connection and pays one
warm-up call outside the measurements.

Guards, enforced here rather than by discipline:
  * SIMPLE refuses to run if src/jev_router.py, the catalog or the harness differ from the
    simple_v1 freeze.
  * `--split test` refuses to run before the final freeze, and refuses any variant whose
    configuration hash differs from the frozen one. `--probe` and `--catalog` are dev-only.
  * The runner reads prompts through router_scoring.inference_inputs() — id and prompt only.
    Labels are joined after all runs are written.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import freeze  # noqa: E402
import router_scoring as rs  # noqa: E402
from jev_router import Router  # noqa: E402
from router_variants import Hybrid, Official, Simple  # noqa: E402

RESULTS = ROOT / "results"
TUNED = HERE / "tuned_params.json"
FINAL_FILES = ["benchmarks/router_variants.py", "benchmarks/router_scoring.py",
               "benchmarks/run_router_benchmark.py", "benchmarks/skill_details_v1.json",
               "benchmarks/tuned_params.json",
               "benchmarks/router_test_prompts.jsonl", "benchmarks/router_test_labels.jsonl",
               "benchmarks/router_dev_prompts.jsonl", "benchmarks/router_dev_labels.jsonl"]
ALL = ["simple", "official_default", "official_tuned", "hybrid"]


def tuned() -> dict:
    return json.loads(TUNED.read_text(encoding="utf-8")) if TUNED.is_file() else {}


def build(name: str, probe: bool, catalog_path: str | None):
    catalog = None
    if catalog_path:
        catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))["skills"]
    r = Router(catalog=catalog, timeout=10.0)
    if name == "simple":
        return Simple(r)
    if name == "official_default":
        return Official(r, None, "official_default", probe)
    if name == "official_tuned":
        return Official(r, tuned().get("official_tuned"), "official_tuned", probe)
    if name == "hybrid":
        return Hybrid(r, tuned().get("hybrid"), "hybrid", probe)
    raise SystemExit(f"unknown variant {name}")


def freeze_final() -> None:
    if not TUNED.is_file():
        raise SystemExit("tuned_params.json missing — tune on dev first")
    configs = {}
    for name in ALL:
        v = build(name, False, None)
        configs[name] = v.config()
        v.r.close()
    m = freeze.take_final_freeze(configs, FINAL_FILES)
    print(json.dumps(m, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"])
    ap.add_argument("--variants", default=",".join(ALL))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--first-run", type=int, default=1)
    ap.add_argument("--probe", action="store_true", help="dev only: force every second pass")
    ap.add_argument("--catalog", default=None, help="dev only: alternative catalog (SIMPLE-style variants)")
    ap.add_argument("--label", default="", help="suffix for result files")
    ap.add_argument("--freeze-final", action="store_true")
    args = ap.parse_args()

    if args.freeze_final:
        freeze_final()
        return 0
    if not args.split:
        ap.error("--split is required")
    names = [v.strip() for v in args.variants.split(",") if v.strip()]

    freeze.verify_simple()
    if args.split == "test":
        if args.probe or args.catalog:
            raise SystemExit("--probe/--catalog are development-set tools only")
    if args.catalog and names != ["simple"] and names != ["hybrid"]:
        raise SystemExit("--catalog applies to one SIMPLE-style variant at a time")

    variants = [build(n, args.probe, args.catalog) for n in names]
    config_hashes = {}
    for v in variants:
        cfg = v.config()
        if args.catalog:
            cfg["catalog_override"] = args.catalog
        config_hashes[v.name] = (freeze.verify_final(v.name, cfg) if args.split == "test"
                                 else freeze.config_hash(cfg))

    cases = rs.inference_inputs(args.split)          # id + prompt, nothing else
    print(f"{len(cases)} {args.split} cases × {names} × {args.runs} run(s)"
          + (" [PROBE]" if args.probe else "") + (f" [catalog {args.catalog}]" if args.catalog else ""))

    for v in variants:                                # warm-up, outside the measurements
        v.decide("warm up the connection")

    suffix = ("_probe" if args.probe else "") + (f"_{args.label}" if args.label else "")
    for run in range(args.first_run, args.first_run + args.runs):
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        records = {v.name: [] for v in variants}
        t0 = time.perf_counter()
        for i, c in enumerate(cases, 1):
            line = []
            for v in variants:
                d = v.decide(c["prompt"])
                d["id"] = c["id"]
                records[v.name].append(d)
                line.append(f"{v.name[:8]}={str(d['pred'])[:22]:<22}{'*' if d['second_pass'] else ' '}")
            print(f"  run{run} {i:>3}/{len(cases)} {c['id']:<5} " + " ".join(line), flush=True)
        for v in variants:
            out = RESULTS / v.name / f"{args.split}{suffix}_run{run}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({
                "variant": v.name, "split": args.split, "run": run, "started": started,
                "probe": args.probe, "catalog": args.catalog, "config_hash": config_hashes[v.name],
                "simple_v1": freeze.verify_simple()["spec_sha256"][:16],
                "model": v.r.model, "records": records[v.name]}, indent=1, ensure_ascii=False),
                encoding="utf-8")
        print(f"run {run} done in {time.perf_counter() - t0:.0f}s")

    for v in variants:
        v.r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
