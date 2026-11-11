#!/usr/bin/env python3
"""Local push-to-talk dictation daemon: tiny always-on process + a short-lived model worker.

  dictate serve | toggle | start | stop | status | doctor | dashboard | quit
  dictate typetest      type a test phrase after 4 s
  dictate replay FILE   transcribe a 16 kHz mono wav through the full pipeline (no typing)
  dictate memstat       memory of every process in the chain

The daemon imports no numpy and no model code (about 10 MB). The model lives in worker.py, which is started when
you press the hotkey, stays for `worker_linger_s` after the last use, then exits and returns ALL of its memory.
"""
import json, os, shutil, socket, subprocess, sys, threading, time, tomllib, wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import (IS_WIN, RUNTIME, SOCK, CONF_DIR, STATE, START_HINT, ctl_listen, ctl_request, ctl_unpack)
SR = 16000

DEFAULTS = {
    "model": str(HERE.parent / "models" / "faster-whisper-base.en"), "compute_type": "int8", "beam_size": 2,
    "device": "auto",            # "cuda" | "cpu" | "auto" (cuda if a GPU is usable, else cpu). Env: DICTATE_DEVICE
    "gpu_compute_type": "float16",  # compute type used when device is cuda (compute_type applies to cpu)
    "threads": 4, "remove_fillers": True, "auto_gain": True, "chunk_s": 28.0, "min_speech_s": 0.25,
    "halluc_filter": True, "min_window_s": 15.0, "window_step_s": 5.0,
    "worker_linger_s": 30,       # keep the model process this long after the last use, then exit (frees everything)
    "max_record_s": 600, "output": "auto", "trailing_space": True,
    "save_audio": True, "keep_audio": 100, "notify": True, "sounds": True, "tray": True,
    "adaptive_threads": True,     # drop to 1 engine thread when the machine is busy (see Worker.pick_threads)
    "busy_idle_cpus": 4,          # "busy" = fewer than this many idle logical CPUs
    "pin_threads": True,          # one engine thread per PHYSICAL core, sleeping (not spinning) waits: ~40% less CPU
    "type_delay_ms": 6,           # pause between typed characters (terminal UIs mis-handle instant bursts)
    "type_settle_ms": 350,        # wait at least this long after the stop hotkey before typing (modifiers released)
    "hotkey": "ctrl+alt+space",   # Windows only: global hotkey the daemon registers itself (Linux uses the GNOME shortcut)
    "mic_device": None,           # Windows only: sounddevice input device name or index (None = system default)
}
if IS_WIN:
    DEFAULTS["tray"] = False      # no tray icon on Windows yet; beeps signal start/done/error
WORKER_KEYS = ("model", "compute_type", "device", "gpu_compute_type", "beam_size", "threads", "remove_fillers", "auto_gain", "chunk_s",
               "min_speech_s", "halluc_filter", "min_window_s", "window_step_s")


def load_config():
    cfg = dict(DEFAULTS)
    f = CONF_DIR / "config.toml"
    if f.exists():
        cfg.update(tomllib.loads(f.read_text()))
    cfg.update(json.loads(os.environ.get("DICTATE_CFG", "{}")))
    if os.environ.get("DICTATE_DEVICE"):
        cfg["device"] = os.environ["DICTATE_DEVICE"]
    return cfg


