#!/usr/bin/env python3
"""
Dahua VTH client — cloud API (dmss-di.dolynkcloud.com) + direct DHIP (TCP/5000).

Reverse-engineered from DMSS APK + live PCAP. See DahuaConsole/NEXT_STEPS.md
for full research notes.
"""

import audioop          # G.711 a-law encode (verified vs captured frames)
import os
import hashlib
import hmac as _hmac
import base64
import secrets
import json
import logging
import queue
import re
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

log = logging.getLogger(__name__)

PCS_BASE = "https://dmss-di.dolynkcloud.com"
PCS_PATH = "/pcs/v1"
OPENAPI_HOST = "dmss-di.dolynkcloud.com"

SVN_OPEN_VTH_DOOR = "222387"
SVN_LOGIN         = "228782"

RELAY_TTL = 62  # relay hard-closes at ~66s (KeepLive-Time 60 + grace); use the full window
# Rotate relay sessions at the ~65s TTL by minting a fresh token+session (the old
# default). DISABLED by default 2026-06-21: it re-mints every ~65s = ~20x more cloud
# calls than DMSS (which keeps ONE session alive by re-sending the same PLAY/token every
# ~40s — see memory dahua-relay-keepalive-SOLVED). Over-minting contributed to the account
# flag. With rotation OFF, a stream just runs ~65s then DROPS (no re-mint). The proper fix
# is the keepalive (planned), after which this stays off for good. STREAM_ROTATE=1 restores.
STREAM_ROTATE = os.environ.get("STREAM_ROTATE", "") == "1"
# Relay keepalive interval. DMSS holds ONE relay session indefinitely by re-sending the
# SAME PLAY (same token) every ~40s on the held socket (decoded from a 23-min capture —
# memory dahua-relay-keepalive-SOLVED). We do the same: one token, kept alive, instead of
# re-minting every ~65s (~20x fewer cloud calls → far less flag-prone). 40s has margin to
# the ~65s TTL. Tunable.
RELAY_KEEPALIVE_SECS = float(os.environ.get("DAHUA_RELAY_KEEPALIVE", "40"))

CLIENT_UA = (
    "eyJjbGllbnRUeXBlIjoicGhvbmUiLCJjbGllbnRWZXJzaW9uIjoiVjIuNS4xMCIsImNsaWVu"
    "dE9WIjoiQW5kcm9pZCAxNiIsImNsaWVudE9TIjoiQW5kcm9pZCIsInRlcm1pbmFsTW9kZWwi"
    "OiJzYW1zdW5nIiwidGVybWluYWxJZCI6IiIsImFwcGlkIjoiZG1zc2Jhc2VhcHAiLCJwcm9q"
    "ZWN0IjoiQmFzZSIsImxhbmd1YWdlIjoiZW4tR0IiLCJjbGllbnRQcm90b2NvbFZlcnNpb24i"
    "OiJWNi4wLjAiLCJ0aW1lem9uZU9mZnNldCI6IjE0NDAwIiwidGVybWluYWxCcmFuZCI6IiIs"
    "InBob25lQXJlYSI6IjEifQ=="
)

LC_HEADERS = {
    "x-lc-mac":        "ff:ff:ff:ff:ff:ff",
    "x-lc-clientType": "Android",
    "x-lc-os":         "16",
    "x-lc-sdkVersion": "V3.5",
    "x-lc-safeCode":   "com.mm.android.DMSS5D08264B44E0E53FBCCC70B4F016474CC6C5AB5C",
    "x-lc-apiVer":     "1.5",
}


class DahuaError(Exception):
    pass


# ---------------------------------------------------------------------------
# Cloud login — mint a fresh OAuth Bearer (accessToken) from account creds
# Reverse-engineered from DMSS (usermodule.signup.b + StringUtils + EncryptUtilKt),
# verified byte-for-byte vs captured login traffic. Lets the service self-refresh
# its Bearer instead of relying on a captured/hardcoded token.
# ---------------------------------------------------------------------------

GATEWAY_BASE   = "https://dmss.dolynkcloud.com"
LOGIN_IV       = b"0a52uuEvqlOLc5TO"


def _get_account_passwd(password: str) -> str:
    key = hashlib.md5(b"DAHUAKEY").hexdigest().lower().encode()       # AES-256 key
    msg = hashlib.md5(password.encode()).hexdigest().lower().encode()
    ct  = AES.new(key, AES.MODE_CBC, LOGIN_IV).encrypt(pad(msg, 16))
    return base64.b64encode(ct).decode().rstrip("=")


def _login_password(password: str, salt: str, random: str) -> str:
    def h(k, m): return _hmac.new(k.encode(), m.encode(), hashlib.sha512).hexdigest()
    return h(random, h(salt, _get_account_passwd(password)))


def _dd_headers(account: str, area_code: str, country: str, terminal_id: str) -> dict:
    return {
        "x-dd-time":          str(int(time.time() * 1000)),
        "x-dd-nonce":         secrets.token_hex(16),
        "x-dd-clientversion": "2.5.10",
        "x-dd-clienttype":    "phone",
        "x-dd-client":        "Android",
        "x-dd-traceid":       secrets.token_hex(16),
        "x-dd-transcode":     "dmss",
        "x-dd-account":       account,
        "x-dd-country":       country,
        "x-dd-projectid":     "Base",
        "x-dd-language":      "en-US",
        "x-dd-terminalid":    terminal_id,
        "x-dd-signature":     "",            # not required for getSalt/login
        "content-type":       "application/json",
        "user-agent":         "okhttp/4.12.0",
    }


def refresh_bearer(account: str, password: str, area_code: str = "971",
                   country: str = "AE", terminal_id: str = "1a063af88b462024",
                   terminal_name: str = "intercom") -> str:
    """Log in with account credentials and return a fresh accessToken (Bearer)."""
    r = requests.post(f"{GATEWAY_BASE}/gateway/dcloud-user/userManage/v1/getSalt",
                      headers=_dd_headers(account, area_code, country, terminal_id),
                      data=json.dumps({"account": account, "areaCode": area_code}),
                      timeout=20)
    r.raise_for_status()
    d = r.json()
    if str(d.get("code")) != "0":
        raise DahuaError(f"getSalt failed: {d}")
    salt, random = d["data"]["salt"], d["data"]["random"]

    body = {
        "account": account,
        "areaCode": area_code,
        "multiTerminalValidationFlag": True,
        "oldPassword": _get_account_passwd(password),
        "password": _login_password(password, salt, random),
        "terminalName": terminal_name,
    }
    r = requests.post(f"{GATEWAY_BASE}/gateway/dcloud-user/userManage/v1/login",
                      headers=_dd_headers(account, area_code, country, terminal_id),
                      data=json.dumps(body), timeout=20)
    r.raise_for_status()
    d = r.json()
    if str(d.get("code")) != "0":
        raise DahuaError(f"login failed: {d}")
    return d["data"]["accessToken"]


