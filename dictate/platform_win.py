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
VK_RETURN, VK_TAB, VK_BACK = 0x0D, 0x09, 0x08
SPECIAL_KEYS = {"enter": VK_RETURN, "backspace": VK_BACK}


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

    def type(self, text, delay_ms=None):
        self._send(_key_events(text), self.delay if delay_ms is None else delay_ms / 1000)

    def key(self, name, n=1):
        """Press 'enter' or 'backspace' n times."""
        self._send([(SPECIAL_KEYS[name], 0, False)] * max(0, int(n)), max(self.delay, 0.004))

    def _send(self, events, delay):
        u = _user32()
        for vk, unit, uni in events:
            flags = KEYEVENTF_UNICODE if uni else 0
            down, up = INPUT(type=INPUT_KEYBOARD), INPUT(type=INPUT_KEYBOARD)
            down.ki = KEYBDINPUT(vk, unit, flags, 0, 0)
            up.ki = KEYBDINPUT(vk, unit, flags | KEYEVENTF_KEYUP, 0, 0)
            arr = (INPUT * 2)(down, up)
            if u.SendInput(2, arr, ctypes.sizeof(INPUT)) != 2:
                raise RuntimeError(f"SendInput failed (error {ctypes.get_last_error() or ctypes.GetLastError()}); "
                                   "the focused window may be running as administrator")
            if delay:
                time.sleep(delay)


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


class Debounce:
    """True at most once per `window` seconds (swallows key bounce / auto-repeat that would toggle twice)."""
    def __init__(self, window=0.35):
        self.window, self.last = window, 0.0

    def __call__(self, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last < self.window:
            return False
        self.last = now
        return True


def start_hotkeys(binds, log=lambda *a, **k: None):
    """Register global hotkeys [(spec, callback), ...]; each callback runs on a fresh thread. Specs are checked
    first (ValueError names the bad one). A hotkey another program already owns is logged and skipped."""
    parsed = [(spec, *parse_hotkey(spec), cb) for spec, cb in binds if spec]

    def loop():
        u = ctypes.windll.user32
        handlers = {}
        for i, (spec, mods, vk, cb) in enumerate(parsed, start=1):
            if not u.RegisterHotKey(None, i, mods | 0x4000, vk):            # MOD_NOREPEAT
                err = ctypes.GetLastError()
                msg = f"could not register hotkey {spec} (error {err}); it is probably used by another program"
                log("hotkey_fail", error=msg); sys.stderr.write(msg + "\n"); continue
            handlers[i] = (cb, Debounce())
            log("hotkey_ok", hotkey=spec)
        if not handlers:
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam in handlers:            # WM_HOTKEY
                cb, ok_to_fire = handlers[msg.wParam]
                if ok_to_fire():
                    threading.Thread(target=cb, daemon=True).start()
    t = threading.Thread(target=loop, daemon=True, name="hotkey"); t.start()
    return t


def start_hotkey(spec, callback, log=lambda *a, **k: None):
    return start_hotkeys([(spec, callback)], log)


# ----------------------------------------------------------------------------- on-screen indicator
INDICATOR_SIZE = 14
_COLORS = {"rec": 0x002828E0, "busy": 0x0000A0F0, "error": 0x002828E0}   # COLORREF is 0x00BBGGRR: red, amber
_KEY = 0x00FF00FF                                                         # magenta = transparent


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HANDLE), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class Indicator:
    """A small red dot at the top centre of the screen while recording (amber while transcribing), hidden when idle:
    the Windows counterpart of the Linux top-bar icon. Topmost, click-through, never takes focus. (A notification-area
    icon would land in the hidden overflow on Windows 11, so it would not be seen.) Same set(state) as the Linux Tray."""
    def __init__(self, enabled=True):
        self.proc, self.hwnd, self.state = None, None, "idle"
        if enabled:
            ready = threading.Event()
            threading.Thread(target=self._run, args=(ready,), daemon=True, name="indicator").start()
            ready.wait(3)

    def set(self, state):
        self.state = state
        if self.hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self.hwnd, 0x8000, 0, 0)      # WM_APP: show/hide/repaint
            except Exception:
                pass

    def _run(self, ready):
        try:
            self._loop(ready)
        except Exception as e:
            sys.stderr.write(f"indicator disabled: {e}\n"); ready.set()

    def _loop(self, ready):
        u, g, k = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.DefWindowProcW.restype = LRESULT
        u.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                      wintypes.HINSTANCE, wintypes.LPVOID)
        u.CreateWindowExW.restype = wintypes.HWND
        u.BeginPaint.restype = wintypes.HDC
        u.BeginPaint.argtypes = (wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
        u.EndPaint.argtypes = (wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
        u.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
        u.SetLayeredWindowAttributes.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_byte, wintypes.DWORD)
        u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.InvalidateRect.argtypes = (wintypes.HWND, ctypes.c_void_p, wintypes.BOOL)
        u.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        g.CreateSolidBrush.argtypes, g.CreateSolidBrush.restype = (wintypes.DWORD,), wintypes.HBRUSH
        g.SelectObject.argtypes, g.SelectObject.restype = (wintypes.HDC, wintypes.HGDIOBJ), wintypes.HGDIOBJ
        g.GetStockObject.restype = wintypes.HGDIOBJ
        g.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        g.Ellipse.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
        k.GetModuleHandleW.restype = wintypes.HMODULE
        S = INDICATOR_SIZE

        def wndproc(hwnd, msg, wp, lp):
            try:
                if msg == 0x8000:                                            # WM_APP from set()
                    if self.state == "idle":
                        u.ShowWindow(hwnd, 0)                                # SW_HIDE
                    else:
                        u.ShowWindow(hwnd, 4)                                # SW_SHOWNOACTIVATE
                        u.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # stay on top
                        u.InvalidateRect(hwnd, None, True)
                    return 0
                if msg == 0x000F:                                            # WM_PAINT
                    ps = PAINTSTRUCT()
                    hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
                    brush = g.CreateSolidBrush(_COLORS.get(self.state, _COLORS["rec"]))
                    old_b = g.SelectObject(hdc, brush)
                    old_p = g.SelectObject(hdc, g.GetStockObject(8))         # NULL_PEN: no outline
                    g.Ellipse(hdc, 0, 0, S, S)
                    g.SelectObject(hdc, old_p); g.SelectObject(hdc, old_b); g.DeleteObject(brush)
                    u.EndPaint(hwnd, ctypes.byref(ps))
                    return 0
            except Exception:
                pass
            return u.DefWindowProcW(hwnd, msg, wp, lp)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

        self._wndproc = WNDPROC(wndproc)                                     # keep a reference: ctypes must not free it
        hinst = k.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc, hInstance=hinst,
                         hbrBackground=g.CreateSolidBrush(_KEY), lpszClassName="DictateIndicator")
        u.RegisterClassExW(ctypes.byref(wc))
        x = (u.GetSystemMetrics(0) - S) // 2                                 # top centre of the primary screen
        ex = 0x00000008 | 0x00000080 | 0x00080000 | 0x00000020 | 0x08000000  # TOPMOST TOOLWINDOW LAYERED TRANSPARENT NOACTIVATE
        hwnd = u.CreateWindowExW(ex, "DictateIndicator", "Dictate", 0x80000000, x, 6, S, S, None, None, hinst, None)
        if not hwnd:
            raise OSError(f"CreateWindowEx failed ({ctypes.GetLastError()})")
        u.SetLayeredWindowAttributes(hwnd, _KEY, 0, 0x1)                    # LWA_COLORKEY: magenta is see-through
        self.hwnd = hwnd
        ready.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg)); u.DispatchMessageW(ctypes.byref(msg))


