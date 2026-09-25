"""Join shadow decisions with what Claude actually did on the same prompt — locally.

    python scripts/shadow_correlate.py            # SIMPLE skill agreement (shadow_skill)
    python scripts/shadow_correlate.py --facts    # per-turn observed facts, for the agent layer
    python scripts/shadow_correlate.py --json

For each telemetry line, find the same user turn in the local Claude Code transcript
(~/.claude/projects/<cwd-slug>/<session_id>.jsonl, or any project folder holding that
session) by the prompt's 12-char SHA-256, then read the records up to the next turn boundary
and derive **facts** about the turn:

  * which skills Claude loaded (Skill tool calls, by name)
  * how many tool calls, by tool name and by category
    (filesystem / shell / web / github / mcp / subagent / meta / other)
  * whether it used the web, a subagent (Agent / Task / Workflow), or Mercury
  * total and largest tool-output size, and whether a compaction happened
  * how long the turn lasted, from the prompt to the turn's last record

Everything is read and derived here, on this machine. Tool inputs and outputs are inspected
only to classify them (e.g. "is this Bash command `gh ...`", "does it run the Mercury
client") and to measure their length; no text from them is printed, stored or sent anywhere.
Nothing goes to TypeSafe.

Turn boundaries, learned from real transcripts (2026-09-23):

  * a human prompt: a `user` record with text content, not `isMeta`, not a tool result, and
    `origin.kind == "human"` (or no origin, in older transcripts);
  * a **mid-turn** prompt: an `attachment` of type `queued_command` — a message sent while
    Claude was still working, absorbed into the running turn (`absorbed_mid_turn`). The
    previous code did not see these at all; in one real session they were 5 of 5 prompts.
    The work after one is a continuation, so its facts are marked `boundary: "mid_turn"`
    and the report treats them as ambiguous rather than as clean evidence;
  * a background-task notification (`origin.kind == "task-notification"`) ends the current
    turn without starting a prompt turn: work that follows answers the notification.

Caveat: "Claude loaded skill X" is evidence that X was relevant, not proof; "Claude loaded
nothing" is weaker still. Claude's behaviour is the reference here, not ground truth.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "telemetry" / "shadow.jsonl"
PROJECTS = Path.home() / ".claude" / "projects"

# --- tool classification ----------------------------------------------------------------

FILESYSTEM = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit", "LS"}
SHELL = {"Bash", "PowerShell", "Monitor", "BashOutput", "KillShell", "KillBash"}
WEB = {"WebSearch", "WebFetch"}
SUBAGENT = {"Agent", "Task", "Workflow"}
# Bookkeeping and UI tools: calling them is not "using a tool to do the work".
META = {"Skill", "ToolSearch", "TodoWrite", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
        "SendMessage", "ListAgents", "ScheduleWakeup", "SendUserFile", "SuggestSkills",
        "TaskStop", "ListSkills", "SearchSkills"}
META_PREFIXES = ("mcp__ccd_session__", "mcp__ccd_view__", "mcp__ccd_sidebar__", "mcp__ccd_window__",
                 "mcp__ccd_session_mgmt__", "mcp__ccd_settings__", "mcp__visualize__read_me")
GITHUB_PREFIXES = ("mcp__github", "mcp__ccd_pr__")
WEB_MARKERS = ("browser", "chrome", "fetch", "search_web", "web_search")

# Shell commands that only read files: Claude often uses `cat`/`grep` via Bash where the
# filesystem tools would do, so the category follows what the command does, not the tool.
READONLY_FS_CMDS = {"cd", "cat", "head", "tail", "ls", "grep", "rg", "find", "sed", "wc", "echo",
                    "stat", "file", "tree", "diff", "xxd", "get-content", "get-childitem",
                    "select-string", "test-path", "type", "dir"}
SPLIT_CMD = re.compile(r"&&|\|\||;|\|")
WEB_CMDS = {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm"}
EXTERNAL_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost|\[::1\])", re.I)
MERCURY_CMD = re.compile(r"mercury_(client|router)|run_worker_benchmark|inceptionlabs", re.I)

TOOL_TYPES = ("filesystem", "shell", "web", "github", "mcp")


def _first_tokens(cmd: str) -> list[str]:
    toks = []
    for seg in SPLIT_CMD.split(cmd or ""):
        seg = seg.strip()
        # Skip leading env assignments (FOO=1 cmd ...).
        words = [w for w in seg.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        if words:
            toks.append(words[0].strip("\"'").lower())
    return toks


def classify(name: str, tool_input: dict | None) -> str:
    """Category of one tool call. Reads the input only to classify it; never returns it."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name in META or name.startswith(META_PREFIXES):
        return "meta"
    if name in SUBAGENT:
        return "subagent"
    if name in FILESYSTEM:
        return "filesystem"
    if name in WEB:
        return "web"
    if name in SHELL:
        toks = _first_tokens(str(inp.get("command") or ""))
        if "gh" in toks:
            return "github"
        if WEB_CMDS & set(toks) and EXTERNAL_URL.search(str(inp.get("command") or "")):
            return "web"
        if toks and all(t in READONLY_FS_CMDS for t in toks):
            return "filesystem"
        return "shell"
    if name.startswith(GITHUB_PREFIXES):
        return "github"
    if name.startswith("mcp__"):
        return "web" if any(m in name.lower() for m in WEB_MARKERS) else "mcp"
    return "other"   # Artifact, NotebookRead, ... : real work, no tool_type bucket


