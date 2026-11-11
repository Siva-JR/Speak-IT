"""Memory lifecycle test for ONE candidate: load -> use -> leak check -> unload -> idle -> reload.
Reports RSS, PSS (proportional, fairer for shared/mmapped pages) and USS (unique to this process)."""
import argparse
import ctypes
import gc
import json
import time
from pathlib import Path

import psutil

from bench.audio import load_audio
from bench.engines import make


def snap(label, proc, extra=None):
    m = proc.memory_full_info()
    d = {"stage": label, "rss_mb": round(m.rss / 1e6, 1), "pss_mb": round(m.pss / 1e6, 1),
         "uss_mb": round(m.uss / 1e6, 1)}
    d.update(extra or {})
    return d


def trim():
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)   # hand freed heap back to the OS
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--iters", type=int, default=100)
    ap.add_argument("--idle-s", type=float, default=20)
    a = ap.parse_args()
    cand = json.loads(a.candidate)
    items = [json.loads(l) for l in open(a.manifest) if l.strip()]
    items = [i for i in items if i["ref"].strip()][:12]
    for it in items:
        it["audio"] = load_audio(it["path"])

    proc = psutil.Process()
    st = [snap("process_start", proc)]
    eng = make(cand)
    t = time.perf_counter(); eng.load(); load_s = time.perf_counter() - t
    st.append(snap("after_load", proc, {"load_s": round(load_s, 3)}))
    eng.transcribe(items[0]["audio"])
    st.append(snap("after_1st_inference", proc))
    for i in range(a.iters):
        eng.transcribe(items[i % len(items)]["audio"])
        if i + 1 in (20, a.iters):
            st.append(snap(f"after_{i + 1}_inferences", proc))
    eng.unload(); trim()
    st.append(snap("after_unload", proc))
    time.sleep(a.idle_s)
    st.append(snap(f"idle_{int(a.idle_s)}s_after_unload", proc))
    eng2 = make(cand)
    t = time.perf_counter(); eng2.load(); reload_s = time.perf_counter() - t
    eng2.transcribe(items[0]["audio"])
    st.append(snap("after_reload_1st_inference", proc, {"reload_s": round(reload_s, 3)}))

    by = {s["stage"]: s for s in st}
    leak = by[f"after_{a.iters}_inferences"]["uss_mb"] - by["after_20_inferences"]["uss_mb"]
    out = {"name": cand["name"], "stages": st,
           "leak_uss_mb": round(leak, 1),
           "model_cost_uss_mb": round(by["after_1st_inference"]["uss_mb"] - by["process_start"]["uss_mb"], 1),
           "freed_by_unload_uss_mb": round(by[f"after_{a.iters}_inferences"]["uss_mb"] - by["after_unload"]["uss_mb"], 1),
           "reload_s": round(reload_s, 3)}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=2)
    print(json.dumps({k: v for k, v in out.items() if k != "stages"}))


if __name__ == "__main__":
    main()
