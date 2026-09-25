"""Mercury 2.5 as a fast/cheap worker — faithfulness, latency and cost.

    python benchmarks/run_worker_benchmark.py                     # Mercury, instant + low
    python benchmarks/run_worker_benchmark.py --score-only claude_answers.json

The question is NOT "is Mercury better than Claude" — it is not, and the benchmark is not
built to argue that. The question is **where Mercury is good enough while being much faster
and much cheaper**, so that a router could hand it that class of work.

Scoring is deliberately mechanical and identical for every model, so the comparison cannot
drift with the scorer's mood:

  fact recall     fraction of `must_contain` facts that survive into the answer. This is the
                  faithfulness measure that matters for context compaction: dropping a fact
                  the downstream reader needs is the failure mode, not clumsy prose.
  violations      `must_not_contain` hits — contradictions and hallucination traps. Any
                  violation is disqualifying for a compaction use case.
  budget          whether the answer respected `max_words`.
  json exact      for extraction cases, per-key exact match against `json_expect`.

A model's answers can be scored without re-running it: `--score-only <file.json>` takes a
{case_id: answer_text} map. That is how the Claude arm is scored — there is no Anthropic
key in this project's .env, so the Claude answers are produced in-session and scored here
by exactly the same code, and Claude's latency/cost columns are left empty rather than
filled with numbers that were never measured.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

from mercury_client import MercuryClient, cost_usd, text_of  # noqa: E402

CASES = json.loads((HERE / "worker_cases.json").read_text(encoding="utf-8"))["cases"]


def case_input(case: dict) -> str:
    if case.get("fixture"):
        return (FIXTURES / case["fixture"]).read_text(encoding="utf-8")
    return case.get("inline_input", "")


def user_message(case: dict) -> str:
    body = case_input(case)
    budget = f"\n\nAnswer in at most {case['max_words']} words." if case.get("max_words") else ""
    return f"{case['prompt']}{budget}\n\n---\n{body}\n---"


def _find(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE | re.DOTALL) is not None


def score_answer(case: dict, answer: str) -> dict:
    groups = case.get("must_contain", [])
    hits = [any(_find(p, answer) for p in group) for group in groups]
    missed = [group[0] for group, ok in zip(groups, hits) if not ok]
    violations = [group[0] for group in case.get("must_not_contain", [])
                  if any(_find(p, answer) for p in group)]
    words = len(answer.split())
    out = {
        "fact_recall": round(sum(hits) / len(hits), 4) if hits else 1.0,
        "facts": f"{sum(hits)}/{len(hits)}" if hits else "n/a",
        "missed_facts": missed,
        "violations": violations,
        "words": words,
        "within_budget": (words <= case["max_words"]) if case.get("max_words") else True,
    }
    if case.get("json_expect"):
        parsed, keys_ok = None, {}
        blob = answer
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.DOTALL)
        if fence:
            blob = fence.group(1)
        else:
            brace = re.search(r"\{.*\}", answer, re.DOTALL)
            if brace:
                blob = brace.group(0)
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for k, want in case["json_expect"].items():
                got = parsed.get(k)
                keys_ok[k] = (str(got).strip().lower() == str(want).strip().lower())
        out["json_parsed"] = parsed is not None
        out["json_keys_ok"] = keys_ok
        out["json_exact"] = (round(sum(keys_ok.values()) / len(case["json_expect"]), 4)
                             if keys_ok else 0.0)
    return out


def run_mercury(effort: str) -> dict[str, dict]:
    answers = {}
    with MercuryClient() as client:
        client.chat("ready?", max_tokens=8, reasoning_effort="instant")  # warm the connection
        for case in CASES:
            kw = {}
            if case.get("json_expect"):
                kw["response_format"] = {"type": "json_object"}
            started = time.perf_counter()
            try:
                resp = client.chat(
                    [{"role": "user", "content": user_message(case)}],
                    max_tokens=2000, temperature=0, reasoning_effort=effort, **kw)
                answer = text_of(resp)
                rec = {"answer": answer, "latency_ms": resp["latency_ms"],
                       "input_tokens": resp["usage"]["prompt_tokens"],
                       "output_tokens": resp["usage"]["completion_tokens"],
                       "cost_usd": resp.get("cost_usd", cost_usd(resp["usage"])), "error": None}
            except Exception as exc:  # noqa: BLE001
                rec = {"answer": "", "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                       "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                       "error": f"{type(exc).__name__}: {exc}"[:160]}
            answers[case["id"]] = rec
            s = score_answer(case, rec["answer"])
            print(f"  {case['id']:<24} facts {s['facts']:<7} "
                  f"viol {len(s['violations'])}  {rec['latency_ms']:7.0f}ms  "
                  f"{rec['output_tokens']:5d} out  ${rec['cost_usd']:.6f}"
                  + (f"  ERR {rec['error']}" if rec["error"] else ""))
    return answers


def summarise(label: str, answers: dict[str, dict]) -> dict:
    rows = []
    for case in CASES:
        rec = answers.get(case["id"])
        if rec is None:
            continue
        rows.append({"id": case["id"], "kind": case["kind"],
                     **{k: v for k, v in rec.items() if k != "answer"},
                     **score_answer(case, rec.get("answer", ""))})
    n = len(rows)
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms")]
    jsons = [r["json_exact"] for r in rows if "json_exact" in r]
    s = {
        "n": n,
        "mean_fact_recall": round(sum(r["fact_recall"] for r in rows) / n, 4) if n else 0,
        "perfect_recall_cases": sum(1 for r in rows if r["fact_recall"] == 1.0),
        "total_violations": sum(len(r["violations"]) for r in rows),
        "cases_with_violations": sum(1 for r in rows if r["violations"]),
        "within_budget": sum(1 for r in rows if r["within_budget"]),
        "json_exact": round(sum(jsons) / len(jsons), 4) if jsons else None,
        "errors": sum(1 for r in rows if r.get("error")),
        "median_latency_ms": round(sorted(lat)[len(lat) // 2], 1) if lat else None,
        "total_latency_ms": round(sum(lat), 1) if lat else None,
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "total_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in rows), 8),
        "rows": rows,
    }
    print(f"\n--- {label} " + "-" * max(0, 58 - len(label)))
    print(f"  mean fact recall   : {s['mean_fact_recall']:.1%}  "
          f"({s['perfect_recall_cases']}/{n} cases kept every fact)")
    print(f"  violations         : {s['total_violations']} across {s['cases_with_violations']} cases")
    print(f"  within word budget : {s['within_budget']}/{n}")
    if s["json_exact"] is not None:
        print(f"  json exact-match   : {s['json_exact']:.1%}")
    if s["median_latency_ms"]:
        print(f"  latency            : median {s['median_latency_ms']}ms, "
              f"{s['total_latency_ms'] / 1000:.1f}s for all {n}")
        print(f"  cost               : ${s['total_cost_usd']:.6f} "
              f"({s['input_tokens']} in / {s['output_tokens']} out)")
    if s["errors"]:
        print(f"  ERRORS             : {s['errors']}")
    for r in rows:
        if r["missed_facts"] or r["violations"]:
            print(f"    {r['id']:<24} missed={r['missed_facts']} viol={r['violations']}")
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--efforts", default="instant,low")
    ap.add_argument("--score-only", default=None,
                    help="JSON file mapping case id -> answer text (or -> {answer: ...})")
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    out_path = HERE / "results_worker.json"
    existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {}

    if args.score_only:
        raw = json.loads(Path(args.score_only).read_text(encoding="utf-8"))
        answers = {k: (v if isinstance(v, dict) else {"answer": v}) for k, v in raw.items()}
        label = args.label or Path(args.score_only).stem
        existing[label] = summarise(label, answers)
        existing[label]["answers"] = {k: v.get("answer", "") for k, v in answers.items()}
        out_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        print(f"\nWritten to {out_path}")
        return 0

    for effort in args.efforts.split(","):
        effort = effort.strip()
        print(f"\n=== Mercury 2.5, reasoning_effort={effort} ===")
        answers = run_mercury(effort)
        label = f"mercury-{effort}"
        existing[label] = summarise(label, answers)
        existing[label]["answers"] = {k: v["answer"] for k, v in answers.items()}
    out_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"\nWritten to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
