"""Offline multi-consumer stress and fault test for jevd — spends no Jev credits.

    python tests/multi_consumer_stress.py            # prints a summary, writes tests/out/

What it drives, and what it fakes:

  * REAL: src/jev_sidecar.py (State, make_handler, ThreadingHTTPServer), the frozen Router's
    `_post` (keep-alive, retry, close-on-error), AgentRouter, the unchanged Claude hook
    (`hooks/jev_shadow_hook.py`, imported for `ask_sidecar` and run as a subprocess).
  * FAKE: the upstream. `Router._connection` is patched *in this process only* to open a
    plain HTTP keep-alive connection to a local mock of POST /v1/systemone. The API key is
    replaced by a dummy before import, so the real key never reaches the mock.

The mock answers deterministically from the prompt's hash, so every response can be checked
against the request that produced it: a cross-talk bug (answer delivered to the wrong
caller) shows up as a mismatch, not as noise.

Prompt prefixes select mock behaviour: OK, HANG (longer than the upstream timeout), E500,
FAST500, BAD (200 with a malformed body), DROP1 (first attempt: socket closed with no
response; retry answers normally).

"Codex" here is a second, independently written client (http.client, keep-alive, its own
payload) — a stand-in for any second consumer, not the real Codex hook.
"""

from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "out"
os.environ["jev_api"] = "dummy-offline-test-key"      # before any import reads .env
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "hooks"))

import jev_router  # noqa: E402
import agent_decider  # noqa: E402
import jev_sidecar  # noqa: E402

SKILLS = list(jev_router.load_catalog())
UPSTREAM_TIMEOUT = 1.0
HANG_S = 2.5
OK_DELAY = 0.05
PY = sys.executable

RESULTS: dict = {}
FAILS: list[str] = []


def info(name: str, detail) -> None:
    """A measured fact that is not pass/fail (a finding, or a nondeterministic rate)."""
    RESULTS.setdefault("info", []).append({"name": name, "detail": detail})
    print(f"  [INFO] {name} — {detail}")


def check(name: str, cond: bool, detail="") -> bool:
    RESULTS.setdefault("checks", []).append({"name": name, "pass": bool(cond), "detail": detail})
    if not cond:
        FAILS.append(f"{name}: {detail}")
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def h(prompt: str) -> int:
    return int(sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest()[:12], 16)


def expected_simple(prompt: str) -> dict:
    x = h(prompt)
    return {"skill": SKILLS[x % len(SKILLS)], "complexity_score": float(x % 3),
            "input_tokens": 1000 + x % 500}


def expected_agent(prompt: str) -> dict:
    x = h(prompt)
    return {"tool_type": agent_decider.TOOL_TYPES[x % 6],
            "escalation": agent_decider.ESCALATIONS[x % 5],
            "needs_tool_p": ((x >> 8) % 100) / 100, "input_tokens": 1400 + x % 500}


# --- mock upstream ----------------------------------------------------------------------

class Mock:
    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = Counter()
        self.max_inflight = Counter()
        self.max_total = 0
        self.attempts = Counter()
        self.calls: list[dict] = []
        self.auth = set()

    def enter(self, kind):
        with self.lock:
            self.inflight[kind] += 1
            self.max_inflight[kind] = max(self.max_inflight[kind], self.inflight[kind])
            self.max_total = max(self.max_total, sum(self.inflight.values()))

    def leave(self, kind):
        with self.lock:
            self.inflight[kind] -= 1

    def reset(self):
        with self.lock:
            self.inflight.clear(); self.max_inflight.clear(); self.max_total = 0
            self.calls.clear()


