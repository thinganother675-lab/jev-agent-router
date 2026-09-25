"""Mercury 2.5 as a routing classifier — the external comparison arm for the Jev router.

This exists to *measure* Mercury against `jev_router.py` on identical inputs. It is not a
replacement for it and nothing in the project depends on it.

Fairness rules this file obeys, and why:
  * It reads the same `src/skills_catalog.json` the Jev router reads.
  * It never sees expected labels, the Jev router's answers, or BENCHMARK.md. The benchmark
    driver passes prompts only.
  * It asks one question per prompt, like the Jev router, so the latency comparison is
    request-for-request.
  * Output shape is pinned by native structured output (`response_format: json_schema`,
    `strict: true`), so a parse failure is a real failure rather than a prompt-format artifact.

The one thing that cannot be made equivalent: Jev returns a **calibrated** probability from
an RLCD-trained decision model, while Mercury returns a number it wrote itself. Treat the two
`confidence` columns as different quantities that happen to share a name.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from mercury_client import DEFAULT_MODEL, MercuryClient, cost_usd, text_of

CATALOG_PATH = Path(__file__).with_name("skills_catalog.json")

NONE_CHOICE = "none"
COMPLEXITY_LEVELS = ("trivial", "normal", "complex")

# Mirrors jev_router's bands so the two arms are scored with the same thresholds.
HIGH_CONFIDENCE = 0.75
LOW_CONFIDENCE = 0.45

# reasoning_effort="instant" is the routing setting. At "low" and above Mercury spends
# 250-550 reasoning tokens on a classification, which triples latency and cost for no
# measurable accuracy gain on this task, and silently truncates the answer if max_tokens
# is not raised to cover them (reasoning tokens bill against max_tokens).
ROUTING_EFFORT = "instant"


def route_schema(skill_names: list[str]) -> dict:
    """Strict schema. `skill` is an enum, so an invented skill name is impossible."""
    return {
        "name": "routing_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "enum": [*skill_names, NONE_CHOICE]},
                "complexity": {"type": "string", "enum": list(COMPLEXITY_LEVELS)},
                "confidence": {"type": "number"},
            },
            "required": ["skill", "complexity", "confidence"],
            "additionalProperties": False,
        },
    }


@dataclass
class MercuryDecision:
    action: str
    skill: str | None
    skill_confidence: float
    complexity: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    raw: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))["skills"]


def build_system_prompt(catalog: dict[str, str]) -> str:
    """The catalog, plus the same abstention framing the Jev router's `__none__` option carries.

    BENCHMARK.md's finding was that a well-described none option is what prevents false
    positives. Giving Mercury a weaker abstention instruction than Jev got would rig the
    comparison, so the wording is deliberately parallel.
    """
    lines = "\n".join(f"- {name}: {desc}" for name, desc in catalog.items())
    return (
        "You route developer requests to at most one specialised skill.\n\n"
        "Available skills:\n"
        f"{lines}\n"
        f"- {NONE_CHOICE}: No skill applies: ordinary coding, debugging, refactoring or "
        "discussion that needs neither a specialised reference document nor a particular "
        "file format\n\n"
        "Choose the single best-fitting skill, or "
        f"'{NONE_CHOICE}' if the request is ordinary coding, explanation or conversation.\n"
        "Also rate how much work the task requires:\n"
        "- trivial: a question, a one-line edit, or a cosmetic change needing no investigation\n"
        "- normal: a contained change or a bug fix in one or two known places\n"
        "- complex: multi-file design work, an unclear bug, or a task needing planning "
        "and investigation\n\n"
        "confidence is your probability, from 0.0 to 1.0, that your skill choice is correct."
    )


class MercuryRouter:
    """Same surface as jev_router.Router: construct once, call decide() per prompt."""

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = 30.0,
                 catalog: dict[str, str] | None = None, reasoning_effort: str = ROUTING_EFFORT):
        self.catalog = catalog if catalog is not None else load_catalog()
        self.system = build_system_prompt(self.catalog)
        self.schema = route_schema(list(self.catalog))
        self.reasoning_effort = reasoning_effort
        self.client = MercuryClient(model=model, timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close(); return False

    def decide(self, prompt: str) -> MercuryDecision:
        started = time.perf_counter()
        try:
            resp = self.client.chat(
                [{"role": "system", "content": self.system},
                 {"role": "user", "content": prompt}],
                # 800, not 200: the median routing answer is ~107 output tokens but the
                # tail reaches 200, and a truncated answer surfaces as an unparsable
                # empty string rather than as an error. Measured: 2 of 50 blind cases
                # were truncated at 200, which would have been misread as Mercury
                # failing to honour its own strict schema.
                max_tokens=800,
                temperature=0,
                reasoning_effort=self.reasoning_effort,
                response_format={"type": "json_schema", "json_schema": self.schema},
            )
        except Exception as exc:  # noqa: BLE001 — must never break the caller, like the Jev arm
            return MercuryDecision(action="defer", skill=None, skill_confidence=0.0,
                                   complexity="unknown",
                                   latency_ms=round((time.perf_counter() - started) * 1000, 1),
                                   input_tokens=0, output_tokens=0, cost_usd=0.0,
                                   error=f"{type(exc).__name__}: {exc}"[:200])
        return self._interpret(resp)

    def _interpret(self, resp: dict) -> MercuryDecision:
        body = text_of(resp)
        usage = resp.get("usage", {})
        base = dict(latency_ms=resp["latency_ms"],
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    cost_usd=resp.get("cost_usd", cost_usd(usage)))
        try:
            parsed = json.loads(body)
            skill = parsed["skill"]
            conf = float(parsed["confidence"])
            complexity = parsed["complexity"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return MercuryDecision(action="defer", skill=None, skill_confidence=0.0,
                                   complexity="unknown", raw=body[:200],
                                   error=f"unparsable: {type(exc).__name__}", **base)

        if skill == NONE_CHOICE:
            skill = None
        if skill is None or conf < LOW_CONFIDENCE:
            action, skill = "defer", None
        elif conf >= HIGH_CONFIDENCE:
            action = "use"
        else:
            action = "recommend"
        return MercuryDecision(action=action, skill=skill, skill_confidence=round(conf, 4),
                               complexity=complexity, **base)


def main() -> int:
    import argparse, sys
    ap = argparse.ArgumentParser(description="Ask Mercury 2.5 how to route a prompt.")
    ap.add_argument("prompt", nargs="*")
    ap.add_argument("--effort", default=ROUTING_EFFORT, help="reasoning_effort")
    args = ap.parse_args()
    text = " ".join(args.prompt) if args.prompt else sys.stdin.read()
    if not text.strip():
        return 0
    with MercuryRouter(reasoning_effort=args.effort) as r:
        print(json.dumps(r.decide(text).as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    raise SystemExit(main())