def physical_cpus(n):
    """First logical CPU of each physical core, e.g. [0,1,2,3] on a 4-core/8-thread laptop."""
    seen, out = set(), []
    try:
        cpus = sorted(int(p.name[3:]) for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"))
        for c in cpus:
            sib = (Path(f"/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list")).read_text().strip()
            if sib not in seen:
                seen.add(sib); out.append(c)
    except Exception:
        return []
    return out[:n]


# ----------------------------------------------------------------------------- event log
# A diary of what the daemon does (never the dictated words): one line per event, size-capped and rotated.
EVLOG = STATE / "events.log"
_ev_lock = threading.Lock()


def capture_stderr():
    """Tee fd 2 (daemon + worker tracebacks and warnings) into STATE/errors.log so the dashboard can show them."""
    try:
        os.fstat(2)                                         # no valid stderr (pythonw): nothing to tee
        STATE.mkdir(parents=True, exist_ok=True)
        log = STATE / "errors.log"
        if log.exists() and log.stat().st_size > 1_000_000:
            log.replace(STATE / "errors.log.1")
        r, w = os.pipe(); orig = os.dup(2); os.dup2(w, 2); os.close(w)
        def pump():
            with os.fdopen(r, "rb", 0) as src, open(log, "ab", 0) as dst:
                buf = b""
                while chunk := src.read(4096):
                    try: os.write(orig, chunk)
                    except OSError: pass
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        dst.write(time.strftime("%Y-%m-%dT%H:%M:%S ").encode() + line + b"\n")
        threading.Thread(target=pump, daemon=True, name="stderr-tee").start()
    except Exception as e:
        try: sys.stderr.write(f"stderr capture disabled: {e}\n")
        except Exception: pass


def elog(kind, **kw):
    try:
        now = time.time()
        parts = [time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}", kind]
        for k, v in kw.items():
            if v is None:
                continue
            parts.append(f"{k}={json.dumps(v) if isinstance(v, str) and (not v or ' ' in v) else v}")
        with _ev_lock:
            STATE.mkdir(parents=True, exist_ok=True)
            if EVLOG.exists() and EVLOG.stat().st_size > 1_000_000:       # keep events.log, .1, .2
                for i in (2, 1):
                    src = EVLOG.with_name(f"events.log.{i - 1}") if i > 1 else EVLOG
                    if src.exists():
                        src.replace(EVLOG.with_name(f"events.log.{i}"))
            with open(EVLOG, "a") as f:
                f.write(" ".join(parts) + "\n")
    except Exception:
        pass                                                              # logging must never break dictation


def idle_cpus(sample_s=0.25):
    """How many logical CPUs are idle right now (measured over a short sample of /proc/stat)."""
    def snap():
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
        return v[3] + (v[4] if len(v) > 4 else 0), sum(v[:8])
    try:
        i0, t0 = snap(); time.sleep(sample_s); i1, t1 = snap()
        return round((i1 - i0) / max(1, t1 - t0) * (os.cpu_count() or 1), 1)
    except Exception:
        return float(os.cpu_count() or 1)


_FOCUS_SCRIPT = """
import gi
gi.require_version('Atspi','2.0')
from gi.repository import Atspi
d=Atspi.get_desktop(0); best=''
for i in range(d.get_child_count()):
    a=d.get_child_at_index(i)
    for j in range(a.get_child_count()):
        try:
            w=a.get_child_at_index(j)
            if w.get_state_set().contains(Atspi.StateType.ACTIVE): best=(a.get_name() or '?').replace(' ','_')
        except Exception: pass
print(best)
"""


def focus_app(timeout=2.0):
    """Name of the application that has focus (app name only, no window titles). '' if unknown."""
    try:
        r = subprocess.run(["/usr/bin/python3", "-c", _FOCUS_SCRIPT], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def proc_mem(pid):
    """RSS / PSS / USS (MB) of a process from /proc, no psutil needed."""
    try:
        d = {}
        for line in open(f"/proc/{pid}/smaps_rollup"):
            k, _, v = line.partition(":")
            if k in ("Rss", "Pss", "Private_Clean", "Private_Dirty"):
                d[k] = int(v.split()[0])
        return {"rss_mb": round(d["Rss"] / 1024), "pss_mb": round(d["Pss"] / 1024),
                "uss_mb": round((d["Private_Clean"] + d["Private_Dirty"]) / 1024)}
    except Exception:
        return None


# ----------------------------------------------------------------------------- feedback
def notify(cfg, msg, urgency="low"):
    if cfg["notify"] and shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "dictate", "-u", urgency, "-t", "2500",
                          "-h", "string:x-canonical-private-synchronous:dictate", "Dictate", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


SOUND_DIR = Path("/usr/share/sounds/freedesktop/stereo")
SOUNDS = {"start": "message-new-instant.oga", "done": "complete.oga", "error": "dialog-warning.oga"}


def cue(cfg, name):
    f = SOUND_DIR / SOUNDS[name]
    if cfg["sounds"] and f.exists() and shutil.which("pw-play"):
        subprocess.Popen(["pw-play", str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Tray:
    def __init__(self, enabled):
        self.proc = None
        if enabled:
            try:
                self.proc = subprocess.Popen(["/usr/bin/python3", str(HERE / "tray.py")], stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, text=True, bufsize=1)
            except Exception:
                self.proc = None

    def set(self, state):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.stdin.write(state + "\n"); self.proc.stdin.flush()
        except Exception:
            pass


class PortalTyper:
    def __init__(self, delay_ms=6):
        self.delay_ms = delay_ms
        self.proc, self.ready, self.error = None, threading.Event(), None
        self.lock = threading.Lock()

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self.ready.clear(); self.error = None
        self.proc = subprocess.Popen(["/usr/bin/python3", str(HERE / "portal_typer.py")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
                                     env=dict(os.environ, DICTATE_TYPE_DELAY_MS=str(self.delay_ms)))
        threading.Thread(target=self._read_first, daemon=True).start()

    def _read_first(self):
        line = self.proc.stdout.readline().strip()
        if line == "READY":
            self.ready.set()
        else:
            self.error = line or "helper exited"

    def restart(self, why="retry"):
        elog("portal_restart", why=why)
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        self.ready.clear(); self.error = None
        self.start()

    def type(self, text):
        for attempt in (1, 2):
            try:
                return self._type_once(text)
            except Exception as e:
                if attempt == 2:
                    raise
                sys.stderr.write(f"portal session lost ({e}); restarting the typing helper and retrying\n")
                elog("portal_session_lost", error=str(e)[:200])
                self.restart("session_lost")

    def _type_once(self, text):
        self.start()
        if not self.ready.wait(timeout=200 if not (CONF_DIR / "portal_token").exists() else 15):
            raise RuntimeError(self.error or "portal not ready (approve the keyboard-control prompt?)")
        with self.lock:
            self.proc.stdin.write(json.dumps({"text": text}) + "\n"); self.proc.stdin.flush()
            line = self.proc.stdout.readline().strip()
        if not line.startswith("DONE"):
            raise RuntimeError(line or "typing failed")


def clipboard_set(text):
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), check=False)
            return cmd[0]
    return None


if IS_WIN:      # swap the Linux-specific helpers above for their Windows versions; the Daemon code is shared
    import platform_win as _pw
    notify, cue, focus_app, clipboard_set = _pw.notify, _pw.cue, _pw.focus_app, _pw.clipboard_set
    idle_cpus, proc_mem, PortalTyper = _pw.idle_cpus, _pw.proc_mem, _pw.Typer


# ----------------------------------------------------------------------------- model worker
class Worker:
    def __init__(self, cfg):
        self.cfg, self.p = cfg, None
        self.ready, self.lock = threading.Event(), threading.Lock()
        self.results, self.n = {}, 0
        self.last_used, self.busy = time.time(), 0
        self.load_s, self.started_at, self.device = None, 0, None

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def pick_threads(self, idle=None):
        """4 engine threads normally; 1 when the machine is oversubscribed (a build, a benchmark...). Measured: with all
        CPUs busy, 4 (or 2) spin-synchronising threads never finish a job, while 1 thread finishes in ~2.5x the quiet time."""
        want = int(self.cfg["threads"])
        if not self.cfg.get("adaptive_threads", True):
            return want, None
        idle = idle_cpus() if idle is None else idle
        return (want if idle >= float(self.cfg.get("busy_idle_cpus", 4)) else 1), idle

    def ensure(self, reason="start"):
        with self.lock:
            if self.alive():
                return
            self.ready.clear(); self.results = {}
            threads, idle = self.pick_threads()
            wcfg = {k: self.cfg[k] for k in WORKER_KEYS}; wcfg["threads"] = threads
            env = dict(os.environ, DICTATE_CFG=json.dumps(wcfg),
                       OPENBLAS_NUM_THREADS="1", OMP_WAIT_POLICY="PASSIVE")   # idle threads sleep instead of spinning
            if self.cfg.get("pin_threads") and threads > 1:
                cpus = physical_cpus(threads)
                if len(cpus) >= threads:
                    env["GOMP_CPU_AFFINITY"] = ",".join(map(str, cpus))
            self.p = subprocess.Popen([sys.executable, str(HERE / "worker.py")], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True, bufsize=1, env=env)
            self.threads, self.started_at = threads, time.time()
            elog("worker_spawn", pid=self.p.pid, threads=threads, idle_cpus=idle, reason=reason)
            threading.Thread(target=self._reader, args=(self.p,), daemon=True).start()

    def _reader(self, p):
        for line in p.stdout:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("ready"):
                self.load_s = d.get("load_s"); self.device = d.get("device"); self.ready.set(); elog("worker_ready", load_s=self.load_s)
            elif "job" in d and d["job"] in self.results:
                ev, _ = self.results[d["job"]]
                self.results[d["job"]] = (ev, d); ev.set()

    def submit(self, pcm_path, timeout=300):
        if self.alive() and getattr(self, "threads", 1) > 1 and self.cfg.get("adaptive_threads", True) \
                and getattr(self, "idle_now", 99) < float(self.cfg.get("busy_idle_cpus", 4)):
            elog("worker_restart", reason="machine_busy", idle_cpus=self.idle_now, threads_was=self.threads)
            self.stop()                                  # a multi-thread worker would crawl: respawn with 1 thread
        self.ensure(reason="job")
        if not self.ready.wait(timeout=60):
            raise RuntimeError("model worker did not start")
        self.busy += 1
        try:
            self.n += 1; jid = self.n
            ev = threading.Event(); self.results[jid] = (ev, None)
            self.p.stdin.write(json.dumps({"job": jid, "pcm": str(pcm_path)}) + "\n"); self.p.stdin.flush()
            if not ev.wait(timeout):
                raise RuntimeError("transcription timed out")
            r = self.results.pop(jid)[1]
            if "error" in r:
                raise RuntimeError(r["error"])
            return r
        finally:
            self.busy -= 1
            self.last_used = time.time()

    def stop(self):
        if self.alive():
            try:
                self.p.stdin.write(json.dumps({"cmd": "quit"}) + "\n"); self.p.stdin.flush()
                self.p.wait(timeout=3)
            except Exception:
                self.p.kill()
            elog("worker_exit")
        self.ready.clear()

    def linger_watch(self, is_recording):
        while True:
            time.sleep(1)
            if self.alive() and not self.busy and not is_recording() \
                    and time.time() - self.last_used > float(self.cfg["worker_linger_s"]) \
                    and time.time() - self.started_at > 3:
                self.stop()


# ----------------------------------------------------------------------------- daemon
class Daemon:
    def __init__(self):
        self.cfg = load_config()
        self.tray = Tray(self.cfg["tray"])
        self.typer = PortalTyper(self.cfg["type_delay_ms"])
        self.worker = Worker(self.cfg)
        self.rec, self.chunks, self.rec_thread, self.t_rec = None, [], None, 0
        self.recording, self.busy, self.jobs = False, False, 0
        STATE.mkdir(parents=True, exist_ok=True)

    def portal_mode(self):
        return self.cfg["output"] in ("auto", "portal")

    # --- recording
    def start_rec(self):
        if self.recording:
            return "already recording"
        self.chunks = []
        if IS_WIN:
            try:
                self.rec = _pw.Recorder(self.chunks, self.cfg.get("mic_device"))
            except Exception as e:
                elog("job_error", error=f"microphone: {e}"[:200]); cue(self.cfg, "error")
                return f"error: microphone: {e}"
            self.rec_thread = threading.Thread(target=lambda: None); self.rec_thread.start()
        else:
            self.rec = subprocess.Popen(["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(SR), "-c", "1", "-"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.rec_thread = threading.Thread(target=self._pump, daemon=True); self.rec_thread.start()
        self.recording, self.t_rec = True, time.time()
        elog("rec_start", portal_ready=self.typer.ready.is_set(), mic=getattr(self.rec, "name", None))
        threading.Thread(target=self._cpu_watch, daemon=True).start()       # is the machine busy? (picks engine threads)
        threading.Thread(target=self.worker.ensure, daemon=True).start()   # model loads while you speak
        if self.portal_mode():
            self.typer.start()
        self.tray.set("rec"); cue(self.cfg, "start")
        notify(self.cfg, "🎙 listening…  (press the hotkey again to finish)")
        threading.Thread(target=self._max_guard, daemon=True).start()
        return "recording"

    def _cpu_watch(self):
        t0 = self.t_rec
        while self.recording and self.t_rec == t0:
            self.worker.idle_now = idle_cpus(0.3)
            time.sleep(0.4)

    def _pump(self):
        while True:
            b = self.rec.stdout.read(3200)
            if not b:
                break
            self.chunks.append(b)

    def _max_guard(self):
        t0 = self.t_rec
        while self.recording and self.t_rec == t0:
            if time.time() - t0 > self.cfg["max_record_s"]:
                self.stop_rec(); return
            time.sleep(0.5)

    def stop_rec(self, dry=False):
        if not self.recording:
            return "not recording"
        self.recording = False
        self.rec.terminate(); self.rec.wait(); self.rec_thread.join(timeout=2)
        pcm = b"".join(self.chunks)
        self.target = ""
        if not dry:
            threading.Thread(target=lambda: setattr(self, "target", focus_app()), daemon=True).start()
        elog("rec_stop", audio_s=round(len(pcm) / (SR * 2), 2), dry=dry or None)
        return self.run_job(pcm, time.perf_counter(), dry)

    # --- transcribe + deliver
    def run_job(self, pcm, t_stop, dry=False):
        cfg = self.cfg
        self.busy = True
        self.tray.set("busy")
        tmp = RUNTIME / f"dictate-{os.getpid()}-{self.jobs}.raw"
        self.jobs += 1
        try:
            if len(pcm) < 0.4 * SR * 2:
                notify(cfg, "too short, ignored")
                return "too short"
            tmp.write_bytes(pcm)
            notify(cfg, "⏳ transcribing…")
            elog("job_start", audio_s=round(len(pcm) / (SR * 2), 2))
            r = self.worker.submit(tmp)
            text = r.get("text", "")
            slow = bool(r.get("asr_s") and r.get("audio_s") and r["asr_s"] > r["audio_s"])
            elog("job_done", asr_s=r.get("asr_s"), job_s=r.get("job_s"), chars=len(text), threads=getattr(self.worker, "threads", None),
               slow=slow or None, dropped=len(r.get("dropped") or []) or None)
            if slow:
                notify(cfg, "🐢 the machine is busy, so this dictation is slow", "low")
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **{k: r.get(k) for k in (
                "audio_s", "peak", "rms", "asr_s", "job_s", "chunks", "raw", "dropped", "peak_rss_mb_during_job")},
                "worker_mem": r.get("mem_after_job"), "daemon_mem": proc_mem(os.getpid()),
                "worker_load_s": self.worker.load_s, "text": text}
            if not text:
                notify(cfg, "no speech detected"); cue(cfg, "error")
                self.log(rec, pcm)
                return "no speech"
            if not dry and self.portal_mode():
                wait = float(cfg["type_settle_ms"]) / 1000 - (time.perf_counter() - t_stop)
                if wait > 0:
                    time.sleep(wait)          # do not type while the Ctrl+Alt of the hotkey may still be held
            t_out = time.perf_counter()
            out = "print" if (dry or cfg["output"] == "print") else self.deliver(text + (" " if cfg["trailing_space"] else ""))
            rec.update(output=out, stop_to_text_s=round(time.perf_counter() - t_stop, 3),
                       deliver_s=round(time.perf_counter() - t_out, 3))
            self.log(rec, pcm)
            cue(cfg, "done" if out in ("portal", "print", "clipboard") else "error")
            if out in ("portal", "print"):
                notify(cfg, f"✅ {text[:80]}")
            elif out == "clipboard":
                notify(cfg, f"📋 copied: {text[:80]}  (paste with Ctrl+Shift+V)")
            else:
                notify(cfg, "⚠ could not type it: text saved, run  dictate last", "critical")
            return text
        except Exception as e:
            elog("job_error", error=str(e)[:200])
            notify(cfg, f"error: {e}", "critical"); cue(cfg, "error")
            return f"error: {e}"
        finally:
            tmp.unlink(missing_ok=True)
            self.busy = False
            self.tray.set("idle")

    def deliver(self, text):
        mode = self.cfg["output"]
        target = getattr(self, "target", "") or "unknown"
        if mode in ("auto", "portal"):
            t0 = time.perf_counter()
            elog("type_start", chars=len(text), target=target)
            try:
                self.typer.type(text)
                elog("type_done", secs=round(time.perf_counter() - t0, 2), chars=len(text), target=target)
                return "portal"
            except Exception as e:
                elog("type_fail", error=str(e)[:200], secs=round(time.perf_counter() - t0, 2), target=target)
                if mode == "portal":
                    raise
                sys.stderr.write(f"portal typing failed ({e}); falling back to clipboard\n")
        if clipboard_set(text):
            elog("deliver_fallback", via="clipboard")
            return "clipboard"
        (STATE / "last.txt").write_text(text)
        elog("deliver_fallback", via="file")
        return "file"

    def log(self, rec, pcm):
        with open(STATE / "history.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        if rec.get("text"):
            (STATE / "last.txt").write_text(rec["text"])          # always keep the latest text: `dictate last`
        if self.cfg["save_audio"]:
            d = STATE / "audio"; d.mkdir(exist_ok=True)
            with wave.open(str(d / f"{rec['ts'].replace(':', '')}.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)
            for old in sorted(d.glob("*.wav"))[: -int(self.cfg["keep_audio"])]:
                old.unlink(missing_ok=True)

    # --- control socket
    def memstat(self):
        out = {"daemon": proc_mem(os.getpid())}
        if self.tray.proc: out["tray"] = proc_mem(self.tray.proc.pid)
        if self.typer.proc and self.typer.proc.poll() is None: out["portal_helper"] = proc_mem(self.typer.proc.pid)
        if self.worker.alive(): out["worker"] = proc_mem(self.worker.p.pid)
        procs = [v for v in out.values() if v]
        out["total_uss_mb"] = sum(v["uss_mb"] for v in procs)
        out["total_pss_mb"] = sum(v["pss_mb"] for v in procs)
        return json.dumps(out)

    def handle(self, cmd):
        cmd, _, arg = cmd.partition(" ")
        if cmd == "toggle":
            return self.stop_rec() if self.recording else self.start_rec()
        if cmd == "start": return self.start_rec()
        if cmd == "stop": return self.stop_rec()
        if cmd == "warm": self.worker.ensure(); self.worker.ready.wait(60); return "warm"
        if cmd == "workerquit": self.worker.stop(); return "worker stopped"
        if cmd == "replay":
            with wave.open(arg) as w:
                assert w.getframerate() == SR and w.getnchannels() == 1, "need 16 kHz mono wav"
                pcm = w.readframes(w.getnframes())
            return self.run_job(pcm, time.perf_counter(), dry=True)
        if cmd == "typetest":
            time.sleep(4); self.typer.type("Typing test: The quick brown fox, 123! (A-Z) it's ok? "); return "typed"
        if cmd == "memstat": return self.memstat()
        if cmd == "events":
            n = int(arg) if arg.isdigit() else 40
            return "".join(EVLOG.read_text().splitlines(True)[-n:]) if EVLOG.exists() else "(no events yet)"
        if cmd == "last":
            f = STATE / "last.txt"
            return f.read_text() if f.exists() else "(nothing yet)"
        if cmd == "status":
            return json.dumps({"recording": self.recording, "busy": self.busy, "worker_running": self.worker.alive(),
                               "worker_load_s": self.worker.load_s, "device": self.worker.device, "portal_ready": self.typer.ready.is_set(),
                               "portal_error": self.typer.error})
        if cmd == "quit":
            self.worker.stop()
            threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0))).start()
            return "bye"
        return "unknown command"

    def screen_watch(self):
        """Log screen lock/unlock, and re-open the typing session right after unlock (GNOME kills it while locked)."""
        while True:
            try:
                p = subprocess.Popen(["gdbus", "monitor", "--session", "--dest", "org.gnome.ScreenSaver"],
                                     stdout=subprocess.PIPE, text=True, bufsize=1)
                for line in p.stdout:
                    if "ActiveChanged" in line:
                        locked = "true" in line
                        elog("screen", state="locked" if locked else "unlocked")
                        if not locked and self.portal_mode() and not self.recording:
                            time.sleep(1.5)                       # let the session settle, then get a fresh typing session
                            self.typer.restart("unlock")
                p.wait()
            except Exception as e:
                elog("screen_watch_error", error=str(e)[:100])
            time.sleep(30)

    def portal_watch(self):
        while True:                                  # the desktop portal may not be up yet at login: keep retrying
            time.sleep(10)
            p = self.typer.proc
            if not self.typer.ready.is_set() and (p is None or p.poll() is not None):
                if getattr(self, "_portal_down", None) is None:
                    self._portal_down = time.time(); elog("portal_down", error=self.typer.error)
                self.typer.start()
            elif self.typer.ready.is_set() and getattr(self, "_portal_down", None) is not None:
                elog("portal_up", down_s=round(time.time() - self._portal_down, 1)); self._portal_down = None

    def serve(self):
        s = ctl_listen()
        threading.Thread(target=self.worker.linger_watch, args=(lambda: self.recording,), daemon=True).start()
        if IS_WIN:
            try:
                _pw.start_hotkey(self.cfg["hotkey"], lambda: self.handle("toggle"), elog)
            except ValueError as e:
                elog("hotkey_fail", error=str(e)); sys.stderr.write(f"{e}\n")
        else:
            if self.portal_mode():
                self.typer.start()
                threading.Thread(target=self.portal_watch, daemon=True).start()
            threading.Thread(target=self.screen_watch, daemon=True).start()
        elog("daemon_start", pid=os.getpid(), beam=self.cfg["beam_size"], threads=self.cfg["threads"], output=self.cfg["output"])
        print("dictate daemon ready", flush=True)
        while True:
            c, _ = s.accept()
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        try:
            cmd = ctl_unpack(c.recv(1024))
            if cmd is None:
                c.sendall(b"unauthorized"); return
            if cmd in ("toggle", "stop") and self.recording:          # answer the hotkey instantly
                threading.Thread(target=self.handle, args=(cmd,), daemon=True).start()
                c.sendall(b"processing")
            else:
                c.sendall(str(self.handle(cmd)).encode())
        finally:
            c.close()


# ----------------------------------------------------------------------------- client / doctor
def send(cmd, timeout=60):
    return ctl_request(cmd, timeout)


def doctor_windows():
    import importlib.util
    ok = lambda b: "ok " if b else "FAIL"
    print(f"[{ok(Path(DEFAULTS['model']).exists())}] model: {DEFAULTS['model']}")
    for mod, why in (("faster_whisper", "speech model"), ("sounddevice", "microphone"), ("psutil", "stats"), ("numpy", "audio")):
        print(f"[{ok(importlib.util.find_spec(mod))}] python: {mod} ({why})")
    try:
        print(f"[ok ] microphone: opened {_pw.probe_mic(load_config().get('mic_device'))}")
    except Exception as e:
        print(f"[FAIL] microphone: {e}")
    try:
        _pw.parse_hotkey(DEFAULTS["hotkey"]); print(f"[ok ] hotkey: {load_config()['hotkey']}")
    except ValueError as e:
        print(f"[FAIL] {e}")
    try:
        import ctranslate2
        print(f"[{'ok ' if ctranslate2.get_cuda_device_count() else 'warn'}] CUDA GPUs: {ctranslate2.get_cuda_device_count()} (0 = runs on CPU)")
    except Exception as e:
        print(f"[warn] ctranslate2: {e}")
    try:
        print("[ok ] daemon:", send("status", 5))
    except Exception as e:
        print(f"[FAIL] daemon not running ({e})  ->  {START_HINT}")


def doctor():
    if IS_WIN:
        return doctor_windows()
    ok = lambda b: "ok " if b else "FAIL"
    print(f"[{ok(Path(DEFAULTS['model']).exists())}] model: {DEFAULTS['model']}")
    print(f"[{ok(shutil.which('arecord'))}] arecord (microphone capture)")
    print(f"[{ok(shutil.which('pw-play'))}] pw-play (sound cues)")
    print(f"[{ok(shutil.which('notify-send'))}] notify-send")
    print(f"[{ok(os.environ.get('XDG_SESSION_TYPE') == 'wayland')}] session: {os.environ.get('XDG_SESSION_TYPE')}")
    p = subprocess.run(["/usr/bin/python3", "-c", "import gi"], capture_output=True)
    print(f"[{ok(p.returncode == 0)}] PyGObject (portal typing, tray)")
    print(f"[{ok((CONF_DIR / 'portal_token').exists())}] keyboard-control permission granted")
    print(f"[{'ok ' if shutil.which('wl-copy') or shutil.which('xclip') else 'warn'}] clipboard fallback (wl-copy/xclip)")
    try:
        print("[ok ] daemon:", send("status", 5))
    except Exception as e:
        print(f"[FAIL] daemon not running ({e})  ->  {START_HINT}")


def words_cmd(a):
    """dictate vocab add|rm|list TERM ...   dictate replace add|rm|list "a, b => c"   dictate fixtest "some text"
    Edits ~/.config/dictate/{vocab,replacements}.txt directly; the worker re-reads them on every dictation."""
    import textfix
    conf = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dictate"
    conf.mkdir(parents=True, exist_ok=True)
    rd = lambda n: [l.strip() for l in (conf / n).read_text().splitlines()] if (conf / n).exists() else []
    live = lambda ls: [l for l in ls if l and not l.startswith("#")]
    if a[0] == "fixtest":
        t = " ".join(a[1:])
        t = textfix.apply_rules(t, textfix.parse_rules(live(rd("replacements.txt"))))
        print(textfix.Vocab(live(rd("vocab.txt"))).correct(t)); return
    name = "vocab.txt" if a[0] == "vocab" else "replacements.txt"
    sub, arg = (a[1] if len(a) > 1 else "list"), " ".join(a[2:]).strip()
    lines = rd(name)
    if sub == "list":
        print("\n".join(live(lines)) or "(empty)")
    elif sub == "add" and arg:
        if a[0] == "replace" and "=>" not in arg:
            sys.exit('usage: dictate replace add "heard one, heard two => Correct Spelling"')
        if arg.lower() in [l.lower() for l in lines]:
            print("already there"); return
        (conf / name).write_text("\n".join(lines + [arg]) + "\n"); print("added:", arg)
    elif sub == "rm" and arg:
        keep = [l for l in lines if l.lower() != arg.lower()]
        (conf / name).write_text("\n".join(keep) + "\n"); print("removed" if len(keep) < len(lines) else "not found")
    else:
        sys.exit("usage: dictate vocab|replace add|rm|list ...")


def main():
    cmd = " ".join(sys.argv[1:]) or "toggle"
    if cmd == "serve":
        capture_stderr()
        Daemon().serve()
    elif sys.argv[1:2] == ["dashboard"]:
        os.execv(sys.executable, [sys.executable, str(HERE / "dashboard.py")] + sys.argv[2:])
    elif cmd == "doctor":
        doctor()
    elif sys.argv[1:2] in (["vocab"], ["replace"], ["fixtest"]):
        words_cmd(sys.argv[1:])
    else:
        try:
            print(send(cmd, 300 if cmd.startswith(("replay", "typetest", "warm")) else 15))
        except (FileNotFoundError, ConnectionRefusedError):
            notify({"notify": True, "sounds": True}, f"daemon not running: {START_HINT}", "critical")
            print(f"daemon not running: {START_HINT}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
