"""Report on the agent-decision shadow layer: what Jev predicted vs what Claude did.

    python scripts/agent_shadow_report.py
    python scripts/agent_shadow_report.py --since 2026-09-24 --json
    python scripts/agent_shadow_report.py --include-synthetic     # also smoke/test sessions

Reads telemetry/agent_shadow.jsonl (`shadow_agent_decision` lines), joins each line to the
local Claude Code transcript through scripts/shadow_correlate.py, and scores every
prediction field with one of four statuses:

    correct         the observed turn agrees
    incorrect       the observed turn clearly disagrees
    ambiguous       the observed turn cannot fairly settle it (rules below)
    not_observable  there is nothing to compare with (no transcript, no join, no signal)

Accuracy, precision and recall use only correct + incorrect. Coverage is the share of
decisions that got one of those two. Everything runs locally; nothing is sent anywhere.

The judging rules are deliberately explicit, and their thresholds are constants below so
they can be argued with. They are proxies — Claude's behaviour is the reference, not the
truth: a turn with no subagent does not prove a subagent would not have helped.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shadow_correlate import Correlator, load_jsonl  # noqa: E402

AGENT_LOG = ROOT / "telemetry" / "agent_shadow.jsonl"
CATALOG = ROOT / "src" / "skills_catalog.json"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# --- judging thresholds (proxies; see AGENT_SHADOW.md) ----------------------------------
PEEK_MAX_CALLS = 1                 # a single file read does not refute "no tool needed"
TOOL_MAX_CALLS = 3                 # escalation "tool": 1..3 work calls
MAIN_MIN_CALLS = 10                # escalation "main_claude": >= 10; 4..9 is a grey zone
SUBAGENT_PLAUSIBLE_CALLS = 25      # >= this many calls: a subagent was a defensible choice
LOCAL_RESEARCH_FS_CALLS = 15       # >= this many file reads: "research" was done locally
COMPACT_MAX_SINGLE = 25_000        # one tool output this large = compaction-shaped
COMPACT_TOTAL = 250_000            # or this much tool output in the turn
NO_COMPACT_TOTAL = 40_000          # below this total ...
NO_COMPACT_MAX_SINGLE = 10_000     # ... and below this single output = clearly no
TRIVIAL_MAX_CALLS, TRIVIAL_MAX_S = 1, 90
NORMAL_CALLS, NORMAL_MAX_S = (3, 15), 600
COMPLEX_MIN_CALLS, COMPLEX_MIN_S = 25, 900

BOOL_FIELDS = ("needs_tool", "needs_subagent", "needs_research", "needs_context_compaction")
FIELDS = ("skill", "complexity", "needs_tool", "tool_type", "needs_subagent", "needs_research",
          "needs_context_compaction", "worker_candidate", "escalation")
PROB_KEY = {"needs_tool": "needs_tool_p", "needs_subagent": "needs_subagent_p",
            "needs_research": "needs_research_p",
            "needs_context_compaction": "needs_context_compaction_p",
            "worker_candidate": "worker_mercury_p"}
CONF_KEY = {"tool_type": "tool_type_confidence", "escalation": "escalation_confidence"}

C, I, A, N = "correct", "incorrect", "ambiguous", "not_observable"


def catalog_skills() -> set[str]:
    try:
        return set(json.loads(CATALOG.read_text(encoding="utf-8"))["skills"])
    except (OSError, ValueError, KeyError):
        return set()


def _cmp(pred, actual) -> str:
    return C if pred == actual else I


def judge(field: str, pred, f: dict, catalog: set[str]) -> tuple[str, object]:
    """(status, observed value) for one predicted field against one turn's facts."""
    if pred is None:
        return N, None
    calls = f["tool_calls"]

    if field == "skill":
        # Only skills the router can choose count. Loads of other skills — obsidian-worklog,
        # which CLAUDE.md orders on every substantial task, or anything added after the catalog
        # was written — are invisible to this field (the report counts them separately).
        used = [s for s in f["skills"] if s in catalog]
        if pred == "none":
            return (C if not used else I), (used[0] if used else "none")
        return (C if pred in used else I), (used[0] if used else "none")

    if field == "needs_tool":
        actual = calls > 0
        if pred is False and actual and calls <= PEEK_MAX_CALLS and f["fs_calls"] == calls:
            return A, actual
        return _cmp(pred, actual), actual

    if field == "tool_type":
        cats = {k: v for k, v in f["categories"].items()
                if k in ("filesystem", "shell", "web", "github", "mcp")}
        if not cats:
            return (C if pred == "none" else (A if calls else I)), "none"
        dom = f["tool_type_dominant"]
        if pred == dom:
            return C, dom
        # Claude reads files through Bash as often as through Read: the two are one bucket
        # in practice, so a filesystem/shell swap is not evidence either way.
        if {pred, dom} <= {"filesystem", "shell"}:
            return A, dom
        if cats.get(pred, 0) >= 0.25 * sum(cats.values()):
            return A, dom
        return I, dom

    if field == "needs_subagent":
        actual = f["subagent_used"]
        if pred is True and not actual and calls >= SUBAGENT_PLAUSIBLE_CALLS:
            return A, actual
        return _cmp(pred, actual), actual

    if field == "needs_research":
        actual = f["web_used"]
        if pred is True and not actual and f["fs_calls"] >= LOCAL_RESEARCH_FS_CALLS:
            return A, actual
        return _cmp(pred, actual), actual

    if field == "needs_context_compaction":
        big = (f["compaction_event"] or f["max_tool_output_chars"] >= COMPACT_MAX_SINGLE
               or f["tool_output_chars"] >= COMPACT_TOTAL)
        small = (f["tool_output_chars"] < NO_COMPACT_TOTAL
                 and f["max_tool_output_chars"] < NO_COMPACT_MAX_SINGLE)
        if big:
            return _cmp(pred, True), True
        if small:
            return _cmp(pred, False), False
        return A, None

    if field == "worker_candidate":
        # Whether Mercury *would have helped* is in no transcript. Nothing offloads to Mercury
        # automatically, so a Mercury call in a turn means someone was working *on* Mercury
        # (this project benchmarks it), not that the turn's work suited it. Evidence either
        # way waits for the manual review of the candidates the report lists.
        return (A if f["mercury_called"] else N), None

    if field == "escalation":
        # No branch maps to mercury_then_claude: it is never observed (see worker_candidate).
        if f["subagent_used"]:
            actual = {"subagent"}
        elif calls == 0:
            actual = {"direct"}
        elif calls <= TOOL_MAX_CALLS:
            actual = {"tool"}
        elif calls >= MAIN_MIN_CALLS:
            actual = {"main_claude"}
        else:
            actual = {"tool", "main_claude"}
        shown = "|".join(sorted(actual))
        if pred == "mercury_then_claude" and pred not in actual:
            return (I if actual == {"direct"} else A), shown
        if pred == "subagent" and "main_claude" in actual and calls >= SUBAGENT_PLAUSIBLE_CALLS:
            return A, shown
        if len(actual) > 1:
            return (A if pred in actual else I), shown
        return (C if pred in actual else I), shown

    if field == "complexity":
        dur = f["duration_s"] if f["duration_s"] is not None else 0
        if calls <= TRIVIAL_MAX_CALLS and dur <= TRIVIAL_MAX_S and not f["subagent_used"]:
            actual = "trivial"
        elif calls >= COMPLEX_MIN_CALLS or f["subagent_used"] or dur >= COMPLEX_MIN_S:
            actual = "complex"
        elif NORMAL_CALLS[0] <= calls <= NORMAL_CALLS[1] and dur <= NORMAL_MAX_S:
            actual = "normal"
        else:
            return A, None
        return _cmp(pred, actual), actual

    return N, None


