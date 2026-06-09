# Development Notes

## 2026-06-09 — Dahua intercom service added

New service `intercom/` wraps the Dahua VTH2622GW-W intercom via a FastAPI HTTP API, exposing door unlock, camera stream, and doorbell events for integration with Home Assistant, n8n, iOS Shortcuts, and Tasker.

### Architecture

- `dahua_client.py` — DHIP protocol client (port 5000, direct LAN connection to VTH)
- `main.py` — FastAPI app with `/unlock`, `/stream`, `/events`, `/health` endpoints
- Exposed at `https://intercom.app.vanheerden.ch` via Traefik
- Auth: `X-API-Key` header required on all requests
- Traefik middlewares: rate limit (10 req/min) + IP allowlist (Tailscale + home LAN)

### Credentials

All secrets in `intercom/.env` (gitignored, on NAS share). API key stored in 1Password.

### Current state

- `/unlock` — implemented and working (DHIP `UnlockManager.openDoors`)
- `/stream` — stub, returns 501 (pending `dos_stream.py` promotion from `dahua-research` repo)
- `/events` — stub, returns 501 (pending DHIP event subscription implementation)

### Status

- `/unlock` endpoint reachable at `https://intercom.app.vanheerden.ch/unlock` with valid Let's Encrypt cert
- `/health` confirmed working
- API key configured (stored in 1Password)

### Next steps

- [ ] Test `/unlock` — confirm which param format the VTH accepts: `{"channel": 1}` or `{"DoorIndex": 0}`, then remove the losing branch from `dahua_client.py:open_doors()`
- [ ] Implement `/stream` — promote `dos_stream.py` from `dahua-research` into `dahua_client.get_stream_url()`
- [ ] Implement `/events` — DHIP event subscription for doorbell/motion SSE stream
- [ ] Wire up Home Assistant `rest_command` for unlock
- [ ] Wire up n8n HTTP Request node
- [ ] Wire up iOS Shortcut

## 2026-06-09 — Traefik upgrade (v3.0 → v3.7)

**Root cause:** Docker was updated to 29.x which raised the minimum API version to 1.40. Traefik v3.0–v3.3 hardcode `/v1.24/` API calls and fail entirely — no container discovery, no routing.

**Fix:** Upgraded to `traefik:v3.7` (built June 2026, compatible with Docker 29.x).

**Disk note:** Docker image pulls during troubleshooting (v3.3, v3.7, socket-proxy) left the root volume at 96% used (24GB total). Old images cleaned up but further housekeeping needed.



## 2026-05-03 — Initial repo setup and container migration

### Secrets management
Moved all hardcoded secrets out of compose files into `.env` files:
- `traefik/.env` — Cloudflare API token, email, basicauth hash
- `n8n/.env` — n8n encryption key

Created `.gitignore` covering:
- All `.env` files
- `traefik/acme.json` (contains TLS private keys)
- `actual/actual-data/` (SQLite financial data)
- `n8n/private-config.json` (bank PDF passwords)
- `.claude/` directory

Created `.env.example` files for each service showing required variables without real values.
Created `n8n/private-config.example.json` as a template for the real file.

### NFS share configuration
The development share (`192.168.20.32:/mnt/tank/ds3/development`) is mounted at `/mnt/development` on both the Claude Code VM and the Docker VM.

Two NFS changes were required to run containers from the share:
1. **maproot=root** on TrueNAS — Docker daemon runs as root and needs access to the share. Default `root_squash` maps root to `nobody`, blocking bind mounts. Set via TrueNAS → Shares → NFS → Edit → Advanced → Maproot User = root.
2. **Remount after change** — `sudo umount /mnt/development && sudo mount /mnt/development` required to pick up new export options.

### acme.json permissions
Traefik requires `acme.json` to have permissions `600`. When copied from the NFS share the file had `770`. Fix:
```bash
chmod 600 /mnt/development/docker-homelab/traefik/acme.json
```

### n8n issues

#### Volume naming
Docker Compose prefixes volume names with the project name (derived from the directory name). Both local and share instances use project name `n8n`, creating volumes `n8n_n8n_data` and `n8n_n8n_cache`. The older volumes `n8n_data` and `n8n_cache` were created during troubleshooting and are not used by either instance.

The real data volume is `n8n_n8n_data` (created October 2025).

#### /home/node uid mismatch
The original `n8n-n8n` image (built October 2025) had `/home/node` owned by uid 3000 (the `pvh` host user). This was a latent issue that only surfaced when the container was recreated (rather than restarted) because newer n8n versions write to `/home/node/.cache` on startup. Previous runs had cached this in the container's writable layer.

Fixed by rebuilding the image — the new n8n 2.x base image has correct ownership.

#### n8n base image upgrade (1.114.4 → 2.18.5)
`n8nio/n8n:latest` had not been updated for 6 months locally. Rebuilt with `--pull` to get current version.

The new n8n 2.x base image uses **Docker Hardened Images (Alpine)** which has no package manager (`apk` removed). The original Dockerfile installed `qpdf` via `apk` which now fails.

**qpdf has been temporarily removed.** When PDF decryption workflows are needed, restore using a multi-stage build:
```dockerfile
FROM alpine:3.22 AS qpdf-builder
RUN apk add --no-cache qpdf

FROM n8nio/n8n:latest
USER root
COPY --from=qpdf-builder /usr/bin/qpdf /usr/bin/qpdf
COPY --from=qpdf-builder /usr/lib/libqpdf*.so* /usr/lib/
COPY --from=qpdf-builder /usr/lib/libjpeg*.so* /usr/lib/
COPY --from=qpdf-builder /usr/lib/libz*.so* /usr/lib/
USER node
```
Note: additional library dependencies may need to be copied — test with `qpdf --version` inside the container.

#### Unnecessary n8n_cache volume
During troubleshooting a `n8n_cache:/home/node/.cache` volume was added to the compose file. This caused failures because the new volume was root-owned. Removed — n8n manages its own cache in the container's writable layer without a named volume.

### Running from the share vs local path
Containers were tested running directly from `/mnt/development/docker-homelab/`. While functional, this is not recommended for production because:
- Edits on the Claude Code VM could immediately affect running containers
- NAS unavailability would prevent container restarts
- Development and production are intermingled

**Recommended workflow:**
1. Develop and test on the share (Claude Code VM)
2. Push to GitHub when ready
3. On Docker VM, pull to a local path and run from there
4. Deployments are deliberate `git pull` + `docker compose up -d`

### Image update strategy
- Pre-built images (traefik, portainer, actual): `docker compose pull && docker compose up -d`
- Custom Dockerfile (n8n): `docker compose build --no-cache --pull && docker compose up -d`
- Portainer UI can be used for day-to-day image update management
