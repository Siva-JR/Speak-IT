"""Runs ONE candidate in its own process (clean memory / cold-start numbers)."""
import argparse
import json
import statistics as st
import time
from pathlib import Path

import jiwer
import numpy as np
import pandas as pd
import psutil

from bench.audio import load_audio
from bench.engines import make
from bench.sysmon import SysMon
from bench.textnorm import normalize


def model_size_mb(model):
    from bench.engines import resolve_model
    p = Path(resolve_model(model))
    if p.is_dir():
        return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6, 1)
    return round(p.stat().st_size / 1e6, 1) if p.exists() else None


def pct(xs, q):
    return float(np.percentile(xs, q)) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True, help="JSON candidate config")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--profile", action="store_true", help="cProfile the transcription loop")
    a = ap.parse_args()

    cand = json.loads(a.candidate)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    items = [json.loads(l) for l in open(a.manifest) if l.strip()]
    if a.limit:
        items = items[: a.limit]
    for it in items:
        it["audio"] = load_audio(it["path"])
        it["dur"] = len(it["audio"]) / 16000

    mon = SysMon(a.interval)
    mon.start()
    proc = psutil.Process()
    rss_before = proc.memory_info().rss / 1e6

    mon.mark("load_start")
    t = time.perf_counter()
    eng = make(cand)
    eng.load()
    load_s = time.perf_counter() - t
    mon.mark("load_end")
    rss_after_load = proc.memory_info().rss / 1e6

    # Warm-up (first call is usually slower: lazy init, page faults, caches)
    first_inference_s = None
    mon.mark("warmup_start")
    for i in range(a.warmup):
        t = time.perf_counter()
        eng.transcribe(items[0]["audio"])
        if i == 0:
            first_inference_s = time.perf_counter() - t
    mon.mark("warmup_end")

    prof = None
    if a.profile:
        import cProfile
        prof = cProfile.Profile()
        prof.enable()

    rows = []
    mon.mark("bench_start")
    for r in range(a.repeats):
        for it in items:
            mon.mark(f"clip:{it['id']}:r{r}")
            t = time.perf_counter()
            text, ttft = eng.transcribe(it["audio"])
            lat = time.perf_counter() - t
            rows.append({
                "repeat": r, "id": it["id"], "category": it["category"], "dur_s": it["dur"],
                "latency_s": lat, "ttft_s": ttft, "rtf": lat / it["dur"],
                "ref": it["ref"], "hyp": text,
            })
    mon.mark("bench_end")
    if prof:
        prof.disable()
        prof.dump_stats(str(out / "profile.prof"))
    mon.stop()
    rss_peak = max((r["proc_rss_mb"] for r in mon.rows), default=None)

    df = pd.DataFrame(rows)
    df.to_json(out / "transcripts.jsonl", orient="records", lines=True)
    pd.DataFrame(mon.rows).to_csv(out / "timeseries.csv", index=False)
    json.dump(mon.events, open(out / "events.json", "w"))

    # ---- accuracy (repeat 0; WER only on clips that have a reference) ----
    r0 = df[df.repeat == 0]
    spoken = r0[r0.ref.str.strip() != ""]
    def wer_of(d):
        if d.empty:
            return None
        refs = [normalize(x) for x in d.ref]
        hyps = [normalize(x) for x in d.hyp]
        keep = [i for i, x in enumerate(refs) if x]
        return float(jiwer.wer([refs[i] for i in keep], [hyps[i] for i in keep]))
    wer_by_cat = {c: wer_of(g) for c, g in spoken.groupby("category")}
    silent = r0[r0.ref.str.strip() == ""]
    halluc_words = int(sum(len(normalize(h).split()) for h in silent.hyp))
    consistent = bool(df.groupby("id").hyp.nunique().max() == 1)

    lat = df.latency_s.tolist()
    sp = df[df.ref.str.strip() != ""]
    summary = {
        "name": cand["name"], "config": cand,
        "n_clips": len(items), "repeats": a.repeats, "audio_total_s": round(sum(i["dur"] for i in items), 1),
        "wer": wer_of(spoken), "wer_by_category": wer_by_cat,
        "hallucinated_words_on_silence": halluc_words, "output_deterministic": consistent,
        "latency_p50_s": pct(lat, 50), "latency_p95_s": pct(lat, 95), "latency_mean_s": float(np.mean(lat)),
        "ttft_p50_s": pct(df.ttft_s.tolist(), 50), "ttft_p95_s": pct(df.ttft_s.tolist(), 95),
        "rtf_overall": float(df.latency_s.sum() / df.dur_s.sum()),
        "rtf_p50": pct(df.rtf.tolist(), 50), "rtf_p95": pct(df.rtf.tolist(), 95),
        "rtf_speech_only": float(sp.latency_s.sum() / sp.dur_s.sum()) if len(sp) else None,
        "load_time_s": load_s, "first_inference_s": first_inference_s,
        "rss_before_mb": rss_before, "rss_after_load_mb": rss_after_load, "rss_peak_mb": rss_peak,
        "model_file_mb": model_size_mb(cand["model"]),
        "gpu_telemetry_available": mon.gpu_available,
        "sys": mon.summary(),
    }
    json.dump(summary, open(out / "summary.json", "w"), indent=2)
    print(json.dumps({k: summary[k] for k in ("name", "wer", "rtf_overall", "latency_p50_s", "ttft_p50_s", "rss_peak_mb")}))


if __name__ == "__main__":
    main()
