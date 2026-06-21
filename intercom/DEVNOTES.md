# Intercom Service — Development Notes

Research and reverse engineering lives in `/mnt/development/DahuaConsole/` (separate
project). Bring working solutions here once confirmed in that repo. Full protocol
analysis is in `DahuaConsole/NEXT_STEPS.md` — read it before touching this service.

## Current Status (2026-06-21)

| Feature | Status | Notes |
|---------|--------|-------|
| `/unlock` | **Working** | Cloud API via `dmss-di.dolynkcloud.com` |
| `/frame` | **Working** | Clean full-frame JPEG. Serves instant cached last-frame on cold start (~2ms) before relay wakes |
| `/stream` | **Working** | Async MJPEG; releases relay on unclean disconnect (`is_disconnected()` + supervisor safety net) |
| `/events` | **Working** | SSE + HA webhook fire on doorbell ring via direct DHIP to VTH. CallID dedup is now time-windowed (was a permanent set that dropped re-used CallIDs) |
| `/talk`, `/talk/ws`, `/say` | **Working** | Door speaker — clip / live mic / piper TTS. Confirmed audible |
| Relay keepalive | **Working** | One token mint per viewing session, held by re-sending the same PLAY every ~40s (DMSS's mechanism). Idle-stops cleanly when no viewers. ~20x fewer cloud calls than the old per-65s rotation |
| HA WebRTC Camera card | **Working (verified 2026-06-21)** | Video + door audio + mic→door (open-mic). Sub-second talkback |
| HA Advanced Camera Card | **Working (verified 2026-06-21)** | Same backend; explicit hold-to-talk mic button. Sub-second talkback |
| HA two-way audio via `/talk-ui` | **Working** | Standalone web page (no HA client needed). ~2s talkback latency (non-WebRTC path) |
| Doorbell → HA notification | **Working** | press → `IgnoreInvite` → webhook → HA fires same second. Phone-side delivery via FCM (see notes) |

See **"Session 2026-06-21"** at the bottom for the keepalive/leak fixes, the HA card
verification, and the doorbell-notification investigation.

## Talkback — How It Works (SOLVED & WORKING — confirmed audible at door 2026-06-16/17)

Push audio UP to the door speaker over the same cloud relay we use for video.
Reverse-engineered from a clean OPNsense capture of a real DMSS talk session, plus
an Android-emulator + Frida key extraction. **We can send arbitrary generated
audio** (live mic via `/talk/ws`, WAV clips via `/talk`, piper TTS via `/say`).

> **Talkback LATENCY note:** there is a constant ~2.5–3s delay (mic → door speaker)
> that we could NOT eliminate. DMSS is sub-second over the *same* relay; we matched it
> on every signal readable on the wire (handshake, send pacing, timestamps, the relay's
> SDP response) yet stayed ~2.5–3s. Leading speculation: a server-side client-
> authorization/QoS difference in the encrypted TLS control plane we can't decrypt — so
> no frame/handshake tuning fixes it. Current code is the DMSS-aligned best-observed
> state (ts16 +40, single-socket 3-PLAY handshake). **Full forensic record of all ~9
> experiments + the captures is in the `dahua-intercom` repo:
> `DahuaConsole/TALKBACK_LATENCY_INVESTIGATION.md`.** Experiment git history is on
> branch `talk-singlesocket-latency`. Don't re-chase it with send-side tuning — that
> path is exhausted; the only untried angle is decrypting the TLS control plane
> (rooted phone + Frida).

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

> **All secret values live ONLY in `.env` (gitignored) — never in this repo.**
> The table below lists which `.env` var holds each, plus non-secret device IDs.

| Item | Where / Value |
|------|---------------|
| VTH IP | `192.168.40.55` |
| Device SN | `BG0142EPAJEF6EF` |
| Device credentials | `DAHUA_DEVICE_USERNAME` / `DAHUA_DEVICE_PASSWORD` (in `.env`) |
| Admin credentials | device panel only — not stored in this service |
| Cloud host | `dmss-di.dolynkcloud.com` |
| Bearer token | `DAHUA_BEARER_TOKEN` (in `.env`) — or minted from `DAHUA_ACCOUNT*` |
| pcs-username | `DAHUA_PCS_USERNAME` (in `.env`) |
| VTO channel SN | `BF08FE7PAJE2E59` |
| RandSalt | derived/captured value — in `.env` if needed, not committed |
| Gateway API host | `dmss.dolynkcloud.com/gateway/` *(new API, simpler auth)* |

## Bearer Token Expiry

The Bearer token is long-lived but will eventually expire. To recapture:
1. Install `DahuaConsole/DMSS_patched.apk` (cert pinning bypassed)
2. Run PCAPdroid with TLS key logging
3. Open DMSS, log in — capture the `Authorization: Bearer` header value
4. Update `DAHUA_BEARER_TOKEN` in `.env` and restart the container

---

## Session 2026-06-21 — keepalive/leak fixes, HA cards verified, doorbell investigation

### 1. Relay keepalive (replaces per-65s rotation) — the cloud-flag fix
**Why:** the old design minted a fresh cloud relay token every ~65s while watched
(~20x more cloud calls than DMSS). Heavy minting is what flagged the Dahua account
earlier (see DahuaConsole notes). DMSS holds ONE session alive for 20+ min by
re-sending the SAME `PLAY ...&trackID=31&method=0` (same time/digest token) every ~40s.
**Fix:** `_RelaySession` now runs a keepalive thread doing exactly that (commit `e1ff532`).
`STREAM_ROTATE=1` restores the old rotation if ever needed. Result: **1 token mint per
viewing session**, held indefinitely, re-mint only on genuine session death.

### 2. Viewer-leak / relay-never-stops fix
**Bug 1 (`/stream`):** the sync MJPEG generator could hang in `time.sleep()` on an
unclean client disconnect (tab crash, network drop) and never run `release_viewer()`,
so the relay stayed pinned open forever. **Fix:** `/stream` is now async and checks
`await request.is_disconnected()` each loop; plus a `StreamProxy` supervisor safety net
(`_last_activity` / `_hard_idle_grace`) stops the relay if there's no real consumption
even when `_viewers` leaks. (commit `7ee9566`)

**Bug 2 (the real one):** the keepalive feed loop in `_feed_one_relay_session` checked
only `self._running and proc.poll()` — NOT `self._relay_running`. Because the keepalive
holds the session (and ffmpeg) alive forever, there was no natural break, so when the
supervisor dropped the last viewer (`_relay_running=False`) the loop never noticed and
fed the relay until the container died. (The old rotation masked this — the 65s TTL used
to kill the session and break the loop.) **Fix:** add `self._relay_running` to the loop
condition (commit `d5dad70`). Verified: `kill -9` on a held `/stream` → ffmpeg drops to 0
at the 30s grace and stays 0; new viewer restarts cleanly; keepalive still holds one mint.

### 3. HA cards — BOTH verified working (the blocker was mixed content)
The HA dashboard (`/mnt/ha-config/dashboards/front-door.yaml`, YAML-mode) has 3 tabs:
- **Live** = `custom:webrtc-camera` (AlexxIT) — open-mic, no button.
- **Advanced** = `custom:advanced-camera-card` — explicit hold-to-talk mic button.
- **Classic** = generic camera + `/talk-ui` link + piper TTS quick-replies.

**The bug:** both cards pointed at `http://192.168.20.203:1984/` while HA is served over
HTTPS (required for the mic getUserMedia secure-context). The browser BLOCKED the http://
go2rtc connection as **mixed content** → card stuck "loading", no consumer, relay never
woke. On the Advanced card this showed as the ⓘ "stream hasn't loaded" icon even while
video played. **Fix:** point both cards at the HTTPS Traefik route
`https://intercom-go2rtc.app.vanheerden.ch` (webrtc-camera `server:`; advanced-camera-card
`go2rtc: url:`). Also committed `go2rtc.yaml` `api.origin: "*"` so the cross-origin
WebSocket from HA isn't rejected. The HA dashboard YAML lives in `/mnt/ha-config`, NOT
this repo.

**Verified (deliberate test, one at a time, frames-to-door confirmed):**
| Surface | Frames→door | Talkback latency | Mic control |
|---------|-------------|------------------|-------------|
| talk-ui | 215 | ~2s | Talk toggle |
| WebRTC Live | 287 | **sub-second** | open-mic (no button) |
| Advanced card | 307 | **sub-second** | hold-to-talk button |

Finding: both WebRTC paths are sub-second; talk-ui is ~2s. The go2rtc/WebRTC transport
beats talk-ui's path for uplink latency. Relay behaved perfectly (1 mint/session, clean
idle-stop after every close).

