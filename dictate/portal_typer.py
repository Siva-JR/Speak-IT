#!/usr/bin/python3
"""Types text into the focused window on GNOME/Wayland through the xdg RemoteDesktop portal.
Runs under the SYSTEM python (needs PyGObject). Protocol, one JSON object per stdin line:
    {"text": "hello"}  -> types it, replies "DONE <n>"
First line printed: "READY" or "ERROR <msg>". A one-time permission dialog appears on first run;
the restore token is saved so later runs are silent."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS, PATH, RD = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.RemoteDesktop"
TOKEN_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dictate" / "portal_token"
conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
_n = 0


def request(method, build, timeout_s=180):
    """Call a portal method that answers through a Request::Response signal; return the results dict."""
    global _n
    _n += 1
    tok = f"dict{os.getpid()}x{_n}"
    sender = conn.get_unique_name()[1:].replace(".", "_")
    path = f"/org/freedesktop/portal/desktop/request/{sender}/{tok}"
    loop, box = GLib.MainLoop(), {}

    def on_resp(_c, _s, _p, _i, _m, params):
        box["code"], box["results"] = params.unpack()
        loop.quit()
    sub = conn.signal_subscribe(BUS, "org.freedesktop.portal.Request", "Response", path, None,
                                Gio.DBusSignalFlags.NONE, on_resp)
    conn.call_sync(BUS, PATH, RD, method, build(tok), None, Gio.DBusCallFlags.NONE, -1, None)
    GLib.timeout_add_seconds(timeout_s, loop.quit)
    loop.run()
    conn.signal_unsubscribe(sub)
    if box.get("code") != 0:
        raise RuntimeError(f"{method} failed/cancelled (code {box.get('code')})")
    return box["results"]


def setup():
    V = GLib.Variant
    res = request("CreateSession", lambda t: V("(a{sv})", ({"handle_token": V("s", t),
                                                           "session_handle_token": V("s", f"dictate{os.getpid()}")},)))
    session = res["session_handle"]
    opts = lambda t: {"handle_token": V("s", t), "types": V("u", 1), "persist_mode": V("u", 2)}
    restore = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""

    def sel(t):
        o = opts(t)
        if restore:
            o["restore_token"] = V("s", restore)
        return V("(oa{sv})", (session, o))
    request("SelectDevices", sel)
    res = request("Start", lambda t: V("(osa{sv})", (session, "", {"handle_token": V("s", t)})))
    if res.get("restore_token"):
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(res["restore_token"])
        TOKEN_FILE.chmod(0o600)
    return session


def key(session, keysym, down):
    conn.call_sync(BUS, PATH, RD, "NotifyKeyboardKeysym",
                   GLib.Variant("(oa{sv}iu)", (session, {}, keysym, 1 if down else 0)),
                   None, Gio.DBusCallFlags.NONE, -1, None)


DELAY = float(os.environ.get("DICTATE_TYPE_DELAY_MS", "6")) / 1000


def type_text(session, text):
    n = 0
    for ch in text:
        if ch in "\r\n":
            ks = 0x20                         # newlines become spaces: never auto-press Enter
        elif ch == "\t":
            ks = 0x20
        else:
            ks = ord(ch) if ord(ch) < 0x100 else 0x01000000 | ord(ch)
        key(session, ks, True)
        key(session, ks, False)
        n += 1
        time.sleep(DELAY)                 # pace the keys: Ink/readline TUIs mis-handle instant bursts
    return n


def main():
    try:
        session = setup()
    except Exception as e:
        print(f"ERROR {e}", flush=True)
        return 1
    print("READY", flush=True)
    for line in sys.stdin:
        try:
            msg = json.loads(line)
            print(f"DONE {type_text(session, msg['text'])}", flush=True)
        except Exception as e:
            print(f"ERROR {e}", flush=True)
            if any(k in str(e).lower() for k in ("closed", "does not exist", "no such")):
                return 1                      # session is dead (e.g. screen was locked): let the daemon restart us
    return 0


if __name__ == "__main__":
    sys.exit(main())
