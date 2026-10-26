> Superseded scope: the owner enabled [global local Windows shadow hooks](GLOBAL_SHADOW.md) later on 2026-10-21. The project-only configuration below is historical.

# Windows shadow pilot — 2026-10-21

The approved pilot is limited to `C:\sarychev\Codex\jev_test` and to recommendations written to local telemetry. It does not inject context, select skills, invoke tools/subagents, change models, or apply any recommendation. The old skill catalog is intentionally retained; it is not a validated catalog for today's clients.

## What runs

- Claude Code: project `.claude/settings.json` → `hooks/pilot_shadow_hook.py --consumer claude`.
- Codex: project `.codex/hooks.json` → `hooks/pilot_codex.cmd` → the same Python adapter with `--consumer codex`.
- The adapter rejects other working directories and events. It starts one loopback sidecar on demand, under a cross-process startup lock. No Windows service, startup task or global hook is installed.
- Each accepted prompt makes one `/decide` and one `/agent_decide` request concurrently. The existing upstream client's reconnect retry is unchanged; the hook itself does not retry.
- Prompts in this project are sent to TypeSafe for classification. The adapter does not persist raw prompts and does not call the optional prompt collector. Native Claude/Codex transcripts follow the clients' own settings.
- Logs are under `telemetry/pilot/<claude|codex>/<session>.*.jsonl`, with event receipts, hashes, client/session metadata, decisions and success/failure. Runtime logs and all telemetry remain Git-ignored.
- Hook HTTP timeout: 8 seconds. Host hook timeout: 20 seconds. Failures never return a blocking decision or recommendation to the agent.

## Use

Open Claude Code or a new local Codex chat with the working directory exactly `C:\sarychev\Codex\jev_test`. Existing chats in another directory are outside this pilot. The sidecar starts on the first prompt if absent.

The current Codex command was reviewed and trusted through `/hooks`. If its definition changes, review the new definition there again. No hook-trust bypass was used. Claude's normal project settings load its observer automatically.

From the project directory, inspect health and sanitized telemetry counts:

```powershell
& 'C:/Users/User/AppData/Roaming/uv/python/cpython-3.11-windows-x86_64-none/python.exe' scripts/shadow_pilot_status.py
```

Manual startup, if wanted:

```powershell
& 'C:/Users/User/AppData/Roaming/uv/python/cpython-3.11-windows-x86_64-none/python.exe' scripts/sidecar_runtime.py
```

The existing `.env` remains the key source. Subscription authentication for Claude/Codex is independent of TypeSafe Jev API billing. Cost fields use the existing project's price constant and are estimates, not a billing reconciliation.

## Pause and rollback

Create `telemetry/pilot/DISABLED` to prevent new pilot calls and on-demand starts. Delete that exact flag to resume. This does not cancel already-running calls or stop an idle sidecar.

To uninstall, remove only the Jev handler from the two project configuration files. The original Claude configuration is backed up at `telemetry/pilot/backups/20261021/claude.before.json`; no Codex project hook file existed before this pilot. Unrelated/global portfolio hooks were not changed. Codex's user config gained only the trust record through its own hook browser.

## Verification and limits

- Claude Code 2.1.301 initialized `claude-opus-5-5`; its real UserPromptSubmit hook made two successful Jev calls. Session `96728295-c75f-4a79-bdb5-c0950323dec7`.
- Claude generation was refused by the provider: organization subscription access to Claude Code is disabled. Hook/Jev success does not mean the Opus response succeeded. No new API key, login, organization or billing change was made.
- Codex CLI 0.163.0-alpha.11, `gpt-6.1-sol`, returned the synthetic sentinel. Both Jev endpoints succeeded with the same real session ID, `01a15dd2-4fbd-7aa3-be72-14df4300b741`, and turn metadata.
- A quoted Python executable command worked in Claude but did not produce Jev telemetry in Codex. The explicit `.cmd` adapter resolved Codex invocation; its final definition was trusted through the native browser.
- 21 existing offline tests and 6 pilot isolation/failure tests passed. The pilot tests cover a foreign project/event, kill switch, silent failure, no raw prompt storage, per-client paths and session path traversal.
- These checks validate transport and isolation, not recommendation quality or performance improvement. Recommendation application remains a separate future decision.

- On-demand recovery was verified by stopping only the pilot process (PID 17560) and submitting a real Codex prompt. The hook started PID 28508 and both Jev endpoints succeeded. Final pilot telemetry: Claude 2/2 and Codex 4/4 successful decision records, zero decision failures.