**Known rough edge:** the webrtc-camera card is OPEN-MIC — it captures the OS mic the
whole time it's connected (just to watch), which locks the mic from other apps (e.g.
WhatsApp). The mixer design (below) addresses this.

### 4. Doorbell ring → HA notification investigation
**Path:** physical press → VTO calls VTH → container's `subscribe_events` (persistent DHIP
event stream on TCP/5000) sees `IgnoreInvite+Start` → `on_ring` → fire-and-forget webhook
POST to HA → HA automation (`Front door ring`) fires → `notify` to phone via FCM.

**Findings (all measured on a common UTC clock):**
- **Detection is instant.** Container logs `RING!` and HA's automation flips
  `front_door_ringing` in the **same second**. No latency in the container or HA.
- **DMSS vs us:** DMSS detects the ring within a few seconds of us (lined up via the VTH
  call-log times once clock-corrected) — our pipeline is NOT the bottleneck.
- **The "62s" red herring:** the VTH's embedded `LocaleTime` string was ~62s slow AND on a
  different offset — comparing it to real time looked like a 62s delay. It wasn't; the
  container's own clock matched HA exactly. (VTH later NTP'd to `pool.ntp.org`, which
  fixed the drift but reset its timezone display to UTC — cosmetic, we use container UTC.)
