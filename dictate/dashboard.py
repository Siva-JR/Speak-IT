#!/usr/bin/env python3
"""Dictate dashboard: one page with every log, error, check and timing, for debugging on Linux, macOS and Windows.

  python dashboard.py [--port 8765] [--no-browser]

Standard library only, never imports dictate.py (which is Linux-only), so it runs even when the daemon cannot start.
Reads the state dir (events.log, errors.log, history.jsonl) and serves http://127.0.0.1:PORT (local only, read-only).
"""
import argparse, http.server, importlib.util, json, os, platform, re, secrets, shutil, sys, time, tomllib, webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import STATE, CONF_DIR as CONF, OS_NAME, ctl_request
import learn
MODEL = HERE.parent / "models" / "faster-whisper-base.en"
BAD_EVENTS = {"job_error", "type_fail", "portal_down", "deliver_fallback", "screen_watch_error",
              "portal_session_lost", "worker_restart", "live_error", "undo_refused"}
OS = OS_NAME
TOKEN = secrets.token_hex(16)                       # per run; the page carries it, so other websites cannot start the mic
AUDIO_RE = re.compile(r"^[0-9T-]+\.wav$")           # only the daemon's own recordings can be served


def tail_lines(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 400_000))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def parse_event(line):
    parts = line.split(" ", 2)
    if len(parts) < 2:
        return {"t": "", "kind": "?", "rest": line, "bad": False}
    kind = parts[1]
    return {"t": parts[0], "kind": kind, "rest": parts[2] if len(parts) > 2 else "", "bad": kind in BAD_EVENTS}


HIST_KEYS = ("ts", "audio_s", "peak", "rms", "job_s", "asr_s", "stop_to_text_s", "ready_s", "deliver_s", "output",
             "chunks", "worker_load_s", "note", "error", "peak_rss_mb_during_job", "words", "wpm", "eff_wpm", "app",
             "profile", "live_s", "tail_s", "tail_asr_s", "enter", "device")


def all_history():
    """Every real dictation (not dashboard tests or benchmarks), oldest first."""
    out = []
    for l in tail_lines(STATE / "history.jsonl", 100_000):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if not r.get("dry"):
            out.append(r)
    return out


def _item(r, redact, fixes):
    keep = {k: r.get(k) for k in HIST_KEYS}
    wav = f"{r.get('ts', '').replace(':', '')}.wav"
    keep["wav"] = wav if (STATE / "audio" / wav).is_file() else None
    keep["text"] = f"<{len(r.get('text', ''))} chars>" if redact else r.get("text", "")
    keep["raw"] = "" if redact else r.get("raw", "")
    keep["corrected"] = None if redact else fixes.get(r.get("ts"))
    keep["dropped"] = len(r.get("dropped") or [])
    return keep


def history(n, redact):
    fixes = learn.corrections()
    return [_item(r, redact, fixes) for r in all_history()[-n:]]


def search_history(q, limit, offset, redact):
    """Newest first. q matches the text, the raw transcript, the corrected text or the app name."""
    fixes, q = learn.corrections(), (q or "").strip().lower()
    rows = all_history()[::-1]
    if q:
        rows = [r for r in rows if q in " ".join([r.get("text") or "", r.get("raw") or "", fixes.get(r.get("ts")) or "",
                                                  r.get("app") or ""]).lower()]
    return {"total": len(rows), "items": [_item(r, redact, fixes) for r in rows[offset:offset + limit]]}


def _median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    return round(xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2, 3) if xs else None


