#!/usr/bin/env python3
"""Build data/manifest.jsonl.

Sources
  librispeech test-clean  -> category 'clean'   (read speech, easy)
  librispeech test-other  -> category 'other'   (harder: accents / noisier)
  synthetic silence/noise -> categories 'silence'/'noise' (hallucination check, empty reference)
  data/custom/*.wav + data/custom/refs.tsv -> category 'custom' (YOUR voice, see record_clips.py)
"""
import argparse
import json
import random
import shutil
import tarfile
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "data"
URLS = {
    "clean": "https://www.openslr.org/resources/12/test-clean.tar.gz",
    "other": "https://www.openslr.org/resources/12/test-other.tar.gz",
}


def fetch(cat):
    tgz = D / "raw" / f"test-{cat}.tar.gz"
    tgz.parent.mkdir(parents=True, exist_ok=True)
    if not tgz.exists():
        print(f"downloading {URLS[cat]} ...", flush=True)
        urllib.request.urlretrieve(URLS[cat], tgz)
    ex = D / "raw" / f"test-{cat}"
    if not ex.exists():
        print("extracting", tgz.name, flush=True)
        with tarfile.open(tgz) as t:
            t.extractall(ex)
    return ex


def librispeech(cat, n, rng, max_s):
    root = fetch(cat)
    refs = {}
    for tr in root.rglob("*.trans.txt"):
        for line in tr.read_text().splitlines():
            uid, txt = line.split(" ", 1)
            refs[uid] = (tr.parent / f"{uid}.flac", txt)
    ids = sorted(refs)
    rng.shuffle(ids)
    out = []
    for uid in ids:
        path, txt = refs[uid]
        info = sf.info(path)
        dur = info.frames / info.samplerate
        if 3 <= dur <= max_s:
            dst = D / "clips" / cat / f"{uid}.flac"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, dst)
            out.append({"id": f"{cat}-{uid}", "path": str(dst), "ref": txt, "category": cat})
        if len(out) >= n:
            break
    return out


def synthetic(rng):
    out = []
    d = D / "clips" / "synthetic"
    d.mkdir(parents=True, exist_ok=True)
    g = np.random.default_rng(0)
    for name, sig in [
        ("silence_10s", np.zeros(160000, dtype=np.float32)),
        ("room_noise_10s", (g.normal(0, 0.01, 160000)).astype(np.float32)),
        ("loud_noise_10s", (g.normal(0, 0.08, 160000)).astype(np.float32)),
    ]:
        p = d / f"{name}.wav"
        sf.write(p, sig, 16000)
        out.append({"id": f"synthetic-{name}", "path": str(p), "ref": "",
                    "category": "silence" if "silence" in name else "noise"})
    return out


def clip_ok(p, txt):
    """Reject empty / silent / too-short recordings so they can't poison the benchmark."""
    try:
        x, sr = sf.read(p, dtype="float32")
    except Exception as e:
        return False, f"unreadable ({e})"
    if x.ndim > 1:
        x = x.mean(axis=1)
    dur = len(x) / sr if sr else 0
    need = max(1.5, len(txt.split()) / 4.5)          # >= ~4.5 words/s is the fastest plausible speech
    if dur < need:
        return False, f"too short ({dur:.1f}s, expected >= {need:.1f}s)"
    rms = float(np.sqrt((x ** 2).mean()))
    if rms < 0.004:
        return False, f"almost silent (rms {rms:.4f})"
    return True, ""


def custom():
    refs = D / "custom" / "refs.tsv"
    if not refs.exists():
        return []
    out = []
    for line in refs.read_text().splitlines():
        if "\t" in line:
            fn, txt = line.split("\t", 1)
            p = D / "custom" / fn
            if p.exists():
                ok, why = clip_ok(p, txt)
                if not ok:
                    print(f"  skipping {fn}: {why}")
                    continue
                out.append({"id": f"custom-{p.stem}", "path": str(p), "ref": txt, "category": "custom"})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", type=int, default=25)
    ap.add_argument("--other", type=int, default=25)
    ap.add_argument("--max-seconds", type=float, default=14.0)  # keeps whisper.cpp audio_ctx=768 (~15 s) valid
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--custom-only", action="store_true",
                    help="keep the existing manifest, only (re)load your recordings from data/custom")
    a = ap.parse_args()
    if a.custom_only:
        mf = D / "manifest.jsonl"
        keep = [json.loads(l) for l in mf.read_text().splitlines() if l.strip()]
        keep = [i for i in keep if i["category"] != "custom"]
        new = custom()
        with open(mf, "w") as f:
            for it in keep + new:
                f.write(json.dumps(it) + "\n")
        print(f"manifest: kept {len(keep)} existing clips, added {len(new)} custom clips")
        raise SystemExit
    rng = random.Random(1234)  # fixed seed -> identical clip set on every machine
    items = librispeech("clean", a.clean, rng, a.max_seconds) + librispeech("other", a.other, rng, a.max_seconds)
    items += synthetic(rng) + custom()
    with open(D / "manifest.jsonl", "w") as f:
        for it in items:
            f.write(json.dumps(it) + "\n")
    from collections import Counter
    print("manifest:", dict(Counter(i["category"] for i in items)), "->", D / "manifest.jsonl")
    if not a.keep_raw:
        shutil.rmtree(D / "raw", ignore_errors=True)
