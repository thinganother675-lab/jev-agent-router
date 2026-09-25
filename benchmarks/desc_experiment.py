"""DEV-ONLY: does rewriting catalog descriptions trigger-shaped change routing more than thresholds?

    python benchmarks/desc_experiment.py

Runs SIMPLE and OFFICIAL-DEFAULT with the CURRENT catalog (src/skills_catalog.json) and the
TRIGGER-SHAPED candidate (benchmarks/catalog_trigger_v1.json) on two development sets:

  * router dev split (79 cases)
  * blind_cases_50 from the previous stage — already seen, so development data now

Never touches the final test split. Changes nothing in production: the catalog override is
passed to Router(catalog=...) in-process only. Results: results/dev_experiments/.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import router_scoring as rs  # noqa: E402
import scoring as old  # noqa: E402
from jev_router import Router, load_catalog  # noqa: E402
from router_variants import Official, Simple  # noqa: E402

OUT = ROOT / "results" / "dev_experiments"
CATALOGS = {"current": load_catalog(),
            "trigger": json.loads((HERE / "catalog_trigger_v1.json").read_text(encoding="utf-8"))["skills"]}


def blind50():
    prompts = old.blind_prompts("blind_cases_50.json")
    labels = {c["id"]: c for c in old.load_cases("blind_cases_50.json")}
    lab = {i: {"id": i, "expected": c["expected_skill"], "acceptable": [], "ambiguous": False,
               "category": "adversarial" if i.startswith("C") else "normal", "group": i[0],
               "adversarial": i.startswith("C"), "lang": "en", "tags": [], "source": "synthetic"}
           for i, c in labels.items()}
    return prompts, lab


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sets = {"dev": (rs.inference_inputs("dev"), rs.load_labels("dev")), "blind50": blind50()}
    summary = {}
    for cat_name, catalog in CATALOGS.items():
        for vname, ctor in (("simple", Simple), ("official_default", Official)):
            v = ctor(Router(catalog=catalog, timeout=10.0))
            v.decide("warm up the connection")
            for set_name, (prompts, labels) in sets.items():
                recs = []
                for c in prompts:
                    d = v.decide(c["prompt"])
                    d["id"] = c["id"]
                    recs.append(d)
                rows = [{**labels[r["id"]], "pred": r["pred"], "trace": r["trace"]} for r in recs]
                m = rs.metrics([r for r in rows if not r["ambiguous"]])
                errs = [{"id": r["id"], "outcome": rs.outcome(r, r["pred"]), "expected": r["expected"],
                         "pred": r["pred"]} for r in rows if rs.outcome(r, r["pred"]) != "ok"]
                key = f"{vname}|{cat_name}|{set_name}"
                summary[key] = {"metrics": m, "errors": errs,
                                "fired": dict(Counter(r["pred"] for r in rows if r["pred"]))}
                (OUT / f"desc_{vname}_{cat_name}_{set_name}.json").write_text(
                    json.dumps({"records": recs}, indent=1, ensure_ascii=False), encoding="utf-8")
                print(f"{key:40s} acc {m['acc']:.3f} fp {m['fp']} fn {m['fn']} mis {m['misroute']} "
                      f"cost {m['weighted_cost_per_100']}", flush=True)
            v.r.close()
    (OUT / "desc_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("\nerrors:")
    for k, s in summary.items():
        print(f"  {k:40s} " + ", ".join(f"{e['id']}:{e['outcome']}->{e['pred']}" for e in s["errors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
