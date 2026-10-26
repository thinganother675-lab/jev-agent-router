# Global Windows Jev shadow observation — 2026-10-21

This supersedes the project-only scope in SHADOW_PILOT.md. The owner explicitly authorized persistent transmission of all new local Claude/Codex prompt texts, including confidential content, to TypeSafe Jev.

## Active configuration

- Claude: `C:\Users\User\.claude\settings.json` calls the existing Python interpreter and `hooks/pilot_shadow_hook.py --consumer claude --scope user` directly.
- Codex: `C:\Users\User\.codex\hooks.json` calls `hooks/jev_codex.cmd`, which invokes the same adapter with `--consumer codex --scope user`. The native `/hooks` browser reviewed and trusted this exact definition.
- The former Jev handlers in jev_test's project settings were removed to avoid duplicate calls. Existing portfolio hooks remain unchanged.
- One shared loopback service on 127.0.0.1:8787 starts on demand. No scheduled task or startup service is installed. Runtime/source files must remain in `C:\sarychev\Codex\jev_test`.
- Every new accepted prompt makes one `/decide` and one `/agent_decide` request to the sidecar, which calls `https://api.typesafe.ai/v1/systemone`. Existing upstream reconnect behavior is unchanged. TypeSafe billing is separate from Claude/Codex subscriptions.
- Recommendation application remains off. The hook produces no stdout context, recommendation, tool call or blocking decision. Failure does not stop the agent.
- Logs remain in `telemetry/pilot/<consumer>/<session>.*.jsonl`. Names are retained for continuity; `scope: user` distinguishes the global records. Prompt text is not persisted by this adapter or its optional collector. Native clients maintain their own transcripts.

## Existing chats

An already-open Codex CLI session, created before installation, picked up the user-level hook after native `/hooks` review and then generated successful Jev telemetry on its next synthetic prompt, without restarting that session. New sessions use the same trusted user hook. A pre-existing Claude test conversation was resumed and successfully called Jev through the user-level hook. Global telemetry also observed successful calls from real Claude sessions in ai_lawyer and lazer without messaging or restarting those sessions.

The currently executing turn cannot be processed retroactively. Already-open clients that retain older hook settings may need native `/hooks` review/reload or session resume. No unrelated running processes were restarted. Local Windows hooks do not automatically configure cloud sessions, remote SSH executors or other machines. Individual client modes that deliberately disable hooks remain outside this mechanism.

Claude Code generation still returns an organization subscription restriction; successful Jev hooks do not establish that Opus generation is available.

## Verification

- 29 offline tests passed (21 existing plus 8 adapter tests), including explicit user scope outside the pilot, project default isolation, kill switch, separate logs, path safety and no raw prompt storage.
- Global Claude new-session evidence: session f11b0345-3395-4b1c-a6ca-8e2f567fdb1a, outside jev_test; both endpoint decisions successful.
- Resumed Claude evidence: original pilot session 96728295-c75f-4a79-bdb5-c0950323dec7, now `scope: user`; both decisions successful.
- Already-open Codex evidence: session 01a15e6f-1541-70c3-a513-28da260aa410, turn 01a15e72-fae4-71b2-981a-ff84304473c5, outside jev_test.
- New Codex evidence: session 01a15e74-71d0-7e41-a892-0d46a05e41ba, turn 01a15e74-9521-7ec2-a8ab-bbad5b5a261d, in jev_test, one global handler rather than a global/project duplicate.
- Windows invocation differs: direct quoted Python is verified for Claude; the explicit cmd runner is verified for Codex. An initial cmd runner in Claude failed before contacting Jev and was replaced with the previously proven direct command.

## Pause, status and rollback

Run `scripts/shadow_pilot_status.py` with the configured Python to inspect sanitized counters. Create `telemetry/pilot/DISABLED` to stop new calls and on-demand startup; remove exactly that flag to resume. Existing in-flight calls are not cancelled and an idle sidecar is not stopped by the flag.

Pre-global copies of both user hook configurations and both project configurations are in `telemetry/pilot/backups/20261021-global/`. To uninstall, remove only the Jev handlers from the user configs; preserve unrelated settings/hooks. Restoring the full saved configs also reverts any later unrelated changes, so targeted rollback is preferable. No credentials were copied or changed. No commit or push was made.

- Live-load limit: one Claude prompt produced two client timeout records at the 8-second hook HTTP limit. The adapter failed open and did not retry. Sidecar upstream error counters remained zero when inspected. Do not describe global delivery as lossless; individual shadow observations can be missed.
