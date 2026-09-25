# Codex CLI integration

## Windows development status

The `codex_hook_lab` work reached registration of a trusted project-local no-op `UserPromptSubmit` hook, but did not independently observe its execution or a Jev request. The lab also encountered sandbox ACL setup failures around protected `.codex`/`.agents` paths, a stale long `cua_node` cache path, and Job Object / `--no-daemon` investigation. Those are Windows environment findings, not evidence that Linux has the same problems. The lab's runtime state and evidence are deliberately excluded from this repository.

## Ubuntu proposal

```text
Codex CLI → supported UserPromptSubmit hook → 127.0.0.1:8787 jevd
                                          ↘ Codex-only shadow telemetry
```

1. Install and authenticate Codex CLI under its own user on the VPS. Verify the CLI version's current hook documentation and `/hooks` trust review before registering anything.
2. Start `jev-sidecar.service` and check `/health` plus a synthetic guarded `/decide` request.
3. Create a small Codex-specific hook adapter (still to be implemented and tested). It should parse UTF-8 hook input, redact or minimize the prompt according to the chosen privacy policy, call Jev with a short timeout, write Codex-only telemetry outside Git, print nothing and exit 0 on every failure.
4. Register it at project scope in a local `.codex/hooks.json` or the currently supported mechanism, then observe a real hook invocation with a harmless synthetic prompt. Keep `.codex/` ignored in this repository; any shareable config belongs under `examples/`.
5. Compare shadow decisions with actual Codex actions before allowing local policy to use a recommendation. Do not let Jev execute shell commands or turn a recommendation into an automatic subagent/tool invocation.

Codex hook support and trust behavior can change. [Official OpenAI hook documentation](https://learn.chatgpt.com/docs/hooks) confirms `UserPromptSubmit`, the `prompt` input and `async` handlers; recheck the installed CLI version on the VPS. The proposal above is **experimental**, and no Codex adapter is shipped as production-ready code. Keep HOH and other real workflows unchanged until the hook and privacy path are measured.
