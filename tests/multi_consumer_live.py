"""Small LIVE multi-consumer check against the real Jev API — about 30 calls, ~$0.002.

    python tests/multi_consumer_live.py            # starts its own jevd on :8797, stops it after

Uses an isolated sidecar on port 8797 so that a jevd on the default 8787 (or a Codex lab
probing 8787) is never touched. The sidecar is the unmodified src/jev_sidecar.py, started as
a subprocess and reading the key from .env itself; this script never reads the key.

  1. sequential /decide baseline, 4 prompts                              4 calls
  2. the same 4 /decide + 4 /agent_decide at once, split across the
     Claude hook client and an independent "Codex" client                8 calls
  3. 3 real Claude hook processes + 3 "Codex" clients at the same
     moment, each doing /decide + /agent_decide in parallel             12 calls
  4. fail-open: the hook against a port with nothing listening           0 calls

Cross-talk check without a mock: a concurrent /decide must pick the same skill for the same
prompt as the sequential baseline (the prompts are chosen far from any near-tie).
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "out"
PORT = 8797
PY = sys.executable
HOOK = ROOT / "hooks" / "jev_shadow_hook.py"

PROMPTS = [
    "Сделай презентацию pptx из этого квартального отчёта 📊",
    "Fix the off-by-one error in the pagination loop",
    "Создай Word документ (.docx) с договором поставки",
    "Explain what this regex does: ^\\d{3}-[A-Z]+$  — 中文 test",
]


def call(path: str, prompt: str, consumer: str, timeout: float = 15) -> dict:
    t0 = time.perf_counter()
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        body = json.dumps({"prompt": prompt, "consumer": consumer}, ensure_ascii=False).encode()
        c.request("POST", path, body, {"Content-Type": "application/json", "X-Jev-Sidecar": "1",
                                       "User-Agent": f"{consumer}-live-test"})
        r = c.getresponse()
        data = json.loads(r.read())
        return {"status": r.status, "data": data, "elapsed": time.perf_counter() - t0}
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "elapsed": time.perf_counter() - t0}
    finally:
        c.close()


def health() -> dict:
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
    c.request("GET", "/health")
    return json.loads(c.getresponse().read())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        health()
        print(f"something already listens on {PORT}; refusing to reuse it")
        return 2
    except OSError:
        pass
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    sidecar = subprocess.Popen([PY, str(ROOT / "src" / "jev_sidecar.py"), "--port", str(PORT)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    res: dict = {"checks": []}

    def check(name, cond, detail=""):
        res["checks"].append({"name": name, "pass": bool(cond), "detail": detail})
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    try:
        for _ in range(50):
            try:
                h0 = health()
                break
            except OSError:
                time.sleep(0.2)
        else:
            print("sidecar did not start")
            return 2

        print("[1] sequential baseline")
        base = {p: call("/decide", p, "baseline")["data"] for p in PROMPTS}
        for p, d in base.items():
            print(f"    {d['skill']!s:28} conf={d['skill_confidence']:<7} {d['action']:9} "
                  f"none_p={d['none_probability']}")
        check("baseline /decide all answered without error",
              all(d.get("error") is None for d in base.values()))

        print("[2] concurrent 4 /decide + 4 /agent_decide, two consumers")
        jobs = [("/decide", p, "claude" if i % 2 else "codex") for i, p in enumerate(PROMPTS)] + \
               [("/agent_decide", p, "codex" if i % 2 else "claude") for i, p in enumerate(PROMPTS)]
        barrier = threading.Barrier(len(jobs))

        def run(j):
            barrier.wait()
            return j, call(*j)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(len(jobs)) as ex:
            out = list(ex.map(run, jobs))
        wall = time.perf_counter() - t0
        dec = [(j, r) for j, r in out if j[0] == "/decide"]
        agt = [(j, r) for j, r in out if j[0] == "/agent_decide"]
        same = [r["data"]["skill"] == base[j[1]]["skill"] and
                r["data"]["prompt_sha256_8"] == sha256(j[1].encode()).hexdigest()[:8]
                for j, r in dec]
        drift = [abs(r["data"]["skill_confidence"] - base[j[1]]["skill_confidence"]) for j, r in dec]
        check("concurrent /decide: same skill as sequential baseline, hash matches request",
              all(same), f"max confidence drift {max(drift):.4f}")
        check("concurrent /agent_decide: all ok, schema agent_v1",
              all(r.get("data", {}).get("ok") and r["data"]["schema"] == "agent_v1" for _, r in agt),
              ", ".join(f"{r['data']['decision']['tool_type']}/{r['data']['decision']['escalation']}"
                        for _, r in agt))
        check("8 concurrent requests finished in about two serialized queues, not eight",
              wall < 6, f"wall {wall:.2f}s; per-request "
              + ", ".join(f"{r['elapsed']:.2f}" for _, r in out))
        res["concurrent"] = {"wall_s": round(wall, 2), "drift": drift}

        print("[3] 3 real Claude hook processes + 3 Codex clients at once")
        tmp = Path(tempfile.mkdtemp(prefix="jev_live_"))
        henv = dict(env, JEV_SIDECAR_PORT=str(PORT), JEV_SHADOW_LOG=str(tmp / "claude_shadow.jsonl"),
                    JEV_AGENT_SHADOW_LOG=str(tmp / "claude_agent.jsonl"))
        henv.pop("JEV_SHADOW_COLLECT", None)
        hook_prompts = ["Проверь ревью этого PR на уязвимости", "Add a unit test for parse_date",
                        "Построй график продаж по месяцам 📈"]
        codex_prompts = ["Refactor the config loader to use pathlib", "Почини падающий тест test_auth",
                         "Summarise this 2,000-line build log"]
        procs = []
        for i, p in enumerate(hook_prompts):
            payload = json.dumps({"session_id": f"claude-live-{i}", "cwd": "C:/claude", "prompt": p,
                                  "hook_event_name": "UserPromptSubmit"}, ensure_ascii=False).encode()
            procs.append((subprocess.Popen([PY, str(HOOK)], env=henv, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE), payload))
        cres: list = []

        def codex(p):
            pair: dict = {}
            ts = [threading.Thread(target=lambda path=path: pair.__setitem__(path, call(path, p, "codex")))
                  for path in ("/decide", "/agent_decide")]
            [t.start() for t in ts]
            [t.join() for t in ts]
            cres.append(pair)
        cts = [threading.Thread(target=codex, args=(p,)) for p in codex_prompts]
        [t.start() for t in cts]
        outs = [pr.communicate(b, timeout=30) + (pr.returncode,) for pr, b in procs]
        [t.join() for t in cts]
        lines = {f: [json.loads(x) for x in (tmp / f).read_text(encoding="utf-8").splitlines()]
                 for f in ("claude_shadow.jsonl", "claude_agent.jsonl")}
        check("Claude hook processes: exit 0, empty stdout/stderr",
              all(o == b"" and e == b"" and rc == 0 for o, e, rc in outs))
        check("Claude hook wrote 3 ok shadow_skill + 3 ok shadow_agent_decision lines",
              [len(v) for v in lines.values()] == [3, 3] and
              all(r["ok"] for v in lines.values() for r in v),
              str({k: [r.get("ok") for r in v] for k, v in lines.items()}))
        check("no prompt text in Claude telemetry lines",
              not any(p in json.dumps(r, ensure_ascii=False) for v in lines.values() for r in v
                      for p in hook_prompts))
        check("Codex clients: 3/3 /decide and 3/3 /agent_decide answered ok",
              all(c["/decide"].get("data", {}).get("error") is None and
                  c["/agent_decide"].get("data", {}).get("ok") for c in cres))
        res["hook_lines"] = lines

        print("[4] fail-open with the sidecar unreachable (no API call)")
        denv = dict(henv, JEV_SIDECAR_PORT="1", JEV_SHADOW_LOG=str(tmp / "down.jsonl"),
                    JEV_AGENT_SHADOW_LOG=str(tmp / "down_agent.jsonl"))
        t0 = time.perf_counter()
        pr = subprocess.run([PY, str(HOOK)], env=denv, capture_output=True, timeout=30,
                            input=json.dumps({"session_id": "x", "prompt": "Привет"}).encode())
        down = json.loads((tmp / "down.jsonl").read_text(encoding="utf-8"))
        check("hook with sidecar down: exit 0, silent, records source=unreachable",
              pr.returncode == 0 and pr.stdout == b"" and pr.stderr == b"" and
              down["source"] == "unreachable", f"{time.perf_counter() - t0:.2f}s")

        h1 = health()
        res["health"] = h1
        calls = (h1["decisions"] - h0["decisions"]) + (h1["agent_decisions"] - h0["agent_decisions"])
        spend = h1["spend_usd"] + h1["agent_spend_usd"]
        print(f"\nlive calls {calls}, failures {h1['failures']}+{h1['agent_failures']}, "
              f"spend ${spend:.6f}")
        check("no upstream failures during the live run",
              h1["failures"] == 0 and h1["agent_failures"] == 0)
    finally:
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
            c.request("POST", "/quit", b"", {"X-Jev-Sidecar": "1"})
            c.getresponse().read()
        except OSError:
            pass
        try:
            so, se = sidecar.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            sidecar.kill()
            so, se = sidecar.communicate()
        res["sidecar_stderr_bytes"] = len(se)
        res["sidecar_stdout_tail"] = so.decode("utf-8", "replace").strip().splitlines()[-1:]
    (OUT / "multi_consumer_live.json").write_text(json.dumps(res, indent=2, ensure_ascii=False),
                                                  encoding="utf-8")
    failed = [c for c in res["checks"] if not c["pass"]]
    print(f"{len(res['checks'])} checks, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
