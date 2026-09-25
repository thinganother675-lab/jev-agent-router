# Windows to Ubuntu 24.04 migration checklist

Target: a Germany-hosted Ubuntu 24.04 VPS with Codex CLI, Hermes Agent and a Telegram gateway. This checklist moves the Jev **code and documented findings**, not local Windows state or private datasets.

1. Confirm the new GitHub repository is private and clone it to the VPS. Use a deploy key or authenticated user outside the service account.
2. Create the unprivileged `jev` user and place the checkout at `/opt/jev-agent-router`.
3. Create its Python 3.12 virtual environment. No third-party Python package install is required.
4. Create `/etc/jev-sidecar.env` outside Git with `JEV_API_KEY`; set owner `jev:jev` and mode `0600`.
5. Install `deploy/systemd/jev-sidecar.service`, reload systemd and start the unit.
6. Check `GET /health`, then send one synthetic `/decide` smoke request with the guard header.
7. Confirm port 8787 is loopback-only and no firewall or proxy exposes it.
8. Connect the Codex CLI consumer in shadow mode after verifying its supported hook version and trust flow. Keep its telemetry separate.
9. Connect Hermes through a local adapter that turns Jev typed decisions into recommendations; keep local policy in charge. The Telegram bot remains managed by Hermes.
10. If Mercury is used, configure its key separately and first test worker/compaction tasks with schema-shaped output.
11. Review several days of shadow telemetry, error rates, latency and privacy before considering active routing. Active routing is intentionally disabled now.

Do not migrate `.env`, API keys from Windows, `.claude/`, `.codex/`, `.agents/`, Obsidian/worklog, private prompts, raw telemetry, results, session transcripts, rollout JSONL, caches, runtime backups or sandbox evidence. Create fresh VPS keys/configuration through the relevant providers. The existing research conclusions are summarized in `docs/BENCHMARKS.md`; raw experiment files remain on the development machine.

## Rollback

```bash
sudo systemctl disable --now jev-sidecar.service
```

Disable the Codex/Hermes/Claude shadow adapter configuration and let each agent use its normal path. A missing sidecar should fail open; verify this with a synthetic task. Keep the checked-out version and `/etc/jev-sidecar.env` for a controlled retry, or revoke the VPS key if the host is being retired. Do not expose the secret in rollback logs. To restore a prior code revision, stop the service, check out the known revision from the private repository, and start the service again; recheck `/health` and one synthetic `/decide` call.