def score_row(row: dict, f: dict | None, catalog: set[str]) -> dict[str, tuple[str, object]]:
    d = row.get("decision") or {}
    out = {}
    for field in FIELDS:
        pred = d.get(field)
        if f is None:
            out[field] = (N, None)
        elif not f["responded"] or f["interrupted"]:
            out[field] = (N, None)
        elif f["boundary"] == "mid_turn":
            # A message absorbed into a running turn: the work after it is a continuation of
            # the earlier task, so it cannot cleanly confirm or refute this prediction.
            out[field] = (A if pred is not None else N, None)
        else:
            out[field] = judge(field, pred, f, catalog)
    return out


# --- aggregation ------------------------------------------------------------------------

def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))], 1)


def field_metrics(field: str, items: list[tuple[dict, tuple[str, object]]]) -> dict:
    st = Counter(s for _, (s, _) in items)
    n = sum(st.values())
    judged = st[C] + st[I]
    m = {"n": n, **{k: st[k] for k in (C, I, A, N)},
         "coverage": round(judged / n, 3) if n else None,
         "accuracy": round(st[C] / judged, 3) if judged else None}
    positive = {"worker_candidate": "mercury"}.get(field, True)
    if field in BOOL_FIELDS or field == "worker_candidate":
        tp = fp = fn = tn = 0
        for row, (s, _) in items:
            if s not in (C, I):
                continue
            pred_pos = (row["decision"].get(field) == positive)
            actual_pos = pred_pos if s == C else not pred_pos
            tp += pred_pos and actual_pos
            fp += pred_pos and not actual_pos
            fn += (not pred_pos) and actual_pos
            tn += (not pred_pos) and not actual_pos
        m.update({"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                  "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                  "recall": round(tp / (tp + fn), 3) if tp + fn else None})
    return m


