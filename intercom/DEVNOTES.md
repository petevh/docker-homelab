# Intercom Service — Development Notes

Research and reverse engineering lives in `/mnt/development/DahuaConsole/` (separate
project). Bring working solutions here once confirmed in that repo. Full protocol
analysis is in `DahuaConsole/NEXT_STEPS.md` — read it before touching this service.

## Current Status (2026-06-17)

| Feature | Status | Notes |
|---------|--------|-------|
| `/unlock` | **Working** | Cloud API via `dmss-di.dolynkcloud.com` |
| `/frame` | **Working** | Clean full-frame JPEG (RTP de-interleave fix, 2026-06-12). Wakes on-demand relay via `touch()` (2026-06-17) |
| `/stream` | **Working** | Clean MJPEG |
| `/events` | **Working** | SSE stream fires on doorbell ring via direct DHIP to VTH |
| `/talk` | **Working** | POST a WAV/PCM clip → door speaker. Confirmed audible at door 2026-06-16 |
| `/talk/ws` | **Working** | WebSocket push-to-talk; confirmed audible at door via `/talk-ui` 2026-06-17 (live mic) |
| `/say` | **Working** | piper TTS → door speaker; confirmed audible at door 2026-06-16 |
| HA generic_camera | Ready to wire | Stream quality fixed |
| HA webhook trigger | Ready to wire | Consume `/events` SSE |
| HA two-way audio | **Working via `/talk-ui`** | Duplex web page: HLS downlink + WS mic uplink. Downlink ~3s latency (HLS); uplink low-latency |

## Talkback — How It Works (SOLVED & WORKING — confirmed audible at door 2026-06-16/17)

Push audio UP to the door speaker over the same cloud relay we use for video.
Reverse-engineered from a clean OPNsense capture of a real DMSS talk session, plus
an Android-emulator + Frida key extraction. **We can send arbitrary generated
audio** (live mic via `/talk/ws`, WAV clips via `/talk`, piper TTS via `/say`).

1. **Hold an `encrypt=2` video session** (`/real/1/1/encrypt/RTSV1`). Precondition:
   an idle door 503s the talk PLAY, AND an `encrypt=0` session silently **mutes**
   the uplink audio. `encrypt=2` is mandatory.
2. **6-PLAY handshake** on the `encrypt=2` visualtalk relay, one socket:
   `trackID=31&method=0`, `talktype=talk&trackID=64&method=0`, `trackID=6&method=1`,
   `method=2`, `trackID=31&method=1`, `trackID=70&method=3`. Audio flows after the 6th.
3. **Push audio**: a-law (`audioop.lin2alaw`) @16 kHz, 640 samples/frame (40 ms),
   payload built by the HYBRID recipe below, wrapped in a `DHAV 0xf0` frame → 12-byte
   RTP (pt=8, **marker bit / 0x88**) → `0x24` interleave on **channel 10**, paced
   ~40 ms realtime. **Drain the talk socket while sending** or TCP backpressure stalls.

Code: `TalkbackSession` (`_dhav_audio_frame`, `push`, `_send_frame`) +
`play_audio_clip()` in `dahua_client.py`. One talk session at a time (single device
talk channel — concurrent `/talk/ws` opens get 403). `DAHUA_TALK_MAX_SECONDS`
(default 180) is a hard backstop; push-to-talk also stops on the WS closing.

### THE AUDIO PAYLOAD IS HYBRID-ENCRYPTED (the thing that took weeks)

Each 692-byte `0xf0` voice frame carries a **640-byte a-law payload** that is NOT
plaintext PCMA and NOT fully encrypted — it is a **hybrid split**:

```
payload[0:256]   = AES-128-ECB encrypted a-law   (key below)
payload[256:640] = PLAIN a-law                    (not encrypted)
```

- Key = a static 16-char ASCII string used directly as the 16-byte AES-128 key.
  **The key value is NOT recorded here (no secrets in the repo).** It lives only in
  the gitignored `.env` as `DAHUA_TALK_AES_KEY` (hex), read by `dahua_client.py`
  (`TALK_AES_KEY = bytes.fromhex(os.environ["DAHUA_TALK_AES_KEY"])`). Also in 1Password.
  It was extracted via Android emulator + Frida memory dump + brute-force (no MIKEY
  handshake needed — the effective key is static across sessions, proven because
  2-day-old captured ciphertext replays cleanly). To re-extract if ever lost, see
  memory `dahua_talkback_verified_audio.md` (the emulator+Frida method).
- ENCODE (sending): `enc = AES_ECB(key, alaw[:256]) + alaw[256:]`. This is exactly
  what `_dhav_audio_frame` does. Encrypting all 640 bytes → distorted; plain all 640
  → distorted; **only this 256/384 split is correct.**
