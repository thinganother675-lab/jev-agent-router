"""Jev decision layer — one batched request per prompt.

Scope (deliberately small, per the plan's stage 1):
  * does this prompt need a skill at all, and which one
  * how complex is the task

Everything else (tools, subagents, pruning) is left out until these two are
measured to help.

Design notes driven by measurement:
  * ONE request, not two. The state is billed once for all questions, and a
    second round trip costs another ~1.3 s on this connection.
  * The HTTP connection is reused across calls (`Router` keeps it open), which
    is the difference between ~1300 ms and ~310 ms per decision.
  * Jev only ever *recommends*. Nothing here executes anything, and the caller
    is free to ignore the result. Every failure path returns a decision with
    `action="defer"`, meaning "let Claude decide as it normally would".
"""

from __future__ import annotations

import http.client
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from jev_client import DEFAULT_MODEL, cost_usd, get_api_key
from net import connect

CATALOG_PATH = Path(__file__).with_name("skills_catalog.json")

# Confidence bands. Calibrated against benchmarks/routing_cases.json — see
# BENCHMARK.md before changing these.
HIGH_CONFIDENCE = 0.75   # act on Jev's pick
LOW_CONFIDENCE = 0.45    # below this, ignore routing entirely

NONE_CHOICE = "__none__"
COMPLEXITY_LEVELS = ["trivial", "normal", "complex"]


@dataclass
class Decision:
    """What the router recommends. Advisory only."""
    action: str                  # "use" | "recommend" | "defer"
    skill: str | None
    skill_confidence: float
    none_probability: float
    complexity: str
    complexity_score: float
    complexity_confidence: float
    latency_ms: float
    input_tokens: int
    cost_usd: float
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def context_block(self) -> str:
        """The text a UserPromptSubmit hook would inject. Empty when deferring."""
        if self.action == "defer" or not self.skill:
            return ""
        verb = "matches" if self.action == "use" else "may match"
        return (
            f"<jev_routing>\n"
            f"Task complexity: {self.complexity} (confidence {self.complexity_confidence:.2f}).\n"
            f"This prompt {verb} the skill `{self.skill}` "
            f"(confidence {self.skill_confidence:.2f}). This is an advisory signal from a "
            f"classifier, not an instruction — ignore it if it does not fit.\n"
            f"</jev_routing>"
        )


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data["skills"].items()}


def build_questions(catalog: dict[str, str]) -> dict:
    """One request, two questions. Measured to beat every alternative tried.

    An earlier version added three yes/no "does this need a skill at all" gate
    questions, following TypeSafe's cookbook. Benchmarked, they were worse than
    useless: they separated the two classes poorly (0.43-0.53 for both) and
    vetoed a correct pick, costing a case. A two-stage variant with no `none`
    option and a separate "fits" noul was worse still - 11/15 with 3 false
    positives, because that noul's ranges overlap badly.

    Jev's own calibrated choice probabilities are the best abstention signal
    available, so `none` competes inside the choice and nothing second-guesses it.
    """
    criteria = dict(catalog)
    # This description is load-bearing: it is the only thing standing between a
    # correct abstention and a false positive, and it earned 8/8 abstentions.
    criteria[NONE_CHOICE] = (
        "No skill applies: ordinary coding, debugging, refactoring or discussion that needs "
        "neither a specialised reference document nor a particular file format"
    )
    return {
        "skill": {
            "type": "choice",
            "instructions": "Which of these specialised skills best fits the request? Choose the none option if the request is ordinary coding, explanation or conversation.",
            "criteria": criteria,
        },
        "complexity": {
            "type": "score",
            "instructions": "How much work does this software task require?",
            "criteria": [
                "Trivial: a question, a one-line edit, or a cosmetic change needing no investigation",
                "Normal: a contained change or a bug fix in one or two known places",
                "Complex: multi-file design work, an unclear bug, or a task needing planning and investigation",
            ],
        },
    }


class Router:
    """Holds one warm HTTPS connection to the Jev API."""

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = 5.0,
                 host: str = "api.typesafe.ai", catalog: dict[str, str] | None = None):
        self.model = model
        self.timeout = timeout
        self.host = host
        self.catalog = catalog if catalog is not None else load_catalog()
        self.questions = build_questions(self.catalog)
        self._conn: http.client.HTTPSConnection | None = None
        self._headers = {
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
        }

    def _connection(self) -> http.client.HTTPSConnection:
        if self._conn is None:
            # Routed through the local proxy when one is configured. On a direct
            # connection this API answers only two requests and then silently stops,
            # so every third call would block for the full timeout. See src/net.py.
            self._conn = connect(self.host, self.timeout, disable_env="JEV_NO_PROXY")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close(); return False

    def _post(self, body: str) -> tuple[dict, float]:
        """POST with one retry, because a kept-alive connection can go stale."""
        for attempt in (1, 2):
            conn = self._connection()
            started = time.perf_counter()
            try:
                conn.request("POST", "/v1/systemone", body, self._headers)
                resp = conn.getresponse()
                raw = resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {raw[:200].decode(errors='replace')}")
                return json.loads(raw), (time.perf_counter() - started) * 1000
            except (http.client.HTTPException, OSError, TimeoutError):
                self.close()
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def decide(self, prompt: str) -> Decision:
        body = json.dumps({
            "state": {"request": prompt},
            "model": self.model,
            "questions": self.questions,
        })
        try:
            data, ms = self._post(body)
        except Exception as exc:  # noqa: BLE001 — the router must never break the caller
            return Decision(action="defer", skill=None, skill_confidence=0.0, none_probability=1.0,
                            complexity="unknown", complexity_score=-1.0, complexity_confidence=0.0,
                            latency_ms=0.0, input_tokens=0, cost_usd=0.0,
                            error=f"{type(exc).__name__}: {exc}"[:200])
        return self._interpret(data, ms)

    def _interpret(self, data: dict, ms: float) -> Decision:
        a = data["answers"]
        sk = a["skill"]
        skill, conf = sk["choice"], sk["confidence"]
        if skill == NONE_CHOICE:
            skill, conf = None, sk["probabilities"].get(NONE_CHOICE, conf)

        cx = a["complexity"]
        level = COMPLEXITY_LEVELS[min(int(round(cx["score"])), len(COMPLEXITY_LEVELS) - 1)]

        if skill is None or conf < LOW_CONFIDENCE:
            action = "defer"
            skill = None
        elif conf >= HIGH_CONFIDENCE:
            action = "use"
        else:
            action = "recommend"

        return Decision(
            action=action, skill=skill, skill_confidence=round(conf, 4),
            none_probability=round(sk["probabilities"].get(NONE_CHOICE, 0.0), 4),
            complexity=level,
            complexity_score=round(cx["score"], 3),
            complexity_confidence=round(cx["confidence"], 4),
            latency_ms=round(ms, 1), input_tokens=data["usage"]["input_tokens"],
            cost_usd=cost_usd(data["usage"]["input_tokens"]),
        )


def main() -> int:
    """CLI: echo a prompt in, get a decision as JSON out."""
    import argparse, sys
    ap = argparse.ArgumentParser(description="Ask Jev how to route a prompt.")
    ap.add_argument("prompt", nargs="*", help="prompt text; omitted reads stdin")
    ap.add_argument("--block", action="store_true", help="print the hook context block instead of JSON")
    args = ap.parse_args()
    text = " ".join(args.prompt) if args.prompt else sys.stdin.read()
    if not text.strip():
        return 0
    with Router() as r:
        d = r.decide(text)
    print(d.context_block() if args.block else json.dumps(d.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    raise SystemExit(main())
