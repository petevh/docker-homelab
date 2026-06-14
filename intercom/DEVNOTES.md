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
| `/talk` | **Implemented** | POST a WAV/PCM clip → door speaker. NOTE: "Working" was overstated — talkback frame size was wrong (640 vs verified 644), fixed 2026-06-14; re-verify audibly at the door |
| `/talk/ws` | **Implemented** | WebSocket push-to-talk: stream 16-bit PCM frames live (same 644 fix applies) |
| `/say` | **Implemented** | piper TTS → door speaker; text in, no audio handling. Pending audible door verification |
| HA generic_camera | Ready to wire | Stream quality fixed |
| HA webhook trigger | Ready to wire | Consume `/events` SSE |
| HA two-way audio | Ready to wire | go2rtc + Advanced Camera Card mic button → `/talk/ws` (see below) |

## Talkback — How It Works (partially working — see STATUS below)

Push audio UP to the door speaker over the same cloud relay we use for video.
Reverse-engineered from a clean OPNsense capture of a real DMSS talk session.
Verbatim replay works; encoding our own audio does NOT yet (see ⚠️ STATUS).

1. **Hold an `encrypt=2` video session** (`/real/1/1/encrypt/RTSV1`). Precondition:
   an idle door 503s the talk PLAY, AND an `encrypt=0` session silently **mutes**
   the uplink audio. The audio payload is plaintext PCMA — `encrypt=2` is only the
   negotiated session mode, but the device drops audio without it.
2. **6-PLAY handshake** on the `encrypt=2` visualtalk relay, one socket:
   `trackID=31&method=0`, `talktype=talk&trackID=64&method=0`, `trackID=6&method=1`,
   `method=2`, `trackID=31&method=1`, `trackID=70&method=3`. Audio flows after the 6th.
3. **Push audio**: framed as `DHAV 0xf0` wrapper → 12-byte RTP (pt=8, **marker bit
   set / 0x88**) → `0x24` interleave on **channel 10**, ~40 ms frames, paced
   realtime against an absolute clock. **Drain the talk socket while sending** or
   TCP backpressure stalls cause breakup.

Code: `TalkbackSession` + `play_audio_clip()` in `dahua_client.py`. Only one talk
session at a time (single device talk channel). `DAHUA_TALK_MAX_SECONDS` (default
180) is a hard backstop against a stuck-open mic; push-to-talk also stops on the
WS closing.

### ⚠️ STATUS (2026-06-14): only VERBATIM REPLAY is audibly verified. Sending our OWN audio (`/talk`, `/say`) is NOT working — root cause unsolved.

What is CONFIRMED working (heard clearly at the door):
- **`DahuaConsole/talkback_replay.py <capture>`** — replays the captured DMSS
  talk frames **verbatim** (raw bytes, no decode/re-encode) and the door plays
  them clearly. This is the ONLY proven-audible path. Transport, handshake
  (6-PLAY), encrypt=2 video-hold, single-token session, relay — all proven good.
- Use capture **`522539f6-...vlan04.pcap`** (the real recorded test message,
  374 voice frames ~15s). NOT `2115d068-...` — that one is near-silent (only
  faint birdsong); testing with it wasted hours ("no audio" = nothing to play).

What is NOT working: **`/talk` (POST a WAV) and `/say` (piper TTS) produce no
audio**, even standalone via `play_audio_clip` (no API/container/RTSP-stream
contention). i.e. any path that builds frames from our OWN audio is silent,
while verbatim replay of captured frames is audible.

The unsolved problem — **the 0xf0 frame audio payload layout is not understood.**
Each captured voice frame is 692 bytes:
  `[0:40]`  DHAV header (magic, 0xf0 type, [8:12]seq, [12:16]len=692,
            [16:20]ts32 real-epoch +1/~sec, [20:22]ts16 +20/frame from 100,
            [22]=0x14 extlen, [23]=checksum sum(hdr[0:23])&0xff,
            [24:32]=0x83 ext w/ codec byte 0x0e (=PCM_ALAW per ffmpeg dhav.c),
            [32:40]=const sub-hdr)
  `[40:684]` 644-byte body
  `[684:692]` "dhav"+len trailer
BUT decoding `[40:684]` as a-law = NOISE (not the message). ffmpeg's DHAV demuxer
parses the stream as `pcm_alaw 16000Hz 15.0s` but its output is ALSO distorted
noise. Tried payload offsets 40, 44 (after a constant 4-byte `a4009922` sub-hdr),
300; codecs a-law/μ-law/ADPCM/raw-s16 — NONE decode to clean speech by ear.
`[40:44]` = constant `a4009922` across all frames (a per-frame audio sub-header).
So the real codec / payload offset for the 0xf0 audio body is STILL UNKNOWN.

Implication: we can replay captured audio, but cannot yet synthesize/encode our
own audio (TTS) into the device's format. **NEXT STEP: read FFmpeg
`libavformat/dhav.c` to get the authoritative 0xf0 audio-frame payload layout
(real sub-header size, payload offset, codec), then reverse it for encoding.**
The 4 dahua_client.py fixes this session (audioop a-law encoder, header checksum,
ts16/ts32, single-token session) are correct improvements and kept — but none
made `/talk` audible because the payload-layout problem is upstream of them.

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
