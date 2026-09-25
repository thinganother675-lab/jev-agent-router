# Architecture

`jevd` (`src/jev_sidecar.py`) is a small Python standard-library HTTP server. It listens only on `127.0.0.1:8787` by default and keeps warm HTTPS connections to TypeSafe Jev. A fresh hook process can call loopback instead of repeating an upstream TLS handshake. It has no framework, database, background task or persistence requirement.

```text
consumer hook or adapter ──POST /decide──────┐
                                             ├── jevd ── TypeSafe Jev
consumer hook or adapter ──POST /agent_decide┘
                              ↓
                    local policy and telemetry
```

`/decide` uses the frozen SIMPLE skill catalog choice and task complexity. `/agent_decide` asks a separate batch of typed questions about agent work shape. The two endpoints have independent locks and HTTPS connections; calls to the same endpoint queue. A consumer combines the replies locally. Jev's answer is advisory. Local policy owns skill loading, model choice, tool execution, escalation and any final action.

The server's `GET /health` reports process counters without calling Jev. POST endpoints require `X-Jev-Sidecar: 1` and reject an `Origin` header. The application does not expose a non-loopback bind option. An upstream failure is a defer/fail-open signal to the consumer; consumers must handle malformed or missing responses as no recommendation.

## Multi-consumer findings

The existing stress and live audit found one sidecar suitable for Claude plus Codex at ordinary human pace (typically 1–3 prompts in flight). About six simultaneous prompts across both endpoints were a practical ceiling in that audit, not a capacity guarantee. Overload and upstream retries can exhaust a consumer's 8-second timeout. A second sidecar is unnecessary for this use case.

Keep each consumer's telemetry separate. On Windows, two processes appending to one JSONL file lost lines in a stress test. On a VPS, prefer journald for service events and a per-consumer SQLite database with transactions for durable decision telemetry. For an initial shadow rollout, separate per-consumer JSONL files written by one process each are simpler; rotate them and keep them outside Git. Never rely on cross-process JSONL append as a durable queue.

The sidecar itself does not write telemetry or prompt text. The Claude hook logs prompt hashes and metadata by default, with prompt capture explicitly off. See [security](SECURITY.md) and [Claude integration](CLAUDE_INTEGRATION.md).
