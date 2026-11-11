"""Windows implementations of everything the Linux daemon does with arecord / GNOME portal / pw-play / /proc.
ctypes + sounddevice + winsound + psutil only. Imported by dictate.py on win32 and nowhere else (pure helpers such as
parse_hotkey and the struct layouts are importable anywhere, so they can be unit-tested on Linux)."""
import ctypes, os, sys, threading, time
from ctypes import wintypes
from pathlib import Path

SR = 16000
ULONG_PTR = ctypes.c_size_t


# ----------------------------------------------------------------------------- SendInput typing
# fixed-width types: identical to the Win32 WORD/DWORD/LONG on Windows, and the layout can be checked on any OS
WORD, DWORD, LONG = ctypes.c_uint16, ctypes.c_uint32, ctypes.c_int32


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD), ("time", DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUT_U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]   # mi makes the union the right size


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUT_U)]


INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 1, 0x0002, 0x0004
VK_RETURN, VK_TAB = 0x0D, 0x09


def utf16_units(text):
    """UTF-16 code units of text (astral characters become surrogate pairs, which SendInput expects)."""
    b = text.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def _key_events(text):
    """(vk, unit, is_unicode) per key press, in order. CRLF/LF -> Enter, TAB -> Tab."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in text:
        if ch == "\n":
            out.append((VK_RETURN, 0, False))
        elif ch == "\t":
            out.append((VK_TAB, 0, False))
        else:
            out += [(0, u, True) for u in utf16_units(ch)]
    return out


def _user32():
    u = ctypes.windll.user32
    u.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    u.SendInput.restype = wintypes.UINT
    return u


class Typer:
    """Same interface the daemon uses for the Linux PortalTyper: start/restart/type, ready, error, proc."""
    def __init__(self, delay_ms=6):
        self.delay = delay_ms / 1000
        self.proc, self.error = None, None
        self.ready = threading.Event(); self.ready.set()

    def start(self):
        pass

    def restart(self, why="retry"):
        pass

    def type(self, text):
        u = _user32()
        for vk, unit, uni in _key_events(text):
            flags = KEYEVENTF_UNICODE if uni else 0
            down, up = INPUT(type=INPUT_KEYBOARD), INPUT(type=INPUT_KEYBOARD)
            down.ki = KEYBDINPUT(vk, unit, flags, 0, 0)
            up.ki = KEYBDINPUT(vk, unit, flags | KEYEVENTF_KEYUP, 0, 0)
            arr = (INPUT * 2)(down, up)
            if u.SendInput(2, arr, ctypes.sizeof(INPUT)) != 2:
                raise RuntimeError(f"SendInput failed (error {ctypes.get_last_error() or ctypes.GetLastError()}); "
                                   "the focused window may be running as administrator")
            if self.delay:
                time.sleep(self.delay)


# ----------------------------------------------------------------------------- clipboard / focus
def clipboard_set(text):
    k, u = ctypes.windll.kernel32, ctypes.windll.user32
    k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = (wintypes.UINT, ctypes.c_size_t), wintypes.HGLOBAL
    k.GlobalLock.argtypes, k.GlobalLock.restype = (wintypes.HGLOBAL,), ctypes.c_void_p
    k.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    u.SetClipboardData.argtypes, u.SetClipboardData.restype = (wintypes.UINT, wintypes.HANDLE), wintypes.HANDLE
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):                                   # the clipboard may be briefly held by another app
        if u.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(data))              # GMEM_MOVEABLE
        p = k.GlobalLock(h); ctypes.memmove(p, data, len(data)); k.GlobalUnlock(h)
        u.SetClipboardData(13, h)                         # CF_UNICODETEXT (the clipboard now owns h)
        return "clipboard"
    finally:
        u.CloseClipboard()


def focus_app(timeout=2.0):
    """Executable name (no .exe) of the foreground window's process. '' if unknown."""
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        k.OpenProcess.restype = wintypes.HANDLE
        hwnd = u.GetForegroundWindow()
        pid = wintypes.DWORD(0); u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520); n = wintypes.DWORD(520)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return Path(buf.value).stem.replace(" ", "_")
        finally:
            k.CloseHandle(h)
    except Exception:
        pass
    return ""