# ----------------------------------------------------------------------------- microphone
class Resampler:
    """Streaming mono resampler (box low-pass when downsampling + linear interpolation), enough for speech.
    Keeps phase and a short tail between blocks so block boundaries do not click."""
    def __init__(self, src, dst):
        import numpy as np
        self.np, self.step, self.pos, self.tail = np, src / dst, 0.0, np.zeros(0, dtype=np.float32)
        self.k = max(1, int(round(self.step))) if self.step >= 1.5 else 1

    def process(self, x):
        np = self.np
        buf = np.concatenate([self.tail, x.astype(np.float32, copy=False)])
        if len(buf) < self.k + 1:
            self.tail = buf; return np.zeros(0, dtype=np.float32)
        sm = np.convolve(buf, np.ones(self.k, dtype=np.float32) / self.k, mode="same") if self.k > 1 else buf
        idx = np.arange(self.pos, len(buf) - 1, self.step)
        out = np.interp(idx, np.arange(len(buf)), sm).astype(np.float32)
        nxt = (idx[-1] + self.step) if len(idx) else self.pos
        keep = max(0, int(nxt) - self.k)
        self.tail, self.pos = buf[keep:], nxt - keep
        return out


def candidate_inputs(sd, spec=None):
    """Input device indices to try, best first. spec: None (auto), an index, or a name substring.
    Auto order: the system default (if set), then each host API's own default input (default host API first).
    Other inputs (e.g. 'Stereo Mix', which records the speakers) are NOT tried automatically: recording from the wrong
    source is worse than failing. Choose one explicitly with mic_device. No device names are assumed."""
    devs = sd.query_devices()
    inputs = [i for i, d in enumerate(devs) if d.get("max_input_channels", 0) > 0]
    if not inputs:
        raise RuntimeError("no microphone found (Windows: Settings > Privacy & security > Microphone: allow desktop apps)")
    if spec not in (None, ""):
        if str(spec).lstrip("-").isdigit():
            return [int(spec)]
        hits = [i for i in inputs if str(spec).lower() in devs[i]["name"].lower()]
        if not hits:
            raise RuntimeError(f"mic_device {spec!r} matches no input; available: {[devs[i]['name'] for i in inputs]}")
        return hits
    order = []
    def add(i):
        if isinstance(i, int) and i in inputs and i not in order:
            order.append(i)
    try:
        add(sd.default.device[0])
    except Exception:
        pass
    try:
        apis = sd.query_hostapis()
        first = sd.default.hostapi if isinstance(getattr(sd.default, "hostapi", None), int) else 0
        for a in [first] + [k for k in range(len(apis)) if k != first]:
            add(apis[a].get("default_input_device"))
    except Exception:
        pass
    return order or inputs          # no default info at all: only then consider every input


