# dahua-intercom

Production HTTP API for Dahua DHI-VTH2622GW-W intercom integration.
Exposes door unlock, camera stream, and doorbell events for use with
Home Assistant, n8n, iOS Shortcuts, Tasker, or any HTTP client.

## Endpoints

| Endpoint | Method | Status | Description |
|----------|--------|--------|-------------|
| `/health` | GET | ✅ | Health check |
| `/unlock` | POST | ✅ | Trigger door unlock via Dahua P2P cloud |
| `/stream` | GET | 🚧 | Get VTO camera stream URL |
| `/events` | GET (SSE) | 🚧 | Doorbell/motion event stream |

## Setup

1. Copy `dahua_client.py` (promoted from `dahua-research` repo) into this directory
2. Edit `docker-compose.yml` — set credentials and a strong `DAHUA_API_KEY`
3. Build and run:
   ```bash
   docker compose up -d
   ```

## Usage

### Unlock the door
```bash
curl -X POST http://localhost:8000/unlock \
  -H "X-API-Key: changeme"
```

### Get stream URL (once implemented)
```bash
curl http://localhost:8000/stream \
  -H "X-API-Key: changeme"
```

### API docs (Swagger UI)
```
http://localhost:8000/docs
```

## Home Assistant Integration

### rest_command (unlock)
```yaml
rest_command:
  unlock_front_door:
    url: http://<docker-host>:8000/unlock
    method: POST
    headers:
      X-API-Key: "changeme"
```

### Automation (doorbell → Apple TV)
```yaml
automation:
  - alias: "Doorbell → Apple TV"
    trigger:
      - platform: event
        event_type: dahua_doorbell    # fired by /events SSE listener
    action:
      - service: media_player.play_media
        target:
          entity_id: media_player.apple_tv
        data:
          media_content_id: "{{ states('sensor.dahua_stream_url') }}"
          media_content_type: video/mp4
      - service: rest_command.unlock_front_door  # optional auto-unlock
```

## n8n
- HTTP Request node → POST → `http://<host>:8000/unlock`
- Header: `X-API-Key: changeme`

## iOS Shortcuts
- "Get Contents of URL" action
- Method: POST
- URL: `http://<tailscale-ip>:8000/unlock`
- Header: `X-API-Key: changeme`

## Architecture

```
dahua-research/          ← reverse engineering & testing
  p2p_unlock.py          ← proven unlock logic
  dos_stream.py          ← WIP stream logic
  DahuaConsole/          ← research/debug tool (submodule)

docker-homelab/
  services/
    dahua-intercom/      ← this service (production)
      dahua_client.py    ← promoted from dahua-research when stable
      main.py            ← FastAPI
      Dockerfile
      docker-compose.yml
```

Logic flows from `dahua-research` → promoted to `dahua_client.py` here
once stable. DahuaConsole never referenced in production code.
