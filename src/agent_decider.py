"""Agent-decision shadow layer — predicts the *shape* of the work, not just the skill.

SHADOW ONLY. Nothing here is acted on: no tool is chosen, no subagent is started, no model
is switched, no skill is injected and Mercury is never called. The output is a record for
telemetry, later compared with what Claude actually did (scripts/shadow_correlate.py).

Separate from the SIMPLE skill router on purpose:

  * `src/jev_router.py` is frozen as `simple_v1` (benchmarks/freeze.py). This module does
    not modify it and does not change the request SIMPLE sends; it only reuses the frozen
    Router's connection handling by subclassing.
  * The skill and complexity fields of the composed decision come from the SIMPLE call
    (`skill_source: "simple_v1"`), so the proven router is not re-asked in a new form and
    the benchmarked numbers stay comparable.
  * Telemetry goes to its own file and type (`shadow_agent_decision`), never mixed with
    `shadow_skill`.

One batched request, seven questions, one billing of the prompt:

    needs_tool                noul    will the agent have to call tools at all
    tool_type                 choice  web | filesystem | github | shell | mcp | none
    needs_subagent            noul    would delegating to a subagent help
    needs_research            noul    external lookup (web, docs) needed
    needs_context_compaction  noul    large text volume that would need condensing
    worker_mercury            noul    bounded mechanical text job a cheap model could do
    escalation                choice  direct | tool | main_claude | subagent | mercury_then_claude

`state` is exactly SIMPLE's shape, `{"request": <prompt>}` — no file contents, paths,
history or environment. Jev answers are parsed against closed enums; nothing in an answer
is ever executed, interpolated into a command, or used as anything but a label.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, asdict, field

from jev_client import cost_usd
from jev_router import Router

SCHEMA_VERSION = "agent_v1"
BOOL_THRESHOLD = 0.5

TOOL_TYPES = ("filesystem", "shell", "web", "github", "mcp", "none")
ESCALATIONS = ("direct", "tool", "main_claude", "subagent", "mercury_then_claude")

_AGENT = ("An AI coding agent (Claude Code) receives this request. It works in a local "
          "repository and can read, search and edit files, run shell commands, search the web, "
          "use GitHub and connected MCP services, and delegate work to subagents. ")

QUESTIONS: dict = {
    "needs_tool": {
        "type": "noul",
        "instructions": _AGENT + "Will it need to call tools to handle this request, rather "
                                 "than answering from its own knowledge alone?",
        "criteria": {
            "true": "It must inspect or change files, run commands, or fetch information",
            "false": "It can answer or act from knowledge alone, with no file, command or web access",
        },
    },
    "tool_type": {
        "type": "choice",
        "instructions": _AGENT + "Which kind of tool would it mainly use for this request?",
        "criteria": {
            "filesystem": "Reading, searching or editing files in the local project",
            "shell": "Running commands: tests, builds, scripts, package managers, git, processes",
            "web": "Searching the internet or reading web pages and online documentation",
            "github": "GitHub work: pull requests, issues, CI checks or reviews on github.com",
            "mcp": "A connected external service or app (Figma, Slack, Google Drive, a database, "
                   "a calendar, browser automation)",
            "none": "No tool: a question, explanation or conversation answered from knowledge",
        },
    },
    "needs_subagent": {
        "type": "noul",
        "instructions": _AGENT + "Would this request benefit from delegating part of the work "
                                 "to a separate subagent, instead of the main agent doing it all "
                                 "itself?",
        "criteria": {
            "true": "A broad search across a large codebase, several independent investigations "
                    "in parallel, or a long self-contained research or review job",
            "false": "A focused task the main agent can do directly in a few steps",
        },
    },
    "needs_research": {
        "type": "noul",
        "instructions": _AGENT + "Does this request require looking up information outside the "
                                 "project before it can be answered or done?",
        "criteria": {
            "true": "Needs web search, online documentation, release notes, API references, "
                    "papers or current facts",
            "false": "The project's own files and the agent's general knowledge are enough",
        },
    },
    "needs_context_compaction": {
        "type": "noul",
        "instructions": _AGENT + "Will handling this request involve so much text that condensing "
                                 "it into a shorter summary would be necessary?",
        "criteria": {
            "true": "Long logs, big files or datasets, many command outputs, or a long document "
                    "or transcript to digest",
            "false": "A modest amount of text that fits comfortably in working memory",
        },
    },
    "worker_mercury": {
        "type": "noul",
        "instructions": _AGENT + "Is the core of this request a bounded, mechanical text-processing "
                                 "job that a small, cheap model could do reliably?",
        "criteria": {
            "true": "Summarising or compressing given text or logs, extracting structured fields, "
                    "shortlisting relevant files or items from a list, reformatting data",
            "false": "Needs design judgement, debugging, writing code, decisions or conversation",
        },
    },
    "escalation": {
        "type": "choice",
        "instructions": _AGENT + "What is the lightest execution path that would handle this "
                                 "request well?",
        "criteria": {
            "direct": "Answer directly from knowledge, no tools",
            "tool": "The main agent with a few tool calls: a quick lookup, a small edit, one command",
            "main_claude": "Sustained main-agent work: many steps, multi-file changes, debugging, "
                           "planning",
            "subagent": "Delegate a large exploration, parallel investigation or long review to a "
                        "subagent",
            "mercury_then_claude": "First condense or extract from a large text with a cheap model, "
                                   "then have the main agent reason over the result",
        },
    },
}

NOUL_FIELDS = ("needs_tool", "needs_subagent", "needs_research", "needs_context_compaction",
               "worker_mercury")
CHOICE_FIELDS = {"tool_type": TOOL_TYPES, "escalation": ESCALATIONS}


@dataclass
class AgentDecision:
    """The Jev half of the structured decision. Skill and complexity are added by the caller
    from the frozen SIMPLE call. Advisory, and in this stage never acted on."""
    ok: bool
    decision: dict = field(default_factory=dict)   # the user-facing schema fields
    detail: dict = field(default_factory=dict)     # raw probabilities / confidences
    consistency_flags: list = field(default_factory=list)
    parse_errors: list = field(default_factory=list)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    schema: str = SCHEMA_VERSION
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _noul(answers: dict, key: str, errors: list) -> float | None:
    try:
        p = float(answers[key]["noul"])
    except (KeyError, TypeError, ValueError):
        errors.append(f"{key}: missing or non-numeric noul")
        return None
    if not 0.0 <= p <= 1.0 or math.isnan(p):
        errors.append(f"{key}: noul out of range")
        return None
    return p


def _choice(answers: dict, key: str, allowed: tuple, errors: list) -> tuple[str | None, float | None]:
    try:
        a = answers[key]
        c, conf = a["choice"], float(a["confidence"])
    except (KeyError, TypeError, ValueError):
        errors.append(f"{key}: missing choice or confidence")
        return None, None
    # Closed enum: a value outside the options is dropped, never passed on.
    if c not in allowed:
        errors.append(f"{key}: unexpected choice")
        return None, None
    return c, max(0.0, min(1.0, conf))


def interpret(data: dict) -> tuple[dict, dict, list, list]:
    """Pure function: Jev answers -> (decision, detail, consistency_flags, parse_errors)."""
    answers = data.get("answers") or {}
    errors: list = []
    p = {k: _noul(answers, k, errors) for k in NOUL_FIELDS}
    tool_type, tool_conf = _choice(answers, "tool_type", TOOL_TYPES, errors)
    esc, esc_conf = _choice(answers, "escalation", ESCALATIONS, errors)

    def yes(v):
        return None if v is None else v >= BOOL_THRESHOLD

    decision = {
        "needs_tool": yes(p["needs_tool"]),
        "tool_type": tool_type,
        "needs_subagent": yes(p["needs_subagent"]),
        "needs_research": yes(p["needs_research"]),
        "needs_context_compaction": yes(p["needs_context_compaction"]),
        "worker_candidate": None if p["worker_mercury"] is None
        else ("mercury" if p["worker_mercury"] >= BOOL_THRESHOLD else "none"),
        "escalation": esc,
    }
    # Per-field confidence: a noul's is its distance from a coin flip; a choice's is Jev's own.
    confs = [max(v, 1 - v) for v in p.values() if v is not None]
    confs += [c for c in (tool_conf, esc_conf) if c is not None]
    # Geometric mean: one very unsure field pulls the overall number down more than an
    # arithmetic mean would, without letting the weakest field alone decide it.
    decision["overall_confidence"] = (
        round(math.exp(sum(math.log(max(c, 1e-6)) for c in confs) / len(confs)), 4) if confs else None)

    detail = {f"{k}_p": (round(v, 4) if v is not None else None) for k, v in p.items()}
    detail["tool_type_confidence"] = round(tool_conf, 4) if tool_conf is not None else None
    detail["escalation_confidence"] = round(esc_conf, 4) if esc_conf is not None else None
    for key in CHOICE_FIELDS:
        probs = (answers.get(key) or {}).get("probabilities") or {}
        detail[f"{key}_probabilities"] = {k: round(float(v), 4) for k, v in probs.items()
                                          if k in CHOICE_FIELDS[key]}

    flags = []
    if decision["needs_tool"] is False and tool_type not in (None, "none"):
        flags.append("needs_tool=false but tool_type!=none")
    if decision["needs_tool"] is True and tool_type == "none":
        flags.append("needs_tool=true but tool_type=none")
    # Not flagged: needs_subagent=true with escalation=main_claude. "A subagent would help"
    # and "the lightest adequate path is the main agent" are compatible answers, and on real
    # long prompts Jev gives exactly that pair most of the time.
    if esc == "direct" and decision["needs_tool"] is True:
        flags.append("escalation=direct but needs_tool=true")
    if esc == "mercury_then_claude" and decision["worker_candidate"] == "none":
        flags.append("escalation=mercury_then_claude but worker_candidate=none")
    return decision, detail, flags, errors


class AgentRouter(Router):
    """One warm connection, one batched request. Reuses the frozen Router's connection and
    retry logic by inheritance; the SIMPLE questions it builds in __init__ are never sent."""

    def __init__(self, **kw):
        super().__init__(catalog={}, **kw)
        self.questions = QUESTIONS

    def decide(self, prompt: str) -> AgentDecision:  # type: ignore[override]
        body = json.dumps({"state": {"request": prompt}, "model": self.model,
                           "questions": self.questions})
        started = time.perf_counter()
        try:
            data, ms = self._post(body)
        except Exception as exc:  # noqa: BLE001 — fail open, like SIMPLE
            return AgentDecision(ok=False, latency_ms=round((time.perf_counter() - started) * 1000, 1),
                                 error=f"{type(exc).__name__}: {exc}"[:200])
        decision, detail, flags, errors = interpret(data)
        usage = data.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0)
        return AgentDecision(
            ok=not errors, decision=decision, detail=detail, consistency_flags=flags,
            parse_errors=errors, latency_ms=round(ms, 1), input_tokens=tokens,
            output_tokens=int(usage.get("output_tokens") or 0), cost_usd=cost_usd(tokens),
            error="; ".join(errors)[:200] if errors else None,
        )


def main() -> int:
    """CLI for manual checks: python src/agent_decider.py "prompt" (direct, no sidecar)."""
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Ask Jev for the agent-shape decision (shadow).")
    ap.add_argument("prompt", nargs="*")
    args = ap.parse_args()
    text = " ".join(args.prompt) if args.prompt else sys.stdin.read()
    if not text.strip():
        return 0
    with AgentRouter() as r:
        print(json.dumps(r.decide(text).as_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    raise SystemExit(main())
