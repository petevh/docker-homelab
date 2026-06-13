# Intercom Service — Development Notes

Research and reverse engineering lives in `/mnt/development/DahuaConsole/` (separate
project). Bring working solutions here once confirmed in that repo. Full protocol
analysis is in `DahuaConsole/NEXT_STEPS.md` — read it before touching this service.

## Current Status (2026-06-12)

| Feature | Status | Notes |
|---------|--------|-------|
| `/unlock` | **Working** | Cloud API via `dmss-di.dolynkcloud.com` |
| `/frame` | **Working** | Clean full-frame JPEG (RTP de-interleave fix, 2026-06-12) |
| `/stream` | **Working** | Clean MJPEG |
| `/events` | **Working** | SSE stream fires on doorbell ring via direct DHIP to VTH |
| `/talk` | **Working** | POST a WAV/PCM clip → plays out the door speaker (TTS/announce) |
| `/talk/ws` | **Working** | WebSocket push-to-talk: stream 16-bit PCM frames live |
| HA generic_camera | Ready to wire | Stream quality fixed |
| HA webhook trigger | Ready to wire | Consume `/events` SSE |
| HA two-way audio | Ready to wire | go2rtc + Advanced Camera Card mic button → `/talk/ws` (see below) |

## Talkback — How It Works (verified at the door 2026-06-13)

Push audio UP to the door speaker over the same cloud relay we use for video.
Fully reverse-engineered from a clean OPNsense capture of a real DMSS talk session.

1. **Hold an `encrypt=2` video session** (`/real/1/1/encrypt/RTSV1`). Precondition:
   an idle door 503s the talk PLAY, AND an `encrypt=0` session silently **mutes**
   the uplink audio. The audio payload is plaintext PCMA — `encrypt=2` is only the
   negotiated session mode, but the device drops audio without it.
2. **6-PLAY handshake** on the `encrypt=2` visualtalk relay, one socket:
   `trackID=31&method=0`, `talktype=talk&trackID=64&method=0`, `trackID=6&method=1`,
   `method=2`, `trackID=31&method=1`, `trackID=70&method=3`. Audio flows after the 6th.
3. **Push audio**: PCMA (G.711 a-law) at **16000 Hz** (the SDP's `PCMA/16000` is
   correct — earlier 8 kHz guess played it half-speed/broken), framed as:
   `DHAV 0xf0` wrapper → 12-byte RTP (pt=8, **marker bit set / 0x88**) → `0x24`
   interleave on **channel 10**. ~40 ms / 640-sample frames, paced realtime against
   an absolute clock. **Drain the talk socket while sending** or TCP backpressure
   stalls cause breakup.

Code: `TalkbackSession` + `play_audio_clip()` in `dahua_client.py`. Only one talk
session at a time (single device talk channel). `DAHUA_TALK_MAX_SECONDS` (default
180) is a hard backstop against a stuck-open mic; push-to-talk also stops on the
WS closing.

### HA two-way audio wiring (later — no go2rtc yet)
Standard path once go2rtc is added: publish the existing RTSP `frontdoor` feed to
go2rtc; the Advanced Camera Card mic button captures the browser mic over WebRTC.
Bridge that backchannel to `/talk/ws` (PCM frames) — push-to-talk falls out
naturally (release button → WS closes → `stopTalk`). Full duplex = mic open while
the same card plays the video+downlink-audio feed. `/talk` (POST clip) also works
from HA `rest_command` for TTS/announcements.

## Stream Quality Issue — SOLVED (2026-06-12)

The earlier "corrupt right quarter / camera firmware bug" diagnosis was **WRONG**. The actual
cause: the relay delivers the DHAV/H264 stream as **RTP-over-TCP interleaved**, and
`connect_relay()` only stripped the *first* 16-byte header. Every subsequent ~1456-byte chunk
carries another `0x24 chan len` (4-byte interleave) + 12-byte RTP header, which stayed embedded
in the H264 we fed ffmpeg. Those bytes every 1456B desync EVERY decoder (ffmpeg, VLC,
gstreamer, even Android MediaCodec) at the first chunk boundary → MB row 1 → grey/ghosty
picture. DMSS looks fine because it de-interleaves the RTP properly.

**Fix:** `RtpDeinterleaver` in `dahua_client.py` strips the 4-byte interleave + 12-byte RTP
header from every chunk in the feed loop, BEFORE the DHAV/H264 parsing. Verified: clean
full-frame decode, 0 ffmpeg errors. Full diagnosis in `DahuaConsole/NEXT_STEPS.md`.

It was never encrypted, never a camera bug, never a reconnect/IDR issue — purely the embedded
transport headers. (`-f h264` input with this service's own DHAV frame extraction is fine once
the stream is de-interleaved first.)


## Unlock — How It Works

The VTH does NOT unlock the door via direct DHIP (TCP/5000) — this was exhaustively
tried and is permanently blocked by firmware. See "What Doesn't Work" below.

