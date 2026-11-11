#!/usr/bin/env python3
"""Dictate dashboard: one page with every log, error, check and timing, for debugging on Linux, macOS and Windows.

  python dashboard.py [--port 8765] [--no-browser]

Standard library only, never imports dictate.py (which is Linux-only), so it runs even when the daemon cannot start.
Reads the state dir (events.log, errors.log, history.jsonl) and serves http://127.0.0.1:PORT (local only, read-only).
"""
import argparse, http.server, importlib.util, json, os, platform, shutil, socket, sys, time, tomllib, webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import STATE, CONF_DIR as CONF, OS_NAME, ctl_request
MODEL = HERE.parent / "models" / "faster-whisper-base.en"
BAD_EVENTS = {"job_error", "type_fail", "portal_down", "deliver_fallback", "screen_watch_error",
              "portal_session_lost", "worker_restart"}
OS = OS_NAME


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


def history(n, redact):
    out = []
    for l in tail_lines(STATE / "history.jsonl", n):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        keep = {k: r.get(k) for k in ("ts", "audio_s", "peak", "rms", "job_s", "asr_s", "stop_to_text_s", "deliver_s",
                                      "output", "chunks", "worker_load_s", "note", "error", "peak_rss_mb_during_job")}
        keep["text"] = f"<{len(r.get('text', ''))} chars>" if redact else r.get("text", "")
        keep["raw"] = "" if redact else r.get("raw", "")
        keep["dropped"] = len(r.get("dropped") or [])
        out.append(keep)
    return out


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


def snapshot(redact=False, n_events=300, n_err=200, n_hist=30):
    events = [parse_event(l) for l in tail_lines(STATE / "events.log", n_events)]
    return {"system": system(), "daemon": daemon_status(), "checks": checks(), "config": config(), "events": events,
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

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):   # blocks DNS-rebinding
            return self.send(403, "forbidden", "text/plain")
        if u.path == "/":
            self.send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self.send(200, json.dumps(snapshot(redact=q.get("redact", ["0"])[0] == "1")), "application/json")
        elif u.path == "/api/bundle":
            self.send(200, bundle_text(), "text/plain; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="dictate-diagnostics-{OS}.txt"'})
        else:
            self.send(404, "not found", "text/plain")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dictate Dashboard</title><style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2128;--mut:#6b7380;--line:#e3e6ea;--ok:#1a7f4b;--warn:#a15c00;--bad:#c62828;--bg2:#eef0f3}
@media(prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1b1f24;--fg:#e6e9ed;--mut:#8b94a1;--line:#2a3038;--ok:#4cc38a;--warn:#e0a030;--bad:#f2706b;--bg2:#232830}}
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
</style></head><body>
<header><h1>Dictate Dashboard</h1><span id="pills"></span><span class="sp"></span>
<label><input type="checkbox" id="live" checked> live</label><label><input type="checkbox" id="redact"> hide dictated text</label>
<button id="copy">Copy diagnostics</button><a href="/api/bundle"><button>Download</button></a></header>
<main>
<section id="s-prob"><h2>Problems <span class="n" id="n-prob"></span></h2><div class="body" id="prob"></div></section>
<div class="two">
<section><h2>Checks</h2><div class="body" id="checks"></div></section>
<section><h2>System</h2><div class="body mono row" id="sys"></div></section></div>
<section><h2>Recent dictations <span class="n" id="n-hist"></span></h2><div class="body" id="hist"></div></section>
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
 $("n-hist").textContent=S.history.length;
 $("hist").innerHTML=S.history.length?"<table><tr><th>time</th><th>text</th><th>audio</th><th>asr</th><th>job</th><th>stop→text</th><th>output</th><th></th></tr>"+S.history.slice().reverse().map(h=>
  `<tr><td class="k">${esc((h.ts||"").slice(11,19))}</td><td>${esc(h.text)}${h.raw&&h.raw!==h.text?`<div class="k mono">raw: ${esc(h.raw)}</div>`:""}</td><td>${h.audio_s??""}s</td><td>${h.asr_s??""}</td><td>${h.job_s??""}</td><td>${h.stop_to_text_s??""}</td><td>${esc(h.output||"")}</td><td class="bad">${esc(h.error||h.note||"")}</td></tr>`).join("")+"</table>":'<div class="empty">No dictations yet.</div>';
 const f=$("q").value.toLowerCase(),ev=S.events.filter(e=>!f||(e.kind+" "+e.rest).toLowerCase().includes(f));
 $("n-ev").textContent=S.events.length;
 keepScroll("ev",ev.map(e=>`<div class="row ${e.bad?"bad":""}"><span class="k">${esc(e.t.slice(11))}</span> <b>${esc(e.kind)}</b> ${esc(e.rest)}</div>`).join("")||'<div class="empty">No events.</div>');
 $("n-err").textContent=S.errors.length||"";
 keepScroll("err",S.errors.length?S.errors.map(l=>`<div class="row ${/Error|Traceback|Exception|failed|fatal/i.test(l)?"bad":""}">${esc(l)}</div>`).join(""):'<div class="empty">errors.log is empty.</div>');
 $("cfg").textContent=JSON.stringify(S.config,null,2)}
async function load(){try{S=await (await fetch("/api/state?redact="+($("redact").checked?1:0))).json();render()}
 catch(e){$("pills").innerHTML='<span class="pill bad">dashboard lost connection</span>'}}
$("q").oninput=render;$("redact").onchange=load;
$("copy").onclick=async()=>{const t=await (await fetch("/api/bundle")).text();await navigator.clipboard.writeText(t);$("copy").textContent="Copied";setTimeout(()=>$("copy").textContent="Copy diagnostics",1500)};
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