class Credentials:
    """Holds the Bearer + pcs_username, refreshing the Bearer from account creds
    on demand (on first use and after a token-expired failure). Thread-safe.

    If account/password are not provided, falls back to the static `bearer` and
    cannot self-refresh (refresh() raises).
    """

    def __init__(self, bearer: str = "", pcs_username: str = "",
                 account: str = "", password: str = "", area_code: str = "971",
                 country: str = "AE"):
        self._lock      = threading.Lock()
        self.bearer     = bearer
        self.pcs_username = pcs_username
        self._account   = account
        self._password  = password
        self._area_code = area_code
        self._country   = country

    @property
    def can_refresh(self) -> bool:
        return bool(self._account and self._password)

    def ensure(self) -> str:
        """Return a Bearer, minting one if we don't have a static one."""
        with self._lock:
            if not self.bearer and self.can_refresh:
                self.bearer = refresh_bearer(self._account, self._password,
                                             self._area_code, self._country)
                log.info("Obtained fresh Bearer via account login")
            return self.bearer

    def refresh(self) -> str:
        """Force a new Bearer (e.g. after a 401/expired error)."""
        with self._lock:
            if not self.can_refresh:
                raise DahuaError("Bearer expired and no account credentials to refresh it")
            self.bearer = refresh_bearer(self._account, self._password,
                                         self._area_code, self._country)
            log.info("Refreshed Bearer via account login")
            return self.bearer


# Cloud error codes/markers that mean "Bearer expired / not authenticated".
_AUTH_ERR_MARKERS = ("token", "unauthor", "auth fail", "10002", "10006", "invalid")


def _looks_like_auth_error(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _AUTH_ERR_MARKERS)


def with_bearer_retry(creds: "Credentials", fn: Callable[[str], object]):
    """Call fn(bearer); on an auth-looking failure, refresh the Bearer once and retry."""
    bearer = creds.ensure()
    try:
        return fn(bearer)
    except Exception as e:
        if creds.can_refresh and _looks_like_auth_error(e):
            log.warning("Cloud call failed (%s) — refreshing Bearer and retrying", e)
            return fn(creds.refresh())
        raise


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------

def _encrypt_dev_pwd(plaintext: str, sn: str) -> str:
    """AES-256-CBC encrypt device credential. Key = MD5(SN.upper()).upper()."""
    key = hashlib.md5(sn.upper().encode()).hexdigest().upper().encode()
    iv  = b"HLMUQE2342MABCER"
    ct  = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext.encode(), 16))
    return base64.b64encode(ct).decode()


# ---------------------------------------------------------------------------
# Cloud API signing
# ---------------------------------------------------------------------------

def _sign_pcs(method: str, uri: str, body_bytes: bytes, svn: str,
              bearer: str, pcs_username: str) -> dict:
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
        "Host":            OPENAPI_HOST,
        "openUserId":      "",
        "appSource":       "",
    }


