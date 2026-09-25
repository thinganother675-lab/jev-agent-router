# Windows development notes

Use Python 3.11+ from PowerShell. Keep real keys in the local `.env` file, which Git ignores. Run `python src/jev_sidecar.py --port 8787`, then query `http://127.0.0.1:8787/health`. A guarded `/decide` POST is the API smoke check. The project has no third-party runtime package dependencies.

Claude Code shadow mode was tested here. Its project-local hook reads UTF-8 bytes explicitly, calls the sidecar, and writes separate `telemetry/shadow.jsonl` and `telemetry/agent_shadow.jsonl` files. It does not inject context or enable active routing. Keep consumer logs separate: concurrent processes appending to the same JSONL lost lines in the Windows stress test.

The Codex lab found Windows-specific sandbox ACL and `.codex`/`.agents` protection interactions. A stale long `cua_node` runtime cache path also caused setup failure; Job Object behavior and `--no-daemon` were part of local investigation. These are development-machine observations, **not Linux requirements**. No Codex hook execution or Jev call was conclusively verified in that lab. Do not copy the lab's `.lab_session_home`, runtime backups, protected directories, or diagnostic evidence to the VPS.

For the Linux path, see [Ubuntu setup](UBUNTU_24_04_SETUP.md) and [Codex integration](CODEX_INTEGRATION.md).
