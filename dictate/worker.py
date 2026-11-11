#!/usr/bin/env python3
"""Model host. Runs as a short-lived child of the daemon: loads the model, transcribes jobs, exits when told to
(or when the daemon dies). Every byte it allocates is returned to the OS when it exits.

Protocol (JSON lines): stdin  {"job": id, "pcm": "/path/raw16k.s16le", "dry": bool}
                       stdout {"ready": true, ...} once, then one {"job": id, ...result} per job."""
import ctypes, gc, json, os, re, sys, threading, time
from pathlib import Path
import numpy as np
import psutil

import math
import textfix
HERE = Path(__file__).resolve().parent
SR = 16000
from common import CONF_DIR, IS_WIN
CFG = {"model": str(HERE.parent / "models" / "faster-whisper-base.en"), "compute_type": "int8", "device": "cpu", "gpu_compute_type": "float16", "beam_size": 2,
       "threads": 4, "remove_fillers": True, "auto_gain": True, "chunk_s": 28.0, "min_speech_s": 0.25,
       "min_speech_rms": 0.025, "halluc_filter": True,
       "min_window_s": 15.0, "window_step_s": 5.0}
CFG.update(json.loads(os.environ.get("DICTATE_CFG", "{}")))


# --- shorter encoder window -------------------------------------------------------------------------------
# Whisper's encoder normally runs on a 30 s window padded with silence, so a 4 s dictation pays for 30 s of work.
# CTranslate2 accepts shorter inputs, but windows shorter than ~15 s make the decoder loop ("you you you"),
# so we pad to max(speech length, min_window_s) instead of 30 s. Measured: encoder time -56%, same WER.
import faster_whisper.transcribe as _ft
_orig_pad = _ft.pad_or_trim


def _pad_dyn(array, length=3000, *, axis=-1):
    mw = int(float(CFG["min_window_s"]) * 100)
    if length != 3000 or mw >= 3000 or getattr(array, "ndim", 0) != 2:
        return _orig_pad(array, length, axis=axis)
    step = max(1, int(float(CFG["window_step_s"]) * 100))
    n = array.shape[axis]
    return _orig_pad(array, min(3000, int(math.ceil(max(n, mw) / step) * step)), axis=axis)


_ft.pad_or_trim = _pad_dyn

VOCAB_DEFAULT = "Claude Code\nAnthropic\nPostgreSQL\nRedis\nKubernetes\nmiddleware\nGitHub\nTypeScript\nRust\ncargo\npytest\n"
REPL_DEFAULT = "postgres sequel => PostgreSQL\npostgres sql => PostgreSQL\ncloud code => Claude Code\nclawed code => Claude Code\n"
# phrases Whisper invents at the end of recordings (video-outro / subtitle artefacts)
PHANTOMS = {"thank you", "thank you very much", "thanks for watching", "thank you so much for your time",
            "thank you so much", "you", "bye", "goodbye", "see you", "see you next time", "see you in the next video",
            "i'll see you guys in the next video", "we'll see you in a few minutes", "let's do this", "hello there",
            "please subscribe", "all right", "alright", "okay", "so", "yeah", "hmm", "uh", "um"}
# stock phrases that are safe to strip from the END of a low-confidence segment (never plain words like "you" / "so")
TAIL_PHANTOMS = {"thank you", "thank you very much", "thanks for watching", "thank you so much for your time",
                 "thank you so much", "bye", "goodbye", "see you", "see you next time", "see you in the next video",
                 "i'll see you guys in the next video", "we'll see you in a few minutes", "let's do this",
                 "please subscribe"}
_proc = psutil.Process()


def collapse(t):
    """'all right all right' -> 'all right' (same phrase repeated)."""
    m = re.fullmatch(r"(.+?)(?:\s+\1)+", t)
    return m.group(1) if m else t


def trim():
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)      # hand freed heap back to the OS
    except Exception:
        pass


def mem():
    """RSS/PSS/USS in MB. PSS exists only on Linux; elsewhere it falls back to USS. Stats must never break dictation."""
    try:
        m = _proc.memory_full_info()
        uss = getattr(m, "uss", m.rss)
        return {"rss_mb": round(m.rss / 1e6), "pss_mb": round(getattr(m, "pss", uss) / 1e6), "uss_mb": round(uss / 1e6)}
    except Exception:
        return {}


def read_lines(name, default):
    f = CONF_DIR / name
    if not f.exists():
        CONF_DIR.mkdir(parents=True, exist_ok=True); f.write_text(default)
    return [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]


def nw(t): return re.sub(r"[^a-z0-9' ]", "", t.lower()).strip()


_TYPO = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": " - ",
                       "\u2026": "...", "\u00a0": " "})


