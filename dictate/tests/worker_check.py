"""Run saved recordings through worker.py; show what the hallucination guard dropped vs kept."""
import glob, json, subprocess, sys, tempfile, wave, os
from pathlib import Path
here = Path(__file__).resolve().parent.parent
files = sorted(glob.glob(str(Path.home() / ".local/state/dictate/audio/*.wav")))
env = dict(os.environ, DICTATE_CFG=json.dumps({}))
p = subprocess.Popen([sys.executable, str(here / "worker.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
print("ready:", p.stdout.readline().strip()[:160])
tot_drop = 0
for i, f in enumerate(files):
    w = wave.open(f); assert w.getframerate() == 16000 and w.getnchannels() == 1
    raw = w.readframes(w.getnframes()); tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".raw"); tmp.write(raw); tmp.close()
    p.stdin.write(json.dumps({"job": i, "pcm": tmp.name}) + "\n"); p.stdin.flush()
    r = json.loads(p.stdout.readline()); os.unlink(tmp.name)
    if r.get("error"): print(Path(f).stem[11:], "ERROR", r["error"]); continue
    tot_drop += len(r["dropped"])
    if r["dropped"] or r["audio_s"] < 3:
        print(f"\n{Path(f).stem[11:]} {r['audio_s']}s  KEPT: {r['text'][:110]!r}")
        for d in r["dropped"]: print(f"     DROPPED {d['text']!r:42} {d['why']}")
print("\nclips:", len(files), "| segments dropped:", tot_drop)
p.stdin.write(json.dumps({"cmd": "quit"}) + "\n"); p.stdin.flush()
