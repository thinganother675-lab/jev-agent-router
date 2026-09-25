# Hermes Agent integration proposal

```text
Telegram → Hermes gateway/Agent → local Jev sidecar → typed recommendation
                                     ↓
                              Hermes local policy
                              ├─ Hermes direct action
                              ├─ Codex CLI
                              ├─ Qwen / LM Link
                              ├─ Mercury worker
                              └─ future agent
```

Hermes owns Telegram authentication, conversation state, permissions and actual tool dispatch. Jev is an advisory classifier. A Hermes adapter should send a redacted task description to `/decide` and, if useful, `/agent_decide`; validate the typed response; then let an explicit **local policy** map it to an allowed action. Do not let a Jev response directly run shell commands, choose arbitrary executable names, or broaden Hermes tool permissions.

Start with shadow logging only. Record recommendation, chosen action, latency, errors and a nonreversible prompt hash in Hermes-only telemetry outside Git. Keep raw Telegram text out of telemetry by default. If Jev fails, Hermes continues its normal route. Add allowlists and confidence gates only after observing real tasks, costs and privacy tradeoffs. Mercury is a candidate for bounded worker tasks such as compaction and extraction; Qwen/LM Link and Codex are future local-policy targets, not implemented automatic routes.

Possible integration points depend on the installed Hermes version. Its [event hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks) document gateway hooks (Telegram and other messaging surfaces), plugin hooks that also work in CLI, and shell hooks. Choose the narrowest hook that receives the needed input without intercepting every tool call. Gateway hooks are loaded from the Hermes profile and are not part of this repository's active configuration. Recheck the installed version before deploying an adapter. The [Telegram guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram) covers the gateway's own bot setup and allowlist; Jev needs no Telegram token.
