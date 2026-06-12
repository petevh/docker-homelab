#!/usr/bin/env python3
"""
Dahua VTH Intercom API
Wraps dahua_client.py as a FastAPI service callable from
Home Assistant, n8n, iOS Shortcuts, Tasker, or curl.

POST /unlock          — unlock the door (cloud API)
GET  /frame           — latest JPEG snapshot from VTO camera
GET  /stream          — MJPEG multipart stream from VTO camera
GET  /events          — SSE stream of doorbell ring events
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

from fastapi import FastAPI, HTTPException, Depends, Security, Request
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from dahua_client import (
    DahuaError,
    StreamProxy,
    subscribe_events,
    unlock_door,
)

# ---------------------------------------------------------------------------
# Config — all values from environment, no defaults for secrets
# ---------------------------------------------------------------------------
API_KEY          = os.environ.get("DAHUA_API_KEY", "")
BEARER_TOKEN     = os.environ.get("DAHUA_BEARER_TOKEN", "")
PCS_USERNAME     = os.environ.get("DAHUA_PCS_USERNAME", "")
DEVICE_SN        = os.environ.get("DAHUA_DEVICE_SN", "")
DEVICE_USERNAME  = os.environ.get("DAHUA_DEVICE_USERNAME", "user")
DEVICE_PASSWORD  = os.environ.get("DAHUA_DEVICE_PASSWORD", "")
CHANNEL          = int(os.environ.get("DAHUA_CHANNEL", "1"))
DOOR_INDEX       = int(os.environ.get("DAHUA_DOOR_INDEX", "0"))

VTH_HOST         = os.environ.get("DAHUA_VTH_HOST", "")
VTH_PORT         = int(os.environ.get("DAHUA_VTH_PORT", "5000"))
VTH_USERNAME     = os.environ.get("DAHUA_VTH_USERNAME", "user")
VTH_PASSWORD     = os.environ.get("DAHUA_VTH_PASSWORD", "")

STREAM           = int(os.environ.get("DAHUA_STREAM", "0"))        # 0 = main/HD 1280x720, 1 = sub 352x288
STREAM_WIDTH     = int(os.environ.get("DAHUA_STREAM_WIDTH", "0"))   # 0 = native resolution
STREAM_QUALITY   = int(os.environ.get("DAHUA_STREAM_QUALITY", "5"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dahua_api")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_stream_proxy: Optional[StreamProxy] = None
_ring_listeners: list[asyncio.Queue] = []
_ring_listeners_lock = threading.Lock()


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
    for var, name in [
        (BEARER_TOKEN, "DAHUA_BEARER_TOKEN"),
        (PCS_USERNAME, "DAHUA_PCS_USERNAME"),
        (DEVICE_SN,    "DAHUA_DEVICE_SN"),
        (DEVICE_PASSWORD, "DAHUA_DEVICE_PASSWORD"),
    ]:
        if not var:
            log.warning("%s is not set — unlock/stream calls will fail.", name)

    if BEARER_TOKEN and PCS_USERNAME and DEVICE_SN:
        _stream_proxy = StreamProxy(
            bearer_token=BEARER_TOKEN,
            pcs_username=PCS_USERNAME,
            device_sn=DEVICE_SN,
            channel=CHANNEL,
            stream=STREAM,
            width=STREAM_WIDTH,
            quality=STREAM_QUALITY,
        )
        _stream_proxy.start()
        log.info("Stream proxy started")

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
    )


@app.post("/unlock", response_model=UnlockResponse)
def unlock(auth=Depends(verify_api_key)):
    """
    Trigger door unlock via Dahua P2P cloud API.
    Callable from HA rest_command, n8n, iOS Shortcuts, Tasker, curl.
    """
    log.info("Unlock request → device %s", DEVICE_SN)
    try:
        result = unlock_door(
            bearer_token=BEARER_TOKEN,
            pcs_username=PCS_USERNAME,
            device_sn=DEVICE_SN,
            device_username=DEVICE_USERNAME,
            device_password=DEVICE_PASSWORD,
            channel=CHANNEL,
            door_index=DOOR_INDEX,
        )
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
