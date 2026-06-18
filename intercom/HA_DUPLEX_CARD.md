# HA Full-Duplex Intercom — Advanced / WebRTC Camera Card

Live two-way audio + low-latency video for the front door, inside a Home Assistant
dashboard card. Video and the door's audio come **down** over WebRTC (sub-second);
your microphone goes **up** through go2rtc's backchannel to the door speaker.

## Architecture

```
DOWNLINK (see/hear the door):
  intercom  rtsp://intercom:8554/frontdoor_webrtc  (H264 + Opus)
     → go2rtc (our container)  → WebRTC Camera card  → your browser

UPLINK (talk to the door):
  card mic button  → go2rtc WebRTC backchannel
     → go2rtc exec: ffmpeg reads mic from stdin
     → POST http://intercom:8000/talk/stream  (16k mono PCM)
     → TalkbackSession (AES hybrid encrypt, unchanged) → door speaker
```

Why our own go2rtc (not HA's built-in): HA's built-in go2rtc auto-manages streams
and **cannot define custom `exec` backchannel sources**, which the uplink needs. So
we run our own go2rtc container (in this compose) and point the card at it.

## What's already running (this repo / branch)

- **`POST /talk/stream`** (`main.py`) — ingests a continuous raw-PCM mic stream and
  feeds the existing encrypted `TalkbackSession`. Auth via `?key=` query param.
- **go2rtc container** (`docker-compose.yml` + `go2rtc.yaml`) — stream `frontdoor`:
  downlink from `frontdoor_webrtc`, uplink via the exec backchannel to `/talk/stream`.
  API/UI on `:1984`, WebRTC media on `:8555`.
- **`frontdoor_webrtc`** mediamtx path — H264 copy + Opus (WebRTC-compatible audio).

Verified: go2rtc loads the config and pulls the downlink end-to-end
(`http://192.168.20.203:1984/api/frame.jpeg?src=frontdoor` returns a decoded frame).

## Setup (Home Assistant side)

### 1. Install the WebRTC Camera integration
- HACS → Integrations → search **"WebRTC Camera"** (AlexxIT, `AlexxIT/WebRTC`) → install.
- **Restart Home Assistant.**

> This integration can point at an **external** go2rtc (ours) and ships the
> `custom:webrtc-camera` card used below. No `configuration.yaml` change is needed —
> the card's `server:` key targets our go2rtc directly.

### 2. Add the duplex card to a dashboard
Edit a dashboard → Add Card → **Manual**, and paste:

```yaml
type: custom:webrtc-camera
streams:
  - url: frontdoor          # stream name in our go2rtc.yaml
    mode: webrtc            # WebRTC (low-latency) — required for two-way audio
    media: video,audio,microphone   # video + door audio down, mic up
server: http://192.168.20.203:1984/   # OUR go2rtc (not HA's built-in)
title: Front Door
muted: false
```

`media: ...,microphone` is what enables the mic button (the uplink).

### 3. Use HTTPS
The browser only allows microphone access in a **secure context**, so open HA over
**https** (e.g. the `…app.vanheerden.ch` hostname, not `http://<ip>:8123`) when you
want to talk. Video/audio downlink work over http too; only the mic needs https.

## Testing

1. **Downlink** — the card should show the door with **video + audio**, low latency.
   (Proven server-side; the card just needs to render it.)
2. **Uplink** — tap the **mic button** and speak. Path: card → go2rtc backchannel →
   exec ffmpeg → `/talk/stream` → door. **This fires audio at the door** — test when
   you can hear the speaker.

### What to watch in the logs
```bash
cd intercom
# go2rtc: exec backchannel launching when you press the mic
docker compose logs -f go2rtc | grep -iE "exec|backchannel|frontdoor"
# intercom: the mic stream arriving + frames going to the door
docker compose logs -f intercom | grep -iE "mic stream|Talkback session|frames"
```
Success = `Talk (mic stream) by 'ha'` then `Talkback session open` then frames sent.

## Secrets

The exec backchannel authenticates to `/talk/stream` with the intercom API key,
supplied via **`INTERCOM_API_KEY`** (set in `.env`, the value of the `ha` label in
`DAHUA_API_KEYS`). It is **not** committed — `go2rtc.yaml` uses `${INTERCOM_API_KEY}`
and go2rtc masks it in its API. Set it in `.env` (see `.env.example`).

## Known unknowns / troubleshooting

- **Backchannel audio format**: `go2rtc.yaml` requests `audio=pcm/48000` (s16be) and
  ffmpeg resamples to 16k for the door. If the mic produces no/garbled audio, the
  format go2rtc actually delivers may differ — try `audio=pcma/8000` (and `-f alaw
  -ar 8000` in the exec ffmpeg) instead.
- **No mic button / blocked**: ensure HA is on **https** and `media:` includes
  `microphone`.
- **Downlink black**: the relay is on-demand and cold-starts (~6s). The go2rtc stream
  wakes the intercom relay on connect; give it a few seconds.
- **WebRTC media won't connect remotely**: go2rtc advertises `192.168.20.203:8555`;
  reachable on home-LAN and over Tailscale (subnet router advertises 192.168.20.0/24).
