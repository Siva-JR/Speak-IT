"""One fresh process: time every step from 'python starts' to 'first transcription done'."""
import json, os, sys, time
T0 = time.perf_counter()
import numpy as np, psutil
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
engine, clip, drop_cache = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
proc = psutil.Process()
def uss(): return round(proc.memory_full_info().uss / 1e6)
out = {"engine": engine, "python_start_s": round(time.perf_counter() - T0, 3), "uss_start": uss()}
sys.path.insert(0, str(ROOT))
from bench.audio import load_audio
audio = load_audio(clip)
model_path = ROOT / ("models/faster-whisper-base.en" if engine == "fw" else "models/ggml-base.en-q5_1.bin")
if drop_cache:                                           # evict the model file from the page cache (no root needed)
    for f in ([model_path / "model.bin"] if engine == "fw" else [model_path]):
        fd = os.open(f, os.O_RDONLY); os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED); os.close(fd)
vocab = "Claude Code, Anthropic, PostgreSQL, Redis, Kubernetes, middleware, GitHub, TypeScript, Rust, cargo, pytest."
t = time.perf_counter()
if engine == "fw":
    from faster_whisper import WhisperModel
    out["import_s"] = round(time.perf_counter() - t, 3); out["uss_after_import"] = uss()
    t = time.perf_counter()
    m = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=4, num_workers=1)
    out["model_load_s"] = round(time.perf_counter() - t, 3); out["uss_after_load"] = uss()
    def run():
        segs, _ = m.transcribe(audio, language="en", beam_size=5, temperature=0.0, condition_on_previous_text=False,
                               vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
                               hallucination_silence_threshold=1.0, word_timestamps=True, initial_prompt=vocab)
        return " ".join(s.text.strip() for s in segs)
else:
    from pywhispercpp.model import Model
    out["import_s"] = round(time.perf_counter() - t, 3); out["uss_after_import"] = uss()
    t = time.perf_counter()
    m = Model(str(model_path), params_sampling_strategy=0, redirect_whispercpp_logs_to=os.devnull, n_threads=4,
              language="en", no_context=True, print_progress=False, print_realtime=False, print_timestamps=False,
              audio_ctx=768)
    out["model_load_s"] = round(time.perf_counter() - t, 3); out["uss_after_load"] = uss()
    import bench.engines as E
    def run():
        a = E.vad_trim(audio)
        return " ".join(s.text.strip() for s in m.transcribe(a))
t = time.perf_counter(); txt = run(); out["first_inference_s"] = round(time.perf_counter() - t, 3)
out["uss_after_first"] = uss()
t = time.perf_counter(); run(); out["second_inference_s"] = round(time.perf_counter() - t, 3)
out["total_cold_to_first_text_s"] = round(out["python_start_s"] + out["import_s"] + out["model_load_s"] + out["first_inference_s"], 3)
out["text_len"] = len(txt)
print(json.dumps(out))
