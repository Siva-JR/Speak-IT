"""Engine adapters. Each exposes load() and transcribe(audio) -> (text, time_to_first_text_s)."""
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def resolve_model(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else ROOT / p)


def vad_trim(audio):
    """Keep only speech (Silero VAD bundled with faster-whisper). Returns empty array if no speech."""
    import numpy as np
    from faster_whisper.vad import get_speech_timestamps
    chunks = get_speech_timestamps(audio)
    if not chunks:
        return audio[:0]
    return np.concatenate([audio[c["start"]:c["end"]] for c in chunks])


class FasterWhisper:
    """CTranslate2 through faster-whisper."""

    def __init__(self, cfg):
        self.cfg = cfg

    def load(self):
        from faster_whisper import WhisperModel
        c = self.cfg
        self.m = WhisperModel(
            resolve_model(c["model"]), device="cpu",
            compute_type=c.get("compute_type", "float32"),
            cpu_threads=int(c.get("threads", 4)), num_workers=1,
        )

    def unload(self):
        import gc
        self.m = None
        gc.collect()

    def transcribe_words(self, audio):
        """Word-level timestamps (used by the streaming commit policy). Returns [(start, end, word)]."""
        segs, _ = self.m.transcribe(
            audio, language="en", beam_size=int(self.cfg.get("beam_size", 5)), best_of=1,
            temperature=0.0, condition_on_previous_text=False, word_timestamps=True,
            vad_filter=False,
        )
        return [(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or [])]

    def transcribe(self, audio):
        c = self.cfg
        t0 = time.perf_counter()
        segs, _ = self.m.transcribe(
            audio, language="en", beam_size=int(c.get("beam_size", 5)), best_of=1,
            temperature=0.0, condition_on_previous_text=False, without_timestamps=True,
            vad_filter=bool(c.get("vad", False)),
        )
        first, parts = None, []
        for s in segs:  # generator: decoding happens lazily here
            if first is None:
                first = time.perf_counter() - t0
            parts.append(s.text)
        total = time.perf_counter() - t0
        return "".join(parts).strip(), (first if first is not None else total)


class WhisperCpp:
    """whisper.cpp through pywhispercpp. Quantisation is baked into the ggml file name."""

    def __init__(self, cfg):
        self.cfg = cfg

    def load(self):
        from pywhispercpp.model import Model
        c = self.cfg
        beam = int(c.get("beam_size", 5))
        params = dict(
            n_threads=int(c.get("threads", 4)), language="en", no_context=True,
            print_progress=False, print_realtime=False, print_timestamps=False,
            translate=False, single_segment=bool(c.get("single_segment", False)),
        )
        if c.get("temperature") is not None:
            params["temperature"] = float(c["temperature"])
        if c.get("initial_prompt"):
            params["initial_prompt"] = c["initial_prompt"]
        if c.get("audio_ctx"):
            params["audio_ctx"] = int(c["audio_ctx"])  # shrinks Whisper's 30 s padded window
        if beam > 1:
            params["beam_search"] = {"beam_size": beam, "patience": -1.0}
        self.m = Model(
            resolve_model(c["model"]), params_sampling_strategy=1 if beam > 1 else 0,
            redirect_whispercpp_logs_to=os.devnull, **params,
        )

    def unload(self):
        import gc
        self.m = None
        gc.collect()

    def transcribe(self, audio):
        t0 = time.perf_counter()
        if self.cfg.get("vad"):
            audio = vad_trim(audio)
            if len(audio) < 1600:          # < 0.1 s of speech: nothing to decode
                return "", time.perf_counter() - t0
        segs = self.m.transcribe(audio)
        total = time.perf_counter() - t0   # whisper.cpp has no lazy output: ttft == total
        return " ".join(s.text.strip() for s in segs).strip(), total


ENGINES = {"faster_whisper": FasterWhisper, "whisper_cpp": WhisperCpp}


def make(cfg):
    return ENGINES[cfg["engine"]](cfg)
