# dahua-intercom

Production HTTP API for Dahua DHI-VTH2622GW-W intercom integration.
Exposes door unlock, camera stream, and doorbell events for use with
Home Assistant, n8n, iOS Shortcuts, Tasker, or any HTTP client.

Runs behind Traefik, accessible over Tailscale at `https://intercom.app.vanheerden.ch`.

See `DEVNOTES.md` for research background (why cloud API, why not direct DHIP).

## Endpoints

| Endpoint | Method | Status | Description |
|----------|--------|--------|-------------|
| `/health` | GET | ✅ | Health check |
| `/unlock` | POST | ✅ | Trigger door unlock via Dahua P2P cloud |
| `/stream` | GET | 🚧 | VTO camera stream URL — pending stream research |
| `/events` | GET (SSE) | 🚧 | Doorbell/motion event stream — not yet started |

Swagger UI: `https://intercom.app.vanheerden.ch/docs`

## Setup

```bash
cp .env.example .env
# edit .env — set DAHUA_BEARER_TOKEN, DAHUA_PCS_USERNAME, DAHUA_DEVICE_PASSWORD, DAHUA_API_KEY
docker compose up -d
```

## Quick test

```bash
curl -X POST https://intercom.app.vanheerden.ch/unlock \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Client Integrations

All clients use `https://intercom.app.vanheerden.ch/unlock` — reachable over
Tailscale (Traefik IP allowlist covers `100.64.0.0/10`). No direct port access needed.

### iOS Shortcut

1. Add action: **Get Contents of URL**
2. URL: `https://intercom.app.vanheerden.ch/unlock`
3. Method: `POST`
4. Headers: `X-API-Key` → `YOUR_API_KEY`
5. Add action: **If** → `Contents of URL` contains `true` → show notification "🔓 Unlocked"

Add to Home Screen for one-tap unlock.

### Tasker

Import `unlock_door.xml` (in this directory) — replace `YOUR_API_KEY` in the HTTP
Request action. The task:
1. POST to `https://intercom.app.vanheerden.ch/unlock` with `X-API-Key` header
2. Flashes "🔓 Door Unlocked" on HTTP 200, or "❌ Unlock Failed (HTTP NNN)" otherwise

No AutoWeb needed — native Tasker HTTP Request handles this cleanly.

Assign to a widget, NFC tag, or Tasker scene button as preferred.

### n8n

**HTTP Request node:**
- Method: `POST`
- URL: `https://intercom.app.vanheerden.ch/unlock`
- Authentication: Header Auth → Name: `X-API-Key`, Value: `YOUR_API_KEY`
- Response: check `success` field is `true`

Typical use: trigger from a webhook (HA doorbell event → n8n → unlock).

### Home Assistant

**`configuration.yaml` — rest_command:**
```yaml
rest_command:
  unlock_front_door:
    url: https://intercom.app.vanheerden.ch/unlock
    method: POST
    headers:
      X-API-Key: !secret intercom_api_key
```

**`secrets.yaml`:**
```yaml
intercom_api_key: YOUR_API_KEY
```

**Automation example — doorbell button → unlock:**
```yaml
automation:
  - alias: "Front door button → unlock"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_button
        to: "on"
    action:
      - service: rest_command.unlock_front_door
```

HA runs on the home LAN (`192.168.x.x`) so it hits Traefik directly without Tailscale.

---

## Security

- `X-API-Key` header required on all non-health endpoints
- Traefik IP allowlist: `100.64.0.0/10` (Tailscale) + `192.168.0.0/16` (LAN only)
- Rate limit: 10 req/min, burst 5 (Traefik middleware)
- TLS via Let's Encrypt Cloudflare DNS-01
