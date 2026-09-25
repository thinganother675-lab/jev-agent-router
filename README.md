# Jev agent router

A local sidecar and decision layer around [TypeSafe Jev](https://api.typesafe.ai/) for agent workflows. Jev is a **decision model**, not the main generative LLM. It returns typed recommendations; local policy decides whether to act.

```text
Claude Code / Codex CLI / Hermes Agent
                  ↓
         local Jev sidecar (jevd)
                  ↓
             TypeSafe Jev
                  ↓
     typed decisions → local policy
                  ↓
     agent, tool or worker chosen locally
```

The current implementation supports skill selection and task complexity through `/decide`, plus shadow predictions for tools, research, subagents, context compaction and a Mercury worker candidate through `/agent_decide`. Future model/backend routing belongs in local policy after validation. Jev never executes shell commands or tools.

## Current status

- Claude Code shadow integration was tested. The hook records decisions without changing the agent's behavior.
- The SIMPLE skill router reached about **95% acceptable accuracy on this project's specific holdout and skill catalog**. This does not establish superiority over other routers in general.
- Mercury 2.5 was tested as a worker and compactor; it is not the selected primary router.
- One sidecar was tested with multiple consumers. Claude and Codex should use separate telemetry destinations.
- Codex hook integration remains experimental. Windows hook execution was not conclusively observed in the lab.
- **Active routing is disabled by default.** All integrations should start in shadow mode and fail open.

## Run locally

Requires Python 3.11 or newer. Runtime code uses the Python standard library; there is no package install step.

```bash
cp .env.example .env
# Fill one supported Jev key variable in .env; never commit it.
python3 src/jev_sidecar.py --port 8787
curl http://127.0.0.1:8787/health
curl -sS -H 'X-Jev-Sidecar: 1' -H 'Content-Type: application/json' \
  -d '{"prompt":"Summarize a small change"}' http://127.0.0.1:8787/decide
```

`/health` checks the process only; the POST is the upstream Jev smoke check and consumes API credit. The server binds `127.0.0.1` regardless of the host environment. It also offers `/agent_decide` and a guarded `/quit`; use `systemctl stop` on a VPS.

Jev accepts `jev_api`, `TYPESAFE_API_KEY`, or `JEV_API_KEY` in that priority order. Mercury accepts `mercury_api`, `INCEPTION_API_KEY`, or `MERCURY_API_KEY`. Use `JEV_API_KEY` and `MERCURY_API_KEY` for a new deployment, and avoid setting competing names. The existing legacy names remain supported. On Linux, put secrets in `/etc/jev-sidecar.env` rather than the Git checkout.

## Repository map

| Path | Purpose |
| --- | --- |
| `src/` | Jev and Mercury clients, typed routers and loopback sidecar |
| `hooks/` | Claude Code shadow hook and opt-in local prompt collector |
| `scripts/` | Probes and local shadow telemetry analysis |
| `tests/` | Offline unit and optional live smoke tests |
| `docs/` | Architecture, integrations, security and deployment guides |
| `deploy/systemd/` | Ubuntu sidecar service template |

Start with [architecture](docs/ARCHITECTURE.md), [security](docs/SECURITY.md), and [Ubuntu setup](docs/UBUNTU_24_04_SETUP.md). For migration use [the VPS checklist](docs/VPS_MIGRATION.md). Benchmark results and their limits are in [benchmarks](docs/BENCHMARKS.md).

This private research repository carries no license grant. Raw prompts, telemetry, credentials, generated results, local runtime state and Windows diagnostic backups stay outside Git.
