#!/usr/bin/env python3
"""Streaming benchmark.
  .venv/bin/python run_stream.py                       # all stream candidates in configs/streaming.yaml
  .venv/bin/python run_stream.py --limit-per-cat 3     # quick smoke test
"""
import argparse
import datetime as dt
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

import jiwer
import numpy as np
import pandas as pd
import yaml

from bench.audio import load_audio
from bench.engines import make
from bench.stream import simulate
from bench.textnorm import normalize
from run import wait_cool

ROOT = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--config", default="configs/streaming.yaml")
ap.add_argument("--manifest", default="data/manifest.jsonl")
ap.add_argument("--limit-per-cat", type=int, default=12)
ap.add_argument("--repeats", type=int, default=1)
ap.add_argument("--only", nargs="*")
ap.add_argument("--tag", default="")
a = ap.parse_args()

cfg = yaml.safe_load(open(a.config))
d = cfg.get("defaults", {})
items = [json.loads(l) for l in open(a.manifest) if l.strip()]
cnt, sel = defaultdict(int), []
for it in items:
    if cnt[it["category"]] < a.limit_per_cat or it["category"] in ("custom", "silence", "noise"):
        cnt[it["category"]] += 1
        sel.append(it)
for it in sel:
    it["audio"] = load_audio(it["path"])

out = ROOT / "results" / ("stream-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{a.tag}" if a.tag else ""))
out.mkdir(parents=True)
json.dump(cfg, open(out / "config.json", "w"), indent=2)
from bench import env as envmod
json.dump(envmod.capture(), open(out / "env.json", "w"), indent=2)

rows, summ = [], []
for sc in cfg["streams"]:
    if a.only and sc["name"] not in a.only:
        continue
    print("stream:", sc["name"], flush=True)
    wait_cool(d.get("cooldown_max_temp_c"), d.get("cooldown_timeout_s", 120))
    eng = make(sc["engine"])
    eng.load()
    for _ in range(2):                                   # warm-up
        eng.transcribe(sel[0]["audio"])
        if sc["policy"] == "commit":
            eng.transcribe_words(sel[0]["audio"])
    res = []
    for r in range(a.repeats):
        for it in sel:
            m = simulate(eng, it["audio"], sc["policy"], sc.get("step", 0.5), sc.get("endpoint_s", 0.4),
                         sc.get("min_chunk", 0.6))
            m.update(name=sc["name"], repeat=r, id=it["id"], category=it["category"], ref=it["ref"])
            res.append(m)
    eng.unload()
    rows += res
    df = pd.DataFrame(res)
    sp = df[(df.ref.str.strip() != "") & (df.repeat == 0)]
    refs = [normalize(x) for x in sp.ref]; hyps = [normalize(x) for x in sp.final_text]
    keep = [i for i, x in enumerate(refs) if x]
    wer = jiwer.wer([refs[i] for i in keep], [hyps[i] for i in keep])
    sil = df[(df.ref.str.strip() == "") & (df.repeat == 0)]
    spf = df[df.ref.str.strip() != ""]
    summ.append({
        "name": sc["name"], "policy": sc["policy"], "step_s": sc.get("step", 0.5),
        "wer_pct": round(wer * 100, 2),
        "final_latency_p50_s": round(spf.final_latency_s.median(), 3),
        "final_latency_p95_s": round(spf.final_latency_s.quantile(.95), 3),
        "decode_after_endpoint_p50_s": round(spf.decode_after_endpoint_s.median(), 3),
        "first_text_p50_s": round(spf.first_text_s.median(), 2),
        "commit_lag_p50_s": round(spf.mean_commit_lag_s.median(), 2) if spf.mean_commit_lag_s.notna().any() else None,
        "stream_rtf": round(spf.decode_total_s.sum() / spf.audio_s.sum(), 3),
        "passes_per_clip": round(spf.passes.mean(), 1),
        "flicker_words_per_clip": round(spf.flicker_words.mean(), 1),
        "silence_words": int(sum(len(normalize(t).split()) for t in sil.final_text)),
        "endpoint_s": sc.get("endpoint_s", 0.4),
    })
    pd.DataFrame(rows).to_json(out / "stream_clips.jsonl", orient="records", lines=True)
    pd.DataFrame(summ).to_csv(out / "stream_summary.csv", index=False)
    print(json.dumps(summ[-1]), flush=True)
    time.sleep(d.get("cooldown_s", 10))

t = pd.DataFrame(summ)
(out / "stream_summary.md").write_text("# Streaming benchmark\n\nfinal_latency = end of speech -> final text "
                                       "(includes endpoint delay).\n\n" + t.to_markdown(index=False))
print(t.to_string(index=False))
print("\nresults:", out)
