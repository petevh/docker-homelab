#!/usr/bin/env python3
"""
Dahua VTH client — cloud API (dmss-di.dolynkcloud.com) + direct DHIP (TCP/5000).

Reverse-engineered from DMSS APK + live PCAP. See DahuaConsole/NEXT_STEPS.md
for full research notes.
"""

import hashlib
import hmac as _hmac
import base64
import secrets
import json
import logging
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

RELAY_TTL = 50  # reconnect before relay URL expires at 60s

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
                  device_sn: str, channel: int = 1, stream: int = 0) -> str:  # stream=0 = 1280x720 main/HD; stream=1 = 352x288 sub-stream
    """Return the plain (unencrypted) relay URL for the VTO camera."""
    append_url = f"/real/{channel}/{stream}/RTSV1"
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


# ---------------------------------------------------------------------------
# StreamProxy — manages relay connection + ffmpeg, provides JPEG frames
# ---------------------------------------------------------------------------

class StreamProxy:
    def __init__(self, bearer_token: str, pcs_username: str, device_sn: str,
                 channel: int = 1, stream: int = 0, width: int = 0, quality: int = 5):
        self.bearer_token  = bearer_token
        self.pcs_username  = pcs_username
        self.device_sn     = device_sn
        self.channel       = channel
        self.stream        = stream
        self.width         = width
        self.quality       = quality
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

    def _run_loop(self):
        while self._running:
            try:
                self._run_once()
            except Exception as e:
                log.warning("Stream error: %s — reconnecting in 5s", e)
                time.sleep(5)

    def _run_once(self):
        log.info("Fetching play token...")
        play_token = get_play_token(self.bearer_token, self.pcs_username)
        log.info("Getting relay URL...")
        relay_url = get_relay_url(play_token, self.bearer_token, self.pcs_username,
                                  self.device_sn, self.channel, self.stream)
        log.info("Relay URL: %s", relay_url)

        sock = connect_relay(relay_url)
        sock.settimeout(10)
        log.info("Connected to relay — starting ffmpeg")

        scale = f"scale={self.width}:-2," if self.width else ""
        ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-f", "h264",
            "-i", "pipe:0",
            "-vf", f"{scale}format=yuvj420p",
            "-q:v", str(self.quality),
            "-f", "image2pipe", "-vcodec", "mjpeg",
            "-r", "5",
            "pipe:1",
        ]
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # inherit — goes to container logs
        )

        def feed():
            # DHAV frame types interleaved in stream:
            #   0xfd — H264 SPS+PPS+IDR keyframe (every ~1s)
            #   0xfc — H264 P-frame
            #   0xf0 — Dahua proprietary (not H264) — must be skipped
            # Each H264 frame has an 8-byte sub-header before the H264 start code.
            # We extract raw Annex-B H264 and buffer until we see a complete IDR
            # before feeding to ffmpeg, to avoid partial-GOP decode corruption.
            H264_TYPES = {0xfd, 0xfc}
            DHAV_HDR    = 32
            SUBHDR      = 8
            deint       = RtpDeinterleaver()   # strip RTP-over-TCP interleave headers
            try:
                connect_time = time.time()
                buf         = b""
                idr_buf     = b""   # accumulate H264 until IDR seen
                idr_seen    = False

                while self._running and (time.time() - connect_time < RELAY_TTL):
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    chunk = deint.feed(chunk)   # de-interleave before DHAV parsing
                    if not chunk:
                        continue
                    buf += chunk

                    out = b""
                    pos = 0
                    while True:
                        start = buf.find(b"DHAV", pos)
                        if start < 0:
                            buf = buf[-3:]
                            break
                        if start + DHAV_HDR + SUBHDR > len(buf):
                            buf = buf[start:]
                            break
                        frame_type = buf[start + 4]
                        next_start = buf.find(b"DHAV", start + 4)
                        if next_start < 0:
                            buf = buf[start:]
                            break

                        if frame_type in H264_TYPES:
                            h264 = buf[start + DHAV_HDR + SUBHDR:next_start]
                            if not idr_seen:
                                # Buffer until we see a complete IDR (0xfd frame)
                                if frame_type == 0xfd:
                                    idr_seen = True
                                    out = h264
                                # else: discard P-frames before first IDR
                            else:
                                out += h264
                        pos = next_start

                    if out:
                        try:
                            proc.stdin.write(out)
                            proc.stdin.flush()
                        except BrokenPipeError:
                            break
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                sock.close()

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

        buf = b""
        try:
            while self._running:
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
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            feeder.join(timeout=5)


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
