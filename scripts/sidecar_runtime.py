"""Project-scoped, on-demand startup of the loopback Jev sidecar (stdlib only)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "telemetry" / "pilot"
PORT = 8787


def health() -> dict | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{PORT}/health", timeout=0.5) as response:
            value = json.load(response)
        if value.get("ok") is True and "agent_decisions" in value and "pid" in value:
            return value
    except Exception:
        pass
    return None


def ensure_running() -> bool:
    if (RUNTIME / "DISABLED").exists():
        return False
    if health():
        return True
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # Different clients can submit their first prompt at the same time.
    with (RUNTIME / "startup.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0, 2)
            if not lock.tell():
                lock.write(b"0")
                lock.flush()
            deadline = time.monotonic() + 5
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        return bool(health())
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if (RUNTIME / "DISABLED").exists():
                return False
            if health():
                return True
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            with (RUNTIME / "sidecar.stdout.log").open("ab") as out, (RUNTIME / "sidecar.stderr.log").open("ab") as err:
                subprocess.Popen(
                    [sys.executable, "-u", str(ROOT / "src" / "jev_sidecar.py"), "--port", str(PORT)],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    creationflags=flags, start_new_session=os.name != "nt",
                )
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if health():
                    return True
                time.sleep(0.1)
            return False
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    ok = ensure_running()
    print(json.dumps({"ready": ok, "health": health()}))
    raise SystemExit(0 if ok else 1)
