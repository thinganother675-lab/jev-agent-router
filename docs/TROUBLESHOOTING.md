# Troubleshooting

| Symptom | Check |
| --- | --- |
| `/health` fails | `systemctl status jev-sidecar` and `journalctl -u jev-sidecar -n 50`; check the unit path, virtual environment and port conflict. |
| `/health` works but `/decide` fails | Confirm `/etc/jev-sidecar.env` has one supported Jev key and outbound HTTPS to TypeSafe works. Do not print the key. |
| POST returns 403 | Add `X-Jev-Sidecar: 1`; do not send an `Origin` header. |
| Slow or timed out hook | Check upstream network/proxy and sidecar counters. Requests to the same endpoint queue behind one lock; retries can take longer than an 8-second hook timeout. |
| No shadow telemetry | Verify hook registration/trust, environment path permissions, and that the sidecar is running. Claude and Codex must use separate log paths. |
| Windows sandbox setup fails | Consult local lab findings; `.codex`/`.agents` permissions and stale runtime cache were Windows-specific. Do not apply those fixes blindly on Ubuntu. |
| Service cannot read secret | Check ownership/mode of `/etc/jev-sidecar.env` and that `User=jev` matches. |

The sidecar is intentionally fail open from the agent's point of view. A health check verifies liveness only; a synthetic guarded POST verifies the upstream decision path. Do not place real prompts in diagnostic commands or issue reports.