- **CallID dedup bug found + fixed** (see Status table): the VTH cycles CallIDs 0–9 and
  reuses them; the old permanent `seen_call_ids` set silently dropped a genuine new press
  that reused an old CallID. Now time-windowed (`RING_DEDUP_WINDOW`, 5s) — only suppresses
  the same-instant duplicate burst.
- **A real press emits a burst:** `AutoRegister`, `IgnoreInvite`, `sdLvMsgAndSnapshot`,
  `sdReadFlag`, `sdSnapshot` (all same instant). We correctly pick `IgnoreInvite+Start`.
  Set `DAHUA_EVENT_DEBUG=1` to log every raw event (left on for ongoing diagnostics).

**Remaining (phone-side, not our pipeline):** HA's FCM notification can lag DMSS's instant
ring. Suspected cause = **on-device contention**: DMSS receives the doorbell as a *call*
(SIP/VoIP, full-screen-intent via "Appear on top") that **bypasses notification
permissions** (it rang even after notifications were revoked + force-stop), and that
screen-takeover preempts HA's FCM notification until it ends. Mitigation = stop DMSS
handling the call (disable the app / revoke "Appear on top"). DMSS's dedicated push socket
will always edge out FCM regardless. This is a phone-settings issue, not a code issue.

### 5. Mixer design recorded (NOT built)
The contention (multi-client / multi-mode / household) and the open-mic problem both point
to one architecture: the container holds ONE door talkback session and mixes/arbitrates
client uplinks, with the mic DECOUPLED from the downlink (real push-to-talk). Likely drops
the go2rtc exec backchannel for a direct client→container uplink (closer to talk-ui's
model). Full design captured in auto-memory `intercom-mixer-design`. Design pass pending.
