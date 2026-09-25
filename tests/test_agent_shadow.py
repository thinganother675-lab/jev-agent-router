"""Offline tests for the agent-decision shadow layer. No API calls, no network.

    python tests/test_agent_shadow.py

Covers: strict parsing of Jev answers (closed enums, ranges, garbage), the transcript turn
parser (human / mid-turn / task-notification boundaries, sidechains, tool categories,
Mercury and compaction detection), the judging rules, and the hook's fail-open path with
the sidecar down (empty stdout, exit 0, gap recorded, no prompt text).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks"))

import agent_decider as ad  # noqa: E402
import shadow_correlate as sc  # noqa: E402
import agent_shadow_report as rep  # noqa: E402


def good_answers(**over) -> dict:
    a = {
        "needs_tool": {"type": "noul", "noul": 0.9},
        "tool_type": {"type": "choice", "choice": "shell", "confidence": 0.8,
                      "probabilities": {"shell": 0.8, "filesystem": 0.2}},
        "needs_subagent": {"type": "noul", "noul": 0.1},
        "needs_research": {"type": "noul", "noul": 0.2},
        "needs_context_compaction": {"type": "noul", "noul": 0.7},
        "worker_mercury": {"type": "noul", "noul": 0.6},
        "escalation": {"type": "choice", "choice": "tool", "confidence": 0.7,
                       "probabilities": {"tool": 0.7}},
    }
    a.update(over)
    return {"answers": a, "usage": {"input_tokens": 1400}}


class ParseTests(unittest.TestCase):
    def test_well_formed(self):
        d, det, flags, errs = ad.interpret(good_answers())
        self.assertEqual(errs, [])
        self.assertEqual(d["needs_tool"], True)
        self.assertEqual(d["tool_type"], "shell")
        self.assertEqual(d["worker_candidate"], "mercury")
        self.assertEqual(d["escalation"], "tool")
        self.assertTrue(0 < d["overall_confidence"] <= 1)
        self.assertEqual(det["needs_tool_p"], 0.9)

    def test_unknown_choice_is_dropped_not_passed_on(self):
        d, _, _, errs = ad.interpret(good_answers(
            tool_type={"choice": "rm -rf /", "confidence": 1.0}))
        self.assertIsNone(d["tool_type"])
        self.assertTrue(any("tool_type" in e for e in errs))

    def test_garbage_and_out_of_range(self):
        d, _, _, errs = ad.interpret(good_answers(
            needs_tool={"noul": "yes"}, needs_research={"noul": 1.7}, escalation=None))
        self.assertIsNone(d["needs_tool"])
        self.assertIsNone(d["needs_research"])
        self.assertIsNone(d["escalation"])
        self.assertEqual(len(errs), 3)

    def test_empty_response(self):
        d, _, _, errs = ad.interpret({})
        self.assertIsNone(d["overall_confidence"])
        self.assertEqual(len(errs), 7)

    def test_consistency_flags(self):
        _, _, flags, _ = ad.interpret(good_answers(
            needs_tool={"noul": 0.1}, needs_subagent={"noul": 0.9}))
        self.assertEqual(flags, ["needs_tool=false but tool_type!=none"])   # subagent/main is not a contradiction

    def test_questions_are_one_batch_with_state_shape_of_simple(self):
        self.assertEqual(len(ad.QUESTIONS), 7)
        self.assertEqual(set(ad.QUESTIONS["tool_type"]["criteria"]), set(ad.TOOL_TYPES))
        self.assertEqual(set(ad.QUESTIONS["escalation"]["criteria"]), set(ad.ESCALATIONS))


def rec(**kw) -> dict:
    base = {"sessionId": "s", "timestamp": "2026-09-23T10:00:00.000Z"}
    base.update(kw)
    return base


def human(text, ts):
    return rec(type="user", origin={"kind": "human"}, timestamp=ts,
               message={"role": "user", "content": text})


def tool_use(name, inp, ts, sidechain=False):
    return rec(type="assistant", timestamp=ts, isSidechain=sidechain,
               message={"role": "assistant", "content": [
                   {"type": "tool_use", "id": "t", "name": name, "input": inp}]})


def tool_result(n_chars, ts):
    return rec(type="user", timestamp=ts, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "x" * n_chars}]})


def text_reply(ts):
    return rec(type="assistant", timestamp=ts,
               message={"role": "assistant", "content": [{"type": "text", "text": "ok"}]})


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.jsonl"
        lines = [
            human("first task", "2026-09-23T10:00:00Z"),
            tool_use("Skill", {"skill": "dataviz"}, "2026-09-23T10:00:01Z"),
            tool_use("Bash", {"command": "cd x && cat a.txt | head"}, "2026-09-23T10:00:02Z"),
            tool_result(30_000, "2026-09-23T10:00:03Z"),
            tool_use("Bash", {"command": "gh pr view 3"}, "2026-09-23T10:00:04Z"),
            tool_use("Bash", {"command": "python src/mercury_client.py x"}, "2026-09-23T10:00:05Z"),
            tool_use("WebFetch", {"url": "https://example.com"}, "2026-09-23T10:00:06Z"),
            tool_use("Bash", {"command": "curl -s http://127.0.0.1:8787/health"}, "2026-09-23T10:00:06Z"),
            tool_use("Bash", {"command": "curl -s https://api.example.org/v1"}, "2026-09-23T10:00:06Z"),
            tool_use("Agent", {"subagent_type": "Explore", "prompt": "p"}, "2026-09-23T10:00:07Z"),
            tool_use("Read", {"file_path": "x"}, "2026-09-23T10:00:08Z", sidechain=True),
            tool_use("mcp__ccd_session__mark_chapter", {"title": "t"}, "2026-09-23T10:00:09Z"),
            rec(type="attachment", timestamp="2026-09-23T10:00:10Z",
                attachment={"type": "queued_command", "prompt": "also do this"}),
            tool_use("Edit", {"file_path": "x"}, "2026-09-23T10:00:11Z"),
            rec(type="user", origin={"kind": "task-notification"}, timestamp="2026-09-23T10:00:12Z",
                message={"role": "user", "content": "<task-notification>done</task-notification>"}),
            tool_use("Bash", {"command": "pytest"}, "2026-09-23T10:00:13Z"),
            human("second task", "2026-09-23T10:01:00Z"),
            text_reply("2026-09-23T10:01:05Z"),
            rec(type="user", isMeta=True, timestamp="2026-09-23T10:01:06Z",
                message={"role": "user", "content": "skill body, not a prompt"}),
        ]
        self.path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        self.turns = sc.turns(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def f(self, text):
        return sc.facts(self.turns[sc.prompt_hash(text)][0])

    def test_boundaries(self):
        self.assertEqual(len(self.turns), 3)   # two human prompts + one mid-turn; meta ignored
        self.assertEqual(self.f("first task")["boundary"], "human")
        self.assertEqual(self.f("also do this")["boundary"], "mid_turn")

    def test_first_turn_facts(self):
        f = self.f("first task")
        self.assertEqual(f["skills"], ["dataviz"])
        self.assertEqual(f["categories"].get("filesystem"), 1)   # cat|head via Bash
        self.assertEqual(f["categories"].get("github"), 1)       # gh via Bash
        self.assertEqual(f["categories"].get("web"), 2)           # WebFetch + external curl
        self.assertEqual(f["categories"].get("shell"), 2)         # loopback curl + mercury run
        self.assertTrue(f["subagent_used"])
        self.assertTrue(f["mercury_called"])
        self.assertEqual(f["meta_calls"], 2)                     # Skill + mark_chapter
        self.assertEqual(f["tool_calls"], 7)                     # sidechain Read not counted
        self.assertEqual(f["max_tool_output_chars"], 30_000)
        self.assertEqual(f["duration_s"], 9.0)

    def test_task_notification_ends_turn(self):
        f = self.f("also do this")
        self.assertEqual(f["tools"], {"Edit": 1})                # the post-notification pytest excluded

    def test_second_turn_no_tools(self):
        f = self.f("second task")
        self.assertEqual(f["tool_calls"], 0)
        self.assertTrue(f["responded"])

    def test_no_text_leaks_into_facts(self):
        blob = json.dumps([self.f(t) for t in ("first task", "also do this", "second task")])
        for secret in ("cat a.txt", "pr view", "example.com", "mercury_client", "skill body"):
            self.assertNotIn(secret, blob)


def facts(**over):
    f = {"boundary": "human", "responded": True, "interrupted": False, "skills": [],
         "skill_used": False, "tool_calls": 0, "meta_calls": 0, "tools": {}, "categories": {},
         "tool_type_dominant": None, "web_used": False, "web_calls": 0, "fs_calls": 0,
         "subagent_used": False, "subagent_calls": 0, "subagent_types": {},
         "mercury_called": False, "tool_output_chars": 0, "max_tool_output_chars": 0,
         "compaction_event": False, "duration_s": 10.0}
    f.update(over)
    return f


class JudgeTests(unittest.TestCase):
    cat = {"dataviz", "pdf"}

    def j(self, field, pred, **over):
        return rep.judge(field, pred, facts(**over), self.cat)[0]

    def test_needs_tool(self):
        self.assertEqual(self.j("needs_tool", False), rep.C)
        self.assertEqual(self.j("needs_tool", False, tool_calls=5), rep.I)
        self.assertEqual(self.j("needs_tool", False, tool_calls=1, fs_calls=1), rep.A)

    def test_tool_type_fs_shell_swap_is_ambiguous(self):
        kw = dict(tool_calls=5, categories={"shell": 4, "filesystem": 1}, tool_type_dominant="shell")
        self.assertEqual(self.j("tool_type", "shell", **kw), rep.C)
        self.assertEqual(self.j("tool_type", "filesystem", **kw), rep.A)
        self.assertEqual(self.j("tool_type", "web", **kw), rep.I)

    def test_subagent_and_research(self):
        self.assertEqual(self.j("needs_subagent", True, tool_calls=3), rep.I)
        self.assertEqual(self.j("needs_subagent", True, tool_calls=40), rep.A)
        self.assertEqual(self.j("needs_research", True, web_used=True), rep.C)
        self.assertEqual(self.j("needs_research", True, fs_calls=20), rep.A)

    def test_compaction_bands(self):
        self.assertEqual(self.j("needs_context_compaction", True, max_tool_output_chars=30_000), rep.C)
        self.assertEqual(self.j("needs_context_compaction", True, tool_output_chars=1_000), rep.I)
        self.assertEqual(self.j("needs_context_compaction", False, tool_output_chars=100_000,
                                max_tool_output_chars=15_000), rep.A)

    def test_mercury_is_never_ground_truth(self):
        self.assertEqual(self.j("worker_candidate", "mercury"), rep.N)
        self.assertEqual(self.j("worker_candidate", "mercury", mercury_called=True), rep.A)
        self.assertEqual(self.j("worker_candidate", "none", mercury_called=True), rep.A)
        self.assertEqual(self.j("escalation", "main_claude", mercury_called=True, tool_calls=30), rep.C)

    def test_escalation(self):
        self.assertEqual(self.j("escalation", "direct"), rep.C)
        self.assertEqual(self.j("escalation", "tool", tool_calls=2), rep.C)
        self.assertEqual(self.j("escalation", "tool", tool_calls=6), rep.A)
        self.assertEqual(self.j("escalation", "main_claude", tool_calls=30), rep.C)
        self.assertEqual(self.j("escalation", "mercury_then_claude"), rep.I)
        self.assertEqual(self.j("escalation", "mercury_then_claude", tool_calls=30), rep.A)

    def test_skill_outside_catalog_is_invisible(self):
        self.assertEqual(self.j("skill", "none", skills=["obsidian-worklog"]), rep.C)
        self.assertEqual(self.j("skill", "pdf", skills=["obsidian-worklog"]), rep.I)
        self.assertEqual(self.j("skill", "none", skills=["obsidian-worklog", "dataviz"]), rep.I)
        self.assertEqual(self.j("skill", "dataviz", skills=["obsidian-worklog", "dataviz"]), rep.C)

    def test_mid_turn_rows_are_ambiguous(self):
        row = {"decision": {"needs_tool": True, "skill": "none"}}
        out = rep.score_row(row, facts(boundary="mid_turn", tool_calls=3), self.cat)
        self.assertEqual(out["needs_tool"][0], rep.A)
        self.assertEqual(out["escalation"][0], rep.N)   # nothing predicted


class HookFailOpenTest(unittest.TestCase):
    def test_sidecar_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, JEV_SIDECAR_PORT="9", JEV_SHADOW_TIMEOUT="2",
                       JEV_SHADOW_LOG=str(Path(tmp) / "s.jsonl"),
                       JEV_AGENT_SHADOW_LOG=str(Path(tmp) / "a.jsonl"))
            payload = json.dumps({"session_id": "t", "cwd": "x",
                                  "prompt": "Проверь секретный текст промпта"}, ensure_ascii=False)
            p = subprocess.run([sys.executable, str(ROOT / "hooks" / "jev_shadow_hook.py")],
                               input=payload.encode("utf-8"), capture_output=True, env=env, timeout=30)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout, b"")
            s = (Path(tmp) / "s.jsonl").read_text(encoding="utf-8")
            a = (Path(tmp) / "a.jsonl").read_text(encoding="utf-8")
            self.assertIn('"shadow_skill"', s)
            self.assertIn('"shadow_agent_decision"', a)
            self.assertIn('"ok": false', a)
            self.assertNotIn("секретный", s + a)

    def test_agent_layer_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, JEV_SIDECAR_PORT="9", JEV_SHADOW_TIMEOUT="2", JEV_AGENT_SHADOW="0",
                       JEV_SHADOW_LOG=str(Path(tmp) / "s.jsonl"),
                       JEV_AGENT_SHADOW_LOG=str(Path(tmp) / "a.jsonl"))
            subprocess.run([sys.executable, str(ROOT / "hooks" / "jev_shadow_hook.py")],
                           input=b'{"prompt": "hi", "session_id": "t"}', capture_output=True,
                           env=env, timeout=30)
            self.assertTrue((Path(tmp) / "s.jsonl").exists())
            self.assertFalse((Path(tmp) / "a.jsonl").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
