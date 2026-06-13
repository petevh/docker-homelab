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
import wave

from fastapi import (
    FastAPI, HTTPException, Depends, Security, Request, WebSocket,
    WebSocketDisconnect,
)
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import Response, StreamingResponse
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
API_KEY          = os.environ.get("DAHUA_API_KEY", "")
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


def _on_ring(call_id: str, local_time: str) -> None:
    payload = f'data: {{"event":"doorbell_ring","call_id":"{call_id}","local_time":"{local_time}"}}\n\n'
    with _ring_listeners_lock:
        for q in list(_ring_listeners):
            try:
                q.put_nowait(payload)
            except Exception:
                pass


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

    if not API_KEY:
        log.warning("DAHUA_API_KEY is not set — all requests are unauthenticated.")
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


def verify_api_key(request: Request, key: Optional[str] = Security(api_key_header)):
    if not API_KEY:
        return
    token = key or request.query_params.get("key")
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class UnlockResponse(BaseModel):
    success: bool
    message: str


class HealthResponse(BaseModel):
    status: str
    device_sn: str
    api_key_configured: bool
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
        api_key_configured=bool(API_KEY),
        stream_running=bool(_stream_proxy and _stream_proxy.get_frame()),
        events_configured=bool(VTH_HOST and VTH_PASSWORD),
        talk_configured=_talk_ready(),
        talk_active=_talk_lock.locked(),
    )


@app.post("/unlock", response_model=UnlockResponse)
def unlock(auth=Depends(verify_api_key)):
    """
    Trigger door unlock via Dahua P2P cloud API.
    Callable from HA rest_command, n8n, iOS Shortcuts, Tasker, curl.
    """
    log.info("Unlock request → device %s", DEVICE_SN)
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
            log.info("Unlock successful")
            return UnlockResponse(success=True, message="Door unlocked")
        raise HTTPException(status_code=502, detail="Unlock command rejected by device")
    except HTTPException:
        raise
    except DahuaError as e:
        log.error("Unlock error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.error("Unlock error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/frame")
def frame(auth=Depends(verify_api_key)):
    """Latest JPEG snapshot from VTO camera. Use as HA still_image_url."""
    if not _stream_proxy:
        raise HTTPException(status_code=503, detail="Stream proxy not configured")
    jpeg = _stream_proxy.get_frame()
    if not jpeg:
        raise HTTPException(status_code=503, detail="No frame available yet — stream starting up")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.get("/stream")
def stream(auth=Depends(verify_api_key)):
    """
    MJPEG multipart stream from VTO camera.
    Use as HA stream_source or open directly in a browser/VLC.
    """
    if not _stream_proxy:
        raise HTTPException(status_code=503, detail="Stream proxy not configured")

    def generate():
        last_frame = b""
        while True:
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
                time.sleep(0.05)

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

    if not _talk_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A talk session is already active")
    try:
        # run the blocking relay I/O off the event loop
        frames = await asyncio.to_thread(
            play_audio_clip, _creds, DEVICE_SN, PCS_USERNAME, pcm, rate,
            CHANNEL, TALK_MAX_SECONDS,
        )
        return {"success": True, "frames": frames,
                "seconds": round(frames * 640 / 16000, 2)}
    except DahuaError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.error("Talk error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _talk_lock.release()


@app.websocket("/talk/ws")
async def talk_ws(ws: WebSocket):
    """Live push-to-talk. Open the socket, stream raw 16-bit LE mono PCM frames
    (binary messages) while holding the talk button; close to stop. A hard
    max-duration backstop guards a stuck-open mic.

    Auth: pass ?key=<API_KEY> (browsers can't set WS headers). Sample rate via
    first text message {"rate": 16000} or query ?rate=16000 (default 16000).
    """
    key = ws.query_params.get("key")
    if API_KEY and key != API_KEY:
        await ws.close(code=4401)   # unauthorized
        return
    if not _talk_ready():
        await ws.close(code=1011)
        return
    if not _talk_lock.acquire(blocking=False):
        await ws.close(code=4409)   # already active
        return

    await ws.accept()
    rate = int(ws.query_params.get("rate", "16000"))
    sess = _new_talk_session()
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
