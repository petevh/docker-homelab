#!/usr/bin/env python3
"""
Dahua VTH Intercom API
Wraps dahua_client.py as a FastAPI service callable from
Home Assistant, n8n, iOS Shortcuts, Tasker, or curl.

POST /unlock          — unlock the door (cloud API)
GET  /frame           — latest JPEG snapshot from VTO camera
GET  /stream          — MJPEG multipart stream from VTO camera
GET  /events          — SSE stream of doorbell ring events
POST /talk            — play an audio clip (WAV/PCM) out the door speaker
WS   /talk/ws         — live push-to-talk (stream 16-bit PCM frames)
GET  /health          — health check

Home Assistant config:
    camera:
      - platform: generic
        name: Front Door
        still_image_url: http://<host>:8000/frame
        stream_source: http://<host>:8000/stream

    trigger:
      - platform: webhook
        webhook_id: front_door   # fired by /events consumer, or use doorbell_monitor_vth.py
"""

import asyncio
import os
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import io
import re
import wave

import requests

from fastapi import (
    FastAPI, HTTPException, Depends, Security, Request, WebSocket,
    WebSocketDisconnect,
)
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import Response, StreamingResponse, HTMLResponse
from pydantic import BaseModel

from dahua_client import (
    Credentials,
    DahuaError,
    StreamProxy,
    TalkbackSession,
    play_audio_clip,
    subscribe_events,
    unlock_door,
    with_bearer_retry,
)

