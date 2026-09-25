"""Mercury 2.5 smoke test — does the API work, and what does it cost in time and money?

    python tests/mercury_smoke_test.py

Checks, in order:
  0. GET /v1/models              — the real model IDs, context limits and live pricing
  A. simple completion
  B. reasoning_effort            — all four levels, timed
  C. structured JSON output      — native response_format json_schema, strict
  D. streaming                   — time to first token, tokens/sec, usage chunk
  E. tool-call generation
  F. usage accounting            — prompt/completion/cached token split and cost
  G. connection reuse            — cold (new TLS) vs warm (kept-alive), the number that
                                   decides whether Mercury can sit in a prompt path
  H. auth failure                — must not echo the key

Writes tests/out/mercury_smoke_results.json. The API key is never printed.
"""

from __future__ import annotations

import json
import socket
import ssl
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import mercury_client as mc  # noqa: E402
from mercury_client import MercuryClient, MercuryError, cost_usd, text_of  # noqa: E402

OUT = Path(__file__).parent / "out" / "mercury_smoke_results.json"
results: dict = {}
passed, failed = 0, 0


def check(name: str, ok: bool, detail) -> None:
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    results[name] = {"ok": ok, "detail": detail}


def handshake_breakdown(host: str) -> dict:
    """DNS, TCP, proxy CONNECT and TLS cost separately.

    Measured along the route the client actually uses. From this machine that route
    goes through a local proxy (see mercury_client.proxy_for), so the DNS and TCP legs
    are to 127.0.0.1 and the wide-area cost shows up in CONNECT and TLS.
    """
    proxy = mc.proxy_for(host)
    target = proxy or (host, 443)
    t0 = time.perf_counter()
    addr = socket.getaddrinfo(target[0], target[1], socket.AF_INET, socket.SOCK_STREAM)[0][4]
    t1 = time.perf_counter()
    sock = socket.create_connection(addr, timeout=15)
    t2 = time.perf_counter()
    if proxy:
        sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    t3 = time.perf_counter()
    tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    t4 = time.perf_counter()
    tls.close()
    return {"route": "proxy" if proxy else "direct",
            "dns_ms": round((t1 - t0) * 1000, 1),
            "tcp_ms": round((t2 - t1) * 1000, 1),
            "proxy_connect_ms": round((t3 - t2) * 1000, 1),
            "tls_ms": round((t4 - t3) * 1000, 1),
            "total_ms": round((t4 - t0) * 1000, 1)}


ROUTE_SCHEMA = {
    "name": "route_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "skill": {"type": "string"},
            "complexity": {"type": "string", "enum": ["trivial", "normal", "complex"]},
            "confidence": {"type": "number"},
        },
        "required": ["skill", "complexity", "confidence"],
        "additionalProperties": False,
    },
}

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather in a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["c", "f"]}},
            "required": ["city"],
        },
    },
}


