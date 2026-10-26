"""Offline checks of pilot isolation and failure behavior; no external requests."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pilot_hook", ROOT / "hooks/pilot_shadow_hook.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class PilotTests(unittest.TestCase):
    def invoke(self, payload, ready=True, consumer="codex", scope="project"):
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")), encoding="utf-8")
        records = []
        def fake_ask(url, prompt, out):
            out["data"] = {"ok": True, "error": None, "skill": "none", "complexity": "trivial"}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "argv", ["hook", "--consumer", consumer, "--scope", scope]), \
             patch.object(hook, "ensure_running", return_value=ready) as start, \
             patch.object(hook, "ask_sidecar", side_effect=fake_ask) as ask, \
             patch.object(hook, "write_line", side_effect=lambda row, path: records.append((row, path))), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(hook.main(), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        for row, _ in records:
            self.assertNotIn("prompt", row)
            self.assertNotIn(payload.get("prompt", "UNUSED"), json.dumps(row, ensure_ascii=False))
        return [(row, path) for row, path in records if row["type"] != "hook_received"], start.call_count, ask.call_count

    def payload(self, **extra):
        return dict(cwd=str(ROOT), session_id="test-session", hook_event_name="UserPromptSubmit",
                    prompt="Привет — synthetic private text", model="test-model", **extra)

    def test_other_project_cannot_send(self):
        data = self.payload()
        data["cwd"] = str(ROOT.parent / "different-project")
        self.assertEqual(self.invoke(data), ([], 0, 0))

    def test_other_event_cannot_send(self):
        data = self.payload()
        data["hook_event_name"] = "Stop"
        self.assertEqual(self.invoke(data), ([], 0, 0))

    def test_user_scope_observes_other_project(self):
        data = self.payload()
        data["cwd"] = str(ROOT.parent / "different-project")
        rows, starts, calls = self.invoke(data, scope="user")
        self.assertEqual((starts, calls), (1, 2))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["scope"] == "user" for row, _ in rows))

    def test_user_scope_respects_kill_switch(self):
        with patch.object(Path, "exists", return_value=True):
            self.assertEqual(self.invoke(self.payload(), scope="user"), ([], 0, 0))

    def test_unreachable_is_silent_and_logged(self):
        records, starts, calls = self.invoke(self.payload(), ready=False)
        self.assertEqual((starts, calls), (1, 0))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(row["ok"] is False for row, _ in records))

    def test_no_prompt_storage_and_consumer_isolation(self):
        data = self.payload()
        codex, _, calls = self.invoke(data)
        claude, _, _ = self.invoke(data, consumer="claude")
        self.assertEqual(calls, 2)
        self.assertEqual(len(codex), 2)
        self.assertNotEqual(codex[0][1], claude[0][1])
        for row, path in codex + claude:
            self.assertNotIn("prompt", row)
            self.assertNotIn(data["prompt"], json.dumps(row, ensure_ascii=False))
            self.assertEqual(row["model"], "test-model")
            self.assertEqual(row["mode"], "shadow")
            self.assertEqual(row["session_id"], data["session_id"])
            self.assertTrue(row["ok"])

    def test_session_id_cannot_escape_log_directory(self):
        data = self.payload()
        data["session_id"] = "../../outside"
        records, _, _ = self.invoke(data)
        for _, path in records:
            self.assertEqual(path.parent, ROOT / "telemetry/pilot/codex")

    def test_kill_switch_prevents_calls(self):
        with patch.object(Path, "exists", return_value=True):
            self.assertEqual(self.invoke(self.payload()), ([], 0, 0))


if __name__ == "__main__":
    unittest.main()
