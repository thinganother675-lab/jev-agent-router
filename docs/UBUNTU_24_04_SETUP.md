# Ubuntu 24.04 VPS setup

This guide assumes a fresh Ubuntu 24.04 VPS, SSH access with `sudo`, and a **private** GitHub repository. The sidecar needs outbound HTTPS to TypeSafe Jev. It does not need a public inbound port. Python 3.12 is Ubuntu 24.04's system Python; the code requires Python 3.11+ and uses only the standard library.

## 1. System and service account

```bash
sudo apt update
sudo apt install --no-install-recommends git python3 python3-venv ca-certificates curl
sudo useradd --system --user-group --home-dir /opt/jev-agent-router --shell /usr/sbin/nologin jev
sudo install -d -o jev -g jev -m 0750 /opt/jev-agent-router
```

Arrange read-only authentication to the private repository for the clone (for example, a narrowly scoped deploy key), then clone using the account you use for deployment. Do not paste a GitHub token into a clone URL or shell history. For a first clone with your own authenticated `gh` session, clone elsewhere and move the checked-out repository into `/opt/jev-agent-router`, then set ownership to `jev:jev`. Keep GitHub credentials out of the service account.

```bash
gh repo clone OWNER/jev-agent-router /tmp/jev-agent-router
sudo cp -a /tmp/jev-agent-router/. /opt/jev-agent-router/
sudo chown -R jev:jev /opt/jev-agent-router
sudo -u jev python3 -m venv /opt/jev-agent-router/.venv
```

Substitute the actual private repository name if `jev-agent-router-lab` was used. There is no `pip install`: runtime dependencies are Python's standard library. Do not copy the Windows `.env`, telemetry, `results/`, `.claude/`, `.codex/` or session files.

## 2. Secret file outside Git

Create `/etc/jev-sidecar.env` interactively with a privileged editor. Put only the needed assignments in it, such as `JEV_API_KEY=...` and optionally `MERCURY_API_KEY=...`. Do not paste the values into documentation or terminal output. The Jev service only requires its Jev key; Mercury is optional and currently runs as a separate worker.

```bash
sudo install -o jev -g jev -m 0600 /dev/null /etc/jev-sidecar.env
sudoedit /etc/jev-sidecar.env
sudo chown jev:jev /etc/jev-sidecar.env
sudo chmod 0600 /etc/jev-sidecar.env
```

The code supports legacy `jev_api` and `mercury_api` as well as `TYPESAFE_API_KEY`, `JEV_API_KEY`, `INCEPTION_API_KEY`, and `MERCURY_API_KEY`. A new VPS should use the uppercase names in `.env.example`. The legacy names take priority when both are set.

## 3. Install and start systemd

```bash
sudo cp /opt/jev-agent-router/deploy/systemd/jev-sidecar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jev-sidecar.service
sudo systemctl status jev-sidecar.service
```

The unit uses a dedicated unprivileged user, a read-only checkout, `EnvironmentFile=/etc/jev-sidecar.env`, loopback binding, restart on failure, and journald stdout/stderr. Check health without exposing any prompt:

```bash
curl --fail --silent http://127.0.0.1:8787/health
sudo journalctl -u jev-sidecar.service -n 50 --no-pager
```

Then make one benign Jev smoke request (this calls the paid external API):

```bash
curl --fail --silent --show-error \
  -H 'X-Jev-Sidecar: 1' -H 'Content-Type: application/json' \
  -d '{"prompt":"Classify a simple documentation task"}' \
  http://127.0.0.1:8787/decide
```

Avoid putting real prompts in shell history or journal. The example is synthetic. A successful `/health` alone does not prove the API key or outbound network is working.

## 4. Network, logs and restart

No UFW rule is needed for port 8787 because the process binds only to `127.0.0.1`. Keep the VPS firewall limited to your existing SSH and intentionally exposed services; do not add an inbound rule or reverse proxy for Jev. Codex CLI and Hermes must call the local address on the same host. If an agent runs on another machine, use an authenticated private transport designed for that case, rather than changing the sidecar bind address.

```bash
sudo journalctl -u jev-sidecar.service -f
sudo systemctl restart jev-sidecar.service
sudo systemctl stop jev-sidecar.service
```

The unit's `ProtectHome=true` means the service cannot read files under `/home`; keep the checkout under `/opt` and the secret under `/etc`. `ProtectSystem=strict` makes the service filesystem read-only; this works because `jevd` writes no files. Do not point its working directory or environment file into a protected home directory. Consumer telemetry is separate and needs its own writable location and retention policy.