# ---------------------------------------------------------------------------
# Config — all values from environment, no defaults for secrets
# ---------------------------------------------------------------------------
# API keys: named & individually revocable. DAHUA_API_KEYS="pete:abc,maid:def"
# maps a label → key so you can revoke one person without affecting others, and
# logs show WHO called. DAHUA_API_KEY (single, unlabelled) still works as a key
# labelled "default" for backward compatibility. Tailscale remains the network
# layer; these keys are the per-user application layer.
def _parse_api_keys() -> dict:
    keys: dict[str, str] = {}
    raw = os.environ.get("DAHUA_API_KEYS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        label, _, key = pair.partition(":")
        if key:
            keys[key.strip()] = label.strip() or "unnamed"
    single = os.environ.get("DAHUA_API_KEY", "")
    if single:
        keys.setdefault(single, "default")
    return keys                       # {key_value: label}

API_KEYS         = _parse_api_keys()
AUTH_ENABLED     = bool(API_KEYS)
BEARER_TOKEN     = os.environ.get("DAHUA_BEARER_TOKEN", "")   # optional static token (fallback)
ACCOUNT          = os.environ.get("DAHUA_ACCOUNT", "")        # cloud login (preferred: self-refreshing)
ACCOUNT_PASSWORD = os.environ.get("DAHUA_ACCOUNT_PASSWORD", "")
AREA_CODE        = os.environ.get("DAHUA_AREA_CODE", "971")
COUNTRY          = os.environ.get("DAHUA_COUNTRY", "AE")
PCS_USERNAME     = os.environ.get("DAHUA_PCS_USERNAME", "")
DEVICE_SN        = os.environ.get("DAHUA_DEVICE_SN", "")
DEVICE_USERNAME  = os.environ.get("DAHUA_DEVICE_USERNAME", "user")
DEVICE_PASSWORD  = os.environ.get("DAHUA_DEVICE_PASSWORD", "")
CHANNEL          = int(os.environ.get("DAHUA_CHANNEL", "1"))
DOOR_INDEX       = int(os.environ.get("DAHUA_DOOR_INDEX", "0"))

TALK_MAX_SECONDS = float(os.environ.get("DAHUA_TALK_MAX_SECONDS", "180"))  # hard backstop

VTH_HOST         = os.environ.get("DAHUA_VTH_HOST", "")
VTH_PORT         = int(os.environ.get("DAHUA_VTH_PORT", "5000"))
VTH_USERNAME     = os.environ.get("DAHUA_VTH_USERNAME", "user")
VTH_PASSWORD     = os.environ.get("DAHUA_VTH_PASSWORD", "")

STREAM           = int(os.environ.get("DAHUA_STREAM", "0"))        # 0 = main/HD 1280x720, 1 = sub 352x288
STREAM_WIDTH     = int(os.environ.get("DAHUA_STREAM_WIDTH", "0"))   # 0 = native resolution
STREAM_QUALITY   = int(os.environ.get("DAHUA_STREAM_QUALITY", "5"))
RTSP_PUBLISH_URL = os.environ.get("DAHUA_RTSP_PUBLISH_URL",
                                  "rtsp://127.0.0.1:8554/frontdoor")
# Optional: push doorbell rings straight to a Home Assistant webhook
# (e.g. http://192.168.20.50:8123/api/webhook/front_door). Fire-and-forget,
# in addition to the /events SSE stream.
HA_WEBHOOK_URL   = os.environ.get("HA_WEBHOOK_URL", "")
# Piper TTS voice for /say (path to the .onnx model baked into the image).
PIPER_VOICE      = os.environ.get("PIPER_VOICE", "en_US-amy-medium")
PIPER_MODEL      = os.environ.get("PIPER_MODEL", f"/app/piper/{PIPER_VOICE}.onnx")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dahua_api")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
# Shared credential holder — prefers cloud account login (self-refreshing Bearer);
# falls back to a static DAHUA_BEARER_TOKEN if no account credentials are set.
_creds = Credentials(
    bearer=BEARER_TOKEN,
    pcs_username=PCS_USERNAME,
    account=ACCOUNT,
    password=ACCOUNT_PASSWORD,
    area_code=AREA_CODE,
    country=COUNTRY,
)
_stream_proxy: Optional[StreamProxy] = None
_ring_listeners: list[asyncio.Queue] = []
_ring_listeners_lock = threading.Lock()

# Only one talk uplink at a time (the device has a single talk channel).
_talk_lock = threading.Lock()


def _talk_ready() -> bool:
    """True if we have enough config to open a talk session."""
    return bool((BEARER_TOKEN or _creds.can_refresh) and PCS_USERNAME and DEVICE_SN)


def _new_talk_session() -> TalkbackSession:
    return TalkbackSession(
        creds=_creds, device_sn=DEVICE_SN, pcs_username=PCS_USERNAME,
        channel=CHANNEL, max_seconds=TALK_MAX_SECONDS,
    )


def _post_ha_webhook(call_id: str, local_time: str) -> None:
    try:
        requests.post(
            HA_WEBHOOK_URL,
            json={"event": "doorbell_ring", "call_id": call_id, "local_time": local_time},
            timeout=3,
        )
    except Exception as e:
        log.warning("HA webhook POST failed: %s", e)


def _on_ring(call_id: str, local_time: str) -> None:
    # Pre-warm the cloud relay the instant the bell rings, so video is already
    # up by the time someone opens the app/talk-ui to answer (closes the ~8s
    # cold-start gap for the doorbell-answer flow). touch() starts the relay and
    # resets the idle grace; repeated touches below extend the warm window so it
    # doesn't idle out in the seconds before the door is answered.
    if _stream_proxy:
        def _prewarm():
            for _ in range(8):          # keep warm ~40s past the ring
                _stream_proxy.touch()
                time.sleep(5)
        threading.Thread(target=_prewarm, daemon=True).start()

    payload = f'data: {{"event":"doorbell_ring","call_id":"{call_id}","local_time":"{local_time}"}}\n\n'
    with _ring_listeners_lock:
        for q in list(_ring_listeners):
            try:
                q.put_nowait(payload)
            except Exception:
                pass
    # Push straight to Home Assistant (fire-and-forget on its own thread so a
    # slow/unreachable HA can't stall detection of the next ring).
    if HA_WEBHOOK_URL:
        threading.Thread(
            target=_post_ha_webhook, args=(call_id, local_time), daemon=True
        ).start()


def _start_event_monitor() -> None:
    if not VTH_HOST or not VTH_PASSWORD:
        log.warning("DAHUA_VTH_HOST or DAHUA_VTH_PASSWORD not set — /events will not work")
        return

    def _loop():
        while True:
            try:
                subscribe_events(
                    vth_host=VTH_HOST,
                    vth_port=VTH_PORT,
                    username=VTH_USERNAME,
                    password=VTH_PASSWORD,
                    on_ring=_on_ring,
                )
            except Exception as e:
                log.warning("Event monitor error: %s — reconnecting in 5s", e)
                time.sleep(5)

    threading.Thread(target=_loop, daemon=True).start()
    log.info("Event monitor started (VTH %s:%d)", VTH_HOST, VTH_PORT)


# ---------------------------------------------------------------------------
# Startup/shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _stream_proxy

    if not AUTH_ENABLED:
        log.warning("No API keys configured (DAHUA_API_KEYS/DAHUA_API_KEY) — all requests are unauthenticated.")
    else:
        log.info("API auth enabled — %d key(s): %s", len(API_KEYS),
                 ", ".join(sorted(set(API_KEYS.values()))))
    if not _creds.can_refresh and not BEARER_TOKEN:
        log.warning("No DAHUA_ACCOUNT/DAHUA_ACCOUNT_PASSWORD and no DAHUA_BEARER_TOKEN — "
                    "cloud unlock/stream calls will fail.")
    elif _creds.can_refresh:
        log.info("Cloud auth: account login (self-refreshing Bearer) for %s", ACCOUNT)
    else:
        log.info("Cloud auth: static DAHUA_BEARER_TOKEN (will not self-refresh)")
    for var, name in [
        (PCS_USERNAME, "DAHUA_PCS_USERNAME"),
        (DEVICE_SN,    "DAHUA_DEVICE_SN"),
        (DEVICE_PASSWORD, "DAHUA_DEVICE_PASSWORD"),
    ]:
        if not var:
            log.warning("%s is not set — unlock/stream calls will fail.", name)

    has_bearer = BEARER_TOKEN or _creds.can_refresh
    if has_bearer and PCS_USERNAME and DEVICE_SN:
        _stream_proxy = StreamProxy(
            creds=_creds,
            device_sn=DEVICE_SN,
            channel=CHANNEL,
            stream=STREAM,
            width=STREAM_WIDTH,
            quality=STREAM_QUALITY,
            rtsp_publish_url=RTSP_PUBLISH_URL,
        )
        _stream_proxy.start()
        log.info("Stream proxy started (RTSP: %s)", RTSP_PUBLISH_URL)

    _start_event_monitor()

    yield

    if _stream_proxy:
        _stream_proxy.stop()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Dahua VTH Intercom API",
    description="Door unlock, camera stream, and doorbell events for Dahua VTH2622GW-W",
    version="2.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# API key auth
# ---------------------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _label_for(token: Optional[str]) -> Optional[str]:
    """Return the label for a key, or None if unknown."""
    return API_KEYS.get(token) if token else None


def verify_api_key(request: Request, key: Optional[str] = Security(api_key_header)):
    """Returns the caller's key label (for logging). Raises 401 on a bad/missing
    key. If no keys are configured at all, auth is open (returns 'anonymous')."""
    if not AUTH_ENABLED:
        return "anonymous"
    token = key or request.query_params.get("key")
    label = _label_for(token)
    if label is None:
        client = request.client.host if request.client else "?"
        log.warning("401 — invalid/missing API key from %s on %s", client, request.url.path)
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return label


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class UnlockResponse(BaseModel):
    success: bool
    message: str
    by: str = ""          # caller's API-key label (for HA logbook / audit)


class HealthResponse(BaseModel):
    status: str
    device_sn: str
    api_key_configured: bool
    api_keys_count: int
    stream_running: bool
    events_configured: bool
    talk_configured: bool
    talk_active: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="ok",
        device_sn=DEVICE_SN,
        api_key_configured=AUTH_ENABLED,
        api_keys_count=len(API_KEYS),
        stream_running=bool(_stream_proxy and _stream_proxy.get_frame()),
        events_configured=bool(VTH_HOST and VTH_PASSWORD),
        talk_configured=_talk_ready(),
        talk_active=_talk_lock.locked(),
    )


