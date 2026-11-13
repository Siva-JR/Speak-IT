"""Which keys are physically held right now. Used for hold-to-talk (stop when the hotkey is released) and to type only
after the hotkey's Ctrl/Alt are up (typing while Ctrl is held turns letters into shortcuts).

  Linux   : EVIOCGKEY on the keyboards in /dev/input. Needs read access (the `input` group). It asks the kernel for the
            CURRENT key state only; it never reads the stream of key presses, so nothing you type is seen or stored.
            The devices are opened when a dictation starts and closed when it is done.
  Windows : GetAsyncKeyState.
Without access, `available` is False and the daemon falls back to toggle mode and a fixed settle delay.
Standard library only."""
import glob, os, re, subprocess, sys, time

IS_WIN = sys.platform == "win32"

# ----------------------------------------------------------------------------- Linux evdev key codes
_ROWS = {"qwertyuiop": 16, "asdfghjkl": 30, "zxcvbnm": 44}
EV_CODES = {c: base + i for row, base in _ROWS.items() for i, c in enumerate(row)}
EV_CODES.update({str(d): 2 + (d - 1) % 10 for d in range(10)})            # 1..9 -> 2..10, 0 -> 11
EV_CODES.update({f"f{i}": 58 + i for i in range(1, 11)})                  # F1..F10 = 59..68
EV_CODES.update({"f11": 87, "f12": 88, "space": 57, "return": 28, "enter": 28, "tab": 15, "escape": 1, "esc": 1,
                 "backspace": 14, "insert": 110, "delete": 111, "home": 102, "end": 107, "page_up": 104,
                 "pageup": 104, "page_down": 109, "pagedown": 109, "up": 103, "left": 105, "right": 106,
                 "down": 108, "pause": 119, "grave": 41, "minus": 12, "equal": 13, "semicolon": 39,
                 "apostrophe": 40, "comma": 51, "period": 52, "slash": 53, "backslash": 43,
                 "bracketleft": 26, "bracketright": 27})
EV_MODS = [29, 97, 56, 100, 42, 54, 125, 126]       # left/right Ctrl, Alt, Shift, Super
_KEYBUF = 96                                        # KEY_MAX (0x2ff) / 8 + 1


def _ioc_read(nr, size):
    return (2 << 30) | (size << 16) | (ord("E") << 8) | nr


EVIOCGKEY = _ioc_read(0x18, _KEYBUF)
EVIOCGBIT_KEY = _ioc_read(0x20 + 1, _KEYBUF)        # EV_KEY capabilities


def _bit(buf, code):
    return (buf[code // 8] >> (code % 8)) & 1


def parse_gnome_binding(spec):
    """'<Control><Alt>space' -> 'space' (the non-modifier key, lower case), or None."""
    key = re.sub(r"<[^>]+>", "", spec or "").strip().lower()
    return key or None


def gnome_binding(name="dictate"):
    """The key bound to the Dictate GNOME shortcut, as '<Control><Alt>space', or ''."""
    path = f"/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/{name}/"
    try:
        r = subprocess.run(["gsettings", "get", f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{path}",
                            "binding"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip().strip("'\"") if r.returncode == 0 else ""
    except Exception:
        return ""


class _LinuxKeys:
    def __init__(self, main_code):
        self.fds = []
        for p in sorted(glob.glob("/dev/input/event*")):
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                import fcntl
                caps = bytearray(_KEYBUF)
                fcntl.ioctl(fd, EVIOCGBIT_KEY, caps)
                if _bit(caps, main_code) and (_bit(caps, 29) or _bit(caps, 56)):   # a keyboard with this key
                    self.fds.append(fd); continue
            except OSError:
                pass
            os.close(fd)

    def held(self, codes):
        import fcntl
        for fd in self.fds:
            buf = bytearray(_KEYBUF)
            try:
                fcntl.ioctl(fd, EVIOCGKEY, buf)
            except OSError:
                continue
            if any(_bit(buf, c) for c in codes):
                return True
        return False

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []


class _WinKeys:
    def __init__(self):
        import ctypes
        self.u = ctypes.windll.user32
        self.fds = [True]

    def held(self, codes):
        return any(self.u.GetAsyncKeyState(c) & 0x8000 for c in codes)

    def close(self):
        pass


WIN_MODS = [0x10, 0x11, 0x12, 0x5B, 0x5C]           # Shift, Ctrl, Alt, left/right Win


class KeyWatch:
    """main_held(): is the hotkey's main key down?  mods_held(): is any Ctrl/Alt/Shift/Super down?
    Both return None when key state cannot be read (see `available`)."""
    def __init__(self, main_key=None):
        self.backend, self.main = None, []
        try:
            if IS_WIN:
                import platform_win
                self.backend = _WinKeys()
                self.main = [platform_win.parse_hotkey(main_key)[1]] if main_key else []
                self.mods = WIN_MODS
            else:
                code = EV_CODES.get((main_key or "space").lower(), 57)
                self.backend, self.main, self.mods = _LinuxKeys(code), [code], EV_MODS
        except Exception:
            self.backend = None

    @property
    def available(self):
        return bool(self.backend and self.backend.fds)

    def main_held(self):
        return self.backend.held(self.main) if self.available and self.main else None

    def mods_held(self):
        return self.backend.held(self.mods) if self.available else None

    def wait_released(self, timeout=1.5, include_main=True):
        """Block until no modifier (and not the main key) is held. Returns seconds waited, or None if unknown."""
        if not self.available:
            return None
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if not self.mods_held() and not (include_main and self.main_held()):
                break
            time.sleep(0.01)
        return round(time.monotonic() - t0, 3)

    def close(self):
        if self.backend:
            self.backend.close()


def access_hint():
    """Why key state is unavailable on Linux, and how to fix it ('' if it is available or not Linux)."""
    if IS_WIN:
        return ""
    w = KeyWatch("space")
    ok = w.available; w.close()
    if ok:
        return ""
    return ("hold-to-talk needs read access to /dev/input: sudo usermod -aG input $USER, then log out and in "
            "(without it the hotkey toggles, and typing waits a fixed type_settle_ms)")
