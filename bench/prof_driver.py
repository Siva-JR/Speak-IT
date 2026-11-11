"""Driver for perf / py-spy / memray: load the real worker host, then run N dictations."""
import glob, os, sys, tempfile, time, wave, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dictate"))
n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
kind = sys.argv[2] if len(sys.argv) > 2 else "short"
import worker as W
A = sorted(glob.glob(str(Path.home() / ".local/state/dictate/audio/*.wav")))
def dur(f): w = wave.open(f); return w.getnframes() / w.getframerate()
files = [f for f in A if 3 < dur(f) < 5.6][:5] if kind == "short" else [f for f in A if dur(f) > 55][:1]
raws = []
for f in files:
    w = wave.open(f); t = Path(tempfile.mkdtemp()) / "x.raw"; t.write_bytes(w.readframes(w.getnframes())); raws.append(str(t))
h = W.Host(); h.job(raws[0]); h.job(raws[0])        # load + warm up
print("READY", flush=True); time.sleep(1.0)
t0 = time.time()
for i in range(n): h.job(raws[i % len(raws)])
print(f"done {n} jobs in {time.time()-t0:.1f}s", flush=True)