@app.get("/hls/{path:path}")
def hls_proxy(path: str):
    """Proxy mediamtx HLS (localhost:8888) so the talk-ui page fetches it
    same-origin (no CORS, no extra exposed port, works through Traefik).
    Carries the door's video+audio for the duplex page's downlink.
    No API key on HLS itself (it's only the camera feed, and is already gated
    by Traefik's IP-allowlist + Tailscale); this keeps relative playlist/segment
    URLs clean so the browser's HLS player resolves them correctly."""
    if _stream_proxy:
        _stream_proxy.touch()   # on-demand: start/keep the relay alive while watched
        # On a cold start the relay+mediamtx need a moment to produce the playlist.
        if path.endswith("index.m3u8"):
            for _ in range(20):
                try:
                    if requests.get("http://127.0.0.1:8888/" + path, timeout=2).status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.5)
    try:
        r = requests.get(f"http://127.0.0.1:8888/{path}", timeout=10)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HLS upstream: {e}")
    ct = r.headers.get("Content-Type", "application/octet-stream")
    return Response(content=r.content, media_type=ct,
                    headers={"Cache-Control": "no-cache"})


@app.post("/whep/{path:path}")
async def whep_offer(path: str, request: Request):
    """Proxy mediamtx WebRTC/WHEP signaling (localhost:8889) same-origin so the
    talk-ui page negotiates over HTTPS via Traefik (no mixed-content, no extra
    cert). [SPIKE] Low-latency (<1s) downlink alternative to HLS.

    Only the SDP OFFER/ANSWER handshake goes through here (plain HTTP). The actual
    audio/video is UDP on :8189 (DTLS-SRTP, self-encrypted) and flows DIRECTLY
    browser<->mediamtx — it does NOT pass through this proxy or Traefik.
    The 'Location' header from mediamtx is rewritten to a same-origin /whep/... URL
    so the browser's follow-up PATCH/DELETE come back through here too."""
    if _stream_proxy:
        _stream_proxy.touch()   # on-demand: wake/keep the relay while watched
    body = await request.body()
    ctype = request.headers.get("Content-Type", "application/sdp")
    # Cold-start race: on first load the browser POSTs the offer BEFORE the relay
    # has published the stream to mediamtx, so mediamtx returns 404 ("no one is
    # publishing"). The old check (OPTIONS < 500) passed on 404 and negotiated too
    # early → fell back to HLS. Instead, retry the actual offer until mediamtx
    # accepts it (non-404) or we time out (~15s, covers the cold relay+ffmpeg).
    r = None
    for _ in range(30):
        try:
            r = requests.post(f"http://127.0.0.1:8889/{path}", data=body,
                              headers={"Content-Type": ctype},
                              timeout=10, allow_redirects=False)
            if r.status_code != 404:
                break
        except Exception as e:
            r = None
            last_err = e
        time.sleep(0.5)
    if r is None:
        raise HTTPException(status_code=502, detail=f"WHEP upstream: {last_err}")
    headers = {}
    loc = r.headers.get("Location")
    if loc:
        # rewrite absolute/relative upstream resource path to same-origin /whep/...
        headers["Location"] = "/whep/" + loc.lstrip("/")
    if "ETag" in r.headers:
        headers["ETag"] = r.headers["ETag"]
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("Content-Type", "application/sdp"),
                    headers=headers)


