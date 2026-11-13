#!/usr/bin/env python3
"""Model host. Runs as a short-lived child of the daemon: loads the model, transcribes jobs, exits when told to
(or when the daemon dies). Every byte it allocates is returned to the OS when it exits.

Protocol (JSON lines on stdin, one reply line per request on stdout; {"ready": true, ...} is printed once first):
  {"job": id, "pcm": "/path/raw16k.s16le"}                        transcribe a whole file      -> {"job": id, ...result}
  {"feed": sid, "pcm": path, "upto": nbytes}                      live: the file has grown     -> {"fed": sid, ...}
  {"job": id, "stream": sid, "pcm": path, "final": true}          live: recording ended        -> {"job": id, ...result}
  {"cmd": "drop", "stream": sid} | {"cmd": "mem"} | {"cmd": "quit"}

Live transcription: while you speak, the daemon appends audio to `pcm` and sends `feed` about once a second. Whenever
at least `live_min_chunk_s` of audio ends in a pause, that part is transcribed right away, so when you stop only the
words since the last pause are left. A stream whose state is missing (worker restarted) simply starts from byte 0."""
import ctypes, gc, json, math, os, re, sys, threading, time
from pathlib import Path
import numpy as np
import psutil

import textfix
from common import CONF_DIR, IS_WIN

HERE = Path(__file__).resolve().parent
SR = 16000
CFG = {"model": str(HERE.parent / "models" / "faster-whisper-base.en"), "compute_type": "int8", "device": "cpu",
       "gpu_compute_type": "float16", "beam_size": 2,   # measured on an RTX 4050: int8_float16 is ~20% slower, beam 1 no faster
       "threads": 4, "remove_fillers": True, "auto_gain": True, "chunk_s": 28.0, "min_speech_s": 0.25,
       "min_speech_rms": 0.025, "halluc_filter": True,
       "min_window_s": 15.0, "window_step_s": 5.0,
       "live_min_chunk_s": 8.0,      # never transcribe ahead in pieces shorter than this
       "live_target_s": 0.3,         # ...and only once finishing the rest after you stop would take longer than this
       "live_pause_s": 0.6}          # a silence this long is a safe place to cut
CFG.update(json.loads(os.environ.get("DICTATE_CFG", "{}")))
VAD_PAD = int(0.4 * SR)              # same padding faster-whisper's own vad_filter adds around speech


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


_lists = {}                                           # name -> (mtime, lines): re-read only when the file changes


def read_lines(name, default):
    f = CONF_DIR / name
    try:
        if not f.exists():
            CONF_DIR.mkdir(parents=True, exist_ok=True); f.write_text(default)
        mt = f.stat().st_mtime_ns
        if name not in _lists or _lists[name][0] != mt:
            _lists[name] = (mt, [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")])
        return _lists[name][1]
    except OSError:
        return [l for l in default.splitlines() if l]


def nw(t): return re.sub(r"[^a-z0-9' ]", "", t.lower()).strip()


_TYPO = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": " - ",
                       "…": "...", " ": " "})


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


# ------------------------------------------------------------------ voice activity (ONE silero pass per region)
def speech_spans(audio):
    """Unpadded [(start, end)] sample ranges of speech. Every later step (gating, chunk cuts, what the model sees)
    reuses these spans, so the VAD runs once per region instead of once per step."""
    if len(audio) < SR // 10:
        return []
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    # 250 ms (not 500) so live mode also sees the short breaths people take mid-sentence; pad_spans() merges gaps
    # under 0.8 s back together, so the audio the model hears is the same as with faster-whisper's own vad_filter
    ts = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=250, speech_pad_ms=0))
    return [(t["start"], t["end"]) for t in ts]


def pad_spans(spans, n, pad=VAD_PAD):
    """Pad each span and merge overlaps (what faster-whisper's vad_filter does before decoding)."""
    out = []
    for s, e in spans:
        s, e = max(0, s - pad), min(n, e + pad)
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def plan_chunks(spans, n, max_s):
    """[(a, b)] pieces of an n-sample region, cut in pauses so that none is longer than max_s."""
    mx = int(max_s * SR)
    if n <= mx:
        return [(0, n)]
    cuts, start = [], 0
    for (_s0, e0), (s1, e1) in zip(spans, spans[1:]):
        c = (e0 + s1) // 2
        if e1 - start > mx and c > start:                     # the next word would overflow: cut in the pause before it
            cuts.append((start, c)); start = c
    cuts.append((start, n))
    out = []
    for a, b in cuts:                                         # a single unbroken speech run > max_s: hard split
        while b - a > mx:
            out.append((a, a + mx)); a += mx
        out.append((a, b))
    return out