def _sign_openapi(body_bytes: bytes, bearer: str, pcs_username: str) -> dict:
    sign_key  = hashlib.md5(bearer.encode()).hexdigest()
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    ctype     = "application/json"
    cmd5      = base64.b64encode(hashlib.md5(body_bytes).digest()).decode()
    canonical = (f"{cmd5}\nx-pcs-username:{pcs_username}\n"
                 f"x-pcs-client-ua:{CLIENT_UA}\n")
    sig = base64.b64encode(
        _hmac.new(sign_key.encode(), canonical.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "Host":                  OPENAPI_HOST,
        "Authorization":         f"Bearer {bearer}",
        "Content-Type":          ctype,
        "Content-MD5":           cmd5,
        "companyId":             "",
        "openUserId":            "",
        "appSource":             "",
        "x-pcs-username":        pcs_username,
        "x-pcs-client-ua":       CLIENT_UA,
        "x-pcs-signature":       sig,
        "cos-request-timestamp": timestamp,
        "cos-request-nonce":     secrets.token_urlsafe(24),
        "cos-request-version":   "",
        "cos-request-sign":      "(null)",
        "User-Agent":            "Dalvik/2.1.0 (Linux; U; Android 16; SM-S928B Build/BP2A.250605.031.A3)",
        "Accept-Encoding":       "gzip",
        "Connection":            "close",
    }
    headers.update(LC_HEADERS)
    return headers


def _post_pcs(path: str, body: dict, svn: str, bearer: str, pcs_username: str) -> dict:
    body_bytes = json.dumps({"data": body}, separators=(',', ':')).encode()
    headers    = _sign_pcs("POST", path, body_bytes, svn, bearer, pcs_username)
    resp = requests.post(f"{PCS_BASE}{path}", headers=headers, data=body_bytes, timeout=15)
    try:
        return resp.json()
    except Exception:
        raise DahuaError(f"Non-JSON response {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Cloud: door unlock
# ---------------------------------------------------------------------------

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
    result = _post_pcs(f"{PCS_PATH}/deviceuseroperate.vth.OpenVthDoor", body,
                       SVN_OPEN_VTH_DOOR, bearer_token, pcs_username)
    log.debug("OpenVthDoor response: %s", result)
    if result.get("code") == 10000:
        return True
    raise DahuaError(f"OpenVthDoor failed: {result}")


# ---------------------------------------------------------------------------
# Cloud: stream — play token + relay URL
# ---------------------------------------------------------------------------

def get_play_token(bearer_token: str, pcs_username: str) -> str:
    uri  = "/pcs/v1/dclouduser.account.Login"
    body = json.dumps({
        "data": {"clientPushId": "", "isDimou": False,
                 "terminalId": "", "timezoneOffset": 14400}
    }, separators=(',', ':')).encode()
    headers = _sign_pcs("POST", uri, body, SVN_LOGIN, bearer_token, pcs_username)
    r = requests.post(f"{PCS_BASE}{uri}", headers=headers, data=body, timeout=20)
    d = r.json()
    if d.get("code") != 10000:
        raise DahuaError(f"Login failed: {d}")
    return d["data"]["openUserToken"]


def get_relay_url(play_token: str, bearer_token: str, pcs_username: str,
                  device_sn: str, channel: int = 1, stream: int = 0,
                  encrypt: bool = False) -> str:  # stream=0 = 1280x720 main/HD; stream=1 = 352x288 sub-stream
    """Return the relay URL for the VTO camera.

    encrypt=True requests the encrypt=2 relay (/encrypt/RTSV1). The video proxy
    uses encrypt=0 (plaintext DHAV → ffmpeg). Talkback REQUIRES encrypt=2: the
    device negotiates the talk call as encrypt=2 and silently mutes audio sent on
    an encrypt=0 session (proven 2026-06-13)."""
    append_url = f"/real/{channel}/{stream}/encrypt/RTSV1" if encrypt \
        else f"/real/{channel}/{stream}/RTSV1"
    body_obj   = {"params": {
        "ahEncrypt": False, "token": play_token, "streamId": 0,
        "appendUrl": append_url, "design": "second", "deviceId": device_sn,
    }}
    body_bytes = json.dumps(body_obj, indent="\t").encode()
    headers    = _sign_openapi(body_bytes, bearer_token, pcs_username)
    r = requests.post(f"{PCS_BASE}/openapi/transferStream",
                      headers=headers, data=body_bytes, timeout=20)
    result = r.json().get("result", r.json())
    if str(result.get("code")) != "0":
        raise DahuaError(f"transferStream failed: {r.json()}")
    return result["data"]["url"]


# ---------------------------------------------------------------------------
# Relay PLAY protocol
# ---------------------------------------------------------------------------

def connect_relay(relay_url: str) -> socket.socket:
    """
    TCP connect to relay, send PLAY request, skip HTTP headers + SDP + 16-byte
    transport prefix. Returns socket positioned at start of DHAV stream.

    Intentional protocol typo: 'Accpet-Sdp' (not 'Accept-Sdp').
    """
    host_port, path = relay_url.split("/", 1)
    host, port_str  = host_port.rsplit(":", 1)
    port = int(port_str)

    req = (
        f"PLAY /{path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Accpet-Sdp: Private\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    )

    sock = socket.create_connection((host, port), timeout=15)
    sock.sendall(req.encode())

    hdr = b""
    while b"\r\n\r\n" not in hdr:
        chunk = sock.recv(1)
        if not chunk:
            raise RuntimeError("Connection closed while reading headers")
        hdr += chunk

    headers_raw = hdr[:-4].decode("ascii", errors="replace")
    if "200 OK" not in headers_raw:
        sock.close()
        raise RuntimeError(f"PLAY rejected: {headers_raw[:100]}")

    priv_len = 0
    for line in headers_raw.split("\r\n"):
        m = re.match(r"Private-Length:\s*(\d+)", line, re.IGNORECASE)
        if m:
            priv_len = int(m.group(1))

    sdp = b""
    while len(sdp) < priv_len:
        chunk = sock.recv(min(4096, priv_len - len(sdp)))
        if not chunk:
            break
        sdp += chunk

    # Consume only the FIRST 16-byte transport prefix (4-byte interleave header +
    # 12-byte RTP header). The stream is RTP-over-TCP interleaved: every ~1456-byte
    # chunk carries another such header. RtpDeinterleaver strips the rest in the
    # feed loop — they must be removed BEFORE the DHAV/H264 parsing, or the embedded
    # header bytes corrupt the picture (grey/ghosty, desync at MB row 1).
    prefix = b""
    while len(prefix) < 16:
        chunk = sock.recv(16 - len(prefix))
        if not chunk:
            break
        prefix += chunk

    return sock


class RtpDeinterleaver:
    """De-interleaves the relay's RTP-over-TCP stream into a pure DHAV/H264 byte stream.

    Each interleaved chunk is `0x24 channel(1) length(2BE)` + 12-byte RTP header +
    payload. connect_relay() already consumed the first 16-byte header, so the stream
    begins mid-payload; we emit those leading bytes verbatim, then for every subsequent
    interleave header strip the 4-byte interleave + 12-byte RTP and keep the payload.
    """

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        i = 0
        n = len(self._buf)
        while i < n:
            if self._buf[i] == 0x24 and i + 4 <= n and self._buf[i + 1] < 4:
                length = int.from_bytes(self._buf[i + 2:i + 4], "big")
                if i + 4 + length > n:
                    break  # incomplete chunk; wait for more data
                chunk = self._buf[i + 4:i + 4 + length]
                if len(chunk) >= 12 and chunk[0] in (0x80, 0x90):
                    out += chunk[12:]  # strip 12-byte RTP header
                else:
                    out += chunk
                i += 4 + length
            else:
                nxt = self._buf.find(b"\x24", i + 1)
                if nxt < 0:
                    out += self._buf[i:]
                    i = n
                else:
                    out += self._buf[i:nxt]
                    i = nxt
        self._buf = self._buf[i:]
        return bytes(out)


class _RelaySession:
    """One relay connection, pulled in a background thread. De-interleaves the
    RTP-over-TCP framing and emits only from the first keyframe (0xfd) onward, so a
    consumer gets a clean keyframe-aligned DHAV byte stream. Used in pairs to overlap
    relay reconnects (pre-warm the next before the current expires)."""

    def __init__(self, sock: socket.socket, running, held: bool = False,
                 relay_url: str = ""):
        self._sock    = sock
        self._running = running          # callable -> bool (proxy still alive)
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=256)
        self._stop    = False
        self._thread  = None
        self._ka_thread = None           # keepalive thread (holds the session alive)
        self.dead     = False
        self._ready   = False            # True once connected + synced to a keyframe
        self._held    = held             # if True, discard data until release()
        self._relay_url = relay_url      # host:port/path?token... — to rebuild keepalive PLAY

    def start(self):
        self._thread = threading.Thread(target=self._pull, daemon=True)
        self._thread.start()
        # Keepalive: re-send the SAME PLAY (same token) on this socket every ~40s so
        # the relay doesn't hard-close at ~65s. Mirrors DMSS → one token holds the
        # session indefinitely (no per-65s re-mint). Only if we have the url.
        if self._relay_url:
            self._ka_thread = threading.Thread(target=self._keepalive, daemon=True)
            self._ka_thread.start()

    def _keepalive(self):
        """Re-send PLAY ...&trackID=31&method=0 (original token) every RELAY_KEEPALIVE_SECS
        on the SAME socket, so the relay keeps this session alive past the ~65s TTL.
        The media flows DOWN this socket; the keepalive PLAY goes UP it (TCP duplex)."""
        host_port, path = self._relay_url.split("/", 1)
        host, port = host_port.rsplit(":", 1)
        sep = "&" if "?" in path else "?"
        cseq = 1
        # send the first keepalive ~LEAD seconds before the TTL, then every interval
        while self._running() and not self._stop:
            # sleep in small steps so stop() is responsive
            slept = 0.0
            while slept < RELAY_KEEPALIVE_SECS and self._running() and not self._stop:
                time.sleep(0.5); slept += 0.5
            if self._stop or not self._running():
                break
            req = (f"PLAY /{path}{sep}trackID=31&method=0 HTTP/1.1\r\n"
                   f"Host: {host}:{port}\r\nAccpet-Sdp: Private\r\n"
                   f"Connection: keep-alive\r\nCseq: {cseq}\r\n\r\n")
            try:
                self._sock.sendall(req.encode())
                cseq += 1
            except OSError:
                break   # socket gone; _pull will mark dead

    def is_ready(self) -> bool:
        """True once connected and synced to its first keyframe (safe to switch to)."""
        return self._ready

    def release(self):
        """Stop discarding; begin queueing from the NEXT keyframe (clean splice point)."""
        self._held = False

    def read(self, timeout: float = 0.5):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except Exception:
            pass

    def _pull(self):
        # While held: keep the socket drained and track keyframes, but DON'T queue
        # (so we don't build a stale backlog to dump at switch time). On release,
        # start queueing from the next keyframe — a clean GOP boundary for ffmpeg.
        deint    = RtpDeinterleaver()
        buf      = b""
        emitting = False
        try:
            while self._running() and not self._stop:
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                chunk = deint.feed(chunk)
                if not chunk:
                    continue
                buf += chunk

                # Find the latest keyframe boundary in buf.
                kf, pos = -1, buf.find(b"DHAV")
                while pos >= 0 and pos + 5 <= len(buf):
                    if buf[pos + 4] == 0xfd:
                        kf = pos
                    pos = buf.find(b"DHAV", pos + 4)

                if not emitting:
                    if kf < 0:
                        # haven't seen a keyframe yet; cap buffer, keep waiting
                        if len(buf) > 2_000_000:
                            buf = buf[-4:]
                        continue
                    self._ready = True          # connected & a keyframe is available
                    if self._held:
                        # discard everything up to the most recent keyframe, stay aligned
                        buf = buf[kf:]
                        # keep only a bounded window so we don't grow unbounded while held
                        if len(buf) > 2_000_000:
                            buf = buf[-4:]
                        continue
                    # released: start emitting from this keyframe
                    buf = buf[kf:]
                    emitting = True

                try:
                    self._q.put(buf, timeout=1)
                    buf = b""
                except queue.Full:
                    buf = b""        # drop if consumer stalled; keeps us live
        finally:
            self.dead = True
            try:
                self._sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# StreamProxy — manages relay connection + ffmpeg, provides JPEG frames
# ---------------------------------------------------------------------------

class StreamProxy:
    def __init__(self, creds: "Credentials", device_sn: str,
                 channel: int = 1, stream: int = 0, width: int = 0, quality: int = 5,
                 rtsp_publish_url: str = "rtsp://127.0.0.1:8554/frontdoor"):
        self.creds            = creds
        self.device_sn        = device_sn
        self.channel          = channel
        self.stream           = stream
        self.width            = width
        self.quality          = quality
        self.rtsp_publish_url = rtsp_publish_url
        self._lock         = threading.Lock()
        self._frame: bytes = b""
        self._running      = False
        self._thread: Optional[threading.Thread] = None
        # Last-frame cache: persist the most recent JPEG to disk so /frame can
        # serve an instant (if slightly stale) snapshot even when the relay is
        # cold — closes the ~8s blank-screen gap on a cold page load. Refreshes
        # to live as soon as the relay warms.
        self._frame_cache_path = os.environ.get(
            "DAHUA_FRAME_CACHE", "/tmp/frontdoor_last.jpg")
        self._last_cache_write = 0.0
        try:
            with open(self._frame_cache_path, "rb") as f:
                cached = f.read()
            if len(cached) > 1000:
                self._frame = cached    # warm-start /frame with the last image
        except OSError:
            pass
        # On-demand: relay runs only while there are active viewers. After the
        # last viewer leaves we keep it alive a short grace period, then stop —
        # so we don't hold a cloud relay session (and hammer auth) 24/7.
        self._viewers      = 0
        self._idle_since: Optional[float] = None
        self._idle_grace   = 30.0          # seconds to linger after last viewer
        # Safety net against a LEAKED viewer counter (a client that disconnects
        # uncleanly and never runs release_viewer): track the last time there was
        # REAL consumption (a mediamtx reader, or a /frame touch). If none for
        # _hard_idle_grace, stop the relay even if _viewers is stuck > 0 — so a leak
        # can't pin a cloud relay open indefinitely.
        self._last_activity = time.time()
        self._hard_idle_grace = 60.0
        self._relay_running = False        # is the cloud relay loop active now
        self._loop_thread: Optional[threading.Thread] = None  # the active _run_loop

    def start(self):
        """Begin the idle-watcher. The relay itself only runs while viewers>0."""
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread  = threading.Thread(target=self._supervisor, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def acquire_viewer(self):
        """Long-lived consumer (e.g. /stream MJPEG, /talk/ws) connects."""
        with self._lock:
            self._viewers += 1
            self._idle_since = None
            self._last_activity = time.time()
        self._ensure_relay()

    def release_viewer(self):
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0:
                self._idle_since = time.time()

    def touch(self):
        """Per-request keep-alive (e.g. each HLS segment/playlist fetch). Starts
        the relay if down and resets the idle timer; the grace period keeps it up
        between fetches and stops it ~grace seconds after the player goes away."""
        with self._lock:
            self._idle_since = time.time()  # reset grace from now
            self._last_activity = time.time()
        self._ensure_relay()

    def _ensure_relay(self):
        with self._lock:
            if self._relay_running:
                return
            # Don't start a new loop while the previous one is still tearing down
            # its ffmpeg (proc.terminate can take up to ~3s). Otherwise two ffmpegs
            # briefly publish to the same RTSP path → mediamtx "closing existing
            # publisher" → broken pipe → restart storm. Wait for the old thread.
            if self._loop_thread is not None and self._loop_thread.is_alive():
                return
            self._relay_running = True
            self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
            self._loop_thread.start()

    def _mediamtx_readers(self) -> int:
        """Active reader count across both publish paths, via the mediamtx API.
        WebRTC/HLS/go2rtc/HA viewers consume from mediamtx directly and bypass our
        own viewer counter — so without this the supervisor declares 'idle' while a
        WebRTC stream is live and stops the relay mid-stream. Returns -1 if the API
        can't be reached (treated as 'unknown' → don't stop on that alone)."""
        try:
            base = "http://127.0.0.1:9997/v3/paths/get/"
            total = 0
            for name in ("frontdoor", "frontdoor_webrtc"):
                r = requests.get(base + name, timeout=1)
                if r.status_code == 200:
                    total += len(r.json().get("readers", []))
            return total
        except Exception:
            return -1

    def _supervisor(self):
        """Stop the relay once there are no long-lived viewers AND no recent
        touch (HLS fetch) within the grace period AND mediamtx has no readers.
        Also restarts the relay if a viewer is waiting but the previous loop was
        still tearing down when _ensure_relay was last called."""
        while self._running:
            time.sleep(2)
            with self._lock:
                idle = (self._viewers == 0 and self._idle_since is not None
                        and time.time() - self._idle_since > self._idle_grace)
            # Don't stop while mediamtx still has active readers (WebRTC/HLS/go2rtc/
            # HA). Only consult the API when we'd otherwise stop — keeps it cheap.
            if idle and self._relay_running and self._mediamtx_readers() > 0:
                idle = False
                with self._lock:
                    self._idle_since = time.time()   # reset grace; readers present
                    self._last_activity = time.time()
            # SAFETY NET: if the relay is up but there's been NO real consumption
            # (no mediamtx readers, no recent touch) for _hard_idle_grace, stop it even
            # if _viewers is stuck > 0 (a leaked /stream viewer from an unclean
            # disconnect). The reader count is the ground truth for actual viewing.
            if self._relay_running and not idle:
                if self._mediamtx_readers() > 0:
                    self._last_activity = time.time()
                elif time.time() - self._last_activity > self._hard_idle_grace:
                    log.info("Relay has no readers for %.0fs — stopping (viewer-leak safety net)",
                             self._hard_idle_grace)
                    idle = True
            with self._lock:
                # a viewer is active/recent but the relay isn't running and the old
                # loop has finished tearing down → (re)start it
                wants_relay = (not idle and not self._relay_running
                               and self._idle_since is not None
                               and (self._loop_thread is None
                                    or not self._loop_thread.is_alive()))
            if idle and self._relay_running:
                log.info("Stream idle — stopping cloud relay (on-demand)")
                self._relay_running = False     # _run_loop checks this and exits
            elif wants_relay:
                self._ensure_relay()

    def mark_active(self):
        """Heartbeat: an app-level consumer (e.g. a live /stream MJPEG viewer that
        doesn't appear as a mediamtx reader) is actively pulling. Keeps the viewer-leak
        safety net from stopping the relay while genuinely watched. A LEAKED viewer
        stops calling this → activity goes stale → safety net fires."""
        with self._lock:
            self._last_activity = time.time()

    def get_frame(self) -> bytes:
        with self._lock:
            return self._frame

    def _ffmpeg_cmd(self):
        # Single ffmpeg, two outputs from the de-interleaved DHAV stream:
        #   1. RTSP passthrough to mediamtx: H264 copied (full fps, no re-encode),
        #      PCM audio transcoded to AAC. This is the smooth, low-latency feed.
        #   2. MJPEG snapshots to pipe:1 for the /frame still-image endpoint.
        # ffmpeg's dhav demuxer separates video+audio itself, so we feed the whole
        # (de-interleaved) DHAV stream rather than hand-extracting H264.
        scale = f"scale={self.width}:-2," if self.width else ""
        # WebRTC path = same RTSP host, "<name>_webrtc" — H264 copy + OPUS audio.
        # WebRTC (and go2rtc / HA Advanced Camera Card) require Opus; it cannot
        # carry the AAC used by the HLS/HA path. So we publish a second path with
        # Opus while the primary stays AAC (HLS needs AAC; mpegts+Opus is flaky).
        webrtc_url = self.rtsp_publish_url + "_webrtc"
        return [
            "ffmpeg", "-loglevel", "warning",
            "-fflags", "+genpts+nobuffer",
            "-f", "dhav", "-i", "pipe:0",
            # Output 1: RTSP for HLS + HA (H264 passthrough + AAC audio)
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "aac", "-ar", "16000", "-b:a", "32k",
            "-f", "rtsp", "-rtsp_transport", "tcp", self.rtsp_publish_url,
            # Output 2: RTSP for WebRTC/WHEP (H264 passthrough + OPUS audio).
            # The door's audio has jittery/backward timestamps; libopus rejects
            # non-monotonic input ("Non-monotonic DTS" -> stalls -> broken pipe ->
            # whole ffmpeg restarts -> relay churn -> WebRTC sessions drop). The
            # aresample async filter smooths timestamps to a steady clock so the
            # Opus encoder gets monotonic input and the pipeline stays stable.
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy",
            "-af", "aresample=async=1000:first_pts=0",
            "-c:a", "libopus", "-ar", "48000", "-ac", "1", "-b:a", "32k",
            "-f", "rtsp", "-rtsp_transport", "tcp", webrtc_url,
            # Output 3: MJPEG snapshots
            "-map", "0:v:0",
            "-vf", f"{scale}format=yuvj420p",
            "-q:v", str(self.quality), "-r", "5",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ]

    def _run_loop(self):
        # Runs while there are viewers (_relay_running). ONE persistent ffmpeg +
        # RTSP publish; the relay socket reconnects underneath (50s cycle) feeding
        # the same ffmpeg. Exits when the last viewer leaves (on-demand).
        while self._running and self._relay_running:
            proc = subprocess.Popen(
                self._ffmpeg_cmd(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None,  # inherit — goes to container logs
            )
            reader = threading.Thread(target=self._read_jpeg, args=(proc,), daemon=True)
            reader.start()
            try:
                # Feed relay sessions into this ffmpeg until it (or we) dies.
                _block_wait = 30
                while self._running and self._relay_running and proc.poll() is None:
                    try:
                        # ONE relay session, held alive by its keepalive thread
                        # (re-sends the same PLAY/token every ~40s). It returns only
                        # when the viewer leaves OR the session GENUINELY dies (keepalive
                        # rejected / token truly expired) — the latter is rare, so this
                        # re-mints only at real expiry, not every ~65s. That's the whole
                        # point: ~1 token mint per long viewing session, like DMSS.
                        self._feed_one_relay_session(proc)
                        _block_wait = 30
                    except Exception as e:
                        # Cloud "IP in Block List" (code 10005): hammering makes it
                        # worse. Back off hard (30s→up to 10min) so the block clears.
                        if "Block List" in str(e) or "10005" in str(e):
                            log.warning("Relay: IP blocked by cloud — backing off %ds", _block_wait)
                            time.sleep(_block_wait)
                            _block_wait = min(_block_wait * 2, 600)
                        else:
                            log.warning("Relay session error: %s — reconnecting in 2s", e)
                            time.sleep(2)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                reader.join(timeout=5)
            if self._running:
                log.warning("ffmpeg exited — restarting pipeline in 2s")
                time.sleep(2)

    def _open_relay_session(self, held: bool = False) -> "_RelaySession":
        """Open + authenticate a relay connection and return a started _RelaySession.
        If held, the session connects and stays keyframe-aligned but discards data
        until release() (used to pre-warm the next session without a stale backlog)."""
        pcs_user = self.creds.pcs_username
        play_token = with_bearer_retry(self.creds, lambda b: get_play_token(b, pcs_user))
        relay_url = with_bearer_retry(
            self.creds,
            lambda b: get_relay_url(play_token, b, pcs_user,
                                    self.device_sn, self.channel, self.stream))
        sock = connect_relay(relay_url)
        sock.settimeout(10)
        sess = _RelaySession(sock, lambda: self._running, held=held,
                             relay_url=relay_url)
        sess.start()
        return sess

    def _feed_one_relay_session(self, proc):
        """Feed relay sessions into the persistent ffmpeg with OVERLAP. The relay
        hard-closes each connection at ~66s, so we pre-warm the next session a few
        seconds early (connected + keyframe-aligned, but discarding data). At switch
        time we release it so it emits fresh from its NEXT keyframe — a clean GOP
        boundary — and stop the old one. No starvation gap, no stale-backlog dump.

        KEEPALIVE MODE (default, STREAM_ROTATE off): one relay session, held alive by
        its keepalive thread (re-sends the same PLAY/token every ~40s — DMSS's mechanism,
        memory dahua-relay-keepalive-SOLVED). Feed it until the viewer leaves or it
        GENUINELY dies (keepalive rejected / token truly expired — rare). ~1 token mint
        per viewing session instead of ~20x. STREAM_ROTATE=1 restores the old per-65s
        pre-warm rotation if ever needed."""
        active = self._open_relay_session()
        active.release()     # active emits immediately
        log.info("Relay session connected (feeding persistent ffmpeg)")

        if not STREAM_ROTATE:
            # Keepalive holds this one session; feed until viewer leaves or it truly dies.
            try:
                while self._running and proc.poll() is None:
                    data = active.read(timeout=0.5)
                    if data is None:
                        if active.dead:
                            log.info("Relay session ended (keepalive stopped / token expired)")
                            break
                        continue
                    try:
                        proc.stdin.write(data)
                        proc.stdin.flush()
                    except BrokenPipeError:
                        break
            finally:
                active.stop()
            return

        PREWARM_LEAD = 6     # pre-warm the next session this many seconds before TTL
        nxt = None
        start_t = time.time()
        try:
            while self._running and proc.poll() is None:
                age = time.time() - start_t

                if nxt is None and age >= RELAY_TTL - PREWARM_LEAD:
                    try:
                        nxt = self._open_relay_session(held=True)   # pre-warm, discarding
                    except Exception as e:
                        log.warning("Pre-warm failed: %s", e)

                # Switch once we're at TTL and the next session is connected+aligned.
                if nxt is not None and age >= RELAY_TTL and nxt.is_ready():
                    nxt.release()          # start emitting from its next keyframe
                    active.stop()
                    active = nxt
                    nxt = None
                    start_t = time.time()
                    log.info("Switched to pre-warmed relay session (seamless)")

                data = active.read(timeout=0.5)
                if data is None:
                    if active.dead:
                        break          # active died with no replacement → reconnect
                    continue
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except BrokenPipeError:
                    break
        finally:
            active.stop()
            if nxt is not None:
                nxt.stop()

    def _read_jpeg(self, proc):
        buf = b""
        try:
            while self._running and proc.poll() is None:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi < 0:
                        buf = b""
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi < 0:
                        buf = buf[soi:]
                        break
                    jpeg = buf[soi:eoi + 2]
                    buf  = buf[eoi + 2:]
                    if len(jpeg) > 1000:
                        with self._lock:
                            self._frame = jpeg
                        # Persist occasionally so a cold start has a recent image.
                        now = time.time()
                        if now - self._last_cache_write > 10:
                            self._last_cache_write = now
                            try:
                                tmp = self._frame_cache_path + ".tmp"
                                with open(tmp, "wb") as f:
                                    f.write(jpeg)
                                os.replace(tmp, self._frame_cache_path)
                            except OSError:
                                pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DHIP event subscription — doorbell ring detection
# ---------------------------------------------------------------------------

def _dhip_header(session_id: int, msg_id: int, body_len: int) -> bytes:
    return (
        b"\x20\x00\x00\x00DHIP"
        + struct.pack("<I", session_id)
        + struct.pack("<I", msg_id)
        + struct.pack("<I", body_len)
        + struct.pack("<I", 0)
        + struct.pack("<I", body_len)
        + struct.pack("<I", 0)
    )


def _dhip_send(sock: socket.socket, session_id: int, msg_id: int, body: dict) -> None:
    data = json.dumps(body, separators=(",", ":")).encode()
    sock.sendall(_dhip_header(session_id, msg_id, len(data)) + data)


def _dhip_recv(sock: socket.socket) -> Optional[dict]:
    hdr = b""
    while len(hdr) < 32:
        chunk = sock.recv(32 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    body_len = struct.unpack_from("<I", hdr, 16)[0]
    body = b""
    while len(body) < body_len:
        chunk = sock.recv(body_len - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return {"_raw": body[:64].hex()}


def _dhip_login(sock: socket.socket, username: str, password: str) -> int:
    """
    DHIP gen2 challenge/response login. Returns session_id.
    gen2 = MD5(username + ':' + realm + ':' + password).upper()
    auth = MD5(username + ':' + random + ':' + gen2).upper()
    """
    _dhip_send(sock, 0, 1, {
        "method": "global.login",
        "params": {"userName": username, "password": "", "clientType": "Netscape browsers"},
        "id": 1,
    })
    resp = _dhip_recv(sock)
    if not resp:
        raise RuntimeError("No response to login challenge")

    params = resp.get("params", {})
    realm  = params.get("realm", "")
    random = params.get("random", "")
    sess   = resp.get("session", 0)

    if not realm or not random:
        raise RuntimeError(f"Unexpected challenge response: {resp}")

    gen2 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest().upper()
    auth = hashlib.md5(f"{username}:{random}:{gen2}".encode()).hexdigest().upper()

    _dhip_send(sock, sess, 2, {
        "method": "global.login",
        "params": {
            "userName":      username,
            "password":      auth,
            "clientType":    "Netscape browsers",
            "authorityType": "Default",
            "passwordType":  "Default",
        },
        "session": sess,
        "id": 2,
    })
    resp2 = _dhip_recv(sock)
    if not resp2 or not resp2.get("result"):
        raise RuntimeError(f"Login failed: {resp2}")

    return resp2.get("session", sess)


def subscribe_events(
    vth_host: str,
    vth_port: int,
    username: str,
    password: str,
    on_ring: Callable[[str, str], None],
) -> None:
    """
    Connect to VTH, subscribe to all events, call on_ring(call_id, local_time)
    on each new doorbell ring. Blocks until connection drops, then raises.

    Replay/stale-event filtering: the VTH sends recent events on subscribe with
    UTC == subscribe_time. We capture that baseline and skip events matching it,
    plus any ring older than 10s at connect time.
    """
    log.info("Connecting to VTH at %s:%d", vth_host, vth_port)
    sock = socket.create_connection((vth_host, vth_port), timeout=15)
    sock.settimeout(25)

    try:
        session_id = _dhip_login(sock, username, password)
        log.info("DHIP logged in (session=%d)", session_id)

        _dhip_send(sock, session_id, 3, {
            "method": "eventManager.attach",
            "params": {"codes": ["All"]},
            "session": session_id,
            "id": 3,
        })
        resp = _dhip_recv(sock)
        if not resp or not resp.get("result"):
            raise RuntimeError(f"eventManager.attach failed: {resp}")
        log.info("Subscribed to VTH events — waiting for doorbell ring")

        msg_id        = 100
        last_ka       = time.time()
        KA_INTERVAL   = 15
        subscribe_utc: Optional[float] = None
        seen_call_ids: set = set()

        while True:
            now = time.time()
            if now - last_ka > KA_INTERVAL:
                _dhip_send(sock, session_id, msg_id, {
                    "method": "global.keepAlive",
                    "params": {"timeout": 20, "active": True},
                    "session": session_id,
                    "id": msg_id,
                })
                msg_id  += 1
                last_ka  = now

            try:
                msg = _dhip_recv(sock)
            except socket.timeout:
                continue

            if not msg:
                raise EOFError("connection closed by VTH")

            if msg.get("method") != "client.notifyEventStream":
                continue

            for event in msg.get("params", {}).get("eventList", []):
                code   = event.get("Code", "")
                action = event.get("Action", "")
                data   = event.get("Data", {})

                event_utc = float(data.get("UTC", 0))
                if subscribe_utc is None and event_utc > 0:
                    subscribe_utc = event_utc

                if code == "IgnoreInvite" and action == "Start":
                    real_utc   = float(data.get("RealUTC", 0))
                    call_id    = str(data.get("CallID", ""))
                    local_time = data.get("LocaleTime", "")

                    if subscribe_utc is not None and abs(event_utc - subscribe_utc) < 2:
                        continue  # replayed event
                    if subscribe_utc is not None and (subscribe_utc - real_utc) > 10:
                        continue  # stale event buffered by VTH
                    if call_id in seen_call_ids:
                        continue  # duplicate
                    seen_call_ids.add(call_id)

                    log.info("RING! CallID=%s  %s", call_id, local_time)
                    on_ring(call_id, local_time)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Talkback — push audio UP to the door speaker over the cloud relay
# ---------------------------------------------------------------------------
#
# Fully reverse-engineered + verified at the door (2026-06-13). The recipe:
#   1. Hold an encrypt=2 video session (precondition: idle door 503s talk, and
#      an encrypt=0 session silently MUTES the uplink audio).
#   2. 6-PLAY handshake on the encrypt=2 visualtalk relay (one socket).
#   3. Push PCMA(G711a) @16000Hz, framed as: DHAV 0xf0 wrapper -> RTP (pt=8,
#      MARKER bit set) -> 0x24 interleave on channel 10. Pace at 40ms/frame
#      (640 samples). Drain the socket while sending (TCP backpressure else).
#
# ★ THE AUDIO PAYLOAD IS AES-128-ECB ENCRYPTED (NOT plaintext PCMA). ★
# The encrypt=2 session means the 640-byte a-law payload of each 0xf0 DHAV frame
# must be AES-128-ECB encrypted with TALK_AES_KEY before sending. Key was extracted
# 2026-06-15 via Android emulator + Frida memory dump of DMSS (see DahuaConsole/
# keyhunt_v5.py + memory dahua_talkback_verified_audio.md). VERIFIED: re-encrypting
# the decrypted captured payload reproduces the captured ciphertext byte-for-byte.
# Frame layout: 24B hdr + 20B ext + 640B AES(alaw) + 8B 'dhav' trailer = 692B.
# (Earlier "plaintext PCMA" / "644 samples" notes were WRONG — sending plaintext
#  produced choppy noise because the device decrypted our plaintext as ciphertext.)

from Crypto.Cipher import AES as _AES
# AES-128-ECB talk key, hex, from env DAHUA_TALK_AES_KEY (gitignored .env). No
# hardcoded key in source. Talk endpoints fail clearly if unset.
TALK_AES_KEY = bytes.fromhex(os.environ.get("DAHUA_TALK_AES_KEY", ""))

TALK_AUDIO_RATE   = 16000      # PCMA sample rate (the SDP's PCMA/16000 was right)
TALK_FRAME_SAMPLES = 640       # 40ms/frame @16kHz; 640B a-law payload (AES-encrypted)
# Gap between the handshake PLAYs (single socket). DMSS pipelines them fast.
TALK_HANDSHAKE_GAP = float(os.environ.get("DAHUA_TALK_HANDSHAKE_GAP", "0.05"))
TALK_CHANNEL      = 10         # interleave channel of the uplink
TALK_PTYPE        = 8          # RTP payload type: PCMA
# Talk-start handshake — DMSS-aligned. A fresh relay capture (DahuaConsole/48207c54)
# shows DMSS sends exactly THESE 3 PLAYs to start, then audio immediately. (Old code
# sent 6; the extra 3 — method=2, trackID=31&method=1, trackID=70&method=3 — are NOT
# in DMSS's start; its trackID=6&method=0 / 31&method=3 appear only at TEARDOWN. The
# old "exact 6-PLAY" comment was a misread.) Wire-verified: 3 PLAYs → audio flows.
TALK_HANDSHAKE = [
    "trackID=31&method=0",                 # media session
    "talktype=talk&trackID=64&method=0",   # talk start
    "trackID=6&method=1",
]


def _linear_to_alaw(sample: int) -> int:
    """16-bit signed PCM -> 8-bit A-law (G.711)."""
    ALAW_MAX = 0x7FFF
    sign = 0x00 if sample >= 0 else 0x80
    if sample < 0:
        sample = -sample
    if sample > ALAW_MAX:
        sample = ALAW_MAX
    if sample >= 256:
        exponent = 7
        for exp in range(7, 0, -1):
            if sample >= (1 << (exp + 7)):
                exponent = exp
                break
        mantissa = (sample >> (exponent + 3)) & 0x0F
        alaw = (exponent << 4) | mantissa
    else:
        alaw = sample >> 4
    return (alaw ^ 0x55 ^ sign) & 0xFF


# Constant 20-byte DHAV extension (offset 24..44) ending in the a4009922 marker —
# verified byte-for-byte against captured DMSS frames.
_DHAV_EXT = bytes.fromhex("83010e049500000000010000b308fba6a4009922")


def _dhav_audio_frame(alaw: bytes, idx: int, ts_base: int) -> bytes:
    """Build a Dahua DHAV 0xf0 talk frame from 640 bytes of a-law.

    Layout (692B for 640B payload): 24B header + 20B extension + payload + 8B
    'dhav' trailer. The 640-byte a-law payload is a HYBRID: the FIRST 256 bytes
    are AES-128-ECB encrypted with TALK_AES_KEY; the remaining 384 bytes are PLAIN
    a-law. (Encrypting all 640 produced distorted audio — only the first 256 are
    encrypted; verified: hybrid re-encode reproduces captured frames byte-for-byte,
    decode confirmed clean+correct-speed at the door 2026-06-16.)
    hdr[23] is a checksum = sum(hdr[0:23])&0xFF."""
    enc = _AES.new(TALK_AES_KEY, _AES.MODE_ECB).encrypt(alaw[:256]) + alaw[256:]
    total = 24 + 20 + len(enc) + 8
    hdr = bytearray(24)
    hdr[0:4] = b"DHAV"
    hdr[4]   = 0xf0
    struct.pack_into("<I", hdr, 8, idx & 0xFFFFFFFF)        # frame seq
    struct.pack_into("<I", hdr, 12, total)                  # total len
    # ts16 = per-frame audio clock in the DHAV header. NOTE: clean DMSS captures use
    # +20/frame, but empirically +40 gave the best observed talkback delay (~2.5s vs
    # ~3s) at the door — kept at 40 as the pragmatic best. The talkback latency was NOT
    # fully solved (DMSS is sub-second over the same relay; we match it on every readable
    # wire signal yet stay ~2.5-3s — likely a server-side client-authorization/QoS
    # difference in the TLS control plane we can't decrypt). See memory
    # intercom-webrtc-downlink.md "TALKBACK LATENCY — INVESTIGATION CLOSED".
    ts16 = (100 + idx * 40)
    struct.pack_into("<I", hdr, 16, ts_base & 0xFFFFFFFF)   # session ts (epoch sec)
    struct.pack_into("<H", hdr, 20, ts16 & 0xFFFF)          # +40 (best-observed)
    hdr[22] = 0x14                                          # extension length (20)
    hdr[23] = sum(hdr[0:23]) & 0xFF                         # header checksum
    return bytes(hdr) + _DHAV_EXT + enc + b"dhav" + struct.pack("<I", total)


def _resample_to_16k(samples, src_rate: int):
    """Crude nearest-sample resample of a 16-bit PCM int list to 16000 Hz."""
    if src_rate == TALK_AUDIO_RATE:
        return samples
    ratio = TALK_AUDIO_RATE / src_rate
    return [samples[int(i / ratio)] for i in range(int(len(samples) * ratio))]


class TalkbackSession:
    """One talk uplink to the door. Opens the encrypt=2 video-hold + 6-PLAY
    handshake on start(); feed 16-bit PCM via push() (any rate); close() to stop.

    Thread-safe-ish for one producer. A hard max_seconds backstop guards against
    a stuck-open mic (matches DMSS's own few-minute cutoff). Use as the engine
    behind both POST /talk (whole clip) and a streaming push-to-talk endpoint.
    """

    def __init__(self, creds: "Credentials", device_sn: str, pcs_username: str,
                 channel: int = 1, max_seconds: float = 180.0):
        self.creds = creds
        self.device_sn = device_sn
        self.pcs_username = pcs_username
        self.channel = channel
        self.max_seconds = max_seconds
        self._sock: Optional[socket.socket] = None
        self._hold_sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._drain_thread: Optional[threading.Thread] = None
        self._idx = 0
        self._rtp_seq = secrets.randbits(16)
        self._rtp_ts = secrets.randbits(32)
        self._ts_base = int(time.time())
        self._pcm_buf: list[int] = []
        self._t0 = 0.0
        self._opened_at = 0.0
        self._frames_sent = 0
        self._opened = False
        # Live push-to-talk: cap how far the send buffer may grow so latency
        # can't accumulate (a live mic must stay near the live edge — drop stale
        # audio rather than play out an ever-growing backlog at 40ms/frame).
        # OFF for clip playback (play_audio_clip), where every frame must be sent.
        self.live = False
        self._max_backlog_frames = 3   # ~120ms of slack before we drop oldest

    # -- relay plumbing -----------------------------------------------------
    def _open_relay(self, encrypt: bool) -> tuple[str, str, int]:
        bearer = self.creds.ensure()
        tok = get_play_token(bearer, self.pcs_username)
        url = get_relay_url(tok, bearer, self.pcs_username, self.device_sn,
                            channel=self.channel, stream=0, encrypt=encrypt)
        hp, path = url.split("/", 1)
        host, port = hp.rsplit(":", 1)
        return host, int(port), path, tok

    def start(self):
        # ONE play token + ONE relay URL for BOTH the video-hold and the talk
        # handshake — mirrors the verified talkback_replay.py. Using a second
        # token/relay for the talk (the old code did) opens a session the door
        # doesn't treat as talk-ready → frames accepted but NO audio.
        bearer = self.creds.ensure()
        tok = get_play_token(bearer, self.pcs_username)
        relay_url = get_relay_url(tok, bearer, self.pcs_username, self.device_sn,
                                  channel=self.channel, stream=0, encrypt=True)
        hp, hpath = relay_url.split("/", 1)
        thost, tport = hp.rsplit(":", 1)
        tport = int(tport)
        tpath = hpath

        # SINGLE-SOCKET handshake, mirroring DMSS. The old code opened a SEPARATE
        # video-hold socket, slept 1.5-3s, then opened a SECOND socket for the talk
        # handshake — and DMSS captures show DMSS does it all on ONE socket and sends
        # the first audio frame ~0.13s after the last PLAY (vs our multi-second gap),
        # which is the ~3s talkback delay. The talk handshake already begins with
        # trackID=31&method=0 (the media-session/hold), so the separate hold socket
        # was redundant. Run the whole handshake on one socket, minimal pacing, then
        # send audio immediately (the caller's push() follows right after start()).
        self._sock = socket.create_connection((thost, tport), timeout=15)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sep = "&" if "?" in tpath else "?"
        for cseq, params in enumerate(TALK_HANDSHAKE, 1):
            self._sock.sendall(
                (f"PLAY /{tpath}{sep}{params} HTTP/1.1\r\nHost: {thost}:{tport}\r\n"
                 f"Accpet-Sdp: Private\r\nConnection: keep-alive\r\nCseq: {cseq}\r\n\r\n").encode())
            # brief read for the response, but don't block long — DMSS pipelines fast
            self._sock.settimeout(0.5)
            try:
                self._sock.recv(4096)
            except socket.timeout:
                pass
            time.sleep(TALK_HANDSHAKE_GAP)
        # 3) drain the talk socket while we send (else TCP backpressure stalls)
        self._drain_thread = threading.Thread(target=self._drain, args=(self._sock,),
                                              daemon=True)
        self._drain_thread.start()
        self._opened_at = time.monotonic()   # for the max_seconds backstop
        self._t0 = time.monotonic()           # pacing anchor; re-set at first frame
        self._opened = True
        log.info("Talkback session open (device %s)", self.device_sn)

    def _drain(self, sock):
        sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                if not sock.recv(65536):
                    break
            except socket.timeout:
                pass
            except OSError:
                break

    # -- audio feed ---------------------------------------------------------
    def _send_frame(self, samps: list[int]):
        # Correct G.711 a-law via audioop (verified byte-for-byte vs captured
        # DMSS frames; the old hand-rolled _linear_to_alaw was wrong → silence).
        pcm = struct.pack("<%dh" % len(samps), *samps)
        alaw = audioop.lin2alaw(pcm, 2)
        dhav = _dhav_audio_frame(alaw, self._idx, self._ts_base)
        rtp = struct.pack("!BBHII", 0x80, 0x80 | (TALK_PTYPE & 0x7f),
                          self._rtp_seq & 0xFFFF, self._rtp_ts & 0xFFFFFFFF,
                          0x12345678) + dhav
        frame = struct.pack("!BBH", 0x24, TALK_CHANNEL & 0xFF, len(rtp)) + rtp
        self._sock.sendall(frame)
        self._idx += 1
        self._rtp_seq += 1
        self._rtp_ts += len(samps)
        # Anchor the pacing clock to the FIRST frame actually sent, not to session
        # open. start() runs seconds before the user speaks; if _t0 is the session
        # time, the schedule is already far in the past when audio begins, so every
        # frame's target is < now → no sleep → we BLAST the whole stream faster than
        # realtime into the door's jitter buffer. Anchoring at first frame keeps us
        # paced to real time.
        if self._frames_sent == 0:
            self._t0 = time.monotonic()
        self._frames_sent += 1
        # pace against an absolute schedule (no drift), realtime 40ms/frame
        target = self._t0 + self._frames_sent * (TALK_FRAME_SAMPLES / TALK_AUDIO_RATE)
        d = target - time.monotonic()
        if d > 0:
            time.sleep(d)

    def push(self, pcm16: bytes, src_rate: int = TALK_AUDIO_RATE):
        """Feed 16-bit little-endian mono PCM. Buffers and flushes whole 40ms
        frames. Resamples to 16kHz if src_rate differs. Honors max_seconds."""
        if not self._opened or self._stop.is_set():
            return
        if time.monotonic() - self._opened_at > self.max_seconds:
            log.warning("Talkback max_seconds reached — stopping")
            self.close()
            return
        samples = list(struct.unpack("<%dh" % (len(pcm16) // 2), pcm16))
        if src_rate != TALK_AUDIO_RATE:
            samples = _resample_to_16k(samples, src_rate)
        self._pcm_buf.extend(samples)
        # (Priming is done up-front in start() with silent frames — see there. Here we
        # just send real mic audio with the normal backlog cap + realtime pacing.)
        # Live: bound the backlog so lag can't accumulate. The browser delivers
        # mic audio in bursts and _send_frame paces at 40ms/frame against an
        # absolute clock — so any backlog becomes permanent latency. Drop the
        # OLDEST buffered audio beyond the cap, keeping us near the live edge,
        # and re-anchor the pacing clock to now so the kept audio plays out fresh.
        if self.live:
            cap = self._max_backlog_frames * TALK_FRAME_SAMPLES
            if len(self._pcm_buf) > cap:
                del self._pcm_buf[:len(self._pcm_buf) - cap]
                # reset the absolute pacing schedule to the current send count
                self._t0 = time.monotonic() - self._frames_sent * (
                    TALK_FRAME_SAMPLES / TALK_AUDIO_RATE)
        while len(self._pcm_buf) >= TALK_FRAME_SAMPLES and not self._stop.is_set():
            self._send_frame(self._pcm_buf[:TALK_FRAME_SAMPLES])
            del self._pcm_buf[:TALK_FRAME_SAMPLES]

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        # flush a final partial frame (padded with a-law silence)
        if self._opened and self._pcm_buf and self._sock:
            try:
                pad_n = TALK_FRAME_SAMPLES - len(self._pcm_buf)
                self._send_frame(self._pcm_buf + [0] * pad_n)
            except Exception:
                pass
        for s in (self._sock, self._hold_sock):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        log.info("Talkback session closed (%d frames sent)", self._frames_sent)


def play_audio_clip(creds: "Credentials", device_sn: str, pcs_username: str,
                    pcm16: bytes, src_rate: int, channel: int = 1,
                    max_seconds: float = 180.0) -> int:
    """Convenience: open a talkback session, push a whole PCM clip, close.
    Returns the number of frames sent. Blocks for the clip's real duration."""
    sess = TalkbackSession(creds, device_sn, pcs_username, channel, max_seconds)
    sess.start()
    try:
        sess.push(pcm16, src_rate)
    finally:
        sess.close()
    return sess._frames_sent