def clean_text(text):
    t = text.translate(_TYPO).strip()
    if CFG["remove_fillers"]:
        t = re.sub(r"\b(?:um+|uh+|erm|hmm+)\b[,.]?\s*", "", t, flags=re.I)
    t = textfix.apply_rules(t, textfix.parse_rules(read_lines("replacements.txt", REPL_DEFAULT)))
    t = textfix.Vocab(read_lines("vocab.txt", VOCAB_DEFAULT)).correct(t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)
    return t if re.search(r"[A-Za-z0-9]", t) else ""


def fix_gain(a):
    peak = float(np.abs(a).max()) if len(a) else 0.0
    return (a * min(0.7 / peak, 20.0)).astype(np.float32) if 0.002 < peak < 0.3 else a


# ------------------------------------------------------------------ hallucination guard
def segment_verdict(seg, nwords, is_last, vocab_words):
    """Return None to keep, or a short reason. Thresholds come from your own recordings.
    (Without timestamps each segment is a whole 30 s window, so timing-based rules are not used.)"""
    lp, nsp, cr = seg.avg_logprob, seg.no_speech_prob, seg.compression_ratio
    text = collapse(nw(seg.text))
    if cr > 2.4:
        return f"repetition (compression {cr:.1f})"
    if nsp > 0.6 and lp < -0.8:
        return f"no speech (nsp {nsp:.2f}, logprob {lp:.2f})"
    if text in PHANTOMS and (lp < -0.5 or nsp > 0.2):
        return f"known phantom phrase at low confidence (logprob {lp:.2f}, nsp {nsp:.2f})"
    if nwords and set(text.split()) <= vocab_words and lp < -0.5:
        return "echo of the vocabulary hint"
    return None


def strip_phantom_tail(text, lp):
    """Remove a stock phrase glued to the end of a low-confidence segment ('... Thank you.')."""
    if lp >= -0.6:
        return text
    t = text
    for p in sorted(TAIL_PHANTOMS, key=len, reverse=True):
        m = re.search(r"[\s.,!?]*\b" + re.escape(p) + r"[.!?\s]*$", t, flags=re.I)
        if m and len(nw(t)) > len(p) + 3:
            t = t[: m.start()].rstrip()
            break
    return t


# ------------------------------------------------------------------ chunking (bounded memory per job)
def plan_chunks(audio, max_s):
    """Whole clip if it fits; otherwise cut at pauses so no chunk exceeds max_s."""
    n = len(audio)
    if n <= max_s * SR:
        return [(0, n)]
    from faster_whisper.vad import get_speech_timestamps
    sp = get_speech_timestamps(audio)
    chunks, start, end = [], None, None
    for s in sp:
        if start is None:
            start, end = s["start"], s["end"]
        elif s["end"] - start > max_s * SR:
            chunks.append((max(0, start - int(0.3 * SR)), min(n, end + int(0.3 * SR))))
            start, end = s["start"], s["end"]
        else:
            end = s["end"]
    if start is not None:
        chunks.append((max(0, start - int(0.3 * SR)), min(n, end + int(0.3 * SR))))
    out = []
    for a, b in chunks:                                       # a single unbroken speech run > max_s: hard split
        while b - a > max_s * SR:
            out.append((a, a + int(max_s * SR))); a += int(max_s * SR)
        out.append((a, b))
    return out or [(0, n)]


