# Intercom Service — Development Notes

Research and reverse engineering lives in `/mnt/development/DahuaConsole/` (separate
project). Bring working solutions here once confirmed in that repo. Full protocol
analysis is in `DahuaConsole/NEXT_STEPS.md` — read it before touching this service.

## Current Status (2026-06-10)

| Feature | Status | Notes |
|---------|--------|-------|
| `/unlock` | **Working** | Cloud API via `dmss-di.dolynkcloud.com` |
| `/stream` | Not started | Blocked — see Stream section below |
| `/events` | Not started | DHIP event subscription, not yet researched |
| HA rest_command | Not wired | Pending `/unlock` being confirmed stable |
| n8n HTTP node | Not wired | Pending above |
| iOS Shortcut | Not wired | Pending above |

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

## Stream — Next Steps

Research is ongoing in `DahuaConsole`. Key findings so far:

- P2P stream uses proprietary **PTCP (Pseudo-TCP over UDP)** — not plain RTSP
- A newer **gateway API** (`dmss.dolynkcloud.com/gateway/`) was discovered that uses
  simple Bearer auth (no x-pcs-signature) and returns `playToken`/`playInfo` per device
- VTO camera is channel 1 on the VTH (`channelSn: BF08FE7PAJE2E59`, `channelIp: 192.168.1.100`)
- One more PCAPdroid capture needed: tap VTO live video to find the stream API call

Once the stream URL/approach is confirmed in `DahuaConsole`, promote the logic to
`dahua_client.py` here and implement `get_stream_url()`.

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