@app.api_route("/whep/{path:path}", methods=["PATCH", "DELETE"])
async def whep_ice(path: str, request: Request):
    """Proxy WHEP ICE-trickle (PATCH) and teardown (DELETE) to mediamtx."""
    body = await request.body()
    try:
        r = requests.request(request.method, f"http://127.0.0.1:8889/{path}",
                             data=body,
                             headers={"Content-Type": request.headers.get("Content-Type", "application/trickle-ice-sdpfrag")},
                             timeout=10, allow_redirects=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"WHEP upstream: {e}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("Content-Type", "application/sdp"))


@app.get("/hls.min.js")
def hls_js():
    """Vendored hls.js so the talk page has no external CDN dependency."""
    try:
        with open("/app/hls.min.js", "rb") as f:
            return Response(content=f.read(), media_type="application/javascript")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="hls.min.js not found")


@app.get("/talk-ui", response_class=HTMLResponse)
def talk_ui():
    """Duplex talk web page: live camera + mic (push to /talk/ws) + unlock.
    Open on a phone (mic needs the page; HA dashboards can't capture mic).
    Pass ?key=<api-key>. HA 'Talk' button links here."""
    try:
        with open("/app/talk_ui.html", "r") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="talk_ui.html not found")


@app.post("/unlock", response_model=UnlockResponse)
def unlock(auth=Depends(verify_api_key)):
    """
    Trigger door unlock via Dahua P2P cloud API.
    Callable from HA rest_command, n8n, iOS Shortcuts, Tasker, curl.
    """
    log.info("Unlock request by '%s' → device %s", auth, DEVICE_SN)
    try:
        result = with_bearer_retry(_creds, lambda b: unlock_door(
            bearer_token=b,
            pcs_username=PCS_USERNAME,
            device_sn=DEVICE_SN,
            device_username=DEVICE_USERNAME,
            device_password=DEVICE_PASSWORD,
            channel=CHANNEL,
            door_index=DOOR_INDEX,
        ))
        if result:
            log.info("Unlock successful (by '%s')", auth)
            return UnlockResponse(success=True, message="Door unlocked", by=auth)
        raise HTTPException(status_code=502, detail="Unlock command rejected by device")
    except HTTPException:
        raise
    except DahuaError as e:
        log.error("Unlock error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.error("Unlock error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/wake")
def wake():
    """Wake the on-demand relay. Called by mediamtx's runOnDemand when a reader
    (go2rtc/WebRTC/HA) connects to a path — those consume from mediamtx directly
    and otherwise never wake the relay, so the WebRTC card was dead on a cold start
    until something else (talk-ui /frame) warmed it. No auth: localhost-only call
    from mediamtx, and it only starts the camera relay (no control/unlock)."""
    if _stream_proxy:
        _stream_proxy.touch()
    return {"waking": True}


@app.get("/frame")
def frame(auth=Depends(verify_api_key)):
    """Latest JPEG snapshot from VTO camera. Use as HA still_image_url."""
    if not _stream_proxy:
        raise HTTPException(status_code=503, detail="Stream proxy not configured")
    # On-demand: a snapshot request wakes the relay (and resets the idle grace),
    # same as an HLS fetch. Without this the relay only runs while /stream or /talk
    # holds a viewer, so HA still_image_url / notification thumbnails 503 when idle.
    _stream_proxy.touch()
    jpeg = _stream_proxy.get_frame()
    if not jpeg:
        raise HTTPException(status_code=503, detail="No frame available yet — stream starting up")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.get("/stream")
async def stream(request: Request, auth=Depends(verify_api_key)):
    """
    MJPEG multipart stream from VTO camera.
    Use as HA stream_source or open directly in a browser/VLC.
    """
    if not _stream_proxy:
        raise HTTPException(status_code=503, detail="Stream proxy not configured")

    async def generate():
        _stream_proxy.acquire_viewer()   # on-demand: start relay while watched
        last_frame = b""
        try:
            while True:
                # Detect client disconnect (incl. UNCLEAN: tab crash, network drop).
                # Without this, a sync MJPEG loop can hang in the sleep below and never
                # hit finally → acquire_viewer leaks → relay never idles. is_disconnected
                # makes the release reliable.
                if await request.is_disconnected():
                    break
                _stream_proxy.mark_active()   # this viewer is alive (safety-net heartbeat)
                jpeg = _stream_proxy.get_frame()
                if jpeg and jpeg != last_frame:
                    last_frame = jpeg
                    header = (
                        b"\r\n--dahuaframe\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    )
                    yield header + jpeg
                else:
                    await asyncio.sleep(0.05)
        finally:
            _stream_proxy.release_viewer()

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=dahuaframe",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/events")
async def events(request: Request, auth=Depends(verify_api_key)):
    """
    Server-Sent Events stream of doorbell ring events.
    Each ring sends: data: {"event":"doorbell_ring","call_id":"...","local_time":"..."}

    Home Assistant REST sensor or Node-RED can consume this.
    """
    if not VTH_HOST or not VTH_PASSWORD:
        raise HTTPException(status_code=503, detail="VTH not configured (DAHUA_VTH_HOST / DAHUA_VTH_PASSWORD)")

    queue: asyncio.Queue = asyncio.Queue()
    with _ring_listeners_lock:
        _ring_listeners.append(queue)

    async def generate():
        try:
            yield "data: {\"event\":\"connected\"}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30)
                    yield payload
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            with _ring_listeners_lock:
                try:
                    _ring_listeners.remove(queue)
                except ValueError:
                    pass

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Talkback — push audio UP to the door speaker
# ---------------------------------------------------------------------------

def _pcm_from_upload(body: bytes, content_type: str) -> tuple[bytes, int]:
    """Return (16-bit LE mono PCM bytes, sample_rate) from an uploaded body.
    Accepts a RIFF/WAV (any rate, mono/stereo→mono) or raw 16-bit PCM (assumed
    16kHz mono, or audio/L16;rate=NNNN)."""
    if body[:4] == b"RIFF":
        w = wave.open(io.BytesIO(body), "rb")
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        data = w.readframes(w.getnframes())
        w.close()
        if width != 2:
            raise HTTPException(status_code=415, detail="WAV must be 16-bit PCM")
        if ch == 2:                       # down-mix stereo → mono (take left)
            import struct as _s
            s = _s.unpack("<%dh" % (len(data) // 2), data)
            data = _s.pack("<%dh" % (len(s) // 2), *s[0::2])
        return data, rate
    # raw PCM: honor audio/L16;rate=NNNN, else assume 16kHz
    rate = 16000
    m = re.search(r"rate=(\d+)", content_type or "")
    if m:
        rate = int(m.group(1))
    return body, rate


@app.post("/talk")
async def talk(request: Request, auth=Depends(verify_api_key)):
    """Play an audio clip out the door speaker (TTS, announcements, push-a-file).

    Body: a WAV file (any rate) or raw 16-bit LE mono PCM. For raw PCM set
    Content-Type: audio/L16;rate=16000. Blocks for the clip's real duration.
    Callable from HA rest_command / curl. One talk session at a time.
    """
    if not _talk_ready():
        raise HTTPException(status_code=503, detail="Talkback not configured (need Bearer/account + DAHUA_PCS_USERNAME + DAHUA_DEVICE_SN)")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body — send a WAV or raw 16-bit PCM")
    pcm, rate = _pcm_from_upload(body, request.headers.get("content-type", ""))
    log.info("Talk (clip) by '%s' — %d bytes @ %d Hz", auth, len(pcm), rate)

    if not _talk_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A talk session is already active")
    try:
        # run the blocking relay I/O off the event loop
        frames = await asyncio.to_thread(
            play_audio_clip, _creds, DEVICE_SN, PCS_USERNAME, pcm, rate,
            CHANNEL, TALK_MAX_SECONDS,
        )
        return {"success": True, "frames": frames,
                "seconds": round(frames * 640 / 16000, 2), "by": auth}
    except DahuaError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.error("Talk error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _talk_lock.release()


def _tts_to_wav(text: str) -> bytes:
    """Synthesize text → 16-bit mono WAV bytes via piper. Blocking (run off-loop)."""
    voice = _get_piper_voice()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        voice.synthesize_wav(text, wf)    # piper writes a complete RIFF/WAV
    return buf.getvalue()


_piper_voice = None
_piper_lock = threading.Lock()


def _get_piper_voice():
    """Load (once) and cache the piper voice model."""
    global _piper_voice
    if _piper_voice is None:
        with _piper_lock:
            if _piper_voice is None:
                from piper import PiperVoice
                _piper_voice = PiperVoice.load(PIPER_MODEL,
                                               config_path=f"{PIPER_MODEL}.json")
    return _piper_voice


@app.post("/say")
@app.get("/say")
async def say(request: Request, text: str = "", auth=Depends(verify_api_key)):
    """Speak `text` out the door speaker via piper TTS.

    Pass text as ?text=... (GET) or JSON/body {"text": "..."} (POST). Synthesizes
    to WAV then plays via the same relay as /talk. One talk session at a time.
    Callable from HA rest_command — HA passes plain text, no audio handling.
    """
    if not _talk_ready():
        raise HTTPException(status_code=503, detail="Talkback not configured")
    # text may arrive as query param, JSON body, or raw body
    msg = text or request.query_params.get("text", "")
    if not msg:
        try:
            j = await request.json()
            msg = (j or {}).get("text", "")
        except Exception:
            body = await request.body()
            msg = body.decode("utf-8", "ignore").strip()
    msg = (msg or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="No text — pass ?text=... or {\"text\":\"...\"}")

    log.info("Say by '%s' — %r", auth, msg[:120])
    if not _talk_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A talk session is already active")
    try:
        wav = await asyncio.to_thread(_tts_to_wav, msg)
        pcm, rate = _pcm_from_upload(wav, "audio/wav")
        frames = await asyncio.to_thread(
            play_audio_clip, _creds, DEVICE_SN, PCS_USERNAME, pcm, rate,
            CHANNEL, TALK_MAX_SECONDS,
        )
        return {"success": True, "text": msg, "frames": frames,
                "seconds": round(frames * 640 / 16000, 2), "by": auth}
    except DahuaError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.error("Say error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _talk_lock.release()


@app.post("/talk/stream")
async def talk_stream(request: Request):
    """Ingest a CONTINUOUS raw-PCM mic stream over a streaming HTTP POST and feed
    it to the door, same as /talk/ws but for a producer that can't speak WebSocket.

    Built for the HA Advanced Camera Card path: go2rtc's exec backchannel pipes the
    card's mic to ffmpeg, which POSTs it here as a never-ending body, e.g.
        ffmpeg -f <fmt> -i - -ar 16000 -ac 1 -f s16le -content_type application/octet-stream \
               -method POST http://<intercom>:8000/talk/stream?key=<API_KEY>&rate=16000
    The encryption/framing is unchanged — this just bridges PCM into a TalkbackSession.

    Auth: ?key=<API key> (query, so ffmpeg needs no custom headers). Body = 16-bit LE
    mono PCM at ?rate (default 16000). One talk session at a time (same lock as /talk/ws).
    """
    label = _label_for(request.query_params.get("key"))
    if AUTH_ENABLED and label is None:
        client = request.client.host if request.client else "?"
        log.warning("401 — invalid/missing API key from %s on /talk/stream", client)
        raise HTTPException(status_code=401, detail="invalid API key")
    if not _talk_ready():
        raise HTTPException(status_code=503, detail="talk not configured")
    if not _talk_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="talk already active")

    log.info("Talk (mic stream) by '%s'", label or "anonymous")
    rate = int(request.query_params.get("rate", "16000"))
    sess = _new_talk_session()
    sess.live = True   # live mic: bound backlog so latency can't accumulate
    try:
        await asyncio.to_thread(sess.start)
        async for chunk in request.stream():
            if chunk:
                await asyncio.to_thread(sess.push, chunk, rate)
    except Exception as e:
        log.warning("talk/stream error: %s", e)
    finally:
        await asyncio.to_thread(sess.close)
        _talk_lock.release()
        log.info("Talk (mic stream) ended (by '%s')", label or "anonymous")
    return Response(status_code=204)


@app.websocket("/talk/ws")
async def talk_ws(ws: WebSocket):
    """Live push-to-talk. Open the socket, stream raw 16-bit LE mono PCM frames
    (binary messages) while holding the talk button; close to stop. A hard
    max-duration backstop guards a stuck-open mic.

    Auth: pass ?key=<one of the API keys> (browsers can't set WS headers).
    Sample rate via text message {"rate": 16000} or query ?rate=16000 (default).
    """
    label = _label_for(ws.query_params.get("key"))
    if AUTH_ENABLED and label is None:
        client = ws.client.host if ws.client else "?"
        log.warning("401 — invalid/missing API key from %s on /talk/ws", client)
        await ws.close(code=4401)   # unauthorized
        return
    if not _talk_ready():
        await ws.close(code=1011)
        return
    if not _talk_lock.acquire(blocking=False):
        await ws.close(code=4409)   # already active
        return

    await ws.accept()
    log.info("Talk (push-to-talk) by '%s'", label or "anonymous")
    rate = int(ws.query_params.get("rate", "16000"))
    sess = _new_talk_session()
    sess.live = True   # live mic: bound backlog so latency can't accumulate
    try:
        await asyncio.to_thread(sess.start)
        await ws.send_json({"event": "talk_started", "max_seconds": TALK_MAX_SECONDS})
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                await asyncio.to_thread(sess.push, data, rate)
            elif (text := msg.get("text")) is not None:
                # control frame, e.g. {"rate":16000} or {"cmd":"stop"}
                try:
                    obj = __import__("json").loads(text)
                    if obj.get("cmd") == "stop":
                        break
                    if "rate" in obj:
                        rate = int(obj["rate"])
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error("Talk WS error: %s", e)
    finally:
        await asyncio.to_thread(sess.close)
        _talk_lock.release()
        try:
            await ws.close()
        except Exception:
            pass
