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

The unlock endpoint is only reachable over Tailscale. The Tailscale iOS app
exposes native Shortcuts actions — **Connect** is a no-op if already connected,
so run it unconditionally before the HTTP call.

1. Add action: **Tailscale → Connect**
2. Add action: **Get Contents of URL**
   - URL: `https://intercom.app.vanheerden.ch/unlock`
   - Method: `POST`
   - Headers: `X-API-Key` → `YOUR_API_KEY`
3. Add action: **If** → `Contents of URL` contains `"success": true`
   - Show notification: "Door unlocked"
   - Otherwise: Show notification: "Unlock failed"

Add to Home Screen for one-tap unlock. Tailscale must be installed; the Connect
step handles the case where it was disconnected.

### Tasker

Create a task with an **HTTP Request** action (no AutoWeb needed):
1. Method: `POST`, URL: `https://intercom.app.vanheerden.ch/unlock`
2. Headers: `X-API-Key` → `YOUR_API_KEY`
3. If `%http_response_code` equals `200` → Flash "Door Unlocked", else Flash "Unlock Failed (%http_response_code)"

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

**Automation example — doorbell → Apple TV:**
```yaml
automation:
  - alias: "Doorbell → Apple TV"
    trigger:
      - platform: event
        event_type: dahua_doorbell    # fired by /events SSE listener (not yet implemented)
    action:
      - service: media_player.play_media
        target:
          entity_id: media_player.apple_tv
        data:
          media_content_id: "{{ states('sensor.dahua_stream_url') }}"
          media_content_type: video/mp4
      - service: rest_command.unlock_front_door  # optional auto-unlock
```

---

## Architecture

```
dahua-research/          ← reverse engineering & testing
  p2p_unlock.py          ← proven unlock logic (source for dahua_client.py)
  dos_stream.py          ← WIP stream logic
  NEXT_STEPS.md          ← full protocol research notes

docker-homelab/
  intercom/              ← this service (production)
    dahua_client.py      ← promoted from dahua-research when stable
    main.py              ← FastAPI
    Dockerfile
    docker-compose.yml
    DEVNOTES.md          ← research summary and open items
```

Logic flows from `dahua-research` → promoted to `dahua_client.py` here once stable.
`dahua-research` is never referenced in production code.

---

## Security

- `X-API-Key` header required on all non-health endpoints
- Traefik IP allowlist: `100.64.0.0/10` (Tailscale) + `192.168.0.0/16` (LAN only)
- Rate limit: 10 req/min, burst 5 (Traefik middleware)
- TLS via Let's Encrypt Cloudflare DNS-01
