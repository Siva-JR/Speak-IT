#!/usr/bin/env python3
"""Run the memory lifecycle test for every candidate in a config (each in a fresh process).
  .venv/bin/python run_mem.py --config configs/finalists.yaml
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import yaml

from run import expand

ROOT = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--config", default="configs/finalists.yaml")
ap.add_argument("--manifest", default="data/manifest.jsonl")
ap.add_argument("--iters", type=int, default=100)
ap.add_argument("--idle-s", type=float, default=20)
ap.add_argument("--only", nargs="*")
a = ap.parse_args()

cands = expand(yaml.safe_load(open(a.config)))
if a.only:
    cands = [c for c in cands if c["name"] in a.only]
out = ROOT / "results" / ("mem-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
rows = []
for c in cands:
    print("memtest", c["name"], flush=True)
    f = out / f"{c['name']}.json"
    r = subprocess.run([sys.executable, "-m", "bench.memtest", "--candidate", json.dumps(c), "--manifest", a.manifest,
                        "--out", str(f), "--iters", str(a.iters), "--idle-s", str(a.idle_s)], cwd=ROOT)
    if r.returncode or not f.exists():
        print("  FAILED", c["name"])
        continue
    d = json.load(open(f))
    s = {x["stage"]: x for x in d["stages"]}
    rows.append({
        "name": c["name"],
        "start_uss": s["process_start"]["uss_mb"],
        "loaded_pss": s["after_load"]["pss_mb"], "loaded_uss": s["after_load"]["uss_mb"],
        "active_rss": s[f"after_{a.iters}_inferences"]["rss_mb"],
        "active_pss": s[f"after_{a.iters}_inferences"]["pss_mb"],
        "active_uss": s[f"after_{a.iters}_inferences"]["uss_mb"],
        "unloaded_uss": s["after_unload"]["uss_mb"],
        "model_cost_uss": d["model_cost_uss_mb"],
        "freed_on_unload": d["freed_by_unload_uss_mb"],
        "leak_uss_mb_after_warm": d["leak_uss_mb"],
        "load_s": s["after_load"]["load_s"], "reload_s": d["reload_s"],
    })
df = pd.DataFrame(rows)
df.to_csv(out / "memory.csv", index=False)
(out / "memory.md").write_text(df.to_markdown(index=False))
print(df.to_string(index=False))
print("\nresults:", out)