def _p90(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    return round(xs[int(0.9 * (len(xs) - 1))], 3) if xs else None


def perf():
    """Real-use numbers from history (last 200 dictations with text) + the latest `dictate bench` run."""
    rows = [r for r in all_history() if r.get("text") and r.get("stop_to_text_s") is not None][-200:]
    series = [{k: r.get(k) for k in ("ts", "stop_to_text_s", "ready_s", "deliver_s", "audio_s", "words", "wpm",
                                     "eff_wpm", "live_s", "app")} for r in rows]
    words = sum(r.get("words") or len((r.get("text") or "").split()) for r in rows)
    spoken = sum(r.get("audio_s") or 0 for r in rows)
    agg = {"n": len(rows), "words": words, "spoken_s": round(spoken, 1),
           "stop_to_text_median": _median([r["stop_to_text_s"] for r in rows]),
           "stop_to_text_p90": _p90([r["stop_to_text_s"] for r in rows]),
           "ready_median": _median([r.get("ready_s") for r in rows]),
           "wpm_median": _median([r.get("wpm") or (len(r["text"].split()) / r["audio_s"] * 60 if r.get("audio_s") else None)
                                  for r in rows]),
           "eff_wpm_median": _median([r.get("eff_wpm") or (len(r["text"].split()) / (r["audio_s"] + r["stop_to_text_s"]) * 60
                                                           if r.get("audio_s") else None) for r in rows]),
           "live_share": round(sum(1 for r in rows if (r.get("live_s") or 0) > 0) / len(rows), 3) if rows else None}
    bench = None
    lines = tail_lines(STATE / "bench.jsonl", 1)
    if lines:
        try:
            b = json.loads(lines[-1])
            bench = {"ts": b["ts"], "machine": b.get("machine"), "clips": b.get("clips"), "audio_s": b.get("audio_s"),
                     "modes": {m: {"summary": v["summary"], "device": v.get("device")} for m, v in b["modes"].items()}}
        except (ValueError, KeyError):
            pass
    return {"agg": agg, "series": series[-60:], "bench": bench}


def daemon_status():
    try:
        return {"reachable": True, "status": json.loads(ctl_request("status", 2))}
    except Exception as e:
        return {"reachable": False, "why": f"{type(e).__name__}: {e}"}


def pkg(name):
    if not importlib.util.find_spec(name.replace("-", "_")):
        return None
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "installed"


def cuda_info():
    try:
        import ctranslate2
        return {"devices": ctranslate2.get_cuda_device_count(), "ctranslate2": ctranslate2.__version__,
                "compute_types_cuda": sorted(ctranslate2.get_supported_compute_types("cuda")) if ctranslate2.get_cuda_device_count() else []}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def checks():
    c = []
    add = lambda name, ok, detail="", level="fail": c.append({"name": name, "ok": bool(ok), "detail": detail, "level": level})
    add("model files", (MODEL / "model.bin").exists(), str(MODEL))
    for p in ("faster_whisper", "numpy", "psutil", "num2words"):
        add(f"python: {p}", pkg(p), "pip install " + p.replace("_", "-"))
    add("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    tools = {"linux": ["arecord", "pw-play", "notify-send"], "mac": ["afplay", "osascript", "pbcopy"]}.get(OS, [])
    for t in tools:
        add(f"tool: {t}", shutil.which(t), "needed for mic capture / sound cues / notifications / typing", "warn")
    if OS == "linux":
        add("session: wayland", os.environ.get("XDG_SESSION_TYPE") == "wayland", os.environ.get("XDG_SESSION_TYPE") or "unset", "warn")
        add("keyboard permission token", (CONF / "portal_token").exists(), "approve the GNOME prompt on first start")
    if OS == "windows":
        add("python: sounddevice", pkg("sounddevice"), "pip install sounddevice (microphone)")
        try:
            import platform_win
            import sounddevice as sd      # passive: listing only, never open the mic from a page that polls every 2 s
            names = [sd.query_devices()[i]["name"] for i in platform_win.candidate_inputs(sd, None)]
            add("microphone present", names, f"{len(names)} input(s), first tried: {names[0]}. Run 'dictate doctor' to test opening it")
        except Exception as e:
            add("microphone present", False, f"{type(e).__name__}: {e}")
        add("startup shortcut", (Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/Dictate.lnk").exists(),
            "created by install.ps1 (daemon starts at login)", "warn")
    d = daemon_status()
    add("daemon reachable", d["reachable"], d.get("why", "ok"))
    cu = cuda_info()
    add("CUDA GPU visible", cu.get("devices", 0) > 0, json.dumps(cu), "warn")
    return c


def config():
    cfg = {}
    f = CONF / "config.toml"
    try:
        cfg = tomllib.loads(f.read_text()) if f.exists() else {}
    except Exception as e:
        cfg = {"_parse_error": str(e)}
    env = {k: v for k, v in os.environ.items() if k.startswith("DICTATE_")}
    files = {}
    for n in ("vocab.txt", "replacements.txt"):
        files[n] = (CONF / n).read_text().splitlines() if (CONF / n).exists() else []
    return {"config_file": str(f), "config": cfg, "env": env, "user_lists": files}


def system():
    return {"os": f"{platform.system()} {platform.release()} ({platform.machine()})", "python": sys.version.split()[0],
            "executable": sys.executable, "cpu_count": os.cpu_count(), "state_dir": str(STATE), "config_dir": str(CONF),
            "packages": {p: pkg(p) for p in ("faster-whisper", "ctranslate2", "numpy", "psutil", "onnxruntime")},
            "cuda": cuda_info(), "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def pretty_hotkey(spec):
    """'<Control><Alt>space' / 'ctrl+alt+space' -> 'Ctrl+Alt+Space'."""
    parts = [p for p in re.split(r"[+<>]", spec or "") if p]
    fix = {"control": "Ctrl", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Win", "win": "Win", "primary": "Ctrl"}
    return "+".join(fix.get(p.lower(), p.upper() if len(p) == 1 else p.capitalize()) for p in parts)


def hotkey_info(events):
    """The hotkey configured on THIS machine and whether it is active: {'keys', 'status', 'detail'}."""
    if OS == "linux":
        import subprocess
        path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictate/"
        try:
            r = subprocess.run(["gsettings", "get", f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{path}", "binding"],
                               capture_output=True, text=True, timeout=3)
            spec = r.stdout.strip().strip("'\"")
            if r.returncode == 0 and spec:
                return {"keys": pretty_hotkey(spec), "status": "set", "detail": "GNOME shortcut (re-run install.sh to change; DICTATE_HOTKEY=...)"}
            return {"keys": "", "status": "missing", "detail": "no GNOME shortcut named Dictate: run dictate/install.sh"}
        except Exception as e:
            return {"keys": "", "status": "unknown", "detail": f"could not read GNOME settings ({type(e).__name__}); bind a key to: dictate toggle"}
    if OS == "windows":
        spec = "ctrl+alt+space"
        try:
            f = CONF / "config.toml"
            if f.exists():
                spec = tomllib.loads(f.read_text()).get("hotkey", spec)
        except Exception:
            pass
        state, detail = "unknown", "the daemon has not reported yet"
        for e in reversed(events):                      # newest event wins, but only since the last daemon start
            if e["kind"] == "hotkey_ok":
                state, detail = "active", "registered with Windows"; break
            if e["kind"] == "hotkey_fail":
                state, detail = "failed", e["rest"][:160] + "  (another program uses it: set hotkey in config.toml)"; break
            if e["kind"] == "daemon_start":
                break
        return {"keys": pretty_hotkey(spec), "status": state, "detail": detail}
    return {"keys": "", "status": "none", "detail": f"no hotkey on {OS} yet: use the Start button"}


def snapshot(redact=False, n_events=300, n_err=200, n_hist=30):
    events = [parse_event(l) for l in tail_lines(STATE / "events.log", n_events)]
    return {"hotkey": hotkey_info(events), "system": system(), "daemon": daemon_status(), "checks": checks(), "config": config(), "events": events,
            "problems": [e for e in events if e["bad"]][-50:], "errors": tail_lines(STATE / "errors.log", n_err),
            "history": history(n_hist, redact)}


def bundle_text():
    s = snapshot(redact=True, n_events=400, n_err=300, n_hist=40)
    out = ["# Dictate diagnostics (dictated text redacted)", "", "## system", json.dumps(s["system"], indent=2),
           "", "## daemon", json.dumps(s["daemon"], indent=2), "", "## checks"]
    out += [f"[{'ok' if c['ok'] else c['level'].upper():4}] {c['name']}  {c['detail']}" for c in s["checks"]]
    out += ["", "## config", json.dumps(s["config"], indent=2), "", "## problems (last 50)"]
    out += [f"{e['t']} {e['kind']} {e['rest']}" for e in s["problems"]]
    out += ["", "## errors.log (stderr + tracebacks)"] + s["errors"]
    out += ["", "## events.log"] + [f"{e['t']} {e['kind']} {e['rest']}" for e in s["events"]]
    out += ["", "## recent dictations (timings)"] + [json.dumps(h) for h in s["history"]]
    return "\n".join(out)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)

    def host_ok(self):                               # blocks DNS rebinding and cross-site requests
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return False
        o = self.headers.get("Origin")
        return not o or o.split("//")[-1].split(":")[0] in ("127.0.0.1", "localhost")

    def authed(self, q):
        return secrets.compare_digest(self.headers.get("X-Token") or (q.get("t") or [""])[0], TOKEN)

    def daemon(self, cmd, timeout):
        try:
            return ctl_request(cmd, timeout)
        except Exception as e:
            return json.dumps({"ok": False, "msg": f"daemon not reachable: {type(e).__name__}: {e}"})

    def do_POST(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if not self.host_ok() or not self.authed(q):
            return self.send(403, "forbidden", "text/plain")
        if u.path in ("/api/correct", "/api/suggestion"):
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 100_000)) or b"{}")
            except ValueError:
                return self.send(400, "bad json", "text/plain")
            if u.path == "/api/correct":
                orig = next((r.get("text") for r in all_history() if r.get("ts") == body.get("ts")), None)
                if orig is None or not str(body.get("text", "")).strip():
                    return self.send(404, json.dumps({"error": "no such dictation"}), "application/json")
                sugs = learn.record_correction(body["ts"], orig, str(body["text"]).strip())
                return self.send(200, json.dumps({"suggestions": sugs}), "application/json")
            item = learn.resolve(body.get("id", -1), body.get("action"))
            return self.send(200 if item else 404, json.dumps(item or {"error": "no such suggestion"}), "application/json")
        if u.path == "/api/test/start":
            self.send(200, self.daemon("teststart", 15), "application/json")
        elif u.path == "/api/test/stop":
            self.send(200, self.daemon("teststop", 240), "application/json")     # first run also loads the model
        else:
            self.send(404, "not found", "text/plain")

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if not self.host_ok():
            return self.send(403, "forbidden", "text/plain")
        if u.path == "/":
            self.send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self.send(200, json.dumps(snapshot(redact=q.get("redact", ["0"])[0] == "1")), "application/json")
        elif u.path == "/api/history":
            n = lambda k, d: max(0, min(int((q.get(k) or [d])[0] or d), 500))
            self.send(200, json.dumps(search_history((q.get("q") or [""])[0], n("limit", 50), n("offset", 0),
                                                     (q.get("redact") or ["0"])[0] == "1")), "application/json")
        elif u.path == "/api/perf":
            self.send(200, json.dumps(perf()), "application/json")
        elif u.path == "/api/suggestions":
            self.send(200, json.dumps({"items": learn.suggestions()}), "application/json")
        elif u.path == "/api/bundle":
            self.send(200, bundle_text(), "text/plain; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="dictate-diagnostics-{OS}.txt"'})
        elif u.path == "/api/test/level":
            if not self.authed(q):
                return self.send(403, "forbidden", "text/plain")
            self.send(200, self.daemon("level", 2), "application/json")
        elif u.path.startswith("/api/audio/"):
            name = u.path.rsplit("/", 1)[-1]
            f = STATE / "audio" / name
            if not self.authed(q) or not AUDIO_RE.match(name) or not f.is_file():
                return self.send(404, "not found", "text/plain")
            self.send(200, f.read_bytes(), "audio/wav")
        else:
            self.send(404, "not found", "text/plain")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dictate Dashboard</title><style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2128;--mut:#6b7380;--line:#e3e6ea;--ok:#1a7f4b;--warn:#a15c00;--bad:#c62828;--bg2:#eef0f3;--acc:#2f6fdb}
@media(prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1b1f24;--fg:#e6e9ed;--mut:#8b94a1;--line:#2a3038;--ok:#4cc38a;--warn:#e0a030;--bad:#f2706b;--bg2:#232830;--acc:#6aa0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
h1{font-size:16px;margin:0 8px 0 0}.pill{padding:2px 10px;border-radius:99px;background:var(--bg2);font-size:12px}
.pill.ok{color:var(--ok)}.pill.bad{color:var(--bad)}.pill.warn{color:var(--warn)}.sp{flex:1}
button,label{font:inherit;color:var(--fg)}button{background:var(--bg2);border:1px solid var(--line);border-radius:6px;padding:5px 11px;cursor:pointer}
button:hover{border-color:var(--mut)}main{max-width:1200px;margin:0 auto;padding:16px;display:grid;gap:14px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
section>h2{margin:0;padding:10px 14px;font-size:13px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center}
section>h2 .n{color:var(--mut);font-weight:400}.body{padding:0;max-height:340px;overflow:auto}
table{border-collapse:collapse;width:100%}td,th{padding:5px 14px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:12px;position:sticky;top:0;background:var(--card)}tr:last-child td{border-bottom:0}
.mono{font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.row{padding:3px 14px}.row.bad{color:var(--bad);background:color-mix(in srgb,var(--bad) 8%,transparent)}.k{color:var(--mut)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.empty{padding:14px;color:var(--mut)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:800px){.two{grid-template-columns:1fr}}
input[type=search]{font:inherit;background:var(--bg2);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:3px 8px;margin-left:auto}
details summary{cursor:pointer;padding:10px 14px;font-size:13px;font-weight:600}
.test{padding:14px;display:grid;gap:12px}.trow{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center}
.big{font-size:15px;padding:9px 18px;border-radius:8px;font-weight:600}.big.rec{background:var(--bad);color:#fff;border-color:var(--bad)}
.big:disabled{opacity:.5;cursor:default}kbd{font:12px ui-monospace,Consolas,monospace;background:var(--bg2);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 7px}
canvas{width:100%;height:110px;display:block;background:var(--bg2);border-radius:6px}
.meter{position:relative;height:12px;background:var(--bg2);border-radius:6px;overflow:hidden}.meter i{position:absolute;left:0;top:0;bottom:0;background:var(--ok);width:0}
.meter b{position:absolute;top:0;bottom:0;width:2px;background:var(--mut)}
.stage{display:flex;gap:10px;padding:3px 0}.stage .ic{width:16px;text-align:center}.out{font-size:17px;padding:12px 14px;background:var(--bg2);border-radius:6px;min-height:46px}
.hint{color:var(--mut);font-size:12px}audio{width:100%;max-width:420px;height:34px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line)}
.tile{background:var(--card);padding:10px 14px}.tile .l{color:var(--mut);font-size:12px}.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .s{color:var(--mut);font-size:12px}.chartwrap{position:relative;padding:8px 14px 4px}#chart{height:140px}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;display:none;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.hitem{padding:8px 14px;border-bottom:1px solid var(--line);display:grid;gap:3px}.hitem:last-child{border-bottom:0}
.hmeta{display:flex;flex-wrap:wrap;gap:4px 12px;color:var(--mut);font-size:12px;align-items:center}.hmeta button{padding:1px 8px;font-size:12px}
.htext{font-size:14px}.htext s{color:var(--mut)}textarea{width:100%;font:inherit;background:var(--bg2);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px}
.sug{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;padding:8px 14px;border-bottom:1px solid var(--line)}.sug code{font:13px ui-monospace,Consolas,monospace}
</style></head><body>
<header><h1>Dictate Dashboard</h1><span id="pills"></span><span class="sp"></span>
<label><input type="checkbox" id="live" checked> live</label><label><input type="checkbox" id="redact"> hide dictated text</label>
<button id="copy">Copy diagnostics</button><a href="/api/bundle"><button>Download</button></a></header>
<main>
<section id="s-test"><h2>Live test <span class="n">speak and see exactly where it works or breaks</span></h2><div class="test">
 <div class="trow"><button id="go" class="big">Start listening</button><span id="tstate" class="pill">idle</span><span id="voice" class="pill">voice: -</span>
  <span class="sp"></span><span class="hint">Hotkey on this machine: <kbd id="hk">?</kbd> <span id="hkst" class="pill"></span></span></div>
 <div class="hint" id="hkdetail"></div>
 <canvas id="spec"></canvas>
 <div><div class="meter"><i id="lvl"></i><b id="gate" title="typical speech is above this line"></b></div>
  <div class="hint" id="lvltxt">input level</div></div>
 <div id="warn" class="warn" style="display:none"></div>
 <div id="stages"></div>
 <div class="out mono" id="out">Press the button (or the hotkey), speak, then press Stop. The text appears here and is not typed anywhere.</div>
 <div class="trow"><audio id="play" controls style="display:none"></audio><span class="hint" id="timing"></span></div>
</div></section>
<section id="s-perf"><h2>Performance <span class="n">your real dictations; stop→text includes typing</span></h2>
 <div class="tiles" id="tiles"></div>
 <div class="chartwrap"><div class="hint">Stop → text in place, last <span id="n-chart">0</span> dictations (seconds)</div><svg id="chart" width="100%" role="img" aria-label="stop to text latency per dictation"></svg><div class="tip" id="tip"></div></div>
 <div class="body" id="bench"></div></section>
<section id="s-prob"><h2>Problems <span class="n" id="n-prob"></span></h2><div class="body" id="prob"></div></section>
<div class="two">
<section><h2>Checks</h2><div class="body" id="checks"></div></section>
<section><h2>System</h2><div class="body mono row" id="sys"></div></section></div>
<section id="s-sug"><h2>Suggestions <span class="n">from your corrections; nothing changes until you accept</span></h2><div class="body" id="sugs"></div></section>
<section><h2>History <span class="n" id="n-hist"></span><input type="search" id="hq" placeholder="search dictations…"></h2><div class="body" id="hist" style="max-height:520px"></div></section>
<div class="two">
<section><h2>Events <span class="n" id="n-ev"></span><input type="search" id="q" placeholder="filter…"></h2><div class="body mono" id="ev"></div></section>
<section><h2>Errors / stderr <span class="n" id="n-err"></span></h2><div class="body mono" id="err"></div></section></div>
<section><details><summary>Config, env overrides, vocab and replacements</summary><div class="mono row" id="cfg"></div></details></section>
</main><script>
const $=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
let S=null,stick={};
function keepScroll(id,html){const el=$(id),atEnd=el.scrollHeight-el.scrollTop-el.clientHeight<30;el.innerHTML=html;
 if(stick[id]!==false&&atEnd||el.dataset.init!=="1"){el.scrollTop=el.scrollHeight;el.dataset.init="1"}}
function render(){
 if(!S)return;const d=S.daemon,st=d.status||{},bad=S.checks.filter(c=>!c.ok&&c.level==="fail").length;
 $("pills").innerHTML=`<span class="pill ${d.reachable?"ok":"bad"}">daemon ${d.reachable?"up":"down"}</span>`+
  `<span class="pill ${st.worker_running?"ok":""}">worker ${st.worker_running?"running":"idle"}</span>`+
  `<span class="pill">device ${esc(st.device||"?")}</span><span class="pill ${bad?"bad":"ok"}">${bad?bad+" failing checks":"checks ok"}</span>`+
  `<span class="pill ${S.problems.length?"warn":""}">${S.problems.length} problems</span>`;
 $("n-prob").textContent=S.problems.length||"";
 $("prob").innerHTML=S.problems.length?S.problems.slice().reverse().map(e=>`<div class="row bad mono">${esc(e.t)}  ${esc(e.kind)}  ${esc(e.rest)}</div>`).join(""):'<div class="empty">None. Nothing has gone wrong recently.</div>';
 $("checks").innerHTML="<table>"+S.checks.map(c=>`<tr><td class="${c.ok?"ok":c.level==="warn"?"warn":"bad"}">${c.ok?"ok":c.level==="warn"?"warn":"FAIL"}</td><td>${esc(c.name)}</td><td class="k mono">${c.ok?"":esc(c.detail)}</td></tr>`).join("")+"</table>";
 $("sys").textContent=JSON.stringify(S.system,null,2);
 const f=$("q").value.toLowerCase(),ev=S.events.filter(e=>!f||(e.kind+" "+e.rest).toLowerCase().includes(f));
 $("n-ev").textContent=S.events.length;
 keepScroll("ev",ev.map(e=>`<div class="row ${e.bad?"bad":""}"><span class="k">${esc(e.t.slice(11))}</span> <b>${esc(e.kind)}</b> ${esc(e.rest)}</div>`).join("")||'<div class="empty">No events.</div>');
 $("n-err").textContent=S.errors.length||"";
 keepScroll("err",S.errors.length?S.errors.map(l=>`<div class="row ${/Error|Traceback|Exception|failed|fatal/i.test(l)?"bad":""}">${esc(l)}</div>`).join(""):'<div class="empty">errors.log is empty.</div>');
 $("cfg").textContent=JSON.stringify(S.config,null,2)}
let lastTs=null;
async function load(){try{S=await (await fetch("/api/state?redact="+($("redact").checked?1:0))).json();render();
  const ts=(S.history.at(-1)||{}).ts||"";if(ts!==lastTs){lastTs=ts;loadHist();loadPerf();loadSugs()}}
 catch(e){$("pills").innerHTML='<span class="pill bad">dashboard lost connection</span>'}}
$("q").oninput=render;$("redact").onchange=()=>{lastTs=null;load()};
/* ---------------- performance ---------------- */
const fmt=(v,d=2)=>v==null?"–":(+v).toFixed(d);
function tile(l,v,s){return `<div class="tile"><div class="l">${esc(l)}</div><div class="v">${v}</div><div class="s">${esc(s||"")}</div></div>`}
let P=null;
async function loadPerf(){try{P=await (await fetch("/api/perf")).json();renderPerf()}catch(e){}}
function renderPerf(){if(!P)return;const a=P.agg;
 $("tiles").innerHTML=a.n?tile("Stop → text, median",fmt(a.stop_to_text_median)+" s",`p90 ${fmt(a.stop_to_text_p90)} s`)+
  tile("Speaking speed",fmt(a.wpm_median,0)+" wpm","median words per minute while talking")+
  tile("Effective speed",fmt(a.eff_wpm_median,0)+" wpm","including the wait for the text")+
  tile("Dictated",(a.words||0).toLocaleString()+" words",`${a.n} dictations, ${Math.round(a.spoken_s/60)} min of speech`)
  :'<div class="tile"><div class="l">No dictations with timings yet</div></div>';
 chart(P.series);
 const b=P.bench;if(!b){$("bench").innerHTML='<div class="empty">No benchmark yet. Run <code>dictate bench</code> (plays your saved recordings through the real pipeline; a few minutes).</div>';return}
 const rows=Object.entries(b.modes).map(([m,v])=>{const x=v.summary;return `<tr><td><b>${esc(m)}</b> <span class="k">${esc(v.device||"")}</span></td><td>${fmt(x.ready_s_median)}</td><td>${fmt(x.ready_s_p90)}</td><td>${fmt(x.ready_s_short)}</td><td>${fmt(x.ready_s_medium)}</td><td>${fmt(x.ready_s_long)}</td><td>${fmt(x.ready_s_cold)}</td><td>${fmt(x.wpm_median,0)}</td><td>${fmt(x.eff_wpm_median,0)}</td><td>${x.wer_median==null?"–":(x.wer_median*100).toFixed(1)+"%"}</td></tr>`}).join("");
 $("bench").innerHTML=`<div class="row hint">Latest <code>dictate bench</code>: ${esc(b.ts.replace("T"," "))} · ${esc(b.machine||"")} · ${b.clips} recordings, ${Math.round(b.audio_s)} s. Times are stop → text ready (before typing), in seconds.</div>`+
  `<table><tr><th>mode</th><th>median</th><th>p90</th><th>&lt;10 s</th><th>10–30 s</th><th>&gt;30 s</th><th>cold start</th><th>wpm</th><th>effective wpm</th><th>WER</th></tr>${rows}</table>`}
function chart(ser){const svg=$("chart"),w=svg.clientWidth||600,h=140,pl=34,pr=8,pt=8,pb=18;$("n-chart").textContent=ser.length;
 if(!ser.length){svg.innerHTML="";return}
 const ys=ser.map(d=>d.stop_to_text_s||0),max=Math.max(0.5,...ys),step=max>4?2:max>2?1:max>1?0.5:0.25,top=Math.ceil(max/step)*step;
 const X=i=>pl+(ser.length<2?0:i*(w-pl-pr)/(ser.length-1)),Y=v=>pt+(h-pt-pb)*(1-v/top);
 let g="";for(let v=0;v<=top+1e-9;v+=step)g+=`<line x1="${pl}" x2="${w-pr}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)" stroke-width="1"/><text x="${pl-6}" y="${Y(v)+4}" text-anchor="end" font-size="11" fill="var(--mut)">${+v.toFixed(2)}</text>`;
 const pts=ys.map((v,i)=>`${X(i)},${Y(v)}`).join(" ");
 const last=ser.length-1;
 svg.setAttribute("viewBox",`0 0 ${w} ${h}`);
 svg.innerHTML=g+`<polygon points="${X(0)},${Y(0)} ${pts} ${X(last)},${Y(0)}" fill="var(--acc)" opacity=".1"/><polyline points="${pts}" fill="none" stroke="var(--acc)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`+
  `<circle cx="${X(last)}" cy="${Y(ys[last])}" r="4" fill="var(--acc)" stroke="var(--card)" stroke-width="2"/><text x="${X(last)-8}" y="${Y(ys[last])-8}" text-anchor="end" font-size="11" fill="var(--fg)">${fmt(ys[last])} s</text>`+
  `<line id="xh" y1="${pt}" y2="${h-pb}" stroke="var(--mut)" stroke-width="1" visibility="hidden"/><circle id="xd" r="4" fill="var(--acc)" stroke="var(--card)" stroke-width="2" visibility="hidden"/><rect x="${pl}" y="0" width="${w-pl-pr}" height="${h}" fill="transparent" id="hit"/>`;
 const tip=$("tip"),hit=svg.querySelector("#hit");
 hit.onmousemove=e=>{const r=svg.getBoundingClientRect(),x=(e.clientX-r.left)*w/r.width,i=Math.max(0,Math.min(last,Math.round((x-pl)/((w-pl-pr)/Math.max(1,last))))),d=ser[i];
  const xh=svg.querySelector("#xh"),xd=svg.querySelector("#xd");xh.setAttribute("x1",X(i));xh.setAttribute("x2",X(i));xh.setAttribute("visibility","visible");
  xd.setAttribute("cx",X(i));xd.setAttribute("cy",Y(ys[i]));xd.setAttribute("visibility","visible");
  tip.style.display="block";tip.innerHTML=`<b>${fmt(d.stop_to_text_s)} s</b> stop → text<br>${esc((d.ts||"").replace("T"," "))}<br>${fmt(d.audio_s,1)} s audio · ${d.words??"?"} words${d.wpm?" · "+fmt(d.wpm,0)+" wpm":""}${d.live_s?"<br>"+fmt(d.live_s,1)+" s transcribed while speaking":""}${d.app?"<br>"+esc(d.app):""}`;
  const tx=X(i)*r.width/w;tip.style.left=Math.min(r.width-tip.offsetWidth,Math.max(0,tx+14))+"px";tip.style.top="24px"};
 hit.onmouseleave=()=>{tip.style.display="none";svg.querySelector("#xh").setAttribute("visibility","hidden");svg.querySelector("#xd").setAttribute("visibility","hidden")}}
window.addEventListener("resize",()=>P&&chart(P.series));
/* ---------------- history ---------------- */
let HQ="",HT=null,editing=null;
$("hq").oninput=()=>{clearTimeout(HT);HT=setTimeout(()=>{HQ=$("hq").value;loadHist()},200)};
async function loadHist(){if(editing)return;try{const d=await (await fetch(`/api/history?limit=200&q=${encodeURIComponent(HQ)}&redact=${$("redact").checked?1:0}`)).json();renderHist(d)}catch(e){}}
function renderHist(d){$("n-hist").textContent=HQ?`${d.total} match${d.total===1?"":"es"}`:d.total;
 $("hist").innerHTML=d.items.length?d.items.map(h=>{const shown=h.corrected||h.text;
  return `<div class="hitem" data-ts="${esc(h.ts)}"><div class="htext">${esc(shown)||'<span class="k">(no text)</span>'}${h.corrected?`<div class="k mono">dictated: ${esc(h.text)}</div>`:""}</div>
  <div class="hmeta"><span>${esc((h.ts||"").replace("T"," "))}</span>${h.app?`<span>${esc(h.app)}${h.profile&&h.profile!=="default"?" · "+esc(h.profile):""}</span>`:""}
  <span>${fmt(h.audio_s,1)} s</span>${h.words!=null?`<span>${h.words} words${h.wpm?" · "+fmt(h.wpm,0)+" wpm":""}</span>`:""}
  ${h.stop_to_text_s!=null?`<span>stop→text ${fmt(h.stop_to_text_s)} s</span>`:""}${h.live_s?`<span>${fmt(h.live_s,1)} s live</span>`:""}
  ${h.note||h.error?`<span class="bad">${esc(h.error||h.note)}</span>`:""}
  ${h.text&&!$("redact").checked?`<button data-a="copy">Copy</button><button data-a="fix">Correct</button>`:""}${h.wav?`<button data-a="play" data-wav="${esc(h.wav)}">Play</button>`:""}</div></div>`}).join("")
  :`<div class="empty">${HQ?"No dictation matches.":"No dictations yet."}</div>`;
 $("hist").dataset.items=JSON.stringify(Object.fromEntries(d.items.map(h=>[h.ts,h.corrected||h.text])))}
$("hist").onclick=async e=>{const b=e.target.closest("button");if(!b)return;const it=b.closest(".hitem"),ts=it.dataset.ts,txt=JSON.parse($("hist").dataset.items||"{}")[ts]||"";
 if(b.dataset.a==="copy"){await navigator.clipboard.writeText(txt);b.textContent="Copied";setTimeout(()=>b.textContent="Copy",1200)}
 else if(b.dataset.a==="play"){let a=it.querySelector("audio");if(!a){a=document.createElement("audio");a.controls=true;a.src="/api/audio/"+b.dataset.wav+"?t="+TOKEN;it.appendChild(a)}a.play()}
 else if(b.dataset.a==="fix"){editing=ts;it.querySelector(".htext").innerHTML=`<textarea rows="2">${esc(txt)}</textarea><div class="hmeta"><button data-a="save" class="">Save correction</button><button data-a="cancel">Cancel</button><span>fix the words it got wrong; you will get suggestions to accept</span></div>`;it.querySelector("textarea").focus()}
 else if(b.dataset.a==="cancel"){editing=null;loadHist()}
 else if(b.dataset.a==="save"){const t=it.querySelector("textarea").value;const r=await (await fetch("/api/correct",{method:"POST",headers:{...H,"Content-Type":"application/json"},body:JSON.stringify({ts,text:t})})).json();
  editing=null;await loadHist();loadSugs();const n=(r.suggestions||[]).filter(s=>s.status!=="already").length;
  const m=document.createElement("div");m.className="hint";m.textContent=n?`${n} suggestion${n>1?"s":""} added above`:"Saved. Nothing to learn from it (only punctuation or added/removed words changed).";(document.querySelector(`.hitem[data-ts="${CSS.escape(ts)}"]`)||$("hist")).appendChild(m)}};
/* ---------------- suggestions ---------------- */
async function loadSugs(){try{const d=await (await fetch("/api/suggestions")).json();
 $("sugs").innerHTML=d.items.length?d.items.map(s=>`<div class="sug"><span class="pill">${s.kind==="vocab"?"vocabulary":"replacement"}</span><code>${esc(s.line)}</code>
  <span class="k">heard “${esc(s.heard)}”${s.count>1?` · corrected ${s.count}×`:""}</span><span class="sp"></span><button data-id="${s.id}" data-a="accept">Accept</button><button data-id="${s.id}" data-a="dismiss">Dismiss</button></div>`).join("")
  :'<div class="empty">None. Correct a dictation in History (or run <code>dictate fix "…"</code>) and the words it got wrong show up here.</div>'}catch(e){}}
$("sugs").onclick=async e=>{const b=e.target.closest("button");if(!b)return;b.disabled=true;
 await fetch("/api/suggestion",{method:"POST",headers:{...H,"Content-Type":"application/json"},body:JSON.stringify({id:+b.dataset.id,action:b.dataset.a})});loadSugs()};
$("copy").onclick=async()=>{const t=await (await fetch("/api/bundle")).text();await navigator.clipboard.writeText(t);$("copy").textContent="Copied";setTimeout(()=>$("copy").textContent="Copy diagnostics",1500)};
/* ---------------- live test ---------------- */
const TOKEN="__TOKEN__",H={"X-Token":TOKEN};
const T={state:"idle",t0:0,maxDb:-120,voiceUntil:0,bars:new Array(32).fill(0),peak:0,start:null,polling:false,rms:0};
$("gate").style.left=((20*Math.log10(0.025)+60)/60*100)+"%";
const GATE_DB=20*Math.log10(0.025);                       // the worker's speech gate (min_speech_rms 0.025)
function fft(re,im){const n=re.length;for(let i=1,j=0;i<n;i++){let b=n>>1;for(;j&b;b>>=1)j^=b;j^=b;if(i<j){[re[i],re[j]]=[re[j],re[i]];[im[i],im[j]]=[im[j],im[i]]}}
 for(let len=2;len<=n;len<<=1){const a=-2*Math.PI/len,wr=Math.cos(a),wi=Math.sin(a);for(let i=0;i<n;i+=len){let cr=1,ci=0;for(let k=0;k<len/2;k++){const x=i+k,y=x+len/2,tr=re[y]*cr-im[y]*ci,ti=re[y]*ci+im[y]*cr;re[y]=re[x]-tr;im[y]=im[x]-ti;re[x]+=tr;im[x]+=ti;const nc=cr*wr-ci*wi;ci=cr*wi+ci*wr;cr=nc}}}}
function analyse(b64){
 const N=1024,bin=atob(b64),u8=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)u8[i]=bin.charCodeAt(i);
 const s16=new Int16Array(u8.buffer,0,u8.length>>1),x=new Float32Array(N),off=N-s16.length;
 for(let i=0;i<s16.length;i++)if(off+i>=0)x[off+i]=s16[i]/32768;
 let sum=0,pk=0;for(const v of x){sum+=v*v;pk=Math.max(pk,Math.abs(v))}
 const rms=Math.sqrt(sum/N),re=new Float64Array(N),im=new Float64Array(N);
 for(let i=0;i<N;i++)re[i]=x[i]*(0.5-0.5*Math.cos(2*Math.PI*i/(N-1)));
 fft(re,im);const nb=T.bars.length,lo=100,hi=7600,binHz=16000/N,out=[];
 for(let b=0;b<nb;b++){const f0=lo*Math.pow(hi/lo,b/nb),f1=lo*Math.pow(hi/lo,(b+1)/nb);let m=0;
  for(let k=Math.max(1,Math.floor(f0/binHz));k<=Math.max(Math.floor(f1/binHz),Math.floor(f0/binHz));k++)m=Math.max(m,Math.hypot(re[k],im[k]));
  out.push(Math.min(1,Math.max(0,(20*Math.log10(m/256+1e-9)+85)/65)))}
 return {rms,peak:pk,bands:out}}
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim()}
function draw(){const c=$("spec"),r=window.devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;
 if(c.width!==w*r){c.width=w*r;c.height=h*r}const g=c.getContext("2d");g.setTransform(r,0,0,r,0,0);g.clearRect(0,0,w,h);
 const n=T.bars.length,bw=w/n;g.fillStyle=T.state==="listening"?css("--ok"):css("--mut");
 T.bars.forEach((v,i)=>{const bh=Math.max(2,v*(h-6));g.fillRect(i*bw+2,h-bh-3,bw-4,bh)});requestAnimationFrame(draw)}
function setState(s,label){T.state=s;$("tstate").textContent=label||s;$("go").textContent=s==="listening"?"Stop and transcribe":s==="busy"?"Transcribing…":"Start listening";
 $("go").className="big"+(s==="listening"?" rec":"");$("go").disabled=s==="busy"}
const IC={ok:'<span class="ok">✓</span>',fail:'<span class="bad">✗</span>',warn:'<span class="warn">!</span>',run:'<span class="k">…</span>'};
function stages(list){$("stages").innerHTML=list.map(s=>`<div class="stage"><span class="ic">${IC[s[0]]}</span><div><b>${esc(s[1])}</b> <span class="k">${esc(s[2]||"")}</span></div></div>`).join("")}
function showWarn(t){const w=$("warn");w.style.display=t?"":"none";w.className="row bad";w.textContent=t||""}
async function poll(){if(T.polling)return;T.polling=true;
 while(T.state==="listening"){try{const d=await (await fetch("/api/test/level",{headers:H})).json();
   if(d.msg){showWarn(d.msg);break}
   if(d.s!==undefined){const a=d.s?analyse(d.s):{rms:0,peak:0,bands:T.bars.map(()=>0)};
    T.bars=T.bars.map((o,i)=>Math.max(a.bands[i],o*0.82));T.rms=a.rms;
    const db=a.rms>0?20*Math.log10(a.rms):-120;T.maxDb=Math.max(T.maxDb,db);
    $("lvl").style.width=Math.min(100,Math.max(0,(db+60)/60*100))+"%";
    $("lvltxt").textContent=`input level ${db>-100?db.toFixed(0):"-∞"} dBFS · ${d.secs}s recorded`;
    if(db>GATE_DB-4)T.voiceUntil=Date.now()+400;
    const v=Date.now()<T.voiceUntil;$("voice").textContent=v?"● voice detected":"voice: quiet";$("voice").className="pill"+(v?" ok":"");
    if(d.secs>2.5&&T.maxDb<-52)showWarn("No sound is reaching the app. Check the microphone is the right/unmuted one (Windows: Settings > Privacy & security > Microphone), or speak louder.");
    else if(T.maxDb>=-52)showWarn("");}
  }catch(e){showWarn("lost connection to the dashboard: "+e);break}
  await new Promise(r=>setTimeout(r,80))}
 T.polling=false}
async function start(){
 setState("busy","starting…");showWarn("");T.maxDb=-120;T.bars=T.bars.map(()=>0);$("out").textContent="…";$("play").style.display="none";$("timing").textContent="";
 stages([["run","Opening the microphone"]]);
 let r;try{r=await (await fetch("/api/test/start",{method:"POST",headers:H})).json()}catch(e){r={ok:false,msg:String(e)}}
 if(!r.ok){setState("idle","idle");stages([["fail","Microphone / daemon",r.msg]]);$("out").textContent="Could not start: "+r.msg;return}
 T.start=r;setState("listening","listening");stages([["ok","Microphone opened",`${r.mic||"?"} · ${r.info||""}`],["run","Listening: speak now, then press Stop"]]);poll()}
async function stop(){
 setState("busy","transcribing…");const t0=Date.now();
 const tick=setInterval(()=>{$("tstate").textContent=`transcribing… ${((Date.now()-t0)/1000).toFixed(0)}s`},500);
 let r;try{r=await (await fetch("/api/test/stop",{method:"POST",headers:H})).json()}catch(e){r={error:"dashboard request failed: "+e}}
 clearInterval(tick);setState("idle","done");T.bars=T.bars.map(()=>0);result(r)}
function result(r){
 const L=[],mic=T.start||{};L.push(["ok","Microphone opened",`${r.mic||mic.mic||"?"} · ${r.info||mic.info||""}`]);
 const errs=(S&&S.errors||[]).slice(-6);let failed=false;
 if(!r.mic_bytes){L.push(["fail","Audio captured",`0 bytes: the mic opened but no audio arrived (blocks ${r.mic_blocks??0}${r.mic_status?", status "+r.mic_status:""}${r.mic_error?", error "+r.mic_error:""})`]);failed=true}
 else{const q=(r.peak!=null&&r.peak<0.01);L.push([q?"warn":"ok","Audio captured",`${r.audio_s??"?"} s, peak ${r.peak??"?"}, ${r.mic_blocks} blocks${q?"  (very quiet: wrong mic or muted?)":""}${r.mic_error?"  callback error: "+r.mic_error:""}`])}
 if(!failed){
  if(r.error){L.push(["fail","Speech model / transcription",r.error]);failed=true}
  else if(r.note&&String(r.note).startsWith("too short")||r.msg==="too short"){L.push(["warn","Speech model","recording under 0.4 s: hold it longer"])}
  else{L.push(["ok","Speech model",`${r.device||"?"}${r.load_s!=null?" · loaded in "+r.load_s+" s":""}`]);
   if(r.text)L.push(["ok","Transcribed",`${r.asr_s??"?"} s for ${r.audio_s} s of audio`]);
   else L.push(["warn","Transcribed","no speech detected (peak "+(r.peak??"?")+", rms "+(r.rms??"?")+"): too quiet, or only noise"])}}
 if(failed&&errs.length)L.push(["fail","Last lines of errors.log",errs.join("  |  ")]);
 stages(L);
 $("out").textContent=r.text||(failed?"(failed: see the red step above)":"(no text)");
 const bits=[];if(r.raw&&r.raw!==r.text)bits.push("raw: "+r.raw);if(r.job_s!=null)bits.push("job "+r.job_s+" s");if(r.dropped)bits.push(r.dropped+" phantom phrase(s) dropped");
 $("timing").textContent=bits.join(" · ");
 if(r.wav){const p=$("play");p.src="/api/audio/"+r.wav+"?t="+TOKEN;p.style.display="";$("timing").textContent+=" · play it back to hear what the mic captured"}}
$("go").onclick=()=>{if(T.state==="idle")start();else if(T.state==="listening")stop()};
function showHotkey(){if(!S||!S.hotkey)return;const h=S.hotkey;$("hk").textContent=h.keys||"none";
 const ok=h.status==="active"||h.status==="set";$("hkst").textContent=h.status;$("hkst").className="pill "+(ok?"ok":h.status==="failed"||h.status==="missing"?"bad":"warn");$("hkdetail").textContent=h.detail}
const _render=render;render=function(){_render();showHotkey();if(S&&!S.daemon.reachable&&T.state==="idle"){$("go").disabled=true;$("tstate").textContent="daemon down"}else if(T.state==="idle"){$("go").disabled=false;if($("tstate").textContent==="daemon down")$("tstate").textContent="idle"}};
draw();
setInterval(()=>{if($("live").checked)load()},2000);load();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8765); ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H)
    except OSError as e:
        sys.exit(f"cannot listen on 127.0.0.1:{a.port}: {e}  (try --port N)")
    url = f"http://127.0.0.1:{a.port}"
    print(f"Dictate dashboard: {url}   (state: {STATE})   Ctrl+C to stop")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
