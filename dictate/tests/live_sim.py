"""Feed recorded audio into the live-typing pipeline in REAL TIME; log what would be typed, and when.
   ../.venv/bin/python tests/live_sim.py            (from the dictate/ folder)"""
import sys, time, tempfile, threading
from pathlib import Path
import numpy as np, jiwer
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dictate")); sys.path.insert(0, str(ROOT))
import dictate as d
from bench.audio import load_audio
from bench.textnorm import normalize

d.DEFAULTS.update(tray=False, notify=False, sounds=False, save_audio=False, output="portal")
d.STATE = Path(tempfile.mkdtemp())


class FakeTyper:
    ready = threading.Event()
    def __init__(self): self.events, self.t0 = [], time.time()
    def start(self): pass
    def type(self, text): self.events.append((round(time.time() - self.t0, 2), text))


def run(audio, ref, label):
    dm = d.Daemon(); ft = FakeTyper(); dm.typer = ft
    dm.ensure_model(); dm.decode_words(audio[:32000])          # warm up model + VAD
    dm.chunks = []; dm.rec = None
    ft.t0 = time.time(); dm.begin_session(live=True)
    t0, hop = time.time(), 1600
    for i in range(0, len(audio), hop):                         # real-time feed, 0.1 s hops
        dm.chunks.append((np.clip(audio[i:i + hop], -1, 1) * 32767).astype("<i2").tobytes())
        time.sleep(max(0, (i + hop) / 16000 - (time.time() - t0)))
    t_speech_end = time.time() - ft.t0
    t = time.time(); dm.stop_rec(); stop_s = time.time() - t
    typed = "".join(x for _, x in ft.events).strip()
    first = ft.events[0][0] if ft.events else None
    live_words = sum(len(x.split()) for tt, x in ft.events if tt <= t_speech_end)
    tot_words = max(1, len(typed.split()))
    w = jiwer.wer(normalize(ref), normalize(typed)) * 100 if ref and normalize(ref) else None
    print(f"\n== {label}: audio {len(audio)/16000:.1f}s | first word typed at {first}s | "
          f"stop->done {stop_s:.2f}s | {live_words}/{tot_words} words typed live | passes {dm.live.passes} | "
          f"WER {w if w is None else round(w,1)}% | pass avg {sum(dm.live.pass_s)/max(1,len(dm.live.pass_s)):.2f}s")
    for tt, x in ft.events: print(f"   t={tt:>5}s  +{x!r}")
    return typed


if __name__ == "__main__":
    import json
    man = [json.loads(l) for l in open(ROOT / "data/manifest.jsonl")]
    by = {m["id"]: m for m in man}
    for cid in sys.argv[1:] or ["custom-clip08", "custom-clip01"]:
        m = by[cid]; run(load_audio(m["path"]), m["ref"], cid)
    if not sys.argv[1:]:
        # long dictation: 3 LibriSpeech clips back to back with a short pause between them
        ids = [i for i in by if i.startswith("clean-")][:3]
        parts = [load_audio(by[i]["path"]) for i in ids]
        gap = np.zeros(int(0.6 * 16000), dtype=np.float32)
        audio = np.concatenate([x for p in parts for x in (p, gap)])
        run(audio, " ".join(by[i]["ref"] for i in ids), "long (3 clips, ~" + str(int(len(audio)/16000)) + "s)")
