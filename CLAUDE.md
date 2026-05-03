# docker-homelab

## Project Overview

Docker Compose configurations for all self-hosted services running on the homelab Docker VM. Each service has its own directory with a `docker-compose.yml` and `.env.example`. All services run behind Traefik as a reverse proxy with SSL via Cloudflare DNS-01 challenge.

## Infrastructure Context

- **Docker VM:** Ubuntu 24.04 LTS on Proxmox (HP EliteDesk 800 G6, server VLAN `192.168.20.x`)
- **Docker VM IP:** `192.168.20.40` (Traefik entry point)
- **Repo location:** Cloned to TrueNAS `development` share, mounted on Docker VM at `/mnt/development`
- **Related VMs:** Claude Code VM (development), Proxmox cluster (3 nodes)
- **Domain:** `vanheerden.ch` — containers accessible at `*.app.vanheerden.ch`
- **DNS:** OPNsense Unbound (primary) with wildcard `*.app.vanheerden.ch → 192.168.20.40`, Cloudflare (backup)

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
│       └── dynamic/             # Dynamic config files
├── n8n/
│   ├── docker-compose.yml
│   └── .env.example
├── portainer/
│   ├── docker-compose.yml
│   └── .env.example
├── actual/
│   ├── docker-compose.yml
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
    ↓ (DNS: *.app.vanheerden.ch → 192.168.20.40)
Traefik (192.168.20.40:443)
    ↓ (routes by hostname)
Docker network: traefik (internal bridge)
    ├── n8n          (n8n.app.vanheerden.ch)
    ├── portainer    (portainer.app.vanheerden.ch)
    ├── actual       (actual.app.vanheerden.ch)
    └── paperless    (paperless.app.vanheerden.ch) [planned]
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