MOCK = Mock()


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        MOCK.auth.add(self.headers.get("Authorization"))
        prompt = body["state"]["request"]
        kind = "simple" if "skill" in body["questions"] else "agent"
        mode = prompt.split(":", 1)[0]
        with MOCK.lock:
            MOCK.attempts[(kind, prompt)] += 1
            attempt = MOCK.attempts[(kind, prompt)]
        t0 = time.perf_counter()
        MOCK.enter(kind)
        try:
            if mode == "DROP1" and attempt == 1:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if mode == "HANG":
                time.sleep(HANG_S)
            elif mode not in ("FAST500",):
                time.sleep(OK_DELAY)
            if mode in ("E500", "FAST500"):
                return self._reply(500, {"detail": "mock failure"})
            if mode == "BAD":
                return self._reply(200, {"answers": {}, "usage": {"input_tokens": 1}})
            self._reply(200, self._answer(kind, prompt))
        except OSError:
            pass
        finally:
            MOCK.leave(kind)
            with MOCK.lock:
                MOCK.calls.append({"kind": kind, "prompt": prompt, "attempt": attempt,
                                   "t0": t0, "t1": time.perf_counter()})

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _answer(self, kind, prompt):
        if kind == "simple":
            e = expected_simple(prompt)
            return {"answers": {
                "skill": {"type": "choice", "choice": e["skill"], "confidence": 0.9,
                          "probabilities": {e["skill"]: 0.9, jev_router.NONE_CHOICE: 0.05}},
                "complexity": {"type": "score", "score": e["complexity_score"], "confidence": 0.8}},
                "usage": {"input_tokens": e["input_tokens"], "output_tokens": 0}}
        e = expected_agent(prompt)
        x = h(prompt)
        ans = {k: {"type": "noul", "noul": ((x >> (8 + 4 * i)) % 100) / 100}
               for i, k in enumerate(agent_decider.NOUL_FIELDS)}
        ans["needs_tool"]["noul"] = e["needs_tool_p"]
        ans["tool_type"] = {"type": "choice", "choice": e["tool_type"], "confidence": 0.7,
                            "probabilities": {e["tool_type"]: 0.7}}
        ans["escalation"] = {"type": "choice", "choice": e["escalation"], "confidence": 0.6,
                             "probabilities": {e["escalation"]: 0.6}}
        return {"answers": ans, "usage": {"input_tokens": e["input_tokens"], "output_tokens": 0}}


def start_server(handler_cls, server_cls=ThreadingHTTPServer):
    srv = server_cls(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass   # the mock's own client-went-away noise is not a sidecar finding


mock_srv = start_server(MockHandler, QuietServer)
MOCK_PORT = mock_srv.server_address[1]


def _mock_connection(self):
    if self._conn is None:
        self._conn = http.client.HTTPConnection("127.0.0.1", MOCK_PORT, timeout=self.timeout)
    return self._conn


jev_router.Router._connection = _mock_connection   # test process only; AgentRouter inherits


class RecordingServer(ThreadingHTTPServer):
    """jevd's server with handler exceptions counted (and still printed nowhere)."""
    errors: Counter

    def handle_error(self, request, client_address):
        self.errors[sys.exc_info()[0].__name__] += 1


def start_sidecar(module=jev_sidecar, timeout=UPSTREAM_TIMEOUT):
    router = jev_router.Router(timeout=timeout)
    if hasattr(module, "AgentRouter"):
        state = module.State(router, agent_decider.AgentRouter(timeout=timeout))
    else:
        state = module.State(router)
    srv = RecordingServer(("127.0.0.1", 0), module.make_handler(state))
    srv.errors = Counter()
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, state


SC, STATE = start_sidecar()
PORT = SC.server_address[1]
os.environ["JEV_SIDECAR_PORT"] = str(PORT)
import jev_shadow_hook as hook  # noqa: E402  (the unchanged Claude consumer)


# --- consumers --------------------------------------------------------------------------

def claude_call(path: str, prompt: str, timeout: float | None = None) -> dict:
    """Exactly the Claude hook's request code."""
    out: dict = {}
    old = hook.TIMEOUT_S
    if timeout is not None:
        hook.TIMEOUT_S = timeout
    t0 = time.perf_counter()
    hook.ask_sidecar(f"http://127.0.0.1:{PORT}{path}", prompt, out)
    hook.TIMEOUT_S = old
    out["elapsed"] = time.perf_counter() - t0
    out["prompt"] = prompt
    return out


def codex_call(path: str, prompt: str, timeout: float = 8.0, port: int | None = None,
               raw_body: bytes | None = None, headers: dict | None = None) -> dict:
    """An independent second consumer: http.client, HTTP/1.1 keep-alive, own payload."""
    t0 = time.perf_counter()
    out = {"prompt": prompt}
    conn = http.client.HTTPConnection("127.0.0.1", port or PORT, timeout=timeout)
    try:
        body = raw_body if raw_body is not None else json.dumps(
            {"prompt": prompt, "consumer": "codex", "session_id": "codex-sess"},
            ensure_ascii=False).encode("utf-8")
        hdr = {"Content-Type": "application/json", "X-Jev-Sidecar": "1",
               "User-Agent": "codex-shadow-test"}
        if headers is not None:
            hdr = headers
        conn.request("POST", path, body, hdr)
        r = conn.getresponse()
        raw = r.read()
        out["status"] = r.status
        out["data"] = json.loads(raw) if r.status == 200 else None
        if r.status != 200:
            out["error"] = f"HTTP {r.status}"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}"
    finally:
        conn.close()
    out["elapsed"] = time.perf_counter() - t0
    return out