- Frame layout: 24B header (magic, 0xf0, [8:12]seq, [12:16]len, [16:20]ts32=epoch,
  [20:22]ts16=100+idx*20, [22]=0x14 extlen, [23]=checksum sum(hdr[0:23])&0xff) +
  20B EXT (`83010e049500000000010000b308fba6a4009922`) + payload + `dhav` + len(LE32).

Full RE history (the silent-capture false trail, the cipher hunt, the key
extraction) is in memory `dahua_talkback_verified_audio.md` and
`DahuaConsole/NEXT_STEPS.md`. Working standalone sender: `DahuaConsole/stream_encrypted.py`.

### Why 640, NOT 644 (settle this permanently)

Two different numbers from two different code paths — they are NOT in conflict, and
**production is correct at 640. Do not "fix" it to 644.**

- **640** = the production frame size in `TALK_FRAME_SAMPLES`. The real device payload
  is **640 a-law samples** (= 40.0 ms @16 kHz). Confirmed authoritative by `ffprobe`
  on the raw DHAV stream (every packet size=640, pcm_alaw 16000Hz) and by the hybrid
  256+384=640 split. This is what is audible at the door.
- **644** = a value used only by the OLD verbatim-replay script
  `DahuaConsole/talkback_replay.py` (RTP ts += 644, frame dur 644/16000 = 40.25 ms).
  It came from an early mis-measurement of the captured frame body before the layout
  was understood (the body was wrongly read as `[40:684]` = 644 bytes including 4 bytes
  of sub-header; the actual audio is `[44:684]` = 640). Replay still "worked" because
  it sends captured bytes verbatim and a ~0.25 ms/frame pacing error is inaudible.
- A 2026-06-14 session briefly "fixed" production 640→644 on the theory that 644 was
  the verified value. That theory was wrong (it predated the silent-capture and
  encryption findings). The change was reverted/never needed. **If you see a note
  saying "set 640→644", it is stale — ignore it.**

> CONCRETE EXAMPLE OF THE TRAP (this section exists to prevent repeating it):
> session `2f869d30` (2026-06-17 transcript) was about to ship 640→644 on a
> "692 = 40-header + 644-audio + 8-trailer, byte-exact" argument — BEFORE the
> payload encryption was understood. The math only *looks* exact: it counts the
> 4-byte audio sub-header as audio (real layout is 24B hdr + 20B ext + **640**
> payload + 8B trailer = 692). That session never door-tested 644; it correctly
> refused to declare victory without ears at the door. Git proves the resolution:
> `TALK_FRAME_SAMPLES` was set to 640 once at the first talkback commit (`a2d781a`)
> and NEVER changed — the 644 edit never landed, and the later AES/hybrid encryption
> commits (`40171d3`, `87adaae`) and the confirmed-audible result were all built on
> 640. The real cause of silence was the missing encryption, never the frame size.

### Downlink: there is (effectively) NO return audio on the talk channel

The audio you HEAR from the door does **not** come back over the talkback channel.
The `TalkbackSession` talk socket is treated as **upload-only**: `_drain()` reads and
**discards** whatever the door sends back on it (we drain solely to stop TCP
backpressure stalling our send — we never decode it). Repeated inspection found no
usable return-audio stream there.

Instead, **the door's mic audio is carried inside the VIDEO session's audio track.**
`_ffmpeg_cmd` maps `0:a:0?` off the video DHAV stream (the `connect_relay` PLAY) and
transcodes it to AAC; that is what `/stream`/HLS/`/talk-ui` plays. So "two-way audio"
is: **uplink = talk channel (encrypt=2, hybrid frames); downlink = the video feed's
audio track.** They are independent paths. This is why the downlink inherits the
video path's latency (see below) while the uplink is low-latency.

### `/talk-ui` duplex page + latency

`talk_ui.html` plays the downlink (door video + audio) via **HLS** (`/hls/frontdoor`,
mediamtx mpegts HLS, hls.js `liveSyncDurationCount:3`) and sends the mic uplink over
the **WebSocket** `/talk/ws`. Confirmed working at the door 2026-06-17.

KNOWN LIMITATION: ~3 s downlink delay (HLS segment buffering; mpegts HLS was chosen
over LL-HLS for iOS-Safari compatibility). The uplink is near-live, so it's
asymmetric. Reducing it is a DOWNLINK-only display concern (tune HLS, or a WebRTC
downlink) and is **completely independent of the talkback uplink** — WebRTC/go2rtc
can NOT carry the uplink (the door requires the proprietary hybrid-encrypted 0xf0
a-law frames over encrypt=2; standard WebRTC/Opus would be rejected). Do not redesign
the working uplink to use them.

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