class Tap:
    """What the microphone callback actually did: blocks received, bytes kept, PortAudio status flags, last error."""
    def __init__(self):
        self.blocks, self.bytes, self.status, self.error = 0, 0, set(), None

    def summary(self):
        d = {"mic_blocks": self.blocks, "mic_bytes": self.bytes}
        if self.status:
            d["mic_status"] = ",".join(sorted(self.status))[:80]
        if self.error:
            d["mic_error"] = self.error[:120]
        return d


def open_input(sd, chunks, spec=None, tap=None):
    """Open the first input that works, in whatever format it supports, appending 16 kHz mono int16 bytes to chunks.
    Returns (running stream, device name, description). Raises one error listing every failure."""
    import numpy as np
    tap = tap or Tap()
    failures, devs = [], sd.query_devices()
    for idx in candidate_inputs(sd, spec):
        d = devs[idx]
        native_sr = int(d.get("default_samplerate") or SR)
        formats = [(SR, 1), (native_sr, 1)] + ([(native_sr, int(d["max_input_channels"]))] if d["max_input_channels"] > 1 else [])
        for sr, ch in dict.fromkeys(formats):
            try:
                rs = Resampler(sr, SR) if sr != SR else None
                def inner(indata, ch=ch, rs=rs):
                    if rs is None and ch == 1:
                        b = bytes(indata)
                    else:
                        x = np.frombuffer(indata, dtype=np.int16).astype(np.float32)
                        if ch > 1:
                            x = x.reshape(-1, ch).mean(axis=1)
                        if rs is not None:
                            x = rs.process(x)
                        b = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
                    chunks.append(b)
                    return len(b)
                def cb(indata, frames, t, status, inner=inner):
                    tap.blocks += 1
                    if status:
                        tap.status.add(str(status))
                    try:
                        tap.bytes += inner(indata)
                    except Exception as e:           # never raise inside the audio thread (it would abort the stream)
                        tap.error = f"{type(e).__name__}: {e}"
                st = sd.RawInputStream(samplerate=sr, channels=ch, dtype="int16", blocksize=int(sr / 10), device=idx, callback=cb)
                st.start()
                return st, d["name"], f"{sr} Hz x{ch}" + (" (converted to 16 kHz mono)" if (sr, ch) != (SR, 1) else "")
            except Exception as e:
                failures.append(f"{d['name']} @ {sr} Hz x{ch}: {str(e)[:90]}")
    raise RuntimeError("could not open any microphone: " + " | ".join(failures[:6]))


class Recorder:
    """Microphone capture into `chunks`. Same terminate()/wait() shape as the arecord Popen on Linux."""
    def __init__(self, chunks, device=None):
        import sounddevice as sd
        self.chunks, self.tap = chunks, Tap()
        self.stream, self.name, self.info = open_input(sd, chunks, device, self.tap)

    def terminate(self):
        try:
            self.stream.stop(); self.stream.close()
        except Exception:
            pass

    def wait(self):
        pass

    def stats(self):
        return self.tap.summary()


def selftest_mic(device=None, secs=1.0):
    """Open the microphone, record `secs`, and report whether audio really arrived. Returns (level, message) with
    level 'ok' | 'warn' | 'fail'. Raises if no microphone can be opened."""
    chunks = []
    r = Recorder(chunks, device)
    time.sleep(secs); r.terminate()
    s = r.stats(); head = f"{r.name} ({r.info})"
    if not chunks:
        return "fail", f"{head}: opened but delivered NO audio ({s}). Check Settings > Privacy & security > Microphone"
    import numpy as np
    peak = int(np.abs(np.frombuffer(b"".join(chunks), dtype=np.int16)).max()) if chunks else 0
    if s.get("mic_error"):
        return "fail", f"{head}: callback error {s['mic_error']}"
    if peak < 30:
        return "warn", f"{head}: audio arrives but it is silent (peak {peak}); is the mic muted or the wrong one?"
    return "ok", f"{head}: {s['mic_blocks']} blocks, peak {peak}"


def mic_devices():
    import sounddevice as sd
    return [d["name"] for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]


# ----------------------------------------------------------------------------- feedback / stats
def notify(cfg, msg, urgency="low"):
    pass                                                              # no toast yet: sounds + dashboard cover it


_BEEPS = {"start": [(880, 90)], "done": [(1175, 70), (1568, 70)], "error": [(300, 250)],
          "cancel": [(660, 60), (440, 90)]}


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
