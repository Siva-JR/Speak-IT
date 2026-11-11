#!/usr/bin/env python3
"""Orchestrator: expands the config into candidates, runs each in its own process, then reports.

  .venv/bin/python run.py                       # full run with configs/candidates.yaml
  .venv/bin/python run.py --only baseline       # a single candidate
  .venv/bin/python run.py --limit 3 --repeats 1 # quick smoke test
"""
import argparse
import copy
import datetime as dt
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import yaml

from bench import env as envmod

ROOT = Path(__file__).resolve().parent


def expand(cfg):
    base = cfg["base"]
    cands = [dict(copy.deepcopy(base), name="baseline")]
    for v in cfg.get("vary", []):            # one-factor-at-a-time vs baseline
        (key, values), = v.items()
        for val in values:
            if base.get(key) == val:
                continue
            c = copy.deepcopy(base)
            c[key] = val
            c["name"] = f"{key}={str(val).split('/')[-1]}"
            cands.append(c)
    for c in cfg.get("candidates", []):      # explicit extras (full override of base)
        c2 = dict(copy.deepcopy(base), **c)
        c2.setdefault("name", "extra")
        cands.append(c2)
    return cands


def wait_cool(max_temp, timeout=120):
    if not max_temp:
        return
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            temps = [e.current for k, v in psutil.sensors_temperatures().items()
                     if k == "coretemp" for e in v if (e.label or "").startswith("Package")]
        except Exception:
            return
        if not temps or temps[0] <= max_temp:
            return
        time.sleep(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/candidates.yaml")
    ap.add_argument("--manifest", default="data/manifest.jsonl")
    ap.add_argument("--tag", default="")
    ap.add_argument("--only", nargs="*", help="run only these candidate names")
    ap.add_argument("--repeats", type=int)
    ap.add_argument("--warmup", type=int)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--profile", action="store_true", help="cProfile each candidate")
    ap.add_argument("--list", action="store_true", help="print candidates and exit")
    a = ap.parse_args()

    cfg = yaml.safe_load(open(a.config))
    d = cfg.get("defaults", {})
    repeats = a.repeats or d.get("repeats", 3)
    warmup = a.warmup if a.warmup is not None else d.get("warmup", 2)
    cands = expand(cfg)
    if a.only:
        cands = [c for c in cands if c["name"] in a.only]
    if a.list:
        for c in cands:
            print(json.dumps(c))
        return

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{a.tag}" if a.tag else "")
    out = ROOT / "results" / run_id
    out.mkdir(parents=True)
    shutil.copy(a.config, out / "config.yaml")
    e = envmod.capture()
    e["run_id"], e["repeats"], e["warmup"] = run_id, repeats, warmup
    json.dump(e, open(out / "env.json", "w"), indent=2)

    print(f"run {run_id}: {len(cands)} candidates, repeats={repeats}")
    if e["on_ac_power"] is False:
        print("WARNING: on battery, results will be throttled and not comparable")
    if e["load_avg_1m_at_start"] > 1.5:
        print(f"WARNING: load average {e['load_avg_1m_at_start']:.1f}, close other programs for clean numbers")
    if e["cpu_governor"] not in (None, "performance"):
        print(f"note: CPU governor is '{e['cpu_governor']}' (recorded in env.json)")

    for i, c in enumerate(cands, 1):
        print(f"[{i}/{len(cands)}] {c['name']}", flush=True)
        wait_cool(d.get("cooldown_max_temp_c"), d.get("cooldown_timeout_s", 120))
        cmd = [sys.executable, "-m", "bench.worker", "--candidate", json.dumps(c),
               "--manifest", a.manifest, "--out", str(out / c["name"]),
               "--repeats", str(repeats), "--warmup", str(warmup),
               "--interval", str(d.get("sample_interval_s", 0.1))]
        if a.limit:
            cmd += ["--limit", str(a.limit)]
        if a.profile:
            cmd.append("--profile")
        r = subprocess.run(cmd, cwd=ROOT)
        if r.returncode != 0:
            print(f"  FAILED: {c['name']} (exit {r.returncode})")
        time.sleep(d.get("cooldown_s", 5))

    subprocess.run([sys.executable, "report.py", str(out), "--config", a.config], cwd=ROOT)
    print(f"\nresults: {out}")


if __name__ == "__main__":
    main()