def calibration(field: str, items) -> list[dict]:
    """Nouls: bucket by P(yes), compare with the observed positive rate (and a Brier score).
    Choices: bucket by Jev's confidence, compare with accuracy."""
    out = []
    buckets = defaultdict(list)
    for row, (s, _) in items:
        if s not in (C, I):
            continue
        det = row.get("detail") or {}
        if field in PROB_KEY:
            p = det.get(PROB_KEY[field])
            if p is None:
                continue
            positive = {"worker_candidate": "mercury"}.get(field, True)
            pred_pos = row["decision"].get(field) == positive
            actual_pos = pred_pos if s == C else not pred_pos
            buckets[min(int(p * 5), 4)].append((p, float(actual_pos)))
        elif field in CONF_KEY:
            c = det.get(CONF_KEY[field])
            if c is None:
                continue
            buckets[min(int(c * 5), 4)].append((c, float(s == C)))
    for b in sorted(buckets):
        vals = buckets[b]
        out.append({"bucket": f"{b / 5:.1f}-{(b + 1) / 5:.1f}", "n": len(vals),
                    "mean_predicted": round(statistics.mean(v[0] for v in vals), 3),
                    "observed": round(statistics.mean(v[1] for v in vals), 3)})
    return out


def brier(field: str, items) -> float | None:
    if field not in PROB_KEY:
        return None
    errs = []
    for row, (s, _) in items:
        if s not in (C, I):
            continue
        p = (row.get("detail") or {}).get(PROB_KEY[field])
        if p is None:
            continue
        positive = {"worker_candidate": "mercury"}.get(field, True)
        pred_pos = row["decision"].get(field) == positive
        actual_pos = pred_pos if s == C else not pred_pos
        errs.append((p - float(actual_pos)) ** 2)
    return round(statistics.mean(errs), 4) if errs else None