def verify(path: str, res: dict) -> str | None:
    """None if the response matches its own request, else a reason."""
    d = res.get("data")
    if d is None:
        return f"no data ({res.get('error')})"
    p = res["prompt"].strip()
    if path == "/decide":
        e = expected_simple(p)
        if d.get("error"):
            return f"error {d['error']}"
        if (d["skill"], d["complexity_score"], d["input_tokens"]) != (
                e["skill"], e["complexity_score"], e["input_tokens"]):
            return "CROSS-TALK: answer does not match request"
        if d["prompt_sha256_8"] != sha256(p.encode("utf-8")).hexdigest()[:8]:
            return "CROSS-TALK: prompt hash mismatch"
        return None
    e = expected_agent(p)
    if not d.get("ok"):
        return f"not ok {d.get('error')}"
    if (d["decision"]["tool_type"], d["decision"]["escalation"], d["detail"]["needs_tool_p"],
            d["input_tokens"]) != (e["tool_type"], e["escalation"], e["needs_tool_p"], e["input_tokens"]):
        return "CROSS-TALK: answer does not match request"
    return None


def burst(jobs, workers=None):
    """jobs: list of (fn, args). Released together from a barrier."""
    barrier = threading.Barrier(min(len(jobs), workers or len(jobs)))

    def run(job):
        fn, args = job
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass   # more jobs than workers: later jobs start as workers free up
        return fn(*args)
    with ThreadPoolExecutor(max_workers=workers or len(jobs)) as ex:
        return list(ex.map(run, jobs))


def health(port=None):
    c = http.client.HTTPConnection("127.0.0.1", port or PORT, timeout=3)
    c.request("GET", "/health")
    return json.loads(c.getresponse().read())


# --- scenarios --------------------------------------------------------------------------

def s_contract():
    print("\n[1] /decide contract vs the pre-agent-layer sidecar (frozen/)")
    snapshot = ROOT / "frozen" / "jev_sidecar.pre_agent_layer.py"
    if not snapshot.is_file():
        print("  SKIP historical contract comparison: local frozen snapshot is not distributed")
        r = claude_call("/decide", "OK: hello")
        check("Claude hook client gets a valid /decide answer", verify("/decide", r) is None)
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "old_sidecar", snapshot)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    osrv, _ = start_sidecar(old)
    prompts = ["OK: fix the typo in README", "OK: сделай презентацию pptx по отчёту",
               "E500: upstream down", "OK: 🚀 emoji 中文 العربية"]
    diffs = []
    lat = {"latency_ms", "sidecar_latency_ms"}
    for p in prompts:
        new = codex_call("/decide", p)
        oldr = codex_call("/decide", p, port=osrv.server_address[1])
        nd = {k: v for k, v in (new["data"] or {}).items() if k not in lat}
        od = {k: v for k, v in (oldr["data"] or {}).items() if k not in lat}
        if nd != od or set(new["data"]) != set(oldr["data"]):
            diffs.append(p)
    check("/decide response identical to pre-agent sidecar (keys and values, latency excluded)",
          not diffs, f"{len(prompts)} prompts, diffs={diffs}")
    r = claude_call("/decide", "OK: hello")
    check("Claude hook client gets a valid /decide answer", verify("/decide", r) is None)
    osrv.shutdown()


