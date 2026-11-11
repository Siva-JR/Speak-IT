#!/usr/bin/env python3
"""Recompute WER from saved transcripts with the current normaliser (no model re-run needed).
  .venv/bin/python rescore.py results/<run-dir>...
"""
import sys
from pathlib import Path

import jiwer
import pandas as pd

from bench.textnorm import normalize


def wer(d, text_col):
    refs = [normalize(x) for x in d.ref]; hyps = [normalize(x) for x in d[text_col]]
    keep = [i for i, r in enumerate(refs) if r]
    return round(jiwer.wer([refs[i] for i in keep], [hyps[i] for i in keep]) * 100, 1) if keep else None


EXC = set(Path("data/excluded.txt").read_text().split()) if Path("data/excluded.txt").exists() else set()
rows = []
for run in map(Path, sys.argv[1:]):
    if (run / "stream_clips.jsonl").exists():
        df = pd.read_json(run / "stream_clips.jsonl", lines=True)
        for name, g in df.groupby("name"):
            g = g[(g.repeat == 0) & (g.ref.str.strip() != "") & ~g.id.isin(EXC)]
            r = {"run": run.name[:22], "name": name, "all": wer(g, "final_text")}
            r.update({c: wer(x, "final_text") for c, x in g.groupby("category")})
            rows.append(r)
    for f in sorted(run.glob("*/transcripts.jsonl")):
        g = pd.read_json(f, lines=True)
        g = g[(g.repeat == 0) & (g.ref.str.strip() != "") & ~g.id.isin(EXC)]
        r = {"run": run.name[:22], "name": f.parent.name, "all": wer(g, "hyp")}
        r.update({c: wer(x, "hyp") for c, x in g.groupby("category")})
        rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))