def build(rows: list[dict], include_synthetic: bool) -> dict:
    catalog = catalog_skills()
    if not include_synthetic:
        rows = [r for r in rows if UUID.match(str(r.get("session_id") or ""))]
    ok = [r for r in rows if r.get("ok") and r.get("decision")]
    corr = Correlator()
    scored = []
    for r in ok:
        f = corr.lookup(r)
        scored.append((r, f, score_row(r, f, catalog)))

    dec = [r["decision"] for r in ok]

    def rate(field, value=True):
        vals = [d.get(field) for d in dec if d.get(field) is not None]
        return round(sum(v == value for v in vals) / len(vals), 3) if vals else None

    per_field, calib, briers = {}, {}, {}
    for field in FIELDS:
        items = [(r, s[field]) for r, _, s in scored if (r.get("decision") or {}).get(field) is not None]
        per_field[field] = field_metrics(field, items)
        calib[field] = calibration(field, items)
        briers[field] = brier(field, items)

    # overall_confidence vs the share of judged fields that were right, per decision
    oc = defaultdict(list)
    for r, _, s in scored:
        judged = [v[0] for v in s.values() if v[0] in (C, I)]
        c = r["decision"].get("overall_confidence")
        if judged and c is not None:
            oc[min(int(c * 5), 4)].append((c, sum(x == C for x in judged) / len(judged)))
    overall_cal = [{"bucket": f"{b / 5:.1f}-{(b + 1) / 5:.1f}", "n": len(v),
                    "mean_overall_confidence": round(statistics.mean(x[0] for x in v), 3),
                    "field_accuracy": round(statistics.mean(x[1] for x in v), 3)}
                   for b, v in sorted(oc.items())]

    lat = [r["sidecar_latency_ms"] for r in ok if r.get("sidecar_latency_ms")]
    hook = [r["hook_total_ms"] for r in rows if r.get("hook_total_ms")]
    toks = [r["input_tokens"] for r in ok if r.get("input_tokens")]
    cost = sum(r.get("cost_usd") or 0 for r in rows)

    mercury_review = []
    for r, f, _ in scored:
        d = r["decision"]
        if d.get("worker_candidate") == "mercury" or d.get("escalation") == "mercury_then_claude":
            mercury_review.append({
                "ts": r["ts"][:19], "prompt_sha256_12": r.get("prompt_sha256_12"),
                "worker_mercury_p": (r.get("detail") or {}).get("worker_mercury_p"),
                "escalation": d.get("escalation"),
                "observed": None if f is None else {
                    k: f[k] for k in ("boundary", "tool_calls", "tool_type_dominant",
                                      "tool_output_chars", "max_tool_output_chars",
                                      "mercury_called", "duration_s")}})

    return {
        "decisions": len(rows),
        "succeeded": len(ok),
        "failed": len(rows) - len(ok),
        "failure_sources": dict(Counter(r.get("source") for r in rows if not r.get("ok"))),
        "sessions": len({r.get("session_id") for r in rows}),
        "joined_to_transcript": sum(1 for _, f, _ in scored if f is not None),
        "boundaries": dict(Counter(f["boundary"] for _, f, _ in scored if f is not None)),
        "out_of_catalog_skill_loads": dict(Counter(
            sk for _, f, _ in scored if f is not None for sk in f["skills"] if sk not in catalog)),
        "predicted": {
            "complexity": dict(Counter(d.get("complexity") for d in dec)),
            "needs_tool": rate("needs_tool"),
            "needs_subagent": rate("needs_subagent"),
            "needs_research": rate("needs_research"),
            "needs_context_compaction": rate("needs_context_compaction"),
            "mercury_candidate": rate("worker_candidate", "mercury"),
            "skill_fired": None if rate("skill", "none") is None else round(1 - rate("skill", "none"), 3),
            "tool_type": dict(Counter(d.get("tool_type") for d in dec)),
            "escalation": dict(Counter(d.get("escalation") for d in dec)),
            "consistency_flag_rate": round(sum(1 for r in ok if r.get("consistency_flags")) / len(ok), 3)
            if ok else None,
        },
        "fields": per_field,
        "brier": briers,
        "calibration": calib,
        "overall_confidence_calibration": overall_cal,
        "latency_ms": {"sidecar_median": pct(lat, 50), "sidecar_p95": pct(lat, 95),
                       "hook_median": pct(hook, 50), "hook_p95": pct(hook, 95)},
        "tokens": {"input_per_decision_mean": round(statistics.mean(toks)) if toks else None},
        "cost_usd": {"total": round(cost, 6),
                     "per_decision": round(cost / len(ok), 8) if ok else None},
        "mercury_review": mercury_review,
    }


