#!/usr/bin/env python3
"""
Doorbell ring-source probe (2026-06-21).

STATUS: the VideoTalk attaches below are currently REJECTED by the VTH
(-267976701 / -267976703) — the same authorization wall that blocked local
unlock + local camera at the project's start. eventManager.attach works; the
VideoTalk* services do NOT for our user-level DHIP session.

This is the POST-FRIDA tool: once a Frida/emulator session reveals how DMSS
authenticates to the VTH locally (it gets privileged access we're refused — see
memory intercom-doorbell-missed-call-notification + dahua-relay-capture-constraints),
apply that auth here and these attaches may succeed, giving a press-ONSET ring
event (VideoTalk2Cloud.onRing / VideoTalkPhone.attachCall) instead of
eventManager's IgnoreInvite which only fires at call-END. May also need the
factory.instance -> object_id -> attach two-step (see DahuaConsole instance_create).

Attaches to the VTH's call/talk DHIP services at once (not just eventManager)
and logs every notification with a UTC timestamp, so a single door press shows
WHICH service fires AT PRESS-ONSET vs eventManager's IgnoreInvite at call-END.

The VTH exposes these (found via DahuaConsole --dump service) that our normal
subscribe_events never attaches to:
  VideoTalk2Cloud.onRing / .attach
  VideoTalkPhone.attachCall / .attachCallState
  VideoTalkPeer.attachState
We try each (some may reject the attach — that's fine, we log it and continue),
plus eventManager.attach for the baseline IgnoreInvite.

Run:  python3 ring_probe.py        (reads VTH creds from the container env)
Then press the doorbell. Ctrl-C to stop.
"""
import os, sys, time, datetime, socket

# Reuse the proven DHIP primitives from dahua_client (login/send/recv).
sys.path.insert(0, os.path.dirname(__file__))
from dahua_client import _dhip_login, _dhip_send, _dhip_recv  # noqa: E402

VTH_HOST = os.environ.get("DAHUA_VTH_HOST", "192.168.40.55")
VTH_PORT = int(os.environ.get("DAHUA_VTH_PORT", "5000"))
VTH_USER = os.environ.get("DAHUA_VTH_USERNAME", "user")
VTH_PASS = os.environ.get("DAHUA_VTH_PASSWORD", "")

# (method, params) for each attach we want to try.
ATTACHES = [
    ("eventManager.attach",     {"codes": ["All"]}),
    ("VideoTalk2Cloud.attach",  {}),
    ("VideoTalkPhone.attachCall",      {}),
    ("VideoTalkPhone.attachCallState", {}),
    ("VideoTalkPeer.attachState",      {}),
    ("RecallInfo.attach",       {}),
]


def ts() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S.%f")[:-3]


def main() -> None:
    if not VTH_PASS:
        print("DAHUA_VTH_PASSWORD not set in env", file=sys.stderr)
        sys.exit(1)

    print(f"[{ts()}] connecting {VTH_HOST}:{VTH_PORT} as {VTH_USER}")
    sock = socket.create_connection((VTH_HOST, VTH_PORT), timeout=15)
    sock.settimeout(25)
    session_id = _dhip_login(sock, VTH_USER, VTH_PASS)
    print(f"[{ts()}] logged in (session={session_id})")

    mid = 10
    for method, params in ATTACHES:
        _dhip_send(sock, session_id, mid, {
            "method": method, "params": params,
            "session": session_id, "id": mid,
        })
        resp = _dhip_recv(sock)
        ok = bool(resp and resp.get("result"))
        print(f"[{ts()}] attach {method:32s} -> {'OK' if ok else 'REJECTED ' + str(resp)}")
        mid += 1

    print(f"[{ts()}] === ATTACHED. PRESS THE DOORBELL NOW. Ctrl-C to stop. ===")

    last_ka = time.time()
    while True:
        now = time.time()
        if now - last_ka > 15:
            _dhip_send(sock, session_id, mid, {
                "method": "global.keepAlive",
                "params": {"timeout": 20, "active": True},
                "session": session_id, "id": mid,
            })
            mid += 1
            last_ka = now
        try:
            msg = _dhip_recv(sock)
        except socket.timeout:
            continue
        if not msg:
            print(f"[{ts()}] connection closed by VTH")
            break
        method = msg.get("method", "")
        if method in ("client.notifyEventStream", "") and not msg.get("result"):
            pass
        # Log any push/notify the VTH sends (the whole point).
        if method.startswith("client.") or "params" in msg:
            params = msg.get("params", {})
            # eventManager events come as eventList; call services push their own shapes.
            evlist = params.get("eventList") if isinstance(params, dict) else None
            if evlist:
                for ev in evlist:
                    print(f"[{ts()}] EVENT {method}  Code={ev.get('Code')} "
                          f"Action={ev.get('Action')} CallID={ev.get('Data', {}).get('CallID', '')}")
            else:
                print(f"[{ts()}] NOTIFY {method}  params={params}")


if __name__ == "__main__":
    main()
