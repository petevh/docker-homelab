#!/usr/bin/env python3
"""
Dahua VTH DHIP client library.

Implements:
  unlock_door()      — working (DHIP UnlockManager.openDoors)
  get_stream_url()   — stub, not yet implemented
  subscribe_events() — stub, not yet implemented

All config is passed as arguments — no hardcoded values.
"""

import socket
import json
import struct
import hashlib
import logging

log = logging.getLogger(__name__)


class DahuaError(Exception):
    pass


class DahuaClient:
    """
    Per-request DHIP client. Connects, authenticates, executes one operation,
    then closes the socket.

    TODO: Evaluate whether a persistent session is worth the complexity once
    unlock param format is confirmed and stream/events are implemented.
    """

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._sock = None
        self._session_id = 0
        self._req_id = 0

    # ------------------------------------------------------------------
    # Low-level DHIP framing
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    @staticmethod
    def _md5(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest().upper()

    @staticmethod
    def _build_packet(data: dict, session_id: int = 0) -> bytes:
        payload = json.dumps(data, separators=(',', ':')).encode('latin-1')
        header = struct.pack(
            '<IIII II II',
            0x20000000,   # Magic
            0x50494844,   # "DHIP"
            session_id,
            0x10000000,
            len(payload), 0,
            len(payload), 0,
        )
        return header + payload

    @staticmethod
    def _parse_packet(data: bytes) -> dict | None:
        if len(data) < 32:
            return None
        try:
            return json.loads(data[32:].decode('latin-1'))
        except Exception:
            return None

    def _recv(self, timeout: int = 5) -> dict | None:
        self._sock.settimeout(timeout)
        data = b''
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) >= 32:
                    try:
                        json.loads(data[32:].decode('latin-1'))
                        break
                    except Exception:
                        pass
        except socket.timeout:
            pass
        return self._parse_packet(data)

    def _send(self, payload: dict) -> dict | None:
        packet = self._build_packet(payload, self._session_id)
        self._sock.send(packet)
        return self._recv()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self.host, self.port))
        log.debug("Connected to %s:%s", self.host, self.port)
        self._authenticate()

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def _authenticate(self):
        # Step 1: empty-password login to get challenge
        challenge_resp = self._send({
            "method": "global.login",
            "params": {
                "userName": self.username,
                "password": "",
                "clientType": "Dahua3.0",
                "loginType": "Direct",
                "authorityType": "Default",
            },
            "id": self._next_id(),
            "session": 0,
        })
        if not challenge_resp:
            raise DahuaError("No response to login challenge")

        self._session_id = challenge_resp.get('session', 0)
        params = challenge_resp.get('params', {})
        realm = params.get('realm', '')
        random_val = params.get('random', '')

        # Dahua digest: MD5(MD5(user:realm:pass):random:MD5(user:realm:pass))
        pwd_hash = self._md5(f"{self.username}:{realm}:{self.password}")
        final_hash = self._md5(f"{pwd_hash}:{random_val}:{pwd_hash}")

        # Step 2: authenticated login
        auth_resp = self._send({
            "method": "global.login",
            "params": {
                "userName": self.username,
                "password": final_hash,
                "clientType": "Dahua3.0",
                "loginType": "Direct",
                "authorityType": "Default",
            },
            "id": self._next_id(),
            "session": self._session_id,
        })
        if not auth_resp or not auth_resp.get('result'):
            raise DahuaError("Authentication failed")

        log.debug("Authenticated (session=%s)", self._session_id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def open_doors(self, channel: int, door_index: int) -> bool:
        """
        Send UnlockManager.openDoors.

        TODO: Confirm which param format the VTH2622GW-W accepts, then remove
        the other attempt:
          Option A — {"channel": <n>}
          Option B — {"DoorIndex": <n>}
        Currently tries A then B and returns True if either succeeds.
        """
        # Attempt A
        resp_a = self._send({
            "method": "UnlockManager.openDoors",
            "params": {"doors": [{"channel": channel}]},
            "id": self._next_id(),
            "session": self._session_id,
        })
        log.debug("openDoors (channel) response: %s", resp_a)
        if resp_a and resp_a.get('result'):
            return True

        # Attempt B
        resp_b = self._send({
            "method": "UnlockManager.openDoors",
            "params": {"doors": [{"DoorIndex": door_index}]},
            "id": self._next_id(),
            "session": self._session_id,
        })
        log.debug("openDoors (DoorIndex) response: %s", resp_b)
        return bool(resp_b and resp_b.get('result'))


# ------------------------------------------------------------------
# Public API — called by main.py
# ------------------------------------------------------------------

def unlock_door(
    host: str,
    port: int,
    username: str,
    password: str,
    channel: int = 1,
    door_index: int = 0,
) -> bool:
    with DahuaClient(host, port, username, password) as client:
        return client.open_doors(channel, door_index)


def get_stream_url(**kwargs) -> dict:
    # TODO: implement once dos_stream.py logic is promoted from dahua-research
    raise NotImplementedError("Stream endpoint not yet implemented")


async def subscribe_events(**kwargs):
    # TODO: implement DHIP event subscription (doorbell/motion)
    raise NotImplementedError("Events endpoint not yet implemented")
    yield  # make this a generator so callers can treat it as async iter
