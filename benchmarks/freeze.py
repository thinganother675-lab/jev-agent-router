"""Freeze markers: make it impossible to quietly improve a variant after seeing test results.

Two freezes live here, taken at different moments and for different reasons:

* ``simple_v1`` — taken **before any router-benchmark work**, 2026-09-23. It snapshots the
  SIMPLE router exactly as it ran in shadow mode and on the blind 50: the source, the skill
  catalog, the thresholds, the harness, and the canonical JSON of the questions it sends.
  SIMPLE may never be changed during this stage; the runner refuses to run it if it has been.

* ``final_v1`` — taken **after dev tuning and before the first final-test run**. It hashes
  every variant's full configuration (questions, descriptions, thresholds, excerpts). Once a
  test result exists, a test run whose configuration hash differs is refused — a changed
  algorithm on an already-seen test set is not a test.

    python benchmarks/freeze.py snapshot-simple     # once, before anything else
    python benchmarks/freeze.py verify-simple
    python benchmarks/freeze.py status
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FROZEN = ROOT / "frozen"
SIMPLE_DIR = FROZEN / "simple_v1"
SIMPLE_MANIFEST = SIMPLE_DIR / "MANIFEST.json"
FINAL_MANIFEST = FROZEN / "FINAL_FREEZE.json"

# What SIMPLE is made of. The harness files are included so the *measurement* is frozen
# along with the thing measured.
SIMPLE_FILES = [
    "src/jev_router.py",
    "src/jev_client.py",
    "src/net.py",
    "src/skills_catalog.json",
    "benchmarks/scoring.py",
    "benchmarks/run_comparison.py",
    "benchmarks/run_benchmark.py",
]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def simple_spec() -> dict:
    """The exact request SIMPLE sends, minus the prompt: model, questions, thresholds."""
    sys.path.insert(0, str(ROOT / "src"))
    import jev_router  # noqa: WPS433
    catalog = jev_router.load_catalog()
    return {
        "model": jev_router.DEFAULT_MODEL,
        "questions": jev_router.build_questions(catalog),
        "thresholds": {"HIGH_CONFIDENCE": jev_router.HIGH_CONFIDENCE,
                       "LOW_CONFIDENCE": jev_router.LOW_CONFIDENCE},
        "none_choice": jev_router.NONE_CHOICE,
        "complexity_levels": jev_router.COMPLEXITY_LEVELS,
        "state_shape": {"request": "<prompt>"},
        "catalog_size": len(catalog),
    }


def snapshot_simple() -> dict:
    if SIMPLE_MANIFEST.exists():
        raise SystemExit(f"{SIMPLE_MANIFEST} already exists; a freeze is taken once. "
                         "Delete it by hand only if you mean to start the stage over.")
    SIMPLE_DIR.mkdir(parents=True, exist_ok=True)
    files = {}
    for rel in SIMPLE_FILES:
        src = ROOT / rel
        dst = SIMPLE_DIR / rel.replace("/", "__")
        shutil.copy2(src, dst)
        files[rel] = sha256_file(src)
    spec = simple_spec()
    (SIMPLE_DIR / "SIMPLE_SPEC.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "name": "simple_v1",
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": "SIMPLE router as it ran in shadow mode and on blind_cases_50, frozen "
                   "before the router-architecture benchmark. Must not change during it.",
        "files": files,
        "spec_sha256": sha256_bytes(canonical(spec).encode("utf-8")),
    }
    SIMPLE_MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def verify_simple() -> dict:
    """Raise if SIMPLE or its harness changed since the freeze. Returns the manifest."""
    if not SIMPLE_MANIFEST.exists():
        raise SystemExit("SIMPLE has not been frozen: run `python benchmarks/freeze.py snapshot-simple`")
    m = json.loads(SIMPLE_MANIFEST.read_text(encoding="utf-8"))
    drift = [rel for rel, h in m["files"].items() if sha256_file(ROOT / rel) != h]
    spec_h = sha256_bytes(canonical(simple_spec()).encode("utf-8"))
    if spec_h != m["spec_sha256"]:
        drift.append("<built questions / thresholds>")
    if drift:
        raise SystemExit("SIMPLE changed since the freeze — refusing to run: " + ", ".join(drift))
    return m


# --- final freeze (all variants, before the first test run) ------------------------------

def config_hash(config: dict) -> str:
    return sha256_bytes(canonical(config).encode("utf-8"))[:16]


def take_final_freeze(variant_configs: dict[str, dict], extra_files: list[str]) -> dict:
    if FINAL_MANIFEST.exists():
        raise SystemExit(f"{FINAL_MANIFEST} already exists — the final freeze is taken once.")
    manifest = {
        "name": "final_v1",
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": "Every variant's configuration at the moment dev tuning stopped. "
                   "Final-test runs must match these hashes.",
        "variants": {k: config_hash(v) for k, v in variant_configs.items()},
        "files": {rel: sha256_file(ROOT / rel) for rel in extra_files},
        "simple_v1_spec_sha256": verify_simple()["spec_sha256"],
    }
    FINAL_MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (FROZEN / "FINAL_FREEZE_CONFIGS.json").write_text(
        json.dumps(variant_configs, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def verify_final(variant: str, config: dict) -> str:
    if not FINAL_MANIFEST.exists():
        raise SystemExit("No final freeze yet. The test split may only run after "
                         "`python benchmarks/run_router_benchmark.py --freeze-final`.")
    m = json.loads(FINAL_MANIFEST.read_text(encoding="utf-8"))
    want = m["variants"].get(variant)
    got = config_hash(config)
    if want != got:
        raise SystemExit(f"{variant}: configuration hash {got} != frozen {want}. The test set "
                         "has been seen; a changed variant needs a new holdout set.")
    drift = [rel for rel, h in m["files"].items() if sha256_file(ROOT / rel) != h]
    if drift:
        raise SystemExit("Benchmark code changed since the final freeze: " + ", ".join(drift))
    return got


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "snapshot-simple":
        m = snapshot_simple()
        print(f"frozen simple_v1: spec {m['spec_sha256'][:16]}, {len(m['files'])} files")
    elif cmd == "verify-simple":
        m = verify_simple()
        print(f"simple_v1 intact (spec {m['spec_sha256'][:16]})")
    elif cmd == "status":
        print("simple_v1 :", "frozen" if SIMPLE_MANIFEST.exists() else "NOT frozen")
        print("final_v1  :", "frozen" if FINAL_MANIFEST.exists() else "not yet")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
