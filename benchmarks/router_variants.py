"""The three routing architectures under test, behind one interface.

    SIMPLE    one request: choice(all skills + none) + complexity. The frozen production
              router (src/jev_router.py, simple_v1), called through its own methods so the
              request is byte-identical to what shadow mode sends.
    OFFICIAL  TypeSafe's skill-suggestion cookbook as the `jev-skill-suggestion` mod
              implements it (davila7/claude-code-templates, hooks/policy.ts, read 2026-09-23):
                request 1  `which` = choice over every skill, NO none option, plus three gate
                           nouls; gate = mean of the oriented nouls; below gateThreshold the
                           answer is "no skill" and request 2 never happens
                request 2  top `shortlist` skills by request-1 probability; `which` again over
                           their detail (longer description + " — " + first excerptChars of the
                           SKILL.md body), plus one `fits::<skill>` noul each; if the best fits
                           is below fitsThreshold nothing is suggested, else the winner of the
                           re-read choice; a failed request 2 suggests nothing
              Defaults (plugin.json): shortlist 3, gateThreshold 0.3, fitsThreshold 0.3,
              excerptChars 700. state = {request, recent_context: ""}.
    HYBRID    request 1 = SIMPLE's request, unchanged. Clear cases are accepted there; the
              ambiguous band goes to a second request over the top-N real skills with their
              details and the same `none` option, which may still abstain.

Every decision is a pure function of the recorded answers (`*_decide`), and the live path
calls the same function — so thresholds tuned offline on development-set traces behave
identically when run live. Nothing here can see a label.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from jev_client import cost_usd  # noqa: E402
from jev_router import NONE_CHOICE, Router, build_questions, load_catalog  # noqa: E402

DETAILS_PATH = HERE / "skill_details_v1.json"

# --- official, verbatim from hooks/policy.ts ----------------------------------------------
OFFICIAL_GATE_QUESTIONS = {
    "acts_on_user_system":
        "Is the assistant being asked to act on the user's files, accounts, devices, or online services, rather than only to explain or advise?",
    "would_follow_documented_procedure":
        "Would a careful expert answering this consult a specific documented procedure or set of commands, rather than answering from general understanding?",
    "prose_suffices":
        "Could a knowledgeable generalist fully satisfy this request in prose, with no tools, no documentation, and no access to the user's files or accounts?",
}
OFFICIAL_INVERTED = {"prose_suffices"}
OFFICIAL_WIDE_INSTRUCTIONS = "Which of these skills, if any, is the right one to load to help with the user's latest request?"
OFFICIAL_RERANK_INSTRUCTIONS = ("Exactly one of these skills is the right one to load for the user's latest request. "
                                "Which one? Read what each actually does, not just its name.")
OFFICIAL_DEFAULTS = {"shortlist": 3, "gate_threshold": 0.3, "fits_threshold": 0.3, "excerpt_chars": 700}

# --- hybrid ---------------------------------------------------------------------------------
HYBRID_RERANK_INSTRUCTIONS = ("Which of these skills, if any, should be loaded before working on the request? "
                              "Read what each one actually does. Choose the none option unless the request "
                              "asks for the specific thing a skill does.")
HYBRID_START = {"none_accept": 0.85, "win_accept": 0.85, "gap": 0.40, "top_n": 3,
                "final_min": 0.45, "excerpt_chars": 700}


def load_details() -> dict:
    return json.loads(DETAILS_PATH.read_text(encoding="utf-8"))


def detail_of(name: str, catalog_desc: str, details: dict, excerpt_chars: int) -> str:
    """policy.ts detailOf(): longer description, then ' — ', then the body opening."""
    d = details.get(name, {})
    desc = d.get("description") or catalog_desc
    if len(catalog_desc) > len(desc):
        desc = catalog_desc
    excerpt = (d.get("excerpt") or "").strip()[:max(0, excerpt_chars)]
    return f"{desc} — {excerpt}" if excerpt else desc


def _ranked(probabilities: dict, exclude=()) -> list[tuple[str, float]]:
    return sorted(((k, v) for k, v in probabilities.items() if k not in exclude),
                  key=lambda kv: kv[1], reverse=True)


class _Base:
    name = "base"

    def __init__(self, router: Router):
        self.r = router          # holds the warm connection and the retrying _post
        self.catalog = router.catalog

    def _call(self, state: dict, questions: dict) -> tuple[dict, float]:
        body = json.dumps({"state": state, "model": self.r.model, "questions": questions})
        return self.r._post(body)

    def config(self) -> dict:
        raise NotImplementedError


# ================================================================ SIMPLE
class Simple(_Base):
    name = "simple"

    def config(self) -> dict:
        from freeze import simple_spec
        return {"variant": "simple", "spec": simple_spec()}

    def decide(self, prompt: str) -> dict:
        t0 = time.perf_counter()
        out = {"calls": 1, "input_tokens": 0, "api_ms": [], "second_pass": False, "error": None}
        try:
            data, ms = self._call({"request": prompt}, self.r.questions)
        except Exception as exc:  # noqa: BLE001
            # identical to Router.decide(): any failure defers
            out.update(pred=None, error=f"{type(exc).__name__}: {exc}"[:200], trace={})
            out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return out
        d = self.r._interpret(data, ms)
        probs = data["answers"]["skill"]["probabilities"]
        out.update(pred=d.skill, input_tokens=data["usage"]["input_tokens"], api_ms=[round(ms, 1)],
                   trace={"probs": {k: round(v, 4) for k, v in _ranked(probs)[:6]},
                          "none_p": round(probs.get(NONE_CHOICE, 0.0), 4),
                          "action": d.action, "confidence": d.skill_confidence,
                          "complexity": d.complexity})
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out


# ================================================================ OFFICIAL
def official_decide(trace: dict, p: dict) -> tuple[str | None, str]:
    """policy.ts decide(), on recorded answers. Returns (skill | None, reason)."""
    if trace.get("wide_error"):
        return None, "no answer"
    gate = trace.get("gate")
    if gate is not None and gate < p["gate_threshold"]:
        return None, f"gate {gate:.2f} < {p['gate_threshold']}"
    shortlist = trace["ranked"][:p["shortlist"]]
    if not shortlist:
        return None, "nothing ranked"
    if trace.get("rerank_error") or "rerank_winner" not in trace:
        return None, "rerank gave no answer"
    fits = trace.get("fits", {})
    best = max(fits.values()) if fits else None
    if best is not None and best < p["fits_threshold"]:
        return None, f"nothing fits, best {best:.2f} < {p['fits_threshold']}"
    if trace["rerank_winner"] in shortlist:
        return trace["rerank_winner"], "rerank"
    return None, "rerank winner not on shortlist"


class Official(_Base):
    def __init__(self, router: Router, params: dict | None = None, label: str = "official_default",
                 probe: bool = False):
        super().__init__(router)
        self.p = {**OFFICIAL_DEFAULTS, **(params or {})}
        self.name = label
        self.probe = probe          # dev only: always run request 2, to tune thresholds offline
        self.details = load_details()

    def config(self) -> dict:
        return {"variant": "official", "params": self.p, "wide": self.wide_questions(),
                "gate_inverted": sorted(OFFICIAL_INVERTED), "rerank_instructions": OFFICIAL_RERANK_INSTRUCTIONS,
                "details_file_sha": _sha(DETAILS_PATH)}

    def wide_questions(self) -> dict:
        q = {"which": {"type": "choice", "instructions": OFFICIAL_WIDE_INSTRUCTIONS,
                       "criteria": {k: (v or f"A skill named {k}.") for k, v in self.catalog.items()}}}
        for key, text in OFFICIAL_GATE_QUESTIONS.items():
            q[f"gate::{key}"] = {"type": "noul", "instructions": text}
        return q

    def rerank_questions(self, names: list[str]) -> dict:
        crit = {n: detail_of(n, self.catalog[n], self.details, self.p["excerpt_chars"]) for n in names}
        q = {"which": {"type": "choice", "instructions": OFFICIAL_RERANK_INSTRUCTIONS, "criteria": crit}}
        for n in names:
            desc = self.catalog[n] or crit[n]
            q[f"fits::{n}"] = {"type": "noul", "instructions":
                               f"Does the skill '{n}' do the specific thing the user's request asks for? It is described as: {desc}"}
        return q

    def decide(self, prompt: str) -> dict:
        t0 = time.perf_counter()
        state = {"request": prompt, "recent_context": ""}
        out = {"calls": 0, "input_tokens": 0, "api_ms": [], "second_pass": False, "error": None}
        trace: dict = {}
        try:
            data, ms = self._call(state, self.wide_questions())
            out["calls"] += 1
            out["input_tokens"] += data["usage"]["input_tokens"]
            out["api_ms"].append(round(ms, 1))
            a = data["answers"]
            probs = a["which"].get("probabilities") or {a["which"]["choice"]: a["which"].get("confidence", 0)}
            ranked = _ranked(probs)
            gv = {k: round(a[f"gate::{k}"]["noul"], 4) for k in OFFICIAL_GATE_QUESTIONS if f"gate::{k}" in a}
            oriented = [1 - v if k in OFFICIAL_INVERTED else v for k, v in gv.items()]
            trace.update(ranked=[k for k, _ in ranked], top_probs={k: round(v, 4) for k, v in ranked[:6]},
                         gate_values=gv, gate=round(sum(oriented) / len(oriented), 4) if oriented else None)
        except Exception as exc:  # noqa: BLE001
            trace["wide_error"] = True
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]

        gate_ok = not trace.get("wide_error") and (trace.get("gate") is None or trace["gate"] >= self.p["gate_threshold"])
        if not trace.get("wide_error") and (gate_ok or self.probe):
            names = trace["ranked"][:self.p["shortlist"]]
            try:
                data, ms = self._call(state, self.rerank_questions(names))
                out["calls"] += 1
                out["second_pass"] = True
                out["input_tokens"] += data["usage"]["input_tokens"]
                out["api_ms"].append(round(ms, 1))
                a = data["answers"]
                trace.update(rerank_winner=a["which"]["choice"],
                             rerank_conf=round(a["which"].get("confidence", 0.0), 4),
                             rerank_probs={k: round(v, 4) for k, v in (a["which"].get("probabilities") or {}).items()},
                             fits={n: round(a[f"fits::{n}"]["noul"], 4) for n in names if f"fits::{n}" in a})
            except Exception as exc:  # noqa: BLE001
                trace["rerank_error"] = True
                out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        pred, reason = official_decide(trace, self.p)
        trace["reason"] = reason
        out.update(pred=pred, trace=trace, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        if self.probe:
            out["probe_note"] = "request 2 forced; calls/latency not representative"
        return out


# ================================================================ HYBRID
def hybrid_stage1(probs: dict, p: dict) -> tuple[str, str | None]:
    """('none'|'skill'|'rerank', skill). `probs` is request 1's full distribution."""
    none_p = probs.get(NONE_CHOICE, 0.0)
    if none_p >= p["none_accept"]:
        return "none", None
    ranked_all = _ranked(probs)
    top, p1 = ranked_all[0]
    p2 = ranked_all[1][1] if len(ranked_all) > 1 else 0.0
    if top != NONE_CHOICE and p1 >= p["win_accept"] and (p1 - p2) >= p["gap"]:
        return "skill", top
    return "rerank", None


