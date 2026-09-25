"""Measure what the sidecar actually buys, end to end.

    python scripts/sidecar_probe.py            # assumes jevd is already running

Three routes are timed on the same prompts:

  direct-cold   a fresh Router per decision — what a naive hook does, paying TLS every time
  direct-warm   one Router reused — the in-process best case
  sidecar       a fresh HTTP client per decision over loopback — what a hook actually does

The third is the number that matters: it is measured the way a hook experiences it, a new
client per call, so it includes the loopback round trip and the sidecar's own overhead.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jev_router import Router  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
URL = f"http://127.0.0.1:{PORT}"

PROMPTS = [
    "Fix the failing test in tests/test_tax.py",
    "Turn this quarterly revenue table into a chart for the board deck",
    "Why is my goroutine leaking when I return early?",
    "Write a CLAUDE.md for this repository",
    "Rename the variable usr to user",
    "What does Opus cost per million tokens?",
    "Review the diff on this branch for bugs",
    "Add retry with exponential backoff to the HTTP client",
]


def sidecar_decide(prompt: str) -> tuple[dict, float]:
    """A brand-new connection per call, exactly as a hook process would do it."""
    req = urllib.request.Request(
        f"{URL}/decide", method="POST",
        data=json.dumps({"prompt": prompt}).encode(),
        headers={"Content-Type": "application/json", "X-Jev-Sidecar": "1"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode())
    return body, (time.perf_counter() - started) * 1000


def stats(name: str, samples: list[float]) -> dict:
    s = sorted(samples)
    row = {"route": name, "n": len(s), "min": round(s[0], 1),
           "median": round(statistics.median(s), 1),
           "p90": round(s[min(len(s) - 1, int(0.9 * len(s)))], 1),
           "max": round(s[-1], 1)}
    print(f"  {name:<14} n={row['n']:<3} min={row['min']:>7.1f}  med={row['median']:>7.1f}  "
          f"p90={row['p90']:>7.1f}  max={row['max']:>7.1f}  ms")
    return row


def main() -> int:
    # Confirm jevd is up before spending anything.
    try:
        with urllib.request.urlopen(f"{URL}/health", timeout=3) as r:
            health = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"jevd is not reachable on {URL}: {exc}\n"
              f"start it with:  python src/jev_sidecar.py --port {PORT}", file=sys.stderr)
        return 1
    print(f"jevd up: pid {health['pid']}, model {health['model']}, "
          f"{health['decisions']} decisions so far\n")

    rows = []

    print("direct, new connection every call (what a naive hook pays):")
    cold = []
    for p in PROMPTS[:4]:
        started = time.perf_counter()
        r = Router()
        r.decide(p)
        cold.append((time.perf_counter() - started) * 1000)
        r.close()
    rows.append(stats("direct-cold", cold))

    print("\ndirect, one reused connection (in-process best case):")
    warm = []
    with Router() as r:
        r.decide("warm up")
        for p in PROMPTS:
            started = time.perf_counter()
            r.decide(p)
            warm.append((time.perf_counter() - started) * 1000)
    rows.append(stats("direct-warm", warm))

    print("\nvia sidecar, new HTTP client every call (what a hook actually pays):")
    sc, upstream = [], []
    sidecar_decide("warm up")
    for p in PROMPTS:
        body, ms = sidecar_decide(p)
        sc.append(ms)
        upstream.append(body["latency_ms"])
    rows.append(stats("sidecar", sc))
    rows.append(stats("  of which API", upstream))

    overhead = statistics.median(sc) - statistics.median(upstream)
    saved = statistics.median(cold) - statistics.median(sc)
    print(f"\n  loopback + sidecar overhead : {overhead:.1f} ms above the API call itself")
    print(f"  saved vs a cold direct call : {saved:.1f} ms per prompt")

    out = ROOT / "benchmarks" / "results_sidecar.json"
    out.write_text(json.dumps({"rows": rows, "overhead_ms": round(overhead, 1),
                               "saved_vs_cold_ms": round(saved, 1),
                               "prompts": len(PROMPTS)}, indent=2), encoding="utf-8")
    print(f"\nWritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
