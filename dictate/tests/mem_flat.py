"""Memory-flatness test of the real daemon+worker (separate test instance, output=print, no typing).
Sequence: idle -> warm -> 10 short -> 2 long -> 10 short -> 60s again -> 5 short -> wait for linger -> idle -> 2nd cycle."""
import glob, json, os, subprocess, sys, tempfile, time, wave
from pathlib import Path
here = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="dictate-memtest-"))
env = dict(os.environ, DICTATE_SOCK=str(tmp / "s.sock"), DICTATE_STATE=str(tmp / "state"),
           DICTATE_CFG=json.dumps({"output": "print", "tray": False, "notify": False, "sounds": False,
                                   "save_audio": False, "worker_linger_s": 10}))
daemon = subprocess.Popen([sys.executable, str(here / "dictate.py"), "serve"], env=env, stdout=subprocess.DEVNULL)
time.sleep(1.5)
def cli(*a):
    return subprocess.run([sys.executable, str(here / "dictate.py"), *a], env=env, capture_output=True, text=True).stdout.strip()
def mem(): return json.loads(cli("memstat"))
A = sorted(glob.glob(str(Path.home() / ".local/state/dictate/audio/*.wav")))
def dur(f):
    w = wave.open(f); return w.getnframes() / w.getframerate()
shorts = [f for f in A if 2.5 < dur(f) < 6][:12]; long60 = [f for f in A if dur(f) > 55][0]; long28 = [f for f in A if 27 < dur(f) < 31][0]
hist = tmp / "state" / "history.jsonl"
def last_job():
    return json.loads(hist.read_text().splitlines()[-1]) if hist.exists() else {}
rows = []
def snap(label):
    m = mem(); w = m.get("worker"); d = m["daemon"]; j = last_job() if label.startswith("job") else {}
    rows.append((label, d["uss_mb"], w["uss_mb"] if w else 0, w["rss_mb"] if w else 0, m["total_uss_mb"], j.get("peak_rss_mb_during_job", ""), j.get("audio_s", "")))
snap("idle (no worker)")
print("warm:", cli("warm")); snap("worker loaded (just warmed)")
for i in range(10): cli("replay", shorts[i % len(shorts)]); snap(f"job short #{i+1}")
cli("replay", long28); snap("job 28s dictation")
cli("replay", long60); snap("job 60s dictation (2 chunks)")
for i in range(10): cli("replay", shorts[i % len(shorts)]); snap(f"job short #{i+11}")
cli("replay", long60); snap("job 60s dictation again")
for i in range(5): cli("replay", shorts[i]); snap(f"job short #{i+21}")
print("waiting for worker linger to expire..."); time.sleep(14); snap("after linger (worker exited)")
cli("replay", shorts[0]); snap("job: cycle 2, first dictation (cold start)")
for i in range(3): cli("replay", shorts[i + 1]); snap(f"job cycle2 short #{i+2}")
cli("replay", long60); snap("job cycle2 60s dictation")
cli("replay", shorts[0]); snap("job cycle2 short after 60s")
time.sleep(14); snap("after linger (cycle 2)")
print(f"\n{'state':44}{'daemon USS':>11}{'worker USS':>11}{'worker RSS':>11}{'TOTAL USS':>10}{'job peak RSS':>13}{'audio s':>8}")
for r in rows: print(f"{r[0]:44}{r[1]:>11}{r[2]:>11}{r[3]:>11}{r[4]:>10}{str(r[5]):>13}{str(r[6]):>8}")
cli("quit"); daemon.wait(timeout=5)