def s_parallel(n=16):
    print(f"\n[2] {n} parallel /decide, {n} parallel /agent_decide, mixed 2x{n} across two consumers")
    out = {}
    for label, jobs in [
        ("decide", [(claude_call if i % 2 else codex_call, ("/decide", f"OK: d{i} запрос"))
                    for i in range(n)]),
        ("agent", [(claude_call if i % 2 else codex_call, ("/agent_decide", f"OK: a{i} запрос"))
                   for i in range(n)]),
        ("mixed", [(claude_call, ("/decide", f"OK: md{i}")) for i in range(n // 2)] +
                  [(codex_call, ("/agent_decide", f"OK: ma{i}")) for i in range(n // 2)] +
                  [(codex_call, ("/decide", f"OK: cd{i}")) for i in range(n // 2)] +
                  [(claude_call, ("/agent_decide", f"OK: ca{i}")) for i in range(n // 2)]),
    ]:
        MOCK.reset()
        t0 = time.perf_counter()
        res = burst(jobs)
        wall = time.perf_counter() - t0
        paths = [j[1][0] for j in jobs]
        bad = [(paths[i], r["prompt"], verify(paths[i], r)) for i, r in enumerate(res)
               if verify(paths[i], r)]
        if bad:
            print("    mismatches:", bad[:5])
        out[label] = {"n": len(jobs), "wall_s": round(wall, 2), "bad": bad,
                      "max_inflight": dict(MOCK.max_inflight), "max_total": MOCK.max_total,
                      "p_max_s": round(max(r["elapsed"] for r in res), 2)}
        check(f"{label}: every response matches its own request (no cross-talk)", not bad,
              f"{len(jobs)} requests, wall {wall:.2f}s, slowest {out[label]['p_max_s']}s")
        check(f"{label}: at most one upstream call in flight per endpoint",
              all(v <= 1 for v in MOCK.max_inflight.values()), str(dict(MOCK.max_inflight)))
    check("mixed: /decide and /agent_decide overlap upstream (independent locks)",
          out["mixed"]["max_total"] == 2, f"max concurrent upstream = {out['mixed']['max_total']}")
    RESULTS["parallel"] = out


def s_faults():
    print("\n[3] Faults: stale connection, 500, malformed body, hang; isolation between endpoints")
    r = codex_call("/decide", "DROP1: stale keep-alive")
    check("stale/dropped upstream connection: one retry recovers /decide",
          verify("/decide", r) is None and MOCK.attempts[("simple", "DROP1: stale keep-alive")] == 2)
    r = codex_call("/agent_decide", "DROP1: stale keep-alive agent")
    check("stale/dropped upstream connection: one retry recovers /agent_decide",
          verify("/agent_decide", r) is None)
    r = claude_call("/decide", "E500: x")
    check("upstream 500 -> /decide 200 with action=defer and error (fail-open)",
          r.get("data", {}).get("action") == "defer" and r["data"].get("error", "").startswith("RuntimeError"))
    r = claude_call("/agent_decide", "E500: x")
    check("upstream 500 -> /agent_decide 200 with ok=false (fail-open)",
          r.get("data") is not None and r["data"]["ok"] is False)
    after = claude_call("/decide", "OK: after 500")
    check("connection usable after a 500", verify("/decide", after) is None)

    e0 = sum(SC.errors.values())
    r = codex_call("/agent_decide", "BAD: malformed agent")
    check("malformed upstream 200 -> /agent_decide ok=false with parse_errors",
          r.get("data") is not None and r["data"]["ok"] is False and r["data"]["parse_errors"])
    r = codex_call("/decide", "BAD: malformed simple")
    bad_simple = {"status": r.get("status"), "error": r.get("error"),
                  "handler_exceptions": sum(SC.errors.values()) - e0}
    RESULTS["malformed_simple"] = bad_simple
    check("FINDING malformed upstream 200 -> /decide drops the socket, no JSON (frozen _interpret"
          " raises outside its try)", r.get("data") is None, str(bad_simple))
    after = claude_call("/decide", "OK: after malformed")
    check("/decide still serves after the malformed-body exception (lock released)",
          verify("/decide", after) is None)

    # Isolation: a hanging upstream on one endpoint must not slow the other.
    for hang_path, ok_path in (("/decide", "/agent_decide"), ("/agent_decide", "/decide")):
        MOCK.reset()
        jobs = [(codex_call, (hang_path, f"HANG: {hang_path}", 8.0))] + \
               [(claude_call, (ok_path, f"OK: iso {ok_path} {i}")) for i in range(6)]
        res = burst(jobs)
        ok = res[1:]
        slow = max(x["elapsed"] for x in ok)
        check(f"hang on {hang_path} does not delay {ok_path}",
              all(verify(ok_path, x) is None for x in ok) and slow < 1.0,
              f"slowest {ok_path} {slow:.2f}s while {hang_path} hung; hang result "
              f"{(res[0].get('data') or {}).get('error') or res[0].get('error')}, "
              f"held {res[0]['elapsed']:.2f}s")
        RESULTS[f"isolation_{hang_path}"] = {"slowest_other_s": round(slow, 2),
                                             "hang_elapsed_s": round(res[0]["elapsed"], 2)}


def s_queue():
    print("\n[4] Head-of-line: one hanging /decide, then 5 more, client timeout 1.5 s")
    MOCK.reset()
    n_up0 = len(MOCK.calls)
    hang = threading.Thread(target=lambda: RESULTS.__setitem__(
        "q_hang", claude_call("/decide", "HANG: head of line", 1.5)))
    hang.start()
    time.sleep(0.1)
    res = burst([(claude_call, ("/decide", f"OK: queued {i}", 1.5)) for i in range(5)])
    hang.join()
    time.sleep(HANG_S * 2 + 1)  # let the sidecar drain its queue
    timeouts = sum(1 for r in res if r.get("data") is None)
    queued_upstream = [c for c in MOCK.calls if c["prompt"].startswith("OK: queued")]
    RESULTS["queue"] = {"client_timeouts": timeouts, "upstream_calls_after_abandon":
                        len(queued_upstream), "handler_exceptions": dict(SC.errors),
                        "hang_attempts": MOCK.attempts[("simple", "HANG: head of line")]}
    check("FINDING queued /decide callers time out behind a hung upstream (lock held up to "
          "2 x timeout by the retry)", timeouts == 5, str(RESULTS["queue"]))
    check("FINDING abandoned queued requests are still sent upstream (billed, answer discarded)",
          len(queued_upstream) == 5, f"{len(queued_upstream)} of 5 still reached upstream")
    h2 = claude_call("/decide", "OK: after queue")
    check("sidecar recovers after the queue drains", verify("/decide", h2) is None)


def s_counters(n=120):
    print(f"\n[5] Counter integrity under fast failures ({n} /decide + {n} /agent_decide)")
    before = health()
    jobs = [(codex_call, ("/decide", f"FAST500: c{i}")) for i in range(n)] + \
           [(codex_call, ("/agent_decide", f"FAST500: a{i}")) for i in range(n)]
    res = burst(jobs, workers=16)
    after = health()
    served_d = sum(1 for r in res[:n] if r.get("status") == 200)
    served_a = sum(1 for r in res[n:] if r.get("status") == 200)
    dd = after["decisions"] - before["decisions"]
    da = after["agent_decisions"] - before["agent_decisions"]
    RESULTS["counters"] = {"decide": [served_d, dd], "agent": [served_a, da], "sent": n}
    check("/health counters equal requests actually served (unlocked += lost no updates here)",
          dd == served_d == n and da == served_a == n, f"decide {served_d} served/{dd} counted, "
          f"agent {served_a} served/{da} counted, of {n} each")


def s_backlog():
    print("\n[5b] Listen backlog: simultaneous connects to /health (free, no upstream)")
    res = {}
    for n in (8, 16, 32, 64):
        barrier = threading.Barrier(n)
        errs: list = []

        def go():
            barrier.wait()
            try:
                c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
                c.request("GET", "/health")
                c.getresponse().read()
                c.close()
            except Exception as exc:  # noqa: BLE001
                errs.append(type(exc).__name__)
        for _ in range(3):
            ts = [threading.Thread(target=go) for _ in range(n)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        res[n] = {"attempts": 3 * n, "refused_or_reset": len(errs)}
    RESULTS["backlog"] = res
    check("up to 16 simultaneous connects (8 prompts x 2 endpoints) all accepted",
          all(res[n]["refused_or_reset"] == 0 for n in (8, 16)), str(res))
    info("FINDING default backlog (request_queue_size=5): connects fail in bursts of 32-64 "
         "on Windows", res)


def s_unicode():
    print("\n[6] Unicode, size limits and guards")
    prompts = ["OK: Привет, сделай ревью кода 👀", "OK: И\u0098 byte-edge И и Ё ё",
               "OK: 中文 日本語 한국어", "OK: עברית العربية RTL", "OK: e\u0301 combining",
               "OK: \U0001F9D1\u200D\U0001F4BB ZWJ", "OK: tab\tnewline\nnul\x00end",
               "OK: " + "Ж" * 10000]
    bad = []
    for p in prompts:
        for path in ("/decide", "/agent_decide"):
            for fn in (claude_call, codex_call):
                MOCK.reset()
                r = fn(path, p)
                got = [c["prompt"] for c in MOCK.calls]
                if verify(path, r) or got != [p.strip()]:
                    bad.append((fn.__name__, path, p[:20], verify(path, r)))
    check("Unicode prompts round-trip exactly to upstream and back, both consumers",
          not bad, f"{len(prompts)} prompts x 2 endpoints x 2 consumers; bad={bad}")

    big = "OK: " + "Ж" * 33000                 # ~66 KB UTF-8 > MAX_BODY
    r = codex_call("/decide", big)
    check("prompt over 64 KiB UTF-8 -> 400, not a crash (limit is bytes: ~32k Cyrillic chars)",
          r.get("status") == 400)
    r = codex_call("/decide", "", raw_body=b'{"prompt": "OK: bad \xff\xfe utf8"}')
    check("invalid UTF-8 body -> 400", r.get("status") == 400)

    e0 = Counter(SC.errors)
    lone = b'{"prompt": "OK: lone surrogate \\ud800 here"}'
    r1 = codex_call("/decide", "", raw_body=lone)
    r2 = codex_call("/agent_decide", "", raw_body=lone)
    RESULTS["lone_surrogate"] = {"decide": r1.get("status") or r1.get("error"),
                                 "agent": r2.get("status") or r2.get("error"),
                                 "new_handler_exceptions": dict(SC.errors - e0)}
    check("FINDING lone-surrogate JSON escape -> /decide drops the socket (sha256 encode after "
          "the paid upstream call); /agent_decide answers", r1.get("data") is None and
          r2.get("status") == 200, str(RESULTS["lone_surrogate"]))

    g = [codex_call("/decide", "OK: g", headers={"Content-Type": "application/json"}),
         codex_call("/decide", "OK: g", headers={"X-Jev-Sidecar": "1", "Origin": "http://evil"}),
         codex_call("/agent_decide", "OK: g", headers={"Content-Type": "application/json"}),
         codex_call("/agent_decide", "OK: g", headers={"X-Jev-Sidecar": "1", "Origin": "null"})]
    check("guards: no header / any Origin -> 403 on both endpoints",
          [x.get("status") for x in g] == [403] * 4)
    r = codex_call("/decide", "", raw_body=b'{"prompt": "   "}')
    check("blank prompt -> 400", r.get("status") == 400)


def s_isolation_state():
    print("\n[7] /agent_decide does not mutate shared or global state")
    q_before = copy.deepcopy(agent_decider.QUESTIONS)
    sq_before = copy.deepcopy(STATE.router.questions)
    env_before = {k: v for k, v in os.environ.items() if "jev" in k.lower() or "typesafe" in k.lower()}
    simple_conn = STATE.router._conn
    h0 = health()
    burst([(codex_call, ("/agent_decide", f"OK: st{i}")) for i in range(12)])
    h1 = health()
    check("SIMPLE questions, agent QUESTIONS and env unchanged after agent load",
          agent_decider.QUESTIONS == q_before and STATE.router.questions == sq_before and
          env_before == {k: v for k, v in os.environ.items()
                         if "jev" in k.lower() or "typesafe" in k.lower()})
    check("SIMPLE connection object and counters untouched by /agent_decide",
          STATE.router._conn is simple_conn and h1["decisions"] == h0["decisions"] and
          h1["failures"] == h0["failures"] and h1["spend_usd"] == h0["spend_usd"])
    check("routers never share a connection object",
          STATE.router._conn is None or STATE.router._conn is not STATE.agent_router._conn)
    check("upstream never saw the real key (dummy only)",
          MOCK.auth == {"Bearer dummy-offline-test-key"}, str(len(MOCK.auth)))


def s_hook_processes(n=12):
    print(f"\n[8] {n} Claude hook processes + {n} 'Codex' hook processes into ONE telemetry file")
    tmp = Path(tempfile.mkdtemp(prefix="jev_mc_"))
    env = dict(os.environ, JEV_SIDECAR_PORT=str(PORT), JEV_SHADOW_LOG=str(tmp / "shadow.jsonl"),
               JEV_AGENT_SHADOW_LOG=str(tmp / "agent.jsonl"))
    env.pop("JEV_SHADOW_COLLECT", None)
    procs = []
    for i in range(2 * n):
        who = "claude" if i < n else "codex"
        payload = {"session_id": f"{who}-{i % 3}", "cwd": f"C:/{who}", "prompt":
                   f"OK: hook {who} {i} проверка 🚀", "hook_event_name": "UserPromptSubmit"}
        if who == "codex":
            payload["turn_id"] = f"t{i}"
        p = subprocess.Popen([PY, str(ROOT / "hooks" / "jev_shadow_hook.py")], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append((p, json.dumps(payload, ensure_ascii=False).encode("utf-8")))
    outs = [p.communicate(b, timeout=30) + (p.returncode,) for p, b in procs]
    silent = all(o == b"" and e == b"" and rc == 0 for o, e, rc in outs)
    lines = {f: (tmp / f).read_text(encoding="utf-8").splitlines() for f in ("shadow.jsonl", "agent.jsonl")}
    parsed = {f: [json.loads(x) for x in ls] for f, ls in lines.items()}
    ok = {f: sum(1 for r in rs if r.get("ok")) for f, rs in parsed.items()}
    RESULTS["hook_processes"] = {"lines": {f: len(v) for f, v in lines.items()}, "ok": ok,
                                 "consumer_field_present": any("consumer" in r for r in parsed["shadow.jsonl"])}
    check("every hook process exits 0 with empty stdout and stderr", silent)
    check("one intact JSON line per hook per file under concurrent appends",
          all(len(v) == 2 * n for v in lines.values()), str(RESULTS["hook_processes"]))
    check("all hook lines ok=true (sidecar served both consumers)",
          all(v == 2 * n for v in ok.values()), str(ok))
    check("NOTE records carry no consumer field; Claude and Codex lines are told apart only by "
          "session_id/cwd", not RESULTS["hook_processes"]["consumer_field_present"])


APPEND_WORKER = r"""
import json, sys
from pathlib import Path
path, who, n, size = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
for i in range(n):
    rec = {"who": who, "i": i, "pad": "Ж" * size}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
"""


def s_append_stress(procs=16, per=200):
    print(f"\n[9] Raw append race: {procs} processes x {per} lines, hook's write pattern")
    tmp = Path(tempfile.mkdtemp(prefix="jev_app_"))
    w = tmp / "w.py"
    w.write_text(APPEND_WORKER, encoding="utf-8")
    res = {}
    for size in (600, 5000):   # ~1.2 KB (typical line) and ~10 KB (> 8 KiB io buffer)
        f = tmp / f"a{size}.jsonl"
        ps = [subprocess.Popen([PY, str(w), str(f), f"p{i}", str(per), str(size)]) for i in range(procs)]
        for p in ps:
            p.wait(timeout=120)
        raw = f.read_bytes().decode("utf-8", errors="replace").split("\n")[:-1]
        good = 0
        for x in raw:
            try:
                json.loads(x); good += 1
            except json.JSONDecodeError:
                pass
        res[size] = {"expected": procs * per, "lines": len(raw), "valid": good,
                     "lost_or_torn": procs * per - good}
        info(f"FINDING ~{size * 2 // 1000 or 1} KB lines, {procs} writers on one file: "
             f"open('a') append is not atomic across processes on Windows", res[size])
    RESULTS["append_stress"] = res


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for s in (s_contract, s_parallel, s_faults, s_queue, s_counters, s_backlog, s_unicode,
              s_isolation_state, s_hook_processes, s_append_stress):
        try:
            s()
        except Exception:  # noqa: BLE001
            FAILS.append(f"{s.__name__} crashed")
            traceback.print_exc()
    RESULTS["handler_exceptions_total"] = dict(SC.errors)
    RESULTS["elapsed_s"] = round(time.time() - t0, 1)
    RESULTS["health"] = health()
    (OUT / "multi_consumer_stress.json").write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False),
                                                    encoding="utf-8")
    findings = RESULTS.get("info", []) + [c for c in RESULTS["checks"] if c["name"].startswith(("FINDING", "NOTE"))]
    hard = [c for c in RESULTS["checks"] if not c["pass"]]
    print(f"\n{len(RESULTS['checks'])} checks, {len(hard)} failed, {len(findings)} findings/notes "
          f"asserted, {RESULTS['elapsed_s']}s. Handler exceptions: {dict(SC.errors)}")
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
