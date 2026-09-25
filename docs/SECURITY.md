# Security and privacy

## Data boundary

TypeSafe Jev receives the `state` text that a client sends to `/decide` or `/agent_decide`; the current sidecar forwards the submitted prompt. Do not send API keys, passwords, tokens, private files or credentials. A client can instead send a redacted, minimal task description, but reducing context may reduce decision quality. Full prompts give the router more context while exposing more text to the external service. Choose that tradeoff per consumer before enabling its hook.

The sidecar binds only to `127.0.0.1`, requires `X-Jev-Sidecar: 1` on POST and rejects requests carrying `Origin`. This guards against accidental browser-origin calls but does not authenticate other local processes. Restrict local user access and do not publish or reverse-proxy port 8787. The host firewall should not allow inbound traffic to it.

Jev replies are untrusted recommendations. Parse expected typed values and let local policy decide the permitted action. Never feed a Jev field directly into a shell, tool name, file path or command. On errors, timeouts, missing fields or ambiguous answers, use the normal agent workflow (fail open). Active routing remains off by default.

## Secrets and telemetry

The local `.env` is ignored by Git. On a VPS, store keys in `/etc/jev-sidecar.env`, owned by the service user with mode `0600`; do not put them in the unit, command line, chat transcript or repository. The code accepts `jev_api`, `TYPESAFE_API_KEY`, `JEV_API_KEY` and, for optional Mercury work, `mercury_api`, `INCEPTION_API_KEY`, `MERCURY_API_KEY`. Rotate/revoke keys through their providers if a key is exposed, then replace the local value and restart the service. Avoid putting both legacy and recommended names in one environment because the legacy names take precedence.

Telemetry belongs outside Git and should be separate for Claude, Codex and Hermes. The Claude shadow hook stores hash, size and decision metadata by default; `JEV_SHADOW_LOG_PROMPTS=1` explicitly enables prompt storage and should stay unset. The opt-in collector also creates private benchmark prompts; keep it off on a VPS unless there is a reviewed need. Restrict telemetry file permissions, set retention, and redact any external export. Session transcripts, rollout JSONL, worklogs and raw benchmark results are excluded from this repository.

Service stdout/stderr go to journald. Avoid printing prompts or response bodies into the journal. `/health` contains counters, model and last error; keep it local. The TypeSafe API and optional Mercury API are external services, so their own retention and processing terms apply.
