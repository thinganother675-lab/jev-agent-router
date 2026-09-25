"""Live smoke test of the agent-decision schema — schema and parsing, not a benchmark.

    python tests/agent_shadow_smoke.py                 # 16 prompts through the running sidecar
    python tests/agent_shadow_smoke.py --merge-check   # + would one merged request change SIMPLE?

Sixteen hand-written prompts (Russian and English) with a loose expected shape, only so a
human can eyeball whether the answers are sane. They are NOT a benchmark: 16 cases say
nothing about accuracy, and the real evaluation is real prompts in shadow mode.

What is asserted: every response parses, every field is present and inside its closed enum
or range, cost and tokens are reported, nothing failed. Results go to
tests/out/agent_smoke.json (git-ignored).

--merge-check answers "how many Jev calls per prompt are needed": it sends SIMPLE's frozen
questions alone twice (to measure Jev's own run-to-run noise) and once merged with the agent
questions in a single request, then compares SIMPLE's answers. If the merged deltas are no
bigger than the noise, a future stage could save the second call. This check sends extra
requests directly (not through the sidecar) and changes nothing that is deployed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import agent_decider as ad  # noqa: E402

OUT = ROOT / "tests" / "out" / "agent_smoke.json"
URL = "http://127.0.0.1:8787/agent_decide"

# (prompt, loose expectation) — expectations are for eyeballing only.
CASES = [
    ("что такое async/await в python, объясни коротко", "direct, no tool"),
    ("Fix the typo in README.md: 'recieve' -> 'receive'", "tool, filesystem"),
    ("запусти тесты и почини то, что падает", "shell, main_claude"),
    ("Найди в интернете актуальные цены на Claude API и сравни с GPT", "web, research"),
    ("Review PR #42 on GitHub and leave comments", "github"),
    ("вот лог сборки на 3000 строк, сожми его до ключевых ошибок", "mercury, compaction"),
    ("Extract all email addresses and dates from this pasted text into JSON", "mercury"),
    ("Explore the whole monorepo and map how auth flows through all services", "subagent"),
    ("отрефактори модуль платежей: разнеси по слоям, обнови все вызовы и тесты", "main_claude, complex"),
    ("Create a Figma frame for the settings screen", "mcp"),
    ("какие из этих 40 файлов относятся к биллингу? дай шортлист", "mercury, shortlist"),
    ("Why does this regex not match multiline input: ^foo$", "direct"),
    ("Проверь статус CI на моём PR и перезапусти упавшие джобы", "github"),
    ("Summarise the last 500 lines of the server log and tell me what broke", "mercury_then_claude"),
    ("сделай график выручки по месяцам из sales.csv", "tool, dataviz"),
    ("спасибо, всё отлично", "direct"),
]

FIELDS = {"needs_tool": (True, False), "tool_type": ad.TOOL_TYPES,
          "needs_subagent": (True, False), "needs_research": (True, False),
          "needs_context_compaction": (True, False), "worker_candidate": ("none", "mercury"),
          "escalation": ad.ESCALATIONS}


def via_sidecar(prompt: str) -> dict:
    req = urllib.request.Request(URL, method="POST",
                                 data=json.dumps({"prompt": prompt}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "X-Jev-Sidecar": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def check(d: dict) -> list[str]:
    problems = []
    if not d.get("ok"):
        problems.append(f"not ok: {d.get('error')}")
    dec = d.get("decision") or {}
    for k, allowed in FIELDS.items():
        if dec.get(k) not in allowed:
            problems.append(f"{k}={dec.get(k)!r}")
    oc = dec.get("overall_confidence")
    if not isinstance(oc, float) or not 0 < oc <= 1:
        problems.append(f"overall_confidence={oc!r}")
    if not d.get("input_tokens") or d.get("cost_usd") is None:
        problems.append("usage missing")
    return problems


def merge_check(prompts: list[str]) -> dict:
    import jev_router
    simple_q = jev_router.build_questions(jev_router.load_catalog())
    merged_q = dict(simple_q)
    merged_q.update({f"agent_{k}": v for k, v in ad.QUESTIONS.items()})
    rows = []
    with jev_router.Router() as r:
        def ask(q, prompt):
            body = json.dumps({"state": {"request": prompt}, "model": r.model, "questions": q})
            data, ms = r._post(body)
            return data, ms

        for p in prompts:
            a1, ms1 = ask(simple_q, p)
            a2, _ = ask(simple_q, p)
            b, msb = ask(merged_q, p)

            def sk(x):
                s = x["answers"]["skill"]
                return s["choice"], s["probabilities"], x["answers"]["complexity"]["score"]

            c1, p1, x1 = sk(a1)
            c2, p2, x2 = sk(a2)
            cb, pb, xb = sk(b)
            keys = set(p1) | set(p2) | set(pb)
            rows.append({
                "noise_max_prob_delta": max(abs(p1.get(k, 0) - p2.get(k, 0)) for k in keys),
                "merged_max_prob_delta": max(abs(p1.get(k, 0) - pb.get(k, 0)) for k in keys),
                "noise_complexity_delta": abs(x1 - x2), "merged_complexity_delta": abs(x1 - xb),
                "noise_choice_flip": c1 != c2, "merged_choice_flip": c1 != cb,
                "tokens_simple": a1["usage"]["input_tokens"], "tokens_merged": b["usage"]["input_tokens"],
                "ms_simple": round(ms1), "ms_merged": round(msb),
            })
    agg = {k: round(statistics.mean(r[k] for r in rows), 4)
           for k in rows[0] if not k.endswith("flip")}
    agg.update({"noise_choice_flips": sum(r["noise_choice_flip"] for r in rows),
                "merged_choice_flips": sum(r["merged_choice_flip"] for r in rows),
                "max_merged_prob_delta": max(r["merged_max_prob_delta"] for r in rows),
                "max_noise_prob_delta": max(r["noise_max_prob_delta"] for r in rows)})
    return {"mean": agg, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge-check", action="store_true")
    args = ap.parse_args()

    results, failures = [], 0
    for prompt, expect in CASES:
        t0 = time.perf_counter()
        d = via_sidecar(prompt)
        wall = (time.perf_counter() - t0) * 1000
        problems = check(d)
        failures += bool(problems)
        dec = d.get("decision") or {}
        results.append({"prompt": prompt, "expect": expect, "decision": dec,
                        "detail": d.get("detail"), "flags": d.get("consistency_flags"),
                        "problems": problems, "input_tokens": d.get("input_tokens"),
                        "output_tokens": d.get("output_tokens"),
                        "cost_usd": d.get("cost_usd"), "api_ms": d.get("latency_ms"),
                        "wall_ms": round(wall, 1)})
        print(f"{'FAIL' if problems else ' ok '} tool={str(dec.get('needs_tool')):5} "
              f"type={str(dec.get('tool_type')):10} sub={str(dec.get('needs_subagent')):5} "
              f"res={str(dec.get('needs_research')):5} cmp={str(dec.get('needs_context_compaction')):5} "
              f"wrk={str(dec.get('worker_candidate')):7} esc={str(dec.get('escalation')):19} "
              f"oc={dec.get('overall_confidence')}  | expect: {expect}")
        for pr in problems:
            print("      !!", pr)

    toks = [r["input_tokens"] for r in results if r["input_tokens"]]
    cost = [r["cost_usd"] for r in results if r["cost_usd"] is not None]
    ms = [r["api_ms"] for r in results if r["api_ms"]]
    summary = {"cases": len(CASES), "schema_failures": failures,
               "input_tokens_mean": round(statistics.mean(toks)) if toks else None,
               "output_tokens_mean": round(statistics.mean(r["output_tokens"] or 0 for r in results)),
               "cost_per_decision_usd": round(statistics.mean(cost), 8) if cost else None,
               "api_ms_median": round(statistics.median(ms), 1) if ms else None,
               "api_ms_max": round(max(ms), 1) if ms else None,
               "flagged_inconsistent": sum(1 for r in results if r["flags"])}
    print(f"\n{summary}")

    out = {"summary": summary, "results": results}
    if args.merge_check:
        print("\nmerge check (SIMPLE alone x2 vs SIMPLE merged with agent questions) ...")
        mc = merge_check([c[0] for c in CASES])
        out["merge_check"] = mc
        print(json.dumps(mc["mean"], indent=2))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
