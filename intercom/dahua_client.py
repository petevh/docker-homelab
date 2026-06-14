#!/usr/bin/env python3
"""
Dahua VTH client — cloud API (dmss-di.dolynkcloud.com) + direct DHIP (TCP/5000).

Reverse-engineered from DMSS APK + live PCAP. See DahuaConsole/NEXT_STEPS.md
for full research notes.
"""

import audioop          # G.711 a-law encode (verified vs captured frames)
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

    def __init__(self, sock: socket.socket, running, held: bool = False):
        self._sock    = sock
        self._running = running          # callable -> bool (proxy still alive)
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=256)
        self._stop    = False
        self._thread  = None
        self.dead     = False
        self._ready   = False            # True once connected + synced to a keyframe
        self._held    = held             # if True, discard data until release()

    def start(self):
        self._thread = threading.Thread(target=self._pull, daemon=True)
        self._thread.start()

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

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

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
        return [
            "ffmpeg", "-loglevel", "warning",
            "-fflags", "+genpts+nobuffer",
            "-f", "dhav", "-i", "pipe:0",
            # Output 1: RTSP (H264 passthrough + AAC audio)
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "aac", "-ar", "16000", "-b:a", "32k",
            "-f", "rtsp", "-rtsp_transport", "tcp", self.rtsp_publish_url,
            # Output 2: MJPEG snapshots
            "-map", "0:v:0",
            "-vf", f"{scale}format=yuvj420p",
            "-q:v", str(self.quality), "-r", "5",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ]

    def _run_loop(self):
        # ONE persistent ffmpeg + RTSP publish for the whole lifetime, so the relay's
        # 50s reconnect cycle does NOT tear down the RTSP stream (clients stay connected).
        # The relay socket reconnects underneath and keeps feeding the same ffmpeg stdin;
        # each new relay session is re-synced to a keyframe before its bytes are written.
        while self._running:
            proc = subprocess.Popen(
                self._ffmpeg_cmd(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None,  # inherit — goes to container logs
            )
            reader = threading.Thread(target=self._read_jpeg, args=(proc,), daemon=True)
            reader.start()
            try:
                # Feed relay sessions into this ffmpeg until it (or we) dies.
                while self._running and proc.poll() is None:
                    try:
                        self._feed_one_relay_session(proc)
                    except Exception as e:
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
        sess = _RelaySession(sock, lambda: self._running, held=held)
        sess.start()
        return sess

    def _feed_one_relay_session(self, proc):
        """Feed relay sessions into the persistent ffmpeg with OVERLAP. The relay
        hard-closes each connection at ~66s, so we pre-warm the next session a few
        seconds early (connected + keyframe-aligned, but discarding data). At switch
        time we release it so it emits fresh from its NEXT keyframe — a clean GOP
        boundary — and stop the old one. No starvation gap, no stale-backlog dump."""
        PREWARM_LEAD = 6     # pre-warm the next session this many seconds before TTL
        active = self._open_relay_session()
        active.release()     # active emits immediately
        log.info("Relay session connected (feeding persistent ffmpeg)")
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
#      MARKER bit set) -> 0x24 interleave on channel 10. Pace at 40.25ms/frame
#      (644 samples). Drain the socket while sending (TCP backpressure else).
# The audio PAYLOAD is plaintext PCMA; encrypt=2 is only the session mode.
#
# FRAME SIZE = 644 samples, NOT 640. The device expects the exact 692-byte 0xf0
# DHAV frame DMSS sends (40 hdr + 644 alaw + 8 trailer). A 640-sample frame is
# 688 bytes — 4 bytes short of the byte-for-byte-verified capture, and the door
# accepts it but plays NOTHING. This matches DahuaConsole/talkback_replay.py,
# the only audibly-verified reference. Do not "round to 640" — verified at door.

TALK_AUDIO_RATE   = 16000      # PCMA sample rate (the SDP's PCMA/16000 was right)
TALK_FRAME_SAMPLES = 644       # 40.25ms/frame @16kHz — matches verified 692B capture
TALK_CHANNEL      = 10         # interleave channel of the uplink
TALK_PTYPE        = 8          # RTP payload type: PCMA
TALK_HANDSHAKE = [             # exact 6-PLAY sequence DMSS sends, one socket
    "trackID=31&method=0",                 # media session
    "talktype=talk&trackID=64&method=0",   # talk start
    "trackID=6&method=1",
    "method=2",                            # uplink open
    "trackID=31&method=1",
    "trackID=70&method=3",
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


def _dhav_audio_frame(alaw: bytes, idx: int, ts_base: int) -> bytes:
    """Wrap PCMA bytes in a Dahua DHAV 0xf0 audio frame.

    hdr[23] is a HEADER CHECKSUM = sum(hdr[0:23]) & 0xFF, which the device
    validates — a wrong value there makes the device silently drop the frame
    (this was the silent-/talk bug: it was previously written as a guessed
    counter `0x17 + idx*0x15`). Verified against captured DMSS frames: every
    real frame's byte[23] equals sum of its first 23 header bytes."""
    total = 40 + len(alaw) + 8
    hdr = bytearray(40)
    hdr[0:4] = b"DHAV"
    hdr[4]   = 0xf0
    struct.pack_into("<I", hdr, 8, idx & 0xFFFFFFFF)         # frame seq
    struct.pack_into("<I", hdr, 12, total)                   # total len
    # ts32 must be a REAL, plausible, monotonic epoch-ish timestamp advancing
    # ~1/sec (real frames: 1771730525++). A garbage placeholder (0x12345678 = a
    # 1979 time) makes the device discard frames → silence. ts16 = 100 + idx*20.
    ts16 = (100 + idx * 20)
    struct.pack_into("<I", hdr, 16, (ts_base + ts16 // 1000) & 0xFFFFFFFF)  # session ts (sec)
    struct.pack_into("<H", hdr, 20, ts16 & 0xFFFF)          # +20/frame (ms-ish)
    hdr[22] = 0x14                                           # extension length
    hdr[24:32] = bytes((0x83, 0x01, 0x0e, 0x04, 0x95, 0x00, 0x00, 0x00))  # 0x0e=PCM_ALAW
    hdr[32:40] = bytes((0x00, 0x01, 0x00, 0x00, 0xb3, 0x08, 0xfb, 0xa6))  # const sub-hdr
    hdr[23] = sum(hdr[0:23]) & 0xFF                          # header checksum
    return bytes(hdr) + alaw + b"dhav" + struct.pack("<I", total)


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
        self._frames_sent = 0
        self._opened = False

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
        hhost, hport = hp.rsplit(":", 1)

        # 1) hold the video session so the door is a live media session
        self._hold_sock = socket.create_connection((hhost, int(hport)), timeout=15)
        self._hold_sock.sendall(
            (f"PLAY /{hpath}{'&' if '?' in hpath else '?'}trackID=31&method=0 "
             f"HTTP/1.1\r\nHost: {hhost}:{hport}\r\nAccpet-Sdp: Private\r\n"
             f"Connection: keep-alive\r\nCseq: 1\r\n\r\n").encode())
        threading.Thread(target=self._drain, args=(self._hold_sock,), daemon=True).start()
        time.sleep(3)   # let the media session come up

        # 2) talk on the SAME relay URL (new socket) + 6-PLAY handshake
        thost, tport, tpath = hhost, int(hport), hpath
        self._sock = socket.create_connection((thost, int(tport)), timeout=15)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sep = "&" if "?" in tpath else "?"
        for cseq, params in enumerate(TALK_HANDSHAKE, 1):
            self._sock.sendall(
                (f"PLAY /{tpath}{sep}{params} HTTP/1.1\r\nHost: {thost}:{tport}\r\n"
                 f"Accpet-Sdp: Private\r\nConnection: keep-alive\r\nCseq: {cseq}\r\n\r\n").encode())
            self._sock.settimeout(2.0)
            try:
                self._sock.recv(4096)
            except socket.timeout:
                pass
            time.sleep(0.15)
        # 3) drain the talk socket while we send (else TCP backpressure stalls)
        self._drain_thread = threading.Thread(target=self._drain, args=(self._sock,),
                                              daemon=True)
        self._drain_thread.start()
        self._t0 = time.monotonic()
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
        if time.monotonic() - self._t0 > self.max_seconds:
            log.warning("Talkback max_seconds reached — stopping")
            self.close()
            return
        samples = list(struct.unpack("<%dh" % (len(pcm16) // 2), pcm16))
        if src_rate != TALK_AUDIO_RATE:
            samples = _resample_to_16k(samples, src_rate)
        self._pcm_buf.extend(samples)
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