The working path is Dahua's P2P cloud API:

```
POST https://dmss-di.dolynkcloud.com/pcs/v1/deviceuseroperate.vth.OpenVthDoor
Authorization: Bearer <DAHUA_BEARER_TOKEN>
x-pcs-signature: HMAC-SHA256 signed (see dahua_client.py)

body: {
  "data": {
    "channel": 1,
    "devName": AES256CBC(username, key=MD5(SN)),
    "devPassword": AES256CBC(password, key=MD5(SN)),
    "deviceId": "BG0142EPAJEF6EF",
    "doorIndex": 0
  }
}

Response: {"code": 10000, "desc": "Success"}
```

`devName` and `devPassword` are AES-256-CBC encrypted with:
- Key: `MD5(SN.upper()).upper().encode()` — 32 ASCII hex bytes
- IV: `b"HLMUQE2342MABCER"` — hardcoded constant from `libCommonSDK.so`

## What Doesn't Work (Do Not Retry)

### Direct DHIP (TCP/5000)
All unlock methods tried — `UnlockManager.openDoors`, `VTHMonitor.openDoor`,
`accessControl.openDoor`, `AnalogBusControl.*`, `VideoTalk2Cloud.*` — all return
error `-267976701` ("VTO unreachable").

**Root cause**: VTH firmware checks IP reachability to the VTO at `192.168.1.100`
before executing any unlock command. The VTO is on a proprietary 2-wire bus and has
no IP presence on our network. This gate can never be satisfied.

DHIP auth itself works fine (you can log in, query config, etc.) — it's specifically
the unlock command execution that's blocked.

### Fake VTO Server
Built `fake_vto_server.py` to impersonate the VTO at 192.168.1.100. Device entered
"abnormal network" state (no video, no unlock). Config does NOT auto-revert —
required manual fix via device panel. Abandoned.

### SIP INFO Unlock
VTH listens on UDP 5060. REGISTER works (user=2806, pass=123456, realm=VDP). SIP
INFO with all content types tried — all timeout. Not supported by firmware.

## Stream — How It Works

Full pipeline implemented in `dahua_client.py` (`StreamProxy` class):

1. `POST /pcs/v1/dclouduser.account.Login` → `openUserToken` (= `playToken`)
2. `POST /openapi/transferStream` with `appendUrl=/real/1/1/RTSV1` (no encryption) → relay URL `host:port/live/visualtalk.rtpxav?...`
3. TCP connect → `PLAY` request with `Accpet-Sdp: Private` header (intentional typo in Dahua protocol)
4. Response: HTTP headers + SDP (`Private-Length` bytes) + 16-byte transport prefix + continuous DHAV stream
5. DHAV piped to `ffmpeg -f dhav` → JPEG frames served at `/frame` and `/stream`
6. Auto-reconnects every 50s (relay URL expires at 60s)

The container needs `ffmpeg` — installed via `apt-get` in the Dockerfile.

VTO camera is channel 1 on the VTH (`channelSn: BF08FE7PAJE2E59`, `channelIp: 192.168.1.100`).

## Events — How It Works

`subscribe_events()` in `dahua_client.py` connects directly to VTH TCP/5000, logs in
with DHIP gen2 MD5 auth, subscribes to all events, and calls `on_ring()` on each
`IgnoreInvite+Start` event (= doorbell press).

Replay/stale filtering: VTH replays recent events on subscribe; these are skipped by
comparing `UTC` against the subscribe-time baseline. See `dahua_client.py` comments.

The container must be able to reach the VTH at `DAHUA_VTH_HOST:DAHUA_VTH_PORT`.
If using the default `traefik` network, add `extra_hosts` to docker-compose or switch
to `network_mode: host` if the VTH is on the same subnet as the Docker host.

## Credentials & Device Identifiers

| Item | Value |
|------|-------|
| VTH IP | `192.168.40.55` |
| Device SN | `BG0142EPAJEF6EF` |
| Device credentials | `user` / `***REMOVED***` |
| Admin credentials | `admin` / `***REMOVED***` |
| Cloud host | `dmss-di.dolynkcloud.com` |
| Bearer token | `***REMOVED***-01` *(may expire — recapture via DMSS if needed)* |
| pcs-username | `uuid\***REMOVED***` |
| VTO channel SN | `BF08FE7PAJE2E59` |
| RandSalt | `***REMOVED***` |
| Gateway API host | `dmss.dolynkcloud.com/gateway/` *(new API, simpler auth)* |

## Bearer Token Expiry

The Bearer token is long-lived but will eventually expire. To recapture:
1. Install `DahuaConsole/DMSS_patched.apk` (cert pinning bypassed)
2. Run PCAPdroid with TLS key logging
3. Open DMSS, log in — capture the `Authorization: Bearer` header value
4. Update `DAHUA_BEARER_TOKEN` in `.env` and restart the container
