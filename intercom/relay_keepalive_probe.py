#!/usr/bin/env python3
"""
relay_keepalive_probe.py — answer "can ONE cloud relay session be held past ~66s,
and what closes it?" without DMSS, a phone, or any firewall change.

What it does:
  1. Mints a play_token + signed relay_url exactly like StreamProxy does.
  2. Opens ONE relay PLAY (via connect_relay) and then sends NOTHING back.
  3. Reads passively, logging inter-packet gaps, and records the exact moment +
     manner the cloud closes the socket (clean FIN / RST / timeout) with elapsed s.

Interpretation:
  - Socket dies at a hard ~66s with nothing sent by us
        -> the 66s is the signed-token TTL (time=/digest= in the PLAY url), NOT an
           idle timeout. Keepalive is moot; the fix is re-minting the relay url.
           Our rotate-and-pre-warm approach is correct.
  - Socket keeps delivering well past 66s with nothing sent
        -> there is NO hard limit; our 66s assumption / our own PLAY params are
           what cut it short. Investigate the PLAY, don't rotate.
  (To test whether a periodic uplink EXTENDS a session, set --poke to send an RTSP
   OPTIONS every 20s on the same socket and see if death moves past 66s.)

Run inside the intercom container so it inherits the same env/creds:
  docker compose exec intercom python /app/relay_keepalive_probe.py
  docker compose exec intercom python /app/relay_keepalive_probe.py --poke
"""

import argparse
import os
import socket
import sys
import time

import dahua_client as dc


def build_creds() -> "dc.Credentials":
    return dc.Credentials(
        bearer=os.environ.get("DAHUA_BEARER_TOKEN", ""),
        pcs_username=os.environ.get("DAHUA_PCS_USERNAME", ""),
        account=os.environ.get("DAHUA_ACCOUNT", ""),
        password=os.environ.get("DAHUA_ACCOUNT_PASSWORD", ""),
        area_code=os.environ.get("DAHUA_AREA_CODE", "971"),
        country=os.environ.get("DAHUA_COUNTRY", "AE"),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=float, default=180.0,
                    help="give up after this many seconds if still alive (default 180)")
    ap.add_argument("--poke", action="store_true",
                    help="send an RTSP OPTIONS keepalive every --poke-interval s")
    ap.add_argument("--poke-interval", type=float, default=20.0)
    ap.add_argument("--quiet-gaps", action="store_true",
                    help="only log gaps > 1s (suppress per-packet spam)")
    args = ap.parse_args()

    creds = build_creds()
    pcs_user = os.environ.get("DAHUA_PCS_USERNAME", "")
    device_sn = os.environ.get("DAHUA_DEVICE_SN", "")
    channel = int(os.environ.get("DAHUA_CHANNEL", "1"))
    stream = int(os.environ.get("DAHUA_STREAM", "0"))

    if not (pcs_user and device_sn):
        print("ERROR: DAHUA_PCS_USERNAME / DAHUA_DEVICE_SN not set in env", file=sys.stderr)
        return 2

    print(f"[probe] device_sn={device_sn} channel={channel} stream={stream} "
          f"poke={args.poke} (every {args.poke_interval}s)" if args.poke
          else f"[probe] device_sn={device_sn} channel={channel} stream={stream} poke=off")

    # Mint token + signed relay url exactly like StreamProxy._open_relay_session.
    play_token = dc.with_bearer_retry(creds, lambda b: dc.get_play_token(b, pcs_user))
    relay_url = dc.with_bearer_retry(
        creds,
        lambda b: dc.get_relay_url(play_token, b, pcs_user, device_sn, channel, stream))
    print(f"[probe] relay_url = {relay_url}")

    sock = dc.connect_relay(relay_url)   # PLAY sent; positioned at start of DHAV
    sock.settimeout(2.0)
    print("[probe] PLAY ok — now PASSIVE-reading, sending nothing"
          + (" (except OPTIONS pokes)" if args.poke else ""))

    t0 = time.time()
    last = t0
    last_poke = t0
    total_bytes = 0
    n_chunks = 0
    host = relay_url.split("/", 1)[0]

    def fmt(t):
        return f"{t - t0:7.2f}s"

    while True:
        now = time.time()
        elapsed = now - t0

        if elapsed >= args.max:
            print(f"[probe] {fmt(now)} STILL ALIVE at --max — stopping. "
                  f"({total_bytes} bytes / {n_chunks} chunks). "
                  f"=> NO hard 66s cap on a held session.")
            sock.close()
            return 0

        if args.poke and (now - last_poke) >= args.poke_interval:
            req = (f"OPTIONS /{relay_url.split('/', 1)[1]} RTSP/1.0\r\n"
                   f"CSeq: 99\r\nHost: {host}\r\n\r\n").encode()
            try:
                sock.sendall(req)
                print(f"[probe] {fmt(now)} >>> sent OPTIONS poke")
            except OSError as e:
                print(f"[probe] {fmt(now)} poke send failed: {e}")
            last_poke = now

        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError as e:
            print(f"[probe] {fmt(now)} RECV ERROR after {elapsed:.2f}s: {e!r} "
                  f"(likely RST). total={total_bytes}B")
            return 0

        if data == b"":
            print(f"[probe] {fmt(now)} CLEAN CLOSE (FIN) after {elapsed:.2f}s. "
                  f"total={total_bytes}B / {n_chunks} chunks. "
                  f"=> server closed; if ~66s with no poke, that's the token TTL.")
            return 0

        n_chunks += 1
        total_bytes += len(data)
        gap = now - last
        last = now
        if not args.quiet_gaps or gap > 1.0:
            print(f"[probe] {fmt(now)} +{len(data):5d}B  gap={gap:6.3f}s  total={total_bytes}B")


if __name__ == "__main__":
    sys.exit(main())