class Host:
    def __init__(self):
        from faster_whisper import WhisperModel
        t = time.perf_counter()
        self.device = self._load(WhisperModel)
        self.load_s = time.perf_counter() - t
        trim()

    def _load(self, WhisperModel):
        want = str(CFG["device"]).lower()
        if want in ("cuda", "gpu", "auto"):
            try:
                if IS_WIN:
                    import platform_win; platform_win.add_cuda_dll_dirs()
                import ctranslate2
                if ctranslate2.get_cuda_device_count() < 1:
                    raise RuntimeError("no CUDA device")
                m = WhisperModel(CFG["model"], device="cuda", compute_type=CFG["gpu_compute_type"])
                m.transcribe(np.zeros(SR, dtype=np.float32), language="en")   # force-load cuBLAS/cuDNN now, not mid-dictation
                self.model = m
                return "cuda"
            except Exception as e:
                print(f"[worker] GPU unavailable ({str(e)[:160]}); falling back to CPU", file=sys.stderr, flush=True)
        self.model = WhisperModel(CFG["model"], device="cpu", compute_type=CFG["compute_type"],
                                  cpu_threads=int(CFG["threads"]), num_workers=1)
        return "cpu"

    def transcribe_chunk(self, audio, prompt):
        segs, _ = self.model.transcribe(
            audio, language="en", beam_size=int(CFG["beam_size"]), temperature=0.0,
            condition_on_previous_text=False, vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
            without_timestamps=True,          # ONE encoder pass per 30 s window: no re-encoding of the leftover tail
            initial_prompt=prompt or None)
        return list(segs)

    def job(self, pcm_path):
        t0 = time.perf_counter()
        raw = Path(pcm_path).read_bytes()
        audio = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        dur = len(audio) / SR
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        rms = float(np.sqrt((audio ** 2).mean())) if len(audio) else 0.0
        res = {"audio_s": round(dur, 2), "peak": round(peak, 3), "rms": round(rms, 4), "text": "", "raw": "",
               "dropped": [], "chunks": 0}
        peak_rss = [0]; stop = threading.Event()
        def sampler():
            while not stop.is_set():
                peak_rss[0] = max(peak_rss[0], _proc.memory_info().rss)
                stop.wait(0.025)
        th = threading.Thread(target=sampler, daemon=True); th.start()
        try:
            if dur < 0.4:
                res["note"] = "too short"; return res
            # gate: no real speech -> do not decode at all (this is where phantom phrases come from)
            from faster_whisper.vad import get_speech_timestamps
            sp = get_speech_timestamps(audio)
            speech_s = sum(x["end"] - x["start"] for x in sp) / SR
            res["speech_s"] = round(speech_s, 2)
            if speech_s < float(CFG["min_speech_s"]):
                res["note"] = "no speech detected by VAD"; return res
            speech = np.concatenate([audio[x["start"]:x["end"]] for x in sp])
            res["speech_rms"] = round(float(np.sqrt((speech ** 2).mean())), 4)
            del speech
            if CFG["auto_gain"]:
                audio = fix_gain(audio)
            vocab = read_lines("vocab.txt", VOCAB_DEFAULT)
            vocab_words = {w for v in vocab for w in nw(v).split()}
            base_prompt = (", ".join(vocab) + ".") if vocab else ""
            kept, rawparts, tail = [], [], ""
            plan = plan_chunks(audio, float(CFG["chunk_s"]))
            res["chunks"] = len(plan)
            t_asr = time.perf_counter()
            for a, b in plan:
                segs = self.transcribe_chunk(audio[a:b], (base_prompt + " " + tail).strip())
                last_i = len(segs) - 1
                for i, s in enumerate(segs):
                    rawparts.append(s.text)
                    nwords = len(nw(s.text).split())
                    why = segment_verdict(s, nwords, i == last_i, vocab_words) if CFG["halluc_filter"] else None
                    if why:
                        res["dropped"].append({"text": s.text.strip(), "why": why, "logprob": round(s.avg_logprob, 2),
                                               "nsp": round(s.no_speech_prob, 2), "chunk": [round(a / SR, 1), round(b / SR, 1)]})
                    else:
                        txt = strip_phantom_tail(s.text, s.avg_logprob) if CFG["halluc_filter"] else s.text
                        if txt != s.text:
                            res["dropped"].append({"text": s.text[len(txt):].strip(), "why": "stock phrase stripped from end of segment",
                                                   "logprob": round(s.avg_logprob, 2), "nsp": round(s.no_speech_prob, 2)})
                        kept.append(txt)
                        res.setdefault("segments", []).append({"text": txt.strip()[:60], "logprob": round(s.avg_logprob, 2),
                                                               "nsp": round(s.no_speech_prob, 2), "cr": round(s.compression_ratio, 2)})
                tail = "".join(kept)[-120:]
            res["asr_s"] = round(time.perf_counter() - t_asr, 3)
            res["raw"] = " ".join(x.strip() for x in rawparts)
            res["text"] = clean_text(" ".join(x.strip() for x in kept))
            return res
        finally:
            stop.set(); th.join(timeout=1)
            res["job_s"] = round(time.perf_counter() - t0, 3)
            res["peak_rss_mb_during_job"] = round(peak_rss[0] / 1e6)
            del audio
            trim()
            res["mem_after_job"] = mem()


def main():
    host = Host()
    print(json.dumps({"ready": True, "device": host.device, "load_s": round(host.load_s, 3), "mem": mem()}), flush=True)
    ppid = os.getppid()
    for line in sys.stdin:
        if os.getppid() != ppid:
            break                                              # daemon is gone: exit
        try:
            req = json.loads(line)
            if req.get("cmd") == "quit":
                break
            if req.get("cmd") == "mem":
                print(json.dumps({"mem": mem()}), flush=True); continue
            r = host.job(req["pcm"]); r["job"] = req["job"]
        except Exception as e:
            r = {"job": req.get("job"), "error": str(e)}
        print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
