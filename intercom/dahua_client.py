#!/usr/bin/env python3
"""
Dahua VTH client — cloud API (dmss-di.dolynkcloud.com).

Reverse-engineered from DMSS APK + live PCAP. See DahuaConsole/p2p_unlock.py
for full research notes.
"""

import hashlib
import hmac as _hmac
import base64
import secrets
import json
import logging
from datetime import datetime, timezone

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

log = logging.getLogger(__name__)

PCS_BASE = "https://dmss-di.dolynkcloud.com"
PCS_PATH = "/pcs/v1"

SVN_OPEN_VTH_DOOR = "222387"

CLIENT_UA = (
    "eyJjbGllbnRUeXBlIjoicGhvbmUiLCJjbGllbnRWZXJzaW9uIjoiVjIuNS4xMCIsImNsaWVu"
    "dE9WIjoiQW5kcm9pZCAxNiIsImNsaWVudE9TIjoiQW5kcm9pZCIsInRlcm1pbmFsTW9kZWwi"
    "OiJzYW1zdW5nIiwidGVybWluYWxJZCI6IiIsImFwcGlkIjoiZG1zc2Jhc2VhcHAiLCJwcm9q"
    "ZWN0IjoiQmFzZSIsImxhbmd1YWdlIjoiZW4tR0IiLCJjbGllbnRQcm90b2NvbFZlcnNpb24i"
    "OiJWNi4wLjAiLCJ0aW1lem9uZU9mZnNldCI6IjE0NDAwIiwidGVybWluYWxCcmFuZCI6IiIs"
    "InBob25lQXJlYSI6IjEifQ=="
)


class DahuaError(Exception):
    pass


def _encrypt_dev_pwd(plaintext: str, sn: str) -> str:
    """AES-256-CBC encrypt device credential. Key = MD5(SN.upper()).upper()."""
    key = hashlib.md5(sn.upper().encode()).hexdigest().upper().encode()
    iv  = b"HLMUQE2342MABCER"
    ct  = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext.encode(), 16))
    return base64.b64encode(ct).decode()


def _sign(method: str, uri: str, body_bytes: bytes, svn: str, bearer: str, pcs_username: str) -> dict:
    sign_key = hashlib.md5(bearer.encode()).hexdigest()
    date     = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    nonce    = secrets.token_hex(16)
    ctype    = "application/json"
    cmd5     = base64.b64encode(hashlib.md5(body_bytes).digest()).decode()
    parts = [
        method, uri, cmd5, ctype,
        f"x-pcs-apiver:{svn}",
        f"x-pcs-client-ua:{CLIENT_UA}",
        f"x-pcs-date:{date}",
        f"x-pcs-nonce:{nonce}",
        f"x-pcs-username:{pcs_username}",
    ]
    canonical = "\n".join(parts) + "\n"
    sig = base64.b64encode(
        _hmac.new(sign_key.encode(), canonical.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "Content-Type":    ctype,
        "Content-MD5":     cmd5,
        "x-pcs-apiver":    svn,
        "x-pcs-date":      date,
        "x-pcs-nonce":     nonce,
        "x-pcs-username":  pcs_username,
        "x-pcs-client-ua": CLIENT_UA,
        "x-pcs-signature": sig,
        "Authorization":   f"Bearer {bearer}",
        "User-Agent":      "Dalvik/2.1.0 (Linux; U; Android 16; SM-S928B Build/BP2A.250605.031.A3)",
        "Accept-Encoding": "gzip",
        "Connection":      "close",
        "Host":            "dmss-di.dolynkcloud.com",
    }


def _post(path: str, body: dict, svn: str, bearer: str, pcs_username: str) -> dict:
    body_bytes = json.dumps({"data": body}, separators=(',', ':')).encode()
    headers    = _sign("POST", path, body_bytes, svn, bearer, pcs_username)
    resp = requests.post(f"{PCS_BASE}{path}", headers=headers, data=body_bytes, timeout=15)
    try:
        return resp.json()
    except Exception:
        raise DahuaError(f"Non-JSON response {resp.status_code}: {resp.text[:200]}")


# ------------------------------------------------------------------
# Public API — called by main.py
# ------------------------------------------------------------------

def unlock_door(
    bearer_token: str,
    pcs_username: str,
    device_sn: str,
    device_username: str,
    device_password: str,
    channel: int = 1,
    door_index: int = 0,
) -> bool:
    dev_name = _encrypt_dev_pwd(device_username, device_sn)
    dev_pass = _encrypt_dev_pwd(device_password, device_sn)
    body = {
        "channel":     channel,
        "devName":     dev_name,
        "devPassword": dev_pass,
        "deviceId":    device_sn,
        "doorIndex":   door_index,
    }
    log.info("OpenVthDoor → %s/pcs/v1/deviceuseroperate.vth.OpenVthDoor", PCS_BASE)
    result = _post(f"{PCS_PATH}/deviceuseroperate.vth.OpenVthDoor", body, SVN_OPEN_VTH_DOOR, bearer_token, pcs_username)
    log.debug("OpenVthDoor response: %s", result)
    if result.get("code") == 10000:
        return True
    raise DahuaError(f"OpenVthDoor failed: {result}")


def get_stream_url(**kwargs) -> dict:
    raise NotImplementedError("Stream endpoint not yet implemented")


async def subscribe_events(**kwargs):
    raise NotImplementedError("Events endpoint not yet implemented")
    yield
