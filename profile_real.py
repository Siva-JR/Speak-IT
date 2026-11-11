#!/usr/bin/env python3
"""Profile candidates on your REAL saved dictations (~/.local/state/dictate/audio).
  .venv/bin/python profile_real.py [--only name ...] [--repeats 3]"""
import argparse, datetime as dt, json, shutil, subprocess, sys, time
from pathlib import Path
import pandas as pd
from run import wait_cool
ROOT = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--audio-dir", default=str(Path.home() / ".local/state/dictate/audio"))
ap.add_argument("--repeats", type=int, default=3); ap.add_argument("--only", nargs="*")
a = ap.parse_args()
FW = {"engine": "faster_whisper", "threads": 4, "beam_size": 5}
CPP = {"engine": "whisper_cpp", "model": "models/ggml-base.en-q5_1.bin", "beam_size": 1, "audio_ctx": 768, "vad": True, "threads": 4}
C = [dict(FW, name="fw-int8-t4 (current)"), dict(FW, name="fw-int8-t3", threads=3), dict(FW, name="fw-int8-t2", threads=2),
     dict(FW, name="fw-int8-t4-beam1", beam_size=1),
     dict(CPP, name="cpp-q5-ctx768-t4"), dict(CPP, name="cpp-q5-ctx768-t2", threads=2)]
if a.only: C = [c for c in C if c["name"] in a.only]
out = ROOT / "results" / ("realprofile-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")); out.mkdir(parents=True)
rows = []
for c in C:
    print("profiling", c["name"], flush=True)
    wait_cool(60, 150)
    d = out / c["name"].split()[0]
    r = subprocess.run([sys.executable, "-m", "bench.profile_real", "--candidate", json.dumps(c),
                        "--audio-dir", a.audio_dir, "--out", str(d), "--repeats", str(a.repeats)], cwd=ROOT)
    if r.returncode: print("  FAILED", c["name"]); continue
    time.sleep(10)
print("results:", out)
