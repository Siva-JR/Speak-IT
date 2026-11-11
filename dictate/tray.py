#!/usr/bin/python3
"""Top-bar status icon (StatusNotifierItem, shown by GNOME's built-in appindicator extension).
Reads state words on stdin: idle | rec | busy | error. Idle = hidden, so it only shows when relevant."""
import sys
import threading

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
<property name="IconName" type="s" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/><property name="WindowId" type="i" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewIcon"/><signal name="NewStatus"><arg type="s"/></signal><signal name="NewTitle"/>
</interface></node>"""

STATES = {  # state -> (status, icon, title)
    "idle": ("Passive", "audio-input-microphone-symbolic", "Dictate: idle"),
    "rec": ("Active", "media-record", "Dictate: LISTENING (press the hotkey to finish)"),
    "busy": ("Active", "view-refresh", "Dictate: transcribing"),
    "error": ("Active", "dialog-warning", "Dictate: error"),
}
cur = "idle"
conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
OBJ = "/StatusNotifierItem"


def get_prop(_c, _s, _o, _i, name):
    st = STATES[cur]
    return {"Category": GLib.Variant("s", "ApplicationStatus"), "Id": GLib.Variant("s", "dictate"),
            "Title": GLib.Variant("s", st[2]), "Status": GLib.Variant("s", st[0]),
            "IconName": GLib.Variant("s", st[1]), "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/NO_DMENU"), "WindowId": GLib.Variant("i", 0)}.get(name)


def set_state(s):
    global cur
    if s not in STATES or s == cur:
        return False
    cur = s
    for sig, args in (("NewIcon", None), ("NewTitle", None), ("NewStatus", GLib.Variant("(s)", (STATES[s][0],)))):
        conn.emit_signal(None, OBJ, "org.kde.StatusNotifierItem", sig, args)
    return False


def reader():
    for line in sys.stdin:
        GLib.idle_add(set_state, line.strip())
    GLib.idle_add(lambda: sys.exit(0))


def main():
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    conn.register_object(OBJ, node.interfaces[0], None, get_prop, None)
    name = f"org.kde.StatusNotifierItem-{__import__('os').getpid()}-1"
    Gio.bus_own_name_on_connection(conn, name, Gio.BusNameOwnerFlags.NONE, None, None)
    try:
        conn.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                       "RegisterStatusNotifierItem", GLib.Variant("(s)", (name,)), None,
                       Gio.DBusCallFlags.NONE, 5000, None)
        print("TRAY READY", flush=True)
    except Exception as e:
        print(f"TRAY UNAVAILABLE {e}", flush=True)
    threading.Thread(target=reader, daemon=True).start()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
