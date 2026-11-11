"""Profile ONE candidate on real recorded dictations: per-stage time, CPU time, memory, idle/unload/reload."""
import argparse, gc, json, sys, time, tempfile, ctypes
from pathlib import Path
import numpy as np, psutil, pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "dictate"))
from bench.audio import load_audio
from bench.sysmon import SysMon

STAGE = {k: 0.0 for k in ("vad", "features", "encode", "generate", "align")}


def timed(fn, key):
    def w(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            STAGE[key] += time.perf_counter() - t
    return w


class Prox:                       # times the CTranslate2 calls
    def __init__(self, o): self._o = o
    def __getattr__(self, n):
        f = getattr(self._o, n)
        return timed(f, {"encode": "encode", "generate": "generate", "align": "align"}[n]) \
            if n in ("encode", "generate", "align") else f


def snap(proc):
    m = proc.memory_full_info()
    return {"rss_mb": round(m.rss / 1e6, 1), "pss_mb": round(m.pss / 1e6, 1), "uss_mb": round(m.uss / 1e6, 1)}


def trim():
    gc.collect()
    try: ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception: pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True); ap.add_argument("--audio-dir", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args()
    cand = json.loads(a.candidate); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    clips = sorted(Path(a.audio_dir).glob("*.wav"))
    audios = [(c.stem, load_audio(str(c))) for c in clips]
    proc = psutil.Process()
    mon = SysMon(0.05); mon.start()
    st = {"process_start": snap(proc)}
    cpu0, wall0 = time.process_time(), time.perf_counter()

    if cand["engine"] == "faster_whisper":
        import dictate as d
        import faster_whisper.transcribe as ft
        import faster_whisper.feature_extractor as fe
        d.DEFAULTS.update(tray=False, notify=False, sounds=False, save_audio=False, output="print",
                          live_typing=False, threads=cand.get("threads", 4), beam_size=cand.get("beam_size", 5))
        d.STATE = Path(tempfile.mkdtemp())
        ft.get_speech_timestamps = timed(ft.get_speech_timestamps, "vad")
        fe.FeatureExtractor.__call__ = timed(fe.FeatureExtractor.__call__, "features")
        dm = d.Daemon()

        def load():
            dm.model = None; dm.ensure_model(); dm.model.model = Prox(dm.model.model)
        def run(x):
            return dm.process(x, time.perf_counter(), dry=True)
        def unload():
            dm.model = None
    else:
        import bench.engines as E
        E.vad_trim = timed(E.vad_trim, "vad")
        eng = {"e": None}
        def load():
            eng["e"] = E.make(cand); eng["e"].load()
        def run(x):
            return eng["e"].transcribe(x)[0]
        def unload():
            eng["e"].unload()

    mon.mark("load")
    t = time.perf_counter(); c = time.process_time(); load()
    load_s, load_cpu = time.perf_counter() - t, time.process_time() - c
    st["after_load"] = snap(proc)
    for _ in range(2): run(audios[0][1])                       # warm-up
    st["after_warmup"] = snap(proc)

    rows = []
    mon.mark("bench")
    for r in range(a.repeats):
        for name, x in audios:
            for k in STAGE: STAGE[k] = 0.0
            c = time.process_time(); t = time.perf_counter()
            text = run(x)
            wall = time.perf_counter() - t; cpu = time.process_time() - c
            rows.append({"repeat": r, "clip": name, "audio_s": len(x) / 16000, "wall_s": wall, "cpu_s": cpu,
                         **{f"t_{k}": v for k, v in STAGE.items()}, "text": text if r == 0 else ""})
    mon.mark("bench_end")
    st["after_bench"] = snap(proc)

    # idle behaviour: unload, then measure memory and CPU while doing nothing
    mon.mark("unload"); unload(); trim(); st["after_unload"] = snap(proc)
    c = time.process_time(); t = time.perf_counter(); time.sleep(10)
    idle_cpu_pct = (time.process_time() - c) / (time.perf_counter() - t) * 100
    st["idle_10s_after_unload"] = snap(proc)
    mon.mark("reload"); t = time.perf_counter(); c = time.process_time(); load()
    reload_s = time.perf_counter() - t
    t = time.perf_counter(); run(audios[0][1]); first_after_reload = time.perf_counter() - t
    st["after_reload_first_run"] = snap(proc)
    mon.stop()

    df = pd.DataFrame(rows); df.to_json(out / "utterances.jsonl", orient="records", lines=True)
    pd.DataFrame(mon.rows).to_csv(out / "timeseries.csv", index=False)
    short, long_ = df[df.audio_s < 5], df[df.audio_s > 15]
    other = df.wall_s - df[[c for c in df if c.startswith("t_")]].sum(axis=1)
    tot = df.wall_s.sum()
    summ = {
        "name": cand["name"], "config": cand, "n_clips": len(audios), "repeats": a.repeats,
        "audio_total_s": round(sum(len(x) for _, x in audios) / 16000, 1),
        "load_s": round(load_s, 3), "load_cpu_s": round(load_cpu, 3), "reload_s": round(reload_s, 3),
        "first_run_after_reload_s": round(first_after_reload, 3),
        "lat_p50_s": round(df.wall_s.median(), 3), "lat_p95_s": round(df.wall_s.quantile(.95), 3),
        "lat_short_p50_s": round(short.wall_s.median(), 3) if len(short) else None,
        "lat_long_p50_s": round(long_.wall_s.median(), 3) if len(long_) else None,
        "rtf": round(df.wall_s.sum() / df.audio_s.sum(), 3),
        "cpu_s_per_audio_s": round(df.cpu_s.sum() / df.audio_s.sum(), 3),
        "cores_busy_avg": round(df.cpu_s.sum() / df.wall_s.sum(), 2),
        "cpu_s_per_utt_median": round(df.cpu_s.median(), 2),
        "stage_pct": {k: round(df[f"t_{k}"].sum() / tot * 100, 1) for k in STAGE} | {"other": round(other.sum() / tot * 100, 1)},
        "mem": st, "idle_cpu_pct_10s": round(idle_cpu_pct, 2),
        "sys": mon.summary(),
    }
    json.dump(summ, open(out / "profile.json", "w"), indent=2)
    print(json.dumps({k: summ[k] for k in ("name", "lat_p50_s", "rtf", "cpu_s_per_audio_s", "cores_busy_avg")}))


if __name__ == "__main__":
    main()
