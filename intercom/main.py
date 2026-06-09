#!/usr/bin/env python3
"""
Dahua VTH Intercom API
Wraps dahua_client.py as a FastAPI service callable from
Home Assistant, n8n, iOS Shortcuts, Tasker, or curl.

POST /unlock          — unlock the door
GET  /stream          — get VTO camera stream URL  [not yet implemented]
GET  /events          — SSE stream of doorbell/motion events  [not yet implemented]
GET  /health          — health check
"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Security, Request
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from dahua_client import unlock_door, get_stream_url, subscribe_events

# ---------------------------------------------------------------------------
# Config — all values from environment, no defaults for secrets
# ---------------------------------------------------------------------------
API_KEY       = os.environ.get("DAHUA_API_KEY", "")
VTH_HOST      = os.environ.get("DAHUA_VTH_HOST", "192.168.40.55")
VTH_PORT      = int(os.environ.get("DAHUA_VTH_PORT", "5000"))
VTH_USERNAME  = os.environ.get("DAHUA_VTH_USERNAME", "user")
VTH_PASSWORD  = os.environ.get("DAHUA_VTH_PASSWORD", "")
CHANNEL       = int(os.environ.get("DAHUA_CHANNEL", "1"))
DOOR_INDEX    = int(os.environ.get("DAHUA_DOOR_INDEX", "0"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dahua_api")

# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY:
        log.warning("DAHUA_API_KEY is not set — all requests are unauthenticated. "
                    "Set this variable before exposing the service externally.")
    if not VTH_PASSWORD:
        log.warning("DAHUA_VTH_PASSWORD is not set — unlock calls will fail.")
    yield

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Dahua VTH Intercom API",
    description="Door unlock, camera stream, and doorbell events for Dahua VTH2622GW-W",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# API key auth
# ---------------------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(key: Optional[str] = Security(api_key_header)):
    if not API_KEY:
        return  # No key configured — unauthenticated (warned at startup)
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class UnlockResponse(BaseModel):
    success: bool
    message: str

class StreamResponse(BaseModel):
    stream_url: str
    tls_stream_url: Optional[str] = None
    expires_seconds: Optional[int] = None

class HealthResponse(BaseModel):
    status: str
    vth_host: str
    api_key_configured: bool

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="ok",
        vth_host=VTH_HOST,
        api_key_configured=bool(API_KEY),
    )


@app.post("/unlock", response_model=UnlockResponse)
def unlock(auth=Depends(verify_api_key)):
    """
    Trigger door unlock via DHIP (port 5000) directly to the VTH.
    Callable from HA rest_command, n8n HTTP Request node, iOS Shortcuts, Tasker.
    """
    log.info("Unlock request → %s:%s", VTH_HOST, VTH_PORT)
    try:
        result = unlock_door(
            host=VTH_HOST,
            port=VTH_PORT,
            username=VTH_USERNAME,
            password=VTH_PASSWORD,
            channel=CHANNEL,
            door_index=DOOR_INDEX,
        )
        if result:
            log.info("Unlock successful")
            return UnlockResponse(success=True, message="Door unlocked")
        log.warning("Unlock failed — device returned non-success")
        raise HTTPException(status_code=502, detail="Unlock command rejected by device")
    except HTTPException:
        raise
    except Exception as e:
        log.error("Unlock error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream", response_model=StreamResponse)
def stream(auth=Depends(verify_api_key)):
    """
    Get a temporary VTO camera stream URL.
    Not yet implemented — pending dos_stream.py promotion from dahua-research.
    """
    raise HTTPException(status_code=501, detail="Not yet implemented")


@app.get("/events")
async def events(request: Request, auth=Depends(verify_api_key)):
    """
    Server-Sent Events stream of doorbell press and motion events.
    Not yet implemented — pending DHIP event subscription work.
    """
    raise HTTPException(status_code=501, detail="Not yet implemented")