def render(s: dict) -> None:
    n = s["decisions"]
    if not n:
        print("No agent-decision telemetry yet.\n"
              "Start the sidecar (python src/jev_sidecar.py) and use Claude Code in this project;\n"
              "every prompt appends one line to telemetry/agent_shadow.jsonl.")
        return
    print(f"Agent-decision shadow layer — {n} real decisions, {s['sessions']} session(s)")
    print(f"  succeeded {s['succeeded']}, failed {s['failed']} {s['failure_sources'] or ''}")
    print(f"  joined to a transcript: {s['joined_to_transcript']}  boundaries {s['boundaries']}")
    if s["out_of_catalog_skill_loads"]:
        print(f"  skills Claude loaded that the router cannot choose (not scored): "
              f"{s['out_of_catalog_skill_loads']}")

    p = s["predicted"]
    print("\nWhat Jev predicted")
    print(f"  complexity           : {p['complexity']}")
    for k in ("needs_tool", "needs_subagent", "needs_research", "needs_context_compaction",
              "mercury_candidate", "skill_fired"):
        v = p[k]
        print(f"  {k:<24} : {'-' if v is None else f'{v:.0%}'}")
    print(f"  tool_type            : {p['tool_type']}")
    print(f"  escalation           : {p['escalation']}")
    print(f"  consistency flags    : {p['consistency_flag_rate']}")

    print("\nAgainst what Claude did (accuracy/precision/recall on correct+incorrect only)")
    print(f"  {'field':<26}{'n':>4}{'ok':>4}{'bad':>4}{'amb':>4}{'n/o':>4}  {'cover':>6} "
          f"{'acc':>6} {'prec':>6} {'rec':>6} {'FP':>3} {'FN':>3}  brier")
    for field, m in s["fields"].items():
        def f(x):
            return "   -  " if x is None else f"{x:6.2f}"
        print(f"  {field:<26}{m['n']:>4}{m['correct']:>4}{m['incorrect']:>4}{m['ambiguous']:>4}"
              f"{m['not_observable']:>4}  {f(m['coverage'])} {f(m['accuracy'])} "
              f"{f(m.get('precision'))} {f(m.get('recall'))} {m.get('fp', '-'):>3} "
              f"{m.get('fn', '-'):>3}  {s['brier'].get(field) if s['brier'].get(field) is not None else '-'}")

    print("\nCalibration (bucket: n, mean predicted -> observed)")
    for field, rows in s["calibration"].items():
        if rows:
            print(f"  {field}: " + "  ".join(
                f"[{r['bucket']}] {r['n']}: {r['mean_predicted']:.2f}->{r['observed']:.2f}" for r in rows))
    if s["overall_confidence_calibration"]:
        print("  overall_confidence: " + "  ".join(
            f"[{r['bucket']}] {r['n']}: {r['mean_overall_confidence']:.2f}->{r['field_accuracy']:.2f}"
            for r in s["overall_confidence_calibration"]))

    L = s["latency_ms"]
    print("\nLatency / cost")
    print(f"  agent call via sidecar : median {L['sidecar_median']} ms, p95 {L['sidecar_p95']} ms")
    print(f"  whole hook (both calls): median {L['hook_median']} ms, p95 {L['hook_p95']} ms (async)")
    print(f"  input tokens/decision  : {s['tokens']['input_per_decision_mean']}")
    print(f"  cost                   : ${s['cost_usd']['total']:.6f} total, "
          f"${s['cost_usd']['per_decision'] or 0:.8f} per decision (agent layer only)")

    if s["mercury_review"]:
        print(f"\nMercury candidates for manual review ({len(s['mercury_review'])})")
        for m in s["mercury_review"][-15:]:
            print(f"  {m['ts']} {m['prompt_sha256_12']} p={m['worker_mercury_p']} esc={m['escalation']} "
                  f"observed={m['observed']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(AGENT_LOG))
    ap.add_argument("--since", default=None)
    ap.add_argument("--include-synthetic", action="store_true",
                    help="include non-UUID sessions (smoke tests, manual hook runs)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = [r for r in load_jsonl(Path(args.log))
            if r.get("type") == "shadow_agent_decision"
            and (not args.since or (r.get("ts") or "") >= args.since)]
    s = build(rows, args.include_synthetic)
    if args.json:
        print(json.dumps(s, indent=2, ensure_ascii=False))
    else:
        render(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
