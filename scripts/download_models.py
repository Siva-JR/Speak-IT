#!/usr/bin/env python3
"""Download every model referenced by configs/candidates.yaml into ./models (idempotent)."""
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

ROOT = Path(__file__).resolve().parent.parent
M = ROOT / "models"
M.mkdir(exist_ok=True)

FW = {  # CTranslate2 / faster-whisper
    "faster-whisper-tiny.en": "Systran/faster-whisper-tiny.en",
    "faster-whisper-base.en": "Systran/faster-whisper-base.en",
    "faster-whisper-small.en": "Systran/faster-whisper-small.en",
    "faster-whisper-distil-small.en": "Systran/faster-distil-whisper-small.en",
}
CPP = [  # whisper.cpp ggml
    "ggml-base.en.bin", "ggml-base.en-q8_0.bin", "ggml-base.en-q5_1.bin",
    "ggml-small.en-q5_1.bin", "ggml-tiny.en.bin",
]

failed = []
for name, repo in FW.items():
    try:
        print("->", name, flush=True)
        snapshot_download(repo, local_dir=M / name)
    except Exception as e:
        failed.append((name, str(e)[:120]))
for f in CPP:
    try:
        if (M / f).exists():
            continue
        print("->", f, flush=True)
        hf_hub_download("ggerganov/whisper.cpp", f, local_dir=M)
    except Exception as e:
        failed.append((f, str(e)[:120]))

for p in sorted(M.iterdir()):
    sz = sum(x.stat().st_size for x in p.rglob("*") if x.is_file()) / 1e6 if p.is_dir() else p.stat().st_size / 1e6
    print(f"{sz:8.1f} MB  {p.name}")
if failed:
    print("\nFAILED (remove from config or fix names):")
    for n, e in failed:
        print(" ", n, e)
    sys.exit(1)
