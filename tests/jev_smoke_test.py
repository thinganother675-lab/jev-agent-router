"""Jev smoke test — verifies every documented capability with tiny requests.

Run:  python tests/jev_smoke_test.py
Cost: ~0.0001 USD per full run (a few hundred input tokens total).

Checks: GET /v1/models, yes/no (noul), Choice, Score, multi-question batching,
confidence/probabilities, usage accounting, and error handling on a bad key.
Never prints the API key.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import jev_client as jev  # noqa: E402

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str, resp: dict | None = None) -> None:
    row = {"test": name, "ok": ok, "detail": detail}
    if resp:
        row |= {
            "latency_ms": resp["latency_ms"],
            "input_tokens": resp["usage"]["input_tokens"],
            "output_tokens": resp["usage"]["output_tokens"],
            "cost_usd": resp["cost_usd"],
        }
    RESULTS.append(row)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def t_models() -> None:
    models, ms = jev.list_models()
    names = [m["name"] for m in models]
    record("GET /v1/models", bool(names), f"{len(names)} models: {names} ({ms:.0f}ms)")


def t_noul() -> None:
    r = jev.ask(
        "Fix the typo in the README title.",
        {"is_code": jev.noul("Does this task require editing source code?")},
    )
    a = r["answers"]["is_code"]
    record("noul (yes/no + probability)", a["type"] == "noul" and 0 <= a["noul"] <= 1,
           f"p(yes)={a['noul']:.3f}", r)


def t_choice() -> None:
    r = jev.ask(
        "The login endpoint returns 500 when the password contains a colon.",
        {"kind": jev.choice(
            {"bug": "A defect that must be diagnosed and fixed",
             "feature": "A request for new functionality",
             "question": "A request for information only"},
            "What kind of task is this?")},
    )
    a = r["answers"]["kind"]
    ok = a["choice"] in a["probabilities"] and abs(sum(a["probabilities"].values()) - 1) < 0.05
    record("choice (+confidence, probabilities)", ok,
           f"choice={a['choice']} conf={a['confidence']:.3f} probs={ {k: round(v,3) for k,v in a['probabilities'].items()} }", r)


def t_score() -> None:
    r = jev.ask(
        "Rewrite the authentication layer to support SSO across three services.",
        {"complexity": jev.score(
            ["Trivial: a one-line or cosmetic change",
             "Normal: a contained change in one or two files",
             "Complex: multi-file design work needing planning"],
            "How complex is this software task?")},
    )
    a = r["answers"]["complexity"]
    ok = a["type"] == "score" and 0 <= a["score"] <= len(a["legend"]) - 1
    record("score (+legend, confidence)", ok,
           f"score={a['score']:.2f} conf={a['confidence']:.3f} legend={a['legend']}", r)


def t_batch() -> None:
    """Several questions in one request share one state -> one billing of the state."""
    r = jev.ask(
        "Research the history of the Kuiper belt and draft an article outline.",
        {
            "needs_skill": jev.noul("Does this task need a specialised skill or playbook?"),
            "domain": jev.choice({"coding": "Writing or changing software",
                                  "research": "Gathering and verifying information",
                                  "writing": "Producing prose for humans",
                                  "ops": "Infrastructure, git or deployment work"}),
            "risk": jev.score(["No side effects", "Modifies local files", "Irreversible or external side effects"],
                              "How risky is executing this task?"),
        },
    )
    a = r["answers"]
    record("batched multi-question", len(a) == 3,
           f"needs_skill={a['needs_skill']['noul']:.2f} domain={a['domain']['choice']}"
           f"({a['domain']['confidence']:.2f}) risk={a['risk']['score']:.2f}", r)


def t_latency(n: int = 3) -> None:
    lat = []
    for _ in range(n):
        r = jev.ask("ping", {"q": jev.noul("Is this a greeting?")})
        lat.append(r["latency_ms"])
        time.sleep(0.2)
    record("latency sample", True,
           f"min={min(lat):.0f}ms med={sorted(lat)[len(lat)//2]:.0f}ms max={max(lat):.0f}ms over {n} calls")


def t_bad_key() -> None:
    """Failure path must raise cleanly so callers can fail open."""
    import os
    saved = {k: os.environ.pop(k, None) for k in jev.KEY_NAMES}
    os.environ["jev_api"] = "apik_definitely_invalid_key_for_testing"
    try:
        jev.ask("x", {"q": jev.noul("Is this a test?")})
        record("auth failure handling", False, "expected JevError, got a response")
    except jev.JevError as exc:
        msg = str(exc)
        leaked = any(v and v in msg for v in saved.values())
        record("auth failure handling", not leaked, f"raised JevError, no key in message: {msg[:80]}")
    finally:
        os.environ.pop("jev_api", None)
        for k, v in saved.items():
            if v:
                os.environ[k] = v


def main() -> int:
    print("Jev smoke test — model:", jev.DEFAULT_MODEL, "\n")
    for fn in (t_models, t_noul, t_choice, t_score, t_batch, t_latency, t_bad_key):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - smoke test reports, never crashes
            record(fn.__name__, False, f"{type(exc).__name__}: {exc}")
        print()

    billed = [r for r in RESULTS if "cost_usd" in r]
    total = sum(r["cost_usd"] for r in billed)
    tokens = sum(r["input_tokens"] for r in billed)
    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"{passed}/{len(RESULTS)} passed | {tokens} input tokens | ${total:.6f} this run")

    out = Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)
    (out / "smoke_results.json").write_text(
        json.dumps({"results": RESULTS, "total_input_tokens": tokens, "total_cost_usd": total}, indent=2),
        encoding="utf-8")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