def mercury_call(name: str, tool_input: dict | None) -> bool:
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name in SHELL:
        return bool(MERCURY_CMD.search(str(inp.get("command") or "")))
    return "mercury" in name.lower() or "inception" in name.lower()


def _result_chars(block: dict) -> int:
    c = block.get("content")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(len(b.get("text", "")) for b in c if isinstance(b, dict))
    return 0


# --- transcript parsing -----------------------------------------------------------------

def slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def prompt_hash(text: str) -> str:
    return sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def _origin_kind(o: dict) -> str | None:
    org = o.get("origin")
    return org.get("kind") if isinstance(org, dict) else None


def user_text(o: dict) -> str | None:
    """Text of a human prompt record, or None."""
    if o.get("type") != "user" or o.get("isMeta") or o.get("isCompactSummary"):
        return None
    if _origin_kind(o) not in (None, "human"):
        return None
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            return None
        text = "\n".join(texts)
    else:
        return None
    if text.startswith("[Request interrupted"):
        return None
    return text


def queued_text(o: dict) -> str | None:
    if o.get("type") != "attachment":
        return None
    a = o.get("attachment") or {}
    if a.get("type") != "queued_command":
        return None
    p = a.get("prompt")
    if isinstance(p, str):
        return p
    if isinstance(p, list):
        return "\n".join(b.get("text", "") for b in p if isinstance(b, dict))
    return None


def _ts(o: dict) -> datetime | None:
    t = o.get("timestamp")
    if not isinstance(t, str):
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def _new_turn(boundary: str, o: dict) -> dict:
    return {"boundary": boundary, "start": _ts(o), "end": _ts(o), "skills": [],
            "tools": Counter(), "categories": Counter(), "tool_calls": 0, "meta_calls": 0,
            "subagent_types": Counter(), "mercury_called": False, "tool_output_chars": 0,
            "max_tool_output_chars": 0, "compaction_event": False, "assistant_messages": 0,
            "interrupted": False, "sidechain_records": 0}


def turns(transcript: Path) -> dict[str, list[dict]]:
    """prompt hash -> list of raw turns (a prompt can repeat within one session)."""
    out: dict[str, list[dict]] = {}
    cur = None
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue

        text, boundary = user_text(o), "human"
        if text is None:
            text, boundary = queued_text(o), "mid_turn"
        if text is not None and text.strip():
            cur = _new_turn(boundary, o)
            out.setdefault(prompt_hash(text), []).append(cur)
            continue
        if o.get("type") == "user" and _origin_kind(o) == "task-notification":
            cur = None          # the work that follows answers the notification, not a prompt
            continue
        if cur is None:
            continue

        t = _ts(o)
        if t is not None:
            cur["end"] = t
        if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
            cur["compaction_event"] = True
        if o.get("isCompactSummary"):
            cur["compaction_event"] = True
        if o.get("isSidechain"):
            cur["sidechain_records"] += 1   # subagent internals (older layout): not main-turn work
            continue

        content = (o.get("message") or {}).get("content") if isinstance(o.get("message"), dict) else None
        if o.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    n = _result_chars(b)
                    cur["tool_output_chars"] += n
                    cur["max_tool_output_chars"] = max(cur["max_tool_output_chars"], n)
                if isinstance(b, dict) and b.get("type") == "text" and \
                        str(b.get("text", "")).startswith("[Request interrupted"):
                    cur["interrupted"] = True
            continue
        if o.get("type") != "assistant" or not isinstance(content, list):
            continue
        cur["assistant_messages"] += 1
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                continue
            name, inp = b.get("name", "?"), b.get("input")
            cat = classify(name, inp)
            cur["tools"][name] += 1
            cur["categories"][cat] += 1
            if cat == "meta":
                cur["meta_calls"] += 1
            else:
                cur["tool_calls"] += 1
            if name == "Skill":
                cur["skills"].append((inp or {}).get("skill"))
            if cat == "subagent":
                cur["subagent_types"][(inp or {}).get("subagent_type") or name] += 1
            if mercury_call(name, inp):
                cur["mercury_called"] = True
    return out


