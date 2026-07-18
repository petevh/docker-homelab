# docker-homelab

## Project Overview

Docker Compose configurations for all self-hosted services running on the homelab Docker VM. Each service has its own directory with a `docker-compose.yml` and `.env.example`. All services run behind Traefik as a reverse proxy with SSL via Cloudflare DNS-01 challenge.

## Infrastructure Context

- **Docker VM:** Ubuntu 24.04 LTS on Proxmox (HP EliteDesk 800 G6, server VLAN `192.168.20.x`)
- **Docker VM IP:** `192.168.20.203` (Traefik entry point)
- **Repo location:** Cloned to TrueNAS `development` share, mounted on Docker VM at `/mnt/development`
- **Related VMs:** Proxmox cluster (3 nodes)
- **Note:** Claude Code runs directly on the Docker VM (`docker-vm`, `192.168.20.203`) — there is no separate Claude Code VM
- **Domain:** `vanheerden.ch` — containers accessible at `*.app.vanheerden.ch`
- **DNS:** OPNsense Unbound (primary) with wildcard `*.app.vanheerden.ch → 192.168.20.203`, Cloudflare (backup)

## Repository Structure

```
docker-homelab/
├── CLAUDE.md
├── README.md
├── traefik/
│   ├── docker-compose.yml
│   ├── .env.example
│   └── config/
│       ├── traefik.yml          # Static config
│       └── dynamic/             # File provider configs (non-Docker upstreams)
│           ├── homeassistant.yml
│           └── omada.yml
├── n8n/
│   ├── docker-compose.yml
│   └── .env.example
├── portainer/
│   ├── docker-compose.yml
│   └── .env.example
├── actual/
│   ├── docker-compose.yml
│   └── .env.example
├── intuneget/                   # Intune Winget packaging — builds from an out-of-repo clone
│   ├── docker-compose.yml       #   (build context ../../IntuneGet, NOT tracked here)
│   └── .env.example
├── paperless/                   # Planned — document management
│   ├── docker-compose.yml
│   └── .env.example
└── docs/
    ├── adding-new-service.md    # How to add a new container
    └── network-architecture.md
```

## Network Architecture

```
Internet
    ↓ (DNS: *.app.vanheerden.ch → 192.168.20.203)
Traefik (192.168.20.203:443)
    ↓ (routes by hostname)
Docker network: traefik (internal bridge)
    ├── n8n          (n8n.app.vanheerden.ch)
    ├── portainer    (portainer.app.vanheerden.ch)
    ├── actual       (actual.app.vanheerden.ch)
    ├── intuneget    (intuneget.app.vanheerden.ch)
    └── paperless    (paperless.app.vanheerden.ch) [planned]

File provider (non-Docker upstreams via config/dynamic/)
    ├── ha           (ha.app.vanheerden.ch → 192.168.20.50:8123)
    └── omada        (omada.app.vanheerden.ch → 192.168.99.50:8043)
```

## Traefik Configuration

**SSL:** Let's Encrypt via Cloudflare DNS-01 challenge — no ports 80/443 need to be open to the internet. All certs use `certresolver=cloudflare`.

**Entry points:** `web` (80, redirects to HTTPS), `websecure` (443)

**Docker network:** All containers must join the external `traefik` network to be discovered by Traefik.

**Standard Traefik labels for new services:**
```yaml
labels:
  - "traefik.enable=true"
  - "traefik.docker.network=traefik"
  - "traefik.http.routers.SERVICE.rule=Host(`SERVICE.app.vanheerden.ch`)"
  - "traefik.http.routers.SERVICE.entrypoints=websecure"
  - "traefik.http.routers.SERVICE.tls=true"
  - "traefik.http.routers.SERVICE.tls.certresolver=cloudflare"
  - "traefik.http.services.SERVICE.loadbalancer.server.port=INTERNAL_PORT"
```

**No DNS changes needed** when adding new containers — wildcard DNS already covers `*.app.vanheerden.ch`.

**File provider** (`config/dynamic/`): used for non-Docker upstreams (other VMs, physical hosts). Add a new `.yml` file — Traefik hot-reloads it without restart (`watch: true`). For upstream HTTPS with a self-signed cert, set `serversTransport.insecureSkipVerify: true`.

## Running Services

### Traefik
- **Role:** Reverse proxy, SSL termination, service discovery via Docker labels
- **URL:** `traefik.app.vanheerden.ch` (dashboard)
- **Key config:** Cloudflare API token in `.env` for DNS-01 challenge