def main() -> int:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

    def bill(resp: dict) -> None:
        u = resp.get("usage", {})
        totals["prompt_tokens"] += u.get("prompt_tokens", 0)
        totals["completion_tokens"] += u.get("completion_tokens", 0)
        totals["cost_usd"] += resp.get("cost_usd", cost_usd(u))

    # --- 0. model discovery --------------------------------------------------
    try:
        models, ms = mc.list_models()
        ids = [m["id"] for m in models]
        info = {m["id"]: {"context_length": m.get("context_length"),
                          "max_output_length": m.get("max_output_length"),
                          "pricing": m.get("pricing"),
                          "features": m.get("supported_features")} for m in models}
        check("0_models", mc.DEFAULT_MODEL in ids,
              {"models": ids, "latency_ms": round(ms, 1), "target": mc.DEFAULT_MODEL, "info": info})
    except Exception as exc:  # noqa: BLE001
        check("0_models", False, str(exc)[:300])
        return 1

    # --- G1. cold handshake, measured before anything is warm ----------------
    hs = handshake_breakdown(mc.HOST)
    print(f"      handshake ({hs['route']}): DNS {hs['dns_ms']}ms  TCP {hs['tcp_ms']}ms  "
          f"CONNECT {hs['proxy_connect_ms']}ms  TLS {hs['tls_ms']}ms  total {hs['total_ms']}ms")

    cold_started = time.perf_counter()
    client = MercuryClient()
    cold = client.chat("Reply with the single word: ready", max_tokens=8, reasoning_effort="instant")
    cold_total_ms = round((time.perf_counter() - cold_started) * 1000, 1)
    bill(cold)
    check("G1_cold_request", bool(text_of(cold)),
          {"cold_total_ms": cold_total_ms, "handshake": hs, "reply": text_of(cold).strip()[:40]})

    # --- A. simple completion (warm) -----------------------------------------
    a = client.chat("What is 17 * 23? Answer with the number only.", max_tokens=16,
                    reasoning_effort="instant")
    bill(a)
    check("A_simple_completion", "391" in text_of(a),
          {"reply": text_of(a).strip()[:60], "latency_ms": a["latency_ms"],
           "usage": a["usage"], "cost_usd": a["cost_usd"]})

    # --- B. reasoning, all four efforts --------------------------------------
    riddle = ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
              "How much does the ball cost? Answer with just the amount.")
    effort_rows = {}
    for effort in mc.REASONING_EFFORTS:
        r = client.chat(riddle, max_tokens=2000, reasoning_effort=effort)
        bill(r)
        u = r["usage"]
        effort_rows[effort] = {
            "latency_ms": r["latency_ms"],
            "answer": text_of(r).strip()[-40:],
            "completion_tokens": u["completion_tokens"],
            "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            "correct": "0.05" in text_of(r) or "5 cents" in text_of(r).lower(),
            "finish_reason": r["choices"][0].get("finish_reason"),
            "cost_usd": r["cost_usd"],
        }
        print(f"      reasoning_effort={effort:<8} {r['latency_ms']:7.0f}ms  "
              f"{u['completion_tokens']:4d} out tok  correct={effort_rows[effort]['correct']}")
    check("B_reasoning", any(v["correct"] for v in effort_rows.values()), effort_rows)

    # --- C. structured JSON output -------------------------------------------
    c = client.chat(
        [{"role": "system", "content": "You classify developer requests. Return only JSON."},
         {"role": "user", "content": "Skills available: dataviz, pdf, code-review, none.\n"
                                     "Request: 'Turn this quarterly revenue table into a chart for the board deck.'"}],
        max_tokens=500, reasoning_effort="instant",
        response_format={"type": "json_schema", "json_schema": ROUTE_SCHEMA})
    bill(c)
    try:
        parsed = json.loads(text_of(c))
        ok = set(parsed) == {"skill", "complexity", "confidence"} and \
            parsed["complexity"] in ("trivial", "normal", "complex")
    except json.JSONDecodeError:
        parsed, ok = text_of(c)[:120], False
    check("C_structured_output", ok,
          {"parsed": parsed, "latency_ms": c["latency_ms"], "cost_usd": c["cost_usd"]})

    # --- D. streaming: TTFT and throughput -----------------------------------
    ttft_ms, chunks, stream_usage, last_ms = None, 0, None, 0.0
    text_parts = []
    for elapsed, chunk in client.stream_chat(
            "List the eight planets of the solar system, one per line, no commentary.",
            max_tokens=200, reasoning_effort="instant"):
        if chunk.get("usage"):
            stream_usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                if ttft_ms is None:
                    ttft_ms = round(elapsed, 1)
                text_parts.append(piece)
                chunks += 1
                last_ms = elapsed
    out_tok = (stream_usage or {}).get("completion_tokens", 0)
    tps = round(out_tok / (last_ms / 1000), 1) if last_ms else 0.0
    # Only meaningful with >1 content chunk; Mercury returns short answers whole,
    # so first-token time and last-token time coincide and the rate is undefined.
    gen_ms = last_ms - (ttft_ms or 0)
    tps_after_first = round(out_tok / (gen_ms / 1000), 1) if chunks > 1 and gen_ms > 1 else None
    if stream_usage:
        totals["prompt_tokens"] += stream_usage.get("prompt_tokens", 0)
        totals["completion_tokens"] += stream_usage.get("completion_tokens", 0)
        totals["cost_usd"] += cost_usd(stream_usage)
    check("D_streaming", chunks > 0 and "Neptune" in "".join(text_parts),
          {"ttft_ms": ttft_ms, "total_ms": round(last_ms, 1), "chunks": chunks,
           "output_tokens": out_tok, "tokens_per_sec_wall": tps,
           "tokens_per_sec_after_first": tps_after_first, "usage": stream_usage})

    # --- D2. diffusing=true: does the diffusion model emit intermediate drafts? ---
    d2_ttft, d2_chunks, d2_last, d2_usage = None, 0, 0.0, None
    for elapsed, chunk in client.stream_chat(
            "List the eight planets of the solar system, one per line, no commentary.",
            max_tokens=200, reasoning_effort="instant", diffusing=True):
        if chunk.get("usage"):
            d2_usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            if (ch.get("delta") or {}).get("content"):
                d2_ttft = d2_ttft if d2_ttft is not None else round(elapsed, 1)
                d2_chunks += 1
                d2_last = elapsed
    if d2_usage:
        totals["prompt_tokens"] += d2_usage.get("prompt_tokens", 0)
        totals["completion_tokens"] += d2_usage.get("completion_tokens", 0)
        totals["cost_usd"] += cost_usd(d2_usage)
    check("D2_streaming_diffusing", d2_chunks > 0,
          {"ttft_ms": d2_ttft, "total_ms": round(d2_last, 1), "chunks": d2_chunks,
           "chunks_vs_default": f"{d2_chunks} vs {chunks}", "usage": d2_usage})

    # --- E. tool calling ------------------------------------------------------
    e = client.chat("What's the weather in Tbilisi right now? Use the tool.",
                    tools=[WEATHER_TOOL], tool_choice="auto", max_tokens=500,
                    reasoning_effort="instant")
    bill(e)
    tool_calls = (e["choices"][0].get("message") or {}).get("tool_calls") or []
    args = {}
    if tool_calls:
        try:
            args = json.loads(tool_calls[0]["function"]["arguments"])
        except json.JSONDecodeError:
            args = {"_raw": tool_calls[0]["function"]["arguments"][:80]}
    check("E_tool_calling", bool(tool_calls) and "tbilisi" in json.dumps(args).lower(),
          {"tool": tool_calls[0]["function"]["name"] if tool_calls else None,
           "arguments": args, "finish_reason": e["choices"][0].get("finish_reason"),
           "latency_ms": e["latency_ms"]})

    # --- F. usage accounting --------------------------------------------------
    u = a["usage"]
    fields_ok = all(k in u for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
    sums_ok = u["prompt_tokens"] + u["completion_tokens"] == u["total_tokens"]
    check("F_usage_accounting", fields_ok and sums_ok,
          {"sample_usage": u, "sums_correctly": sums_ok,
           "cached_tokens_reported": "prompt_tokens_details" in u})

    # --- G2. warm vs cold, five each -----------------------------------------
    warm = []
    for _ in range(5):
        r = client.chat("ping", max_tokens=4, reasoning_effort="instant")
        bill(r)
        warm.append(r["latency_ms"])
    cold_runs = []
    for _ in range(3):
        c2 = MercuryClient()
        t = time.perf_counter()
        r = c2.chat("ping", max_tokens=4, reasoning_effort="instant")
        cold_runs.append(round((time.perf_counter() - t) * 1000, 1))
        bill(r)
        c2.close()
    warm_s, cold_s = sorted(warm), sorted(cold_runs)
    check("G2_connection_reuse", min(warm) < min(cold_runs),
          {"warm_ms": warm_s, "warm_median": warm_s[len(warm_s) // 2],
           "cold_ms": cold_s, "cold_median": cold_s[len(cold_s) // 2],
           "handshake_saved_ms": round(cold_s[len(cold_s) // 2] - warm_s[len(warm_s) // 2], 1)})

    # --- H. auth failure must not leak the key --------------------------------
    real = mc.get_api_key()
    bad = MercuryClient()
    bad._headers["Authorization"] = "Bearer sk_invalid_key_for_testing"
    try:
        bad.chat("hi", max_tokens=4)
        check("H_auth_failure", False, "invalid key was accepted")
    except MercuryError as exc:
        msg = str(exc)
        check("H_auth_failure", real not in msg,
              {"raised": type(exc).__name__, "key_in_message": real in msg,
               "message": msg[:120]})
    finally:
        bad.close()

    client.close()

    totals["cost_usd"] = round(totals["cost_usd"], 8)
    print("\n" + "=" * 74)
    print(f"{passed} passed, {failed} failed")
    print(f"Tokens: {totals['prompt_tokens']} in / {totals['completion_tokens']} out")
    print(f"Cost of this run: ${totals['cost_usd']:.6f}")
    results["_totals"] = totals
    results["_summary"] = {"passed": passed, "failed": failed}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Written to {OUT}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