def facts(turn: dict) -> dict:
    """Observed facts of one turn — names, counts and booleans only."""
    cats = {c: n for c, n in turn["categories"].items() if c in TOOL_TYPES}
    dominant = max(cats, key=cats.get) if cats else None
    dur = (turn["end"] - turn["start"]).total_seconds() if turn["start"] and turn["end"] else None
    return {
        "boundary": turn["boundary"],
        "responded": turn["assistant_messages"] > 0,
        "interrupted": turn["interrupted"],
        "skills": [s for s in turn["skills"] if s],
        "skill_used": any(turn["skills"]),
        "tool_calls": turn["tool_calls"],              # work tools, excluding meta
        "meta_calls": turn["meta_calls"],
        "tools": dict(turn["tools"]),
        "categories": dict(turn["categories"]),
        "tool_type_dominant": dominant,
        "web_used": turn["categories"].get("web", 0) > 0,
        "web_calls": turn["categories"].get("web", 0),
        "fs_calls": turn["categories"].get("filesystem", 0),
        "subagent_used": turn["categories"].get("subagent", 0) > 0,
        "subagent_calls": turn["categories"].get("subagent", 0),
        "subagent_types": dict(turn["subagent_types"]),
        "mercury_called": turn["mercury_called"],
        "tool_output_chars": turn["tool_output_chars"],
        "max_tool_output_chars": turn["max_tool_output_chars"],
        "compaction_event": turn["compaction_event"],
        "duration_s": round(dur, 1) if dur is not None else None,
    }


# --- joining telemetry to transcripts ---------------------------------------------------

def find_transcript(session_id: str, cwd: str | None) -> Path | None:
    if cwd:
        t = PROJECTS / slug(cwd) / f"{session_id}.jsonl"
        if t.is_file():
            return t
    hits = list(PROJECTS.glob(f"*/{session_id}.jsonl")) if PROJECTS.is_dir() else []
    return hits[0] if hits else None


class Correlator:
    """Caches parsed transcripts; `lookup(row)` returns facts for one telemetry row or None."""

    def __init__(self):
        self._cache: dict[str, dict[str, list[dict]]] = {}

    def lookup(self, row: dict) -> dict | None:
        sid, h = row.get("session_id"), row.get("prompt_sha256_12")
        if not sid or not h:
            return None
        t = find_transcript(sid, row.get("cwd"))
        if t is None:
            return None
        key = str(t)
        if key not in self._cache:
            self._cache[key] = turns(t)
        cands = self._cache[key].get(h)
        if not cands:
            return None
        # A repeated prompt: pick the turn that started closest to the telemetry timestamp.
        when = _ts({"timestamp": row.get("ts")})
        if when is not None and len(cands) > 1:
            cands = sorted(cands, key=lambda c: abs((c["start"] - when).total_seconds())
                           if c["start"] else 1e12)
        return facts(cands[0])


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--facts", action="store_true", help="print per-turn observed facts")
    ap.add_argument("--log", default=str(LOG))
    args = ap.parse_args()
    rows = load_jsonl(Path(args.log))
    if not rows:
        print("no telemetry yet")
        return 0
    corr = Correlator()
    joined = []
    for r in rows:
        if not r.get("ok"):
            continue
        f = corr.lookup(r)
        if f is None:
            continue
        actual = f["skills"][0] if f["skills"] else None
        joined.append({"ts": r["ts"], "prompt_sha256_12": r.get("prompt_sha256_12"),
                       "predicted": r.get("skill"), "action": r.get("action"),
                       "confidence": r.get("confidence"), "actual_skill": actual, "facts": f})

    agree = Counter()
    for j in joined:
        p, a = j["predicted"], j["actual_skill"]
        agree["both none" if p is None and a is None else
              "same skill" if p == a else
              "jev fired, claude loaded nothing" if a is None else
              "jev silent, claude loaded a skill" if p is None else "different skills"] += 1
    report = {"telemetry_lines": len(rows), "joined_to_transcript": len(joined),
              "agreement": dict(agree), "rows": joined}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"telemetry lines        : {len(rows)}")
    print(f"joined to a transcript : {len(joined)}")
    for k, v in agree.most_common():
        print(f"  {k:34s}: {v}")
    for j in joined[-15:]:
        f = j["facts"]
        print(f"  {j['ts'][:19]}  jev={str(j['predicted']):24s} claude={str(j['actual_skill']):24s} "
              f"tools={f['tool_calls']:<3} {f['boundary']}")
        if args.facts:
            print(f"      categories={f['categories']} web={f['web_used']} subagent={f['subagent_used']} "
                  f"mercury={f['mercury_called']} out={f['tool_output_chars']} "
                  f"compaction={f['compaction_event']} dur={f['duration_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