# ----------------------------------------------------------------------------- global hotkey
MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008, "super": 0x0008}
NAMED = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
         "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
         "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pause": 0x13, "capslock": 0x14,
         "scrolllock": 0x91, "printscreen": 0x2C}


def parse_hotkey(spec):
    """'ctrl+alt+space' -> (modifier_flags, virtual_key). Raises ValueError with a helpful message."""
    mods, key = 0, None
    for part in [p.strip().lower() for p in spec.replace("<", "").replace(">", "+").split("+") if p.strip()]:
        if part in MODS:
            mods |= MODS[part]
        elif key is not None:
            raise ValueError(f"hotkey {spec!r}: more than one non-modifier key")
        elif part in NAMED:
            key = NAMED[part]
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"hotkey {spec!r}: unknown key {part!r}")
    if key is None:
        raise ValueError(f"hotkey {spec!r}: no key given (example: ctrl+alt+space)")
    return mods, key


def start_hotkey(spec, callback, log=lambda *a, **k: None):
    """Register a global hotkey; callback runs on a fresh thread. Returns the listener thread."""
    mods, vk = parse_hotkey(spec)

    def loop():
        u = ctypes.windll.user32
        if not u.RegisterHotKey(None, 1, mods | 0x4000, vk):        # MOD_NOREPEAT
            err = ctypes.GetLastError()
            msg = f"could not register hotkey {spec} (error {err}); it is probably used by another program"
            log("hotkey_fail", error=msg); sys.stderr.write(msg + "\n"); return
        log("hotkey_ok", hotkey=spec)
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:                                 # WM_HOTKEY
                threading.Thread(target=callback, daemon=True).start()
    t = threading.Thread(target=loop, daemon=True, name="hotkey"); t.start()
    return t


# ----------------------------------------------------------------------------- microphone
class Recorder:
    """16 kHz mono int16 capture into `chunks` (a list of bytes). Same terminate()/wait() shape as the arecord Popen."""
    def __init__(self, chunks, device=None):
        import sounddevice as sd
        self.chunks = chunks
        self.stream = sd.RawInputStream(samplerate=SR, channels=1, dtype="int16", blocksize=1600, device=device,
                                        callback=lambda indata, frames, t, status: chunks.append(bytes(indata)))
        self.stream.start()

    def terminate(self):
        try:
            self.stream.stop(); self.stream.close()
        except Exception:
            pass

    def wait(self):
        pass


def mic_devices():
    import sounddevice as sd
    return [d["name"] for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]


# ----------------------------------------------------------------------------- feedback / stats
def notify(cfg, msg, urgency="low"):
    pass                                                              # no toast yet: sounds + dashboard cover it


_BEEPS = {"start": [(880, 90)], "done": [(1175, 70), (1568, 70)], "error": [(300, 250)]}


def cue(cfg, name):
    if not cfg.get("sounds", True):
        return
    def play():
        try:
            import winsound
            for f, ms in _BEEPS[name]:
                winsound.Beep(f, ms)
        except Exception:
            pass
    threading.Thread(target=play, daemon=True).start()


def idle_cpus(sample_s=0.25):
    try:
        import psutil
        return round((100 - psutil.cpu_percent(interval=sample_s)) / 100 * (os.cpu_count() or 1), 1)
    except Exception:
        return float(os.cpu_count() or 1)


def proc_mem(pid):
    try:
        import psutil
        m = psutil.Process(pid).memory_full_info()
        return {"rss_mb": round(m.rss / 1048576), "pss_mb": round(m.uss / 1048576), "uss_mb": round(m.uss / 1048576)}
    except Exception:
        return None


def add_cuda_dll_dirs():
    """pip's nvidia-* wheels keep cuBLAS/cuDNN DLLs under site-packages/nvidia/*/bin; make them loadable."""
    for base in {Path(p) / "nvidia" for p in sys.path if p}:
        if base.is_dir():
            for d in base.glob("*/bin"):
                try:
                    os.add_dll_directory(str(d))
                except OSError:
                    pass
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
