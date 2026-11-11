"""Attribute a process's private memory (USS) to what owns it, at each stage."""
import os, re, sys, time, json, collections
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from bench.audio import load_audio

def breakdown():
    owner = collections.Counter(); cur = None
    for line in open(f"/proc/{os.getpid()}/smaps"):
        if re.match(r"^[0-9a-f]+-[0-9a-f]+ ", line):
            parts = line.split(None, 5); path = parts[5].strip() if len(parts) > 5 else ""
            cur = ("[anon/heap]" if (not path or path.startswith("[heap]") or path.startswith("[anon")) else
                   os.path.basename(path) if "/" in path else path)
        elif line.startswith(("Private_Clean:", "Private_Dirty:")):
            owner[cur] += int(line.split()[1])            # kB
    grp = collections.Counter()
    for k, kb in owner.items():
        key = ("anon/heap (python objects, tensors, buffers, arenas)" if k == "[anon/heap]" else
               "model.bin" if k == "model.bin" else
               "libctranslate2" if "ctranslate2" in k else
               "onnxruntime (VAD)" if "onnxruntime" in k else
               "BLAS/MKL/oneDNN/openmp" if re.search(r"mkl|openblas|dnnl|iomp|gomp|libomp", k) else
               "numpy/scipy libs" if re.search(r"numpy|scipy|_multiarray|libscipy|libgfortran", k) else
               "python + other libs")
        grp[key] += kb
    uss = sum(owner.values())
    return round(uss / 1024), {k: round(v / 1024) for k, v in grp.most_common()}

clip = {"short": "/home/siva/.local/state/dictate/audio/2026-11-10T192031.wav",
        "long": "/home/siva/.local/state/dictate/audio/2026-11-10T191810.wav"}
a_short, a_long = load_audio(clip["short"]), load_audio(clip["long"])
res = {}
res["0 python+numpy"] = breakdown()
from faster_whisper import WhisperModel
res["1 after import faster_whisper"] = breakdown()
m = WhisperModel(str(ROOT / "models/faster-whisper-base.en"), device="cpu", compute_type="int8", cpu_threads=4, num_workers=1)
res["2 after model load"] = breakdown()
vocab = "Claude Code, Anthropic, PostgreSQL, Redis, Kubernetes."
def run(a):
    segs, _ = m.transcribe(a, language="en", beam_size=5, temperature=0.0, condition_on_previous_text=False,
                           vad_filter=True, hallucination_silence_threshold=1.0, word_timestamps=True, initial_prompt=vocab)
    return " ".join(s.text for s in segs)
run(a_short); res["3 after 1st 4s dictation"] = breakdown()
for _ in range(10): run(a_short)
res["4 after 10 more 4s dictations"] = breakdown()
run(a_long); res["5 after one 60s dictation"] = breakdown()
for _ in range(5): run(a_short)
res["6 after 5 more 4s dictations"] = breakdown()
for k, (uss, g) in res.items():
    print(f"\n{k:34} USS {uss:4d} MB")
    for name, mb in g.items():
        if mb >= 3: print(f"      {mb:4d} MB  {name}")
# glibc arenas: count big anonymous reservations
n = 0
for line in open(f"/proc/{os.getpid()}/maps"):
    s, e = line.split()[0].split("-"); 
    if (int(e, 16) - int(s, 16)) in (64 * 1024 * 1024, 132 * 1024 * 1024) and "rw" in line: n += 1
print("\n~64MB reserved arena regions:", n, "| threads:", len(os.listdir(f"/proc/{os.getpid()}/task")))