def live_cut(spans, n, min_chunk_s, pause_s, max_s, short_pause_s=0.25):
    """Where to cut the not-yet-transcribed audio while recording continues, or None to wait for more.
    1. the LATEST pause of at least pause_s that leaves >= min_chunk_s before it;
    2. people who talk without long pauses still breathe: once 2 x min_chunk_s has built up, the LONGEST short pause;
    3. past max_s: the longest pause in the second half, and only if there is none, a hard cut.
    Trailing silence after the last word only counts once it is pause_s long (the speaker may just be breathing)."""
    pause, short, hi = int(pause_s * SR), int(short_pause_s * SR), int(max_s * SR)
    lo = min(int(min_chunk_s * SR), hi)
    if not spans:
        return n - SR if n > 8 * SR else None              # long silence: drop it, keep the last second
    gaps = [(s1 - e0, (e0 + s1) // 2) for (_s0, e0), (s1, _e1) in zip(spans, spans[1:])]
    if n - spans[-1][1] >= pause:
        gaps.append((n - spans[-1][1], spans[-1][1] + pause // 2))
    ok = [c for g, c in gaps if g >= pause and lo <= c <= hi]
    if ok:
        return max(ok)
    if n >= min(2 * lo, hi):
        ok = [(g, c) for g, c in gaps if g >= short and lo <= c <= hi]
        if ok:
            return max(ok)[1]
    if n > hi:
        ok = [(g, c) for g, c in gaps if hi // 2 <= c <= hi]
        return max(ok)[1] if ok else hi
    return None


def read_pcm(path, start=0, end=None):
    """float32 samples [start, end) of a raw 16 kHz s16le file (end=None: to the end)."""
    with open(path, "rb") as f:
        f.seek(start * 2)
        raw = f.read(-1 if end is None else max(0, (end - start) * 2))
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


class Stream:
    """Transcription state of one dictation: how far it has been transcribed, and the kept text so far."""
    def __init__(self, sid=None):
        self.sid, self.pos = sid, 0                         # pos: first sample not yet transcribed
        self.kept, self.raw, self.dropped, self.segments = [], [], [], []
        self.asr_s, self.chunks, self.live_chunks, self.speech_s = 0.0, 0, 0, 0.0


class Host:
    def __init__(self):
        from faster_whisper import WhisperModel
        t = time.perf_counter()
        self.device = self._load(WhisperModel)
        self.load_s = time.perf_counter() - t
        self.streams = {}
        self.rate, self.rate_n = 0.03, 0   # seconds of compute per second of audio window; measured on every decode
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
                ct = CFG["gpu_compute_type"]
                if ct not in ctranslate2.get_supported_compute_types("cuda"):
                    ct = "float32"                          # GPUs without fast half precision
                m = WhisperModel(CFG["model"], device="cuda", compute_type=ct)
                m.transcribe(np.zeros(SR, dtype=np.float32), language="en")   # force-load cuBLAS/cuDNN now, not mid-dictation
                self.model, self.compute_type = m, ct
                return "cuda"
            except Exception as e:
                print(f"[worker] GPU unavailable ({str(e)[:160]}); falling back to CPU", file=sys.stderr, flush=True)
        self.model, self.compute_type = WhisperModel(CFG["model"], device="cpu", compute_type=CFG["compute_type"],
                                                     cpu_threads=int(CFG["threads"]), num_workers=1), CFG["compute_type"]
        return "cpu"

    def transcribe_chunk(self, audio, prompt):
        segs, _ = self.model.transcribe(
            audio, language="en", beam_size=int(CFG["beam_size"]), temperature=0.0,
            condition_on_previous_text=False, vad_filter=False,     # silence is already cut out (speech_spans)
            without_timestamps=True,          # ONE encoder pass per 30 s window: no re-encoding of the leftover tail
            initial_prompt=prompt or None)
        return list(segs)

    # --- one region of audio -> kept text (shared by whole-file jobs and live streams)
    def decode(self, st, audio, spans, offset):
        """Transcribe `audio` (samples [offset, offset+len) of the dictation) using its precomputed speech spans."""
        padded = pad_spans(spans, len(audio))
        speech_s = sum(e - s for s, e in padded) / SR
        st.speech_s += speech_s
        if speech_s < float(CFG["min_speech_s"]):
            return                                          # no real speech: never decode (phantom phrases come from here)
        speech = np.concatenate([audio[s:e] for s, e in padded])
        if CFG["auto_gain"]:
            speech = fix_gain(speech)
        vocab = read_lines("vocab.txt", VOCAB_DEFAULT)
        vocab_words = {w for v in vocab for w in nw(v).split()}
        prompt = ((", ".join(vocab) + ".") if vocab else "") + " " + "".join(st.kept)[-120:]
        t = time.perf_counter()
        segs = self.transcribe_chunk(speech, prompt.strip())
        took = time.perf_counter() - t
        st.asr_s += took
        window = max(len(speech) / SR, float(CFG["min_window_s"]))      # the encoder pays for at least min_window_s
        self.rate = 0.7 * self.rate + 0.3 * took / window if self.rate_n else took / window
        self.rate_n += 1
        st.chunks += 1
        span = [round(offset / SR, 1), round((offset + len(audio)) / SR, 1)]
        for i, s in enumerate(segs):
            st.raw.append(s.text)
            why = segment_verdict(s, len(nw(s.text).split()), i == len(segs) - 1, vocab_words) if CFG["halluc_filter"] else None
            if why:
                st.dropped.append({"text": s.text.strip(), "why": why, "logprob": round(s.avg_logprob, 2),
                                   "nsp": round(s.no_speech_prob, 2), "chunk": span})
                continue
            txt = strip_phantom_tail(s.text, s.avg_logprob) if CFG["halluc_filter"] else s.text
            if txt != s.text:
                st.dropped.append({"text": s.text[len(txt):].strip(), "why": "stock phrase stripped from end of segment",
                                   "logprob": round(s.avg_logprob, 2), "nsp": round(s.no_speech_prob, 2)})
            st.kept.append(txt)
            st.segments.append({"text": txt.strip()[:60], "logprob": round(s.avg_logprob, 2),
                                "nsp": round(s.no_speech_prob, 2), "cr": round(s.compression_ratio, 2)})

    # --- live: the recording file has grown
    def feed(self, sid, path, upto):
        st = self.streams.get(sid)
        if st is None:
            self.streams = {sid: Stream(sid)}               # only one dictation at a time: forget older streams
            st = self.streams[sid]
        end = upto // 2
        if end - st.pos < int(float(CFG["live_min_chunk_s"]) * SR):
            return {"fed": sid, "pos_s": round(st.pos / SR, 2)}
        # how much audio this machine can finish within live_target_s after you stop: wait for at least that much
        min_chunk = max(float(CFG["live_min_chunk_s"]), float(CFG["live_target_s"]) / max(self.rate, 1e-4))
        if (end - st.pos) / SR < min(min_chunk, float(CFG["chunk_s"])):
            return {"fed": sid, "pos_s": round(st.pos / SR, 2)}
        audio = read_pcm(path, st.pos, end)
        spans = speech_spans(audio)
        cut = live_cut(spans, len(audio), min_chunk, float(CFG["live_pause_s"]), float(CFG["chunk_s"]))
        if cut:
            if spans:
                self.decode(st, audio[:cut], [(s, min(e, cut)) for s, e in spans if s < cut], st.pos)
                st.live_chunks += 1
            st.pos += cut
        return {"fed": sid, "pos_s": round(st.pos / SR, 2), "cut": bool(cut)}

    # --- a whole file, or the rest of a live stream
    def job(self, pcm_path, sid=None):
        t0 = time.perf_counter()
        st = self.streams.pop(sid, None) if sid else None
        st = st or Stream(sid)
        whole = read_pcm(pcm_path)
        dur = len(whole) / SR
        peak = float(np.abs(whole).max()) if len(whole) else 0.0
        rms = float(np.sqrt((whole ** 2).mean())) if len(whole) else 0.0
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
            tail = whole[st.pos:]
            del whole
            res["live_s"] = round(st.pos / SR, 2)          # transcribed while you were still speaking
            res["tail_s"] = round(len(tail) / SR, 2)        # left to do after you stopped
            res["live_asr_s"] = round(st.asr_s, 3)
            t_tail = time.perf_counter()
            if len(tail) >= SR // 10:
                spans = speech_spans(tail)
                for a, b in plan_chunks(spans, len(tail), float(CFG["chunk_s"])):
                    sub = [(max(s, a) - a, min(e, b) - a) for s, e in spans if e > a and s < b]
                    if sub:
                        self.decode(st, tail[a:b], sub, st.pos + a)
            res["tail_asr_s"] = round(time.perf_counter() - t_tail, 3)
            res["speech_s"] = round(st.speech_s, 2)
            res.update(chunks=st.chunks, live_chunks=st.live_chunks, dropped=st.dropped, segments=st.segments,
                       asr_s=round(st.asr_s, 3))
            if st.speech_s < float(CFG["min_speech_s"]) and not st.kept:
                res["note"] = "no speech detected by VAD"; return res
            res["raw"] = " ".join(x.strip() for x in st.raw)
            res["text"] = clean_text(" ".join(x.strip() for x in st.kept))
            return res
        finally:
            stop.set(); th.join(timeout=1)
            res["job_s"] = round(time.perf_counter() - t0, 3)
            res["peak_rss_mb_during_job"] = round(peak_rss[0] / 1e6)
            trim()
            res["mem_after_job"] = mem()


def main():
    host = Host()
    print(json.dumps({"ready": True, "device": host.device, "compute_type": host.compute_type,
                      "load_s": round(host.load_s, 3), "mem": mem()}), flush=True)
    ppid = os.getppid()
    for line in sys.stdin:
        if os.getppid() != ppid:
            break                                              # daemon is gone: exit
        req = {}
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "quit":
                break
            if cmd == "mem":
                print(json.dumps({"mem": mem()}), flush=True); continue
            if cmd == "drop":
                host.streams.pop(req.get("stream"), None); continue
            if "feed" in req:
                r = host.feed(req["feed"], req["pcm"], int(req["upto"]))
            else:
                r = host.job(req["pcm"], req.get("stream")); r["job"] = req["job"]
        except Exception as e:
            r = {"job": req.get("job"), "fed": req.get("feed"), "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
