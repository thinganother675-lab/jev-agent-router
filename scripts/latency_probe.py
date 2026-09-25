"""Measures cold-connection vs warm-connection Jev latency.

The difference decides the integration shape: a per-prompt hook pays the cold
cost every time, a long-lived sidecar pays it once.
    python scripts/latency_probe.py
"""
import http.client, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import jev_client as jev

BODY = json.dumps({"state": "ping", "model": "jev-latest",
                   "questions": {"q": {"type": "noul", "instructions": "Is this a greeting?"}}})
HDR = {"Authorization": f"Bearer {jev.get_api_key()}", "Content-Type": "application/json"}

cold = []
for _ in range(3):
    t = time.perf_counter()
    c = http.client.HTTPSConnection("api.typesafe.ai", timeout=30)
    c.request("POST", "/v1/systemone", BODY, HDR); c.getresponse().read(); c.close()
    cold.append((time.perf_counter() - t) * 1000)

c = http.client.HTTPSConnection("api.typesafe.ai", timeout=30); c.connect()
warm = []
for _ in range(5):
    t = time.perf_counter()
    c.request("POST", "/v1/systemone", BODY, HDR); c.getresponse().read()
    warm.append((time.perf_counter() - t) * 1000)
c.close()

print(f"cold (new TLS each call): min={min(cold):.0f} med={sorted(cold)[1]:.0f} max={max(cold):.0f} ms")
print(f"warm (reused connection): min={min(warm):.0f} med={sorted(warm)[2]:.0f} max={max(warm):.0f} ms")
print(f"TLS/TCP overhead        : ~{sorted(cold)[1] - sorted(warm)[2]:.0f} ms per cold call")
