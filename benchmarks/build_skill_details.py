"""Snapshot what a rerank pass may read about each catalog skill: full description + SKILL.md opening.

    python benchmarks/build_skill_details.py      # writes benchmarks/skill_details_v1.json

The official mod's second request reads, per shortlisted skill, `detailOf()`: the longer of
the listing description and the SKILL.md frontmatter description, then " — ", then the first
`excerptChars` (default 700) characters of the SKILL.md body with the frontmatter removed.
With no file found it uses the description alone.

Where the files are on this machine:

* anthropic-skills (pdf, docx, ...): the desktop app's synced skills-plugin directory
* figma: the installed plugin cache under ~/.claude/plugins
* Claude Code's bundled skills (code-review, simplify, run, update-config, ...): compiled into
  claude.exe, **no SKILL.md on disk**. For these the detail is the full live listing
  description, which is exactly what the mod falls back to when it finds no file.

The snapshot is frozen with the benchmark so a later plugin update cannot change what the
rerank read between runs. Excerpts are stored at 2000 chars; variants cut them down.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HOME = Path.home()
OUT = HERE / "skill_details_v1.json"

SEARCH_ROOTS = [
    HOME / "AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/local-agent-mode-sessions/skills-plugin",
    HOME / ".claude/plugins/cache/claude-plugins-official/figma",
]

# The live skill listing of a Claude Code 2.1.280 desktop session on 2026-09-23, for the
# bundled skills that have no SKILL.md on disk. Verbatim except whitespace.
BUNDLED_LISTING = {
    "code-review": "Review the current diff, or a PR number/branch/path target, for correctness bugs (plus reuse/simplification/efficiency cleanups where the model's review recipe covers them) at the given effort level (low/medium: fewer, high-confidence findings; high to max: broader coverage, may include uncertain findings; ultra: deep multi-agent review in the cloud). Pass --comment to post findings as inline PR comments, or --fix to apply the findings to the working tree after the review.",
    "simplify": "Review the changed code for reuse, simplification, efficiency, and altitude cleanups, then apply the fixes. Quality only - it does not hunt for bugs; use /code-review for that.",
    "security-review": "Complete a security review of the pending changes on the current branch",
    "run": "Launch and drive this project's app to see a change working. Use when asked to run, start, or screenshot the app, or to confirm a change works in the real app (not just tests). First looks for a project skill that already covers launching the app; otherwise falls back to built-in patterns per project type (CLI, server, TUI, Electron, browser-driven, library).",
    "init": "Initialize a new CLAUDE.md file with codebase documentation",
    "claude-api": "Reference for the Claude API / Anthropic SDK - model ids, pricing, params, streaming, tool use, MCP, agents, caching, token counting, model migration. Read before answering whenever the prompt names Claude/Anthropic in any form, or the user asks about an LLM's pricing, model choice, limits or caching - never answer from memory.",
    "update-config": "Use this skill to configure the Claude Code harness via settings.json. Automated behaviors (\"from now on when X\", \"each time X\", \"whenever X\", \"before/after X\") require hooks configured in settings.json - the harness executes these, not Claude, so memory/preferences cannot fulfill them. Also use for: permissions (\"allow X\", \"add permission\", \"move permission to\"), env vars (\"set X=Y\"), hook troubleshooting, or any changes to settings.json/settings.local.json files.",
    "keybindings-help": "Use when the user wants to customize keyboard shortcuts, rebind keys, add chord bindings, or modify ~/.claude/keybindings.json. Examples: \"rebind ctrl+s\", \"add a chord shortcut\", \"change the submit key\", \"customize keybindings\".",
    "fewer-permission-prompts": "Scan your transcripts for common read-only Bash and MCP tool calls, then add a prioritized allowlist to project .claude/settings.json to reduce permission prompts.",
    "loop": "Run a prompt or slash command on a recurring interval (e.g. /loop 5m /foo). Omit the interval to let the model self-pace. When the user wants to set up a recurring task, poll for status, or run something repeatedly on an interval (e.g. \"check the deploy every 5 minutes\", \"keep running /babysit-prs\"). Do NOT invoke for one-off tasks.",
    "schedule": "Create, update, list, or run scheduled cloud agents (routines) that execute on a cron schedule. When the user wants to schedule a recurring cloud agent, set up automated tasks, create a cron job for Claude Code, or manage their scheduled agents/routines. Also use when the user wants a one-time scheduled run.",
    "workflow-authoring": "Reference for writing a Workflow tool script (script API and gotchas, resume, quality patterns, worked examples). Load before authoring a script for a workflow the user already opted into.",
    "dataviz": "Use this skill whenever you are about to create ANY chart, graph, plot, dashboard, or data visualization, in ANY output medium. Read it BEFORE writing the first line of chart code, choosing chart colors, building a stat tile / meter / KPI row, or laying out a dashboard.",
    "artifact-design": "Design guidance and fundamentals for Artifacts. Load before writing any artifact, including a skill-instructed Markdown one.",
    "artifact-diagramming": "Diagramming know-how for Artifacts - when a picture earns its place, how to draw one that shows the real mechanism, and the inline-SVG mechanics that keep it legible in both themes.",
    "artifact-capabilities": "Runtime capabilities a published Artifact page can be granted - behavior static HTML cannot provide on its own, such as the page reading live or connected data, remembering what people do on it (a poll, a sign-up sheet, a checklist), keeping state shared across viewers, knowing who is viewing, storing files people add, or handing the viewer a file to save.",
}


def skill_md_for(name: str) -> Path | None:
    short = name.split(":", 1)[-1]
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob(f"{short}/SKILL.md"):
            if "skills-figquery" in p.parts or "workflow-skills" in p.parts:
                continue
            return p
    return None


def split_frontmatter(md: str) -> tuple[str, str]:
    m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?", md)
    if not m:
        return "", md
    return m.group(1), md[m.end():]


def fm_description(fm: str) -> str:
    # single-line or folded (`>`/`|`) descriptions
    m = re.search(r"^description:\s*(.*)$", fm, re.M)
    if not m:
        return ""
    val = m.group(1).strip()
    if val in (">", "|", ">-", "|-"):
        lines = []
        for line in fm[m.end():].splitlines()[1:]:
            if line.startswith((" ", "\t")):
                lines.append(line.strip())
            else:
                break
        val = " ".join(lines)
    return val.strip().strip('"').strip("'")


def main() -> int:
    catalog = json.loads((ROOT / "src/skills_catalog.json").read_text(encoding="utf-8"))["skills"]
    out = {}
    for name, one_line in catalog.items():
        p = skill_md_for(name) if name not in BUNDLED_LISTING else None
        if p:
            fm, body = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            desc = fm_description(fm)
            out[name] = {"catalog": one_line, "description": desc if len(desc) > len(one_line) else one_line,
                         "excerpt": body.strip()[:2000], "source": "SKILL.md"}
        else:
            out[name] = {"catalog": one_line, "description": BUNDLED_LISTING.get(name, one_line),
                         "excerpt": "", "source": "bundled-listing" if name in BUNDLED_LISTING else "catalog-only"}
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    from collections import Counter
    print(Counter(v["source"] for v in out.values()), "->", OUT.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