def hybrid_decide(trace: dict, p: dict) -> tuple[str | None, str]:
    if trace.get("first_error"):
        return None, "request 1 failed"
    stage, skill = hybrid_stage1(trace["probs_full"], p)
    if stage == "none":
        return None, "fast none"
    if stage == "skill":
        return skill, "fast accept"
    if trace.get("rerank_error") or "rerank_winner" not in trace:
        return None, "rerank failed"
    w, c = trace["rerank_winner"], trace["rerank_conf"]
    if w == NONE_CHOICE:
        return None, "rerank abstained"
    if c < p["final_min"]:
        return None, f"rerank conf {c:.2f} < {p['final_min']}"
    return w, "rerank"


class Hybrid(_Base):
    def __init__(self, router: Router, params: dict | None = None, label: str = "hybrid",
                 probe: bool = False):
        super().__init__(router)
        self.p = {**HYBRID_START, **(params or {})}
        self.name = label
        self.probe = probe
        self.details = load_details()
        self.none_desc = self.r.questions["skill"]["criteria"][NONE_CHOICE]

    def config(self) -> dict:
        return {"variant": "hybrid", "params": self.p, "first": self.r.questions,
                "rerank_instructions": HYBRID_RERANK_INSTRUCTIONS, "none_desc": self.none_desc,
                "details_file_sha": _sha(DETAILS_PATH)}

    def rerank_questions(self, names: list[str]) -> dict:
        crit = {n: detail_of(n, self.catalog[n], self.details, self.p["excerpt_chars"]) for n in names}
        crit[NONE_CHOICE] = self.none_desc
        return {"rerank": {"type": "choice", "instructions": HYBRID_RERANK_INSTRUCTIONS, "criteria": crit}}

    def decide(self, prompt: str) -> dict:
        t0 = time.perf_counter()
        state = {"request": prompt}
        out = {"calls": 0, "input_tokens": 0, "api_ms": [], "second_pass": False, "error": None}
        trace: dict = {}
        try:
            data, ms = self._call(state, self.r.questions)   # SIMPLE's request, unchanged
            out["calls"] += 1
            out["input_tokens"] += data["usage"]["input_tokens"]
            out["api_ms"].append(round(ms, 1))
            probs = data["answers"]["skill"]["probabilities"]
            trace["probs_full"] = {k: round(v, 5) for k, v in probs.items()}
            trace["none_p"] = round(probs.get(NONE_CHOICE, 0.0), 4)
        except Exception as exc:  # noqa: BLE001
            trace["first_error"] = True
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]

        if not trace.get("first_error"):
            stage, _ = hybrid_stage1(trace["probs_full"], self.p)
            trace["stage1"] = stage
            if stage == "rerank" or self.probe:
                names = [k for k, _ in _ranked(trace["probs_full"], exclude={NONE_CHOICE})][:self.p["top_n"]]
                trace["shortlist"] = names
                try:
                    data, ms = self._call(state, self.rerank_questions(names))
                    out["calls"] += 1
                    out["second_pass"] = stage == "rerank"
                    out["input_tokens"] += data["usage"]["input_tokens"]
                    out["api_ms"].append(round(ms, 1))
                    a = data["answers"]["rerank"]
                    trace.update(rerank_winner=a["choice"], rerank_conf=round(a.get("confidence", 0.0), 4),
                                 rerank_probs={k: round(v, 4) for k, v in (a.get("probabilities") or {}).items()})
                except Exception as exc:  # noqa: BLE001
                    trace["rerank_error"] = True
                    out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        pred, reason = hybrid_decide(trace, self.p)
        trace["reason"] = reason
        # keep the trace small: the full 37-way distribution only matters for tuning
        if not self.probe:
            trace["probs"] = {k: v for k, v in _ranked(trace.pop("probs_full", {}))[:6]}
        out.update(pred=pred, trace=trace, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        return out


def _sha(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
