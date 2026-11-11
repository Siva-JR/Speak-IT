#!/usr/bin/env python3
"""Timeline graphs of the REAL worker pipeline: CPU cores busy, memory (RSS/USS), frequency, temperature,
with processing stages shaded. Output: results/graphs-<ts>/*.png + timeline.csv
  .venv/bin/python profile_graphs.py [--threads 4]"""
import argparse, datetime as dt, json, os, sys, tempfile, time, wave, glob
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument("--threads", type=int, default=4); ap.add_argument("--tag", default="")
a = ap.parse_args()
os.environ["DICTATE_CFG"] = json.dumps({"threads": a.threads})
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "dictate"))
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
from bench.sysmon import SysMon
import worker as W
import faster_whisper.transcribe as ft, faster_whisper.feature_extractor as fe

mon = SysMon(0.02); intervals = []          # (label, t0, t1)
def timed(fn, label):
    def w(*x, **k):
        t = time.perf_counter() - mon.t0
        try: return fn(*x, **k)
        finally: intervals.append((label, t, time.perf_counter() - mon.t0))
    return w
class Prox:
    def __init__(self, o): self._o = o
    def __getattr__(self, n):
        f = getattr(self._o, n); return timed(f, n) if n in ("encode", "generate", "align") else f
ft.get_speech_timestamps = timed(ft.get_speech_timestamps, "vad")
fe.FeatureExtractor.__call__ = timed(fe.FeatureExtractor.__call__, "features")

A = sorted(glob.glob(str(Path.home() / ".local/state/dictate/audio/*.wav")))
def dur(f): w = wave.open(f); return w.getnframes() / w.getframerate()
shorts = [f for f in A if 3 < dur(f) < 5.6][:6]; l28 = [f for f in A if 27 < dur(f) < 31][0]; l60 = [f for f in A if dur(f) > 55][0]
seq = [("short", f) for f in shorts[:5]] + [("28 s", l28), ("60 s", l60)] + [("short", f) for f in shorts[:3]]

out = ROOT / "results" / ("graphs-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{a.tag}" if a.tag else ""))
out.mkdir(parents=True)
mon.start(); time.sleep(0.5)
t = time.perf_counter() - mon.t0; host = W.Host(); intervals.append(("model load", t, time.perf_counter() - mon.t0))
host.model.model = Prox(host.model.model); time.sleep(0.5)
jobs = []
for label, f in seq:
    w = wave.open(f); tmp = Path(tempfile.mkdtemp()) / "x.raw"; tmp.write_bytes(w.readframes(w.getnframes()))
    t0 = time.perf_counter() - mon.t0; r = host.job(str(tmp)); t1 = time.perf_counter() - mon.t0
    jobs.append((label, round(dur(f), 1), t0, t1)); time.sleep(0.6)
time.sleep(2.0); mon.stop()
df = pd.DataFrame(mon.rows); df.to_csv(out / "timeline.csv", index=False)
json.dump({"intervals": intervals, "jobs": jobs}, open(out / "events.json", "w"))

COL = {"model load": "#999999", "vad": "#4c78a8", "features": "#72b7b2", "encode": "#e45756", "generate": "#f58518", "align": "#b279a2"}
def draw(xlim, name, title):
    fig, ax = plt.subplots(3, 1, figsize=(13, 8.5), sharex=True, gridspec_kw={"height_ratios": [2.2, 2, 1.3]})
    for lab, s, e in intervals:
        if e < xlim[0] or s > xlim[1]: continue
        for x in ax: x.axvspan(s, e, color=COL[lab], alpha=0.22 if lab != "model load" else 0.15, lw=0)
    d = df[(df.t >= xlim[0]) & (df.t <= xlim[1])]
    ax[0].plot(d.t, d.proc_cpu_pct / 100, color="black", lw=1); ax[0].set_ylabel("CPU cores busy (process)")
    ax[0].axhline(a.threads, color="gray", ls=":", lw=0.8)
    ax[1].plot(d.t, d.proc_rss_mb, label="RSS (incl. shared libs)", color="#1f77b4", lw=1.2)
    u = d.dropna(subset=["proc_uss_mb"]); ax[1].plot(u.t, u.proc_uss_mb, label="USS (private)", color="#2ca02c", lw=1.2); ax[1].legend(loc="upper right")
    ax[1].set_ylabel("memory (MB)")
    ax[2].plot(d.t, d.cpu_freq_mhz, color="#d62728", lw=1); ax[2].set_ylabel("CPU MHz", color="#d62728")
    b = ax[2].twinx(); b.plot(d.t, d.cpu_pkg_temp_c, color="#9467bd", lw=1); b.set_ylabel("pkg temp °C", color="#9467bd")
    for lab, du, s, e in jobs:
        if e < xlim[0] or s > xlim[1]: continue
        ax[0].text(s, ax[0].get_ylim()[1] * 0.97, f"{lab}\n{e - s:.2f}s", fontsize=7, va="top")
    ax[2].set_xlabel("time (s)"); ax[2].set_xlim(*xlim)
    h = [plt.Rectangle((0, 0), 1, 1, color=c, alpha=0.4) for c in COL.values()]; ax[0].legend(h, COL.keys(), ncol=6, loc="upper center", fontsize=8, bbox_to_anchor=(0.5, 1.18))
    fig.suptitle(title, y=0.995); fig.tight_layout(); fig.savefig(out / name, dpi=110); plt.close(fig)
T = df.t.max()
draw((0, T), "1_whole_session.png", f"Whole session, {a.threads} threads: load, 5 short, 28 s, 60 s, 3 short dictations")
j = [x for x in jobs if x[0] == "short"][1]; draw((j[2] - 0.5, j[3] + 0.8), "2_one_short_dictation.png", f"One short dictation ({j[1]} s of audio)")
j = [x for x in jobs if x[0] == "60 s"][0]; draw((j[2] - 0.5, j[3] + 0.8), "3_one_60s_dictation.png", "One 60 s dictation (cut into chunks)")
draw((0, [x for x in intervals if x[0] == "model load"][0][2] + 1.5), "4_model_load.png", "Model load and first moments")
print(out)
s = pd.DataFrame([{"stage": l, "total_s": e - s} for l, s, e in intervals]).groupby("stage").total_s.sum().round(2).to_dict()
print("stage time (s):", s); print("jobs:", [(l, d, round(e - s, 2)) for l, d, s, e in jobs])