### n8n
- **Role:** Workflow automation
- **URL:** `n8n.app.vanheerden.ch`
- **Key env vars:**
  - `N8N_HOST` — external hostname for webhook URL generation
  - `N8N_PROTOCOL=https` — ensures webhooks generate correct https:// URLs
  - `N8N_PORT=5678` — internal listening port (Traefik label must match)
- **Note:** `N8N_PORT` controls internal port only — external access is always via Traefik on 443

### Portainer
- **Role:** Docker container management UI
- **URL:** `portainer.app.vanheerden.ch`

### Actual Budget
- **Role:** Personal finance / budgeting
- **URL:** `actual.app.vanheerden.ch`
- **Note:** No webhook/callback URLs — no HOST/PROTOCOL env vars needed

### IntuneGet
- **Role:** Self-hosted [IntuneGet](https://github.com/ugurkocde/IntuneGet) — browse the Winget catalog and generate/upload `.intunewin` packages to Intune, without giving the public `intuneget.com` any M365 credentials. Single-user (Pete only); targets the **Kemyion** tenant.
- **URL:** `intuneget.app.vanheerden.ch`
- **Build context is OUT of this repo:** builds from a pinned clone at `/mnt/development/IntuneGet` (`git clone ugurkocde/IntuneGet`) — there is no published image. The image only updates when you rebuild after pulling that clone:
  ```
  git -C /mnt/development/IntuneGet fetch --tags && git -C /mnt/development/IntuneGet checkout <tag>
  cd /mnt/development/docker-homelab/intuneget && docker compose build && docker compose up -d
  ```
- **Two-switch Supabase trap:** the app must run in local mode. The catalog source is chosen by `isSupabaseConfigured()` (presence of `NEXT_PUBLIC_SUPABASE_*`), **not** by `DATABASE_MODE`. Both must agree: `DATABASE_MODE=sqlite` **and** no `NEXT_PUBLIC_SUPABASE_*` anywhere (including `.env`, since `env_file` passes the whole file through). Leaving a Supabase URL set silently re-enables the hosted catalog and remote token storage.
- **Entra redirect URIs — TWO are required** (upstream's `.env.example` says to register the bare domain; that is **wrong** and sign-in fails with a redirect-mismatch):
  - `https://intuneget.app.vanheerden.ch/redirect` — **SPA** type. MSAL sign-in; hardcoded as `${window.location.origin}/redirect` (`lib/msal-config.ts`), received by `app/redirect/page.tsx`.
  - `https://intuneget.app.vanheerden.ch/auth/consent-callback` — admin-consent return; hardcoded in `getAdminConsentUrl()` (`lib/msal-config.ts`) + `lib/onboarding-utils.ts`, received by `app/auth/consent-callback/page.tsx`. A plain browser redirect from Microsoft, so it belongs under the **Web** platform (SPA may also work — move it to Web if consent errors with a mismatch).
  - Do **not** add `/api/msp/tenants/consent-callback` — that's the MSP multi-customer flow, unused here.
- **Key env vars:**
  - `AZURE_CLIENT_ID` — injected at runtime by the server (the `NEXT_PUBLIC_*` client-ID var is inlined empty at build time, so this plain name is the one that matters)
  - `AZURE_AD_CLIENT_SECRET` — Entra client secret (single-tenant app registered in Kemyion)
  - `PACKAGER_MODE=local`, `PACKAGER_API_KEY` — shared secret with the Windows packager
- **Windows packager dependency:** actual `.intunewin` packaging + upload runs on a separate Windows VM (`@intuneget/packager`), which polls this web app outbound using `PACKAGER_API_KEY`. Set up after the web app is confirmed healthy.
- **Data:** named volume `intuneget_data:/data` holds both `intuneget.db` and the downloaded catalog snapshot.
- **Catalog snapshot override (optional):** the read-only catalog defaults to downloading upstream's GitHub `catalog-latest` release, which upstream **froze on 2026-07-10** — so apps whose winget manifest has since moved (e.g. Chrome) 404 on the live fetch. To point at a snapshot you control, set one of (read by `lib/catalog/snapshot-store.ts`, all commented in the compose): `CATALOG_SNAPSHOT_BASE_URL` (override the release base, must be https), `CATALOG_SNAPSHOT_FILE` (explicit local `.sqlite`, **skips all networking**), `CATALOG_SNAPSHOT_DIR` (where the unpacked `catalog.sqlite` lives, defaults to `dirname(DATABASE_PATH)` = `/data`). The active catalog is a **self-hosted harvest**: `CATALOG_SNAPSHOT_FILE=/data/catalog.local.sqlite`, built by `intuneget/scripts/build-catalog.mjs` — a merge of the frozen upstream snapshot (kept for curation: categories, icons, descriptions) with svrooij's live winget-pkgs-index (current app list + versions). Installers are fetched live at package time so the catalog carries no installer bytes. See `intuneget/DESIGN.md` §5.
- **Catalog refresh is scheduled via cron** (on `docker-vm`, `pvh`'s crontab — NOT in git). `intuneget/scripts/refresh-catalog.sh` runs the whole pipeline non-interactively: fetch the svrooij index → build inside the container → sanity-check (≥10k apps, required tables) → atomic swap into `catalog.local.sqlite` → `docker compose restart` (needed — `CATALOG_SNAPSHOT_FILE` caches the DB handle for the process lifetime, so a swapped file is ignored until restart) → wait for healthy. Any failure leaves the live catalog untouched and exits non-zero. Reinstall the schedule after a VM rebuild:
  ```
  ( crontab -l 2>/dev/null | grep -v refresh-catalog.sh; \
    echo "30 4 * * * /mnt/development/docker-homelab/intuneget/scripts/refresh-catalog.sh >> /mnt/development/docker-homelab/intuneget/logs/refresh-catalog.log 2>&1" ) | crontab -
  ```
  Depends on the frozen curation base `/data/catalog.frozen.bak` (on the named volume, not in git); the script aborts loudly if it's missing (e.g. fresh volume — rebuild it from an upstream snapshot first). Logs: `intuneget/logs/refresh-catalog.log` (gitignored).

### Home Assistant
- **Role:** Home automation
- **URL:** `ha.app.vanheerden.ch`
- **Upstream:** `http://192.168.20.50:8123` (haOS VM on server VLAN, same as docker-vm)
- **Config:** `traefik/config/dynamic/homeassistant.yml` (file provider)
- **Note:** Requires `http.use_x_forwarded_for: true` and `trusted_proxies: 192.168.20.203` in HA's `configuration.yaml`

### Omada Controller
- **Role:** TP-Link Omada network controller (APs, switches)
- **URL:** `omada.app.vanheerden.ch`
- **Upstream:** `https://192.168.99.50:8043` (self-signed cert — `insecureSkipVerify: true`)
- **Config:** `traefik/config/dynamic/omada.yml` (file provider)

### Paperless-ngx (planned)
- **Role:** Document management with OCR
- **URL:** `paperless.app.vanheerden.ch`
- **Integration:** Nextcloud external storage mount for family document access
- **See:** Personal knowledge management system documentation

## Planned Additions

Services to be added as part of active projects:

| Service | Repo | Purpose |
|---------|------|---------|
| FastAPI + React | `energy-monitor` | Sunsynk solar dashboard |
| PostgreSQL | `energy-monitor` | Time-series energy data |
| Paperless-ngx | `docker-homelab` | Document management |
| Wazuh agents | `homelab-soc` | SIEM log shipping |

## Secrets & Environment Variables

**Never commit:**
- `.env` files containing real values
- Cloudflare API tokens
- Passwords or API keys of any kind

**Pattern:**
- `.env.example` — committed, shows required variables with placeholder values
- `.env` — gitignored, contains real values, stored on NAS share alongside compose files

**1Password:** All secrets stored in 1Password. `.env` files on the NAS share reference these values — treat the NAS share as a trusted environment (ZFS encrypted, Tailscale-only access).

## Coding Conventions

- One directory per service — no monolithic compose files
- All services use `restart: unless-stopped`
- All services join the external `traefik` network
- Volume paths use named volumes where possible, bind mounts only where necessary
- Keep `.env.example` up to date whenever new env vars are added
- Document any non-obvious configuration decisions in `docs/`

## Development Workflow

1. Develop/test compose changes on Claude Code VM (same NAS share)
2. `docker compose up -d` on Docker VM to apply
3. Commit working configs to `main`
4. `main` branch = what's running in production

## Adding a New Service

1. Create directory: `mkdir service-name`
2. Create `docker-compose.yml` using standard Traefik labels template above
3. Create `.env.example` with all required variables
4. Add to this `CLAUDE.md` under Running Services
5. No DNS changes needed if using `*.app.vanheerden.ch`
6. Deploy: `cd /mnt/development/docker-homelab/service-name && docker compose up -d`
