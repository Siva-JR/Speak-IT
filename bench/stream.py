"""Streaming simulator on a VIRTUAL clock.

Audio "arrives" in real time but we don't sleep: virtual time advances by the REAL measured decode time,
so a slow decoder falls behind exactly as it would live. Headline metric = final latency: time from the
end of speech until the final text is available (includes the endpoint-detection delay).

Policies
  rolling : every `step` s re-decode the growing utterance buffer (partials), then one final pass.
            Works with any engine. Final latency ~ offline latency of the whole utterance.
  commit  : whisper_streaming-style local agreement. Decode the uncommitted buffer with word timestamps;
            words that two consecutive passes agree on are committed and the buffer is trimmed to them.
            The final pass only has to decode the short uncommitted tail. faster-whisper only.
"""
import time

from bench.textnorm import normalize

SR = 16000


def _norm_word(w):
    n = normalize(w)
    return n


def _prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _flicker(displays):
    """Words shown then retracted between consecutive displays (visual instability)."""
    total = 0
    for prev, cur in zip(displays, displays[1:]):
        p, c = prev.split(), cur.split()
        total += len(p) - _prefix_len(p, c)
    return total


def simulate(eng, audio, policy="commit", step=0.5, endpoint_s=0.4, min_chunk=0.6, max_buf=14.0):
    L = len(audio) / SR
    now = 0.0                 # virtual time the decoder becomes free
    next_sched = step         # next scheduled pass
    decode_total, passes = 0.0, 0
    displays, first_text, first_commit = [], None, None
    committed, lag = [], []   # committed: [(norm_word, raw_word)]
    buf_start, prev_hyp = 0.0, []

    def run_text(a, b):
        t = time.perf_counter()
        txt, _ = eng.transcribe(audio[int(a * SR):int(b * SR)])
        return txt, time.perf_counter() - t

    def run_words(a, b):
        t = time.perf_counter()
        w = eng.transcribe_words(audio[int(a * SR):int(b * SR)])
        return [(s + a, e + a, x) for s, e, x in w], time.perf_counter() - t

    while True:
        start = max(now, next_sched)
        if start >= L:
            break
        avail = min(start, L)
        next_sched = start + step
        if avail - buf_start < min_chunk:
            now = start
            continue
        if policy == "rolling":
            txt, d = run_text(buf_start, avail)
            decode_total += d; passes += 1
            now = start + d
            displays.append(txt)
            if txt and first_text is None:
                first_text = now
        else:
            words, d = run_words(buf_start, avail)
            decode_total += d; passes += 1
            now = start + d
            hyp = [(s, e, w) for s, e, w in words if _norm_word(w)]
            n = _prefix_len([_norm_word(w) for _, _, w in prev_hyp], [_norm_word(w) for _, _, w in hyp])
            for s, e, w in hyp[:n]:
                committed.append(w)
                lag.append(now - e)
                if first_commit is None:
                    first_commit = now
            if n:
                buf_start = min(hyp[n - 1][1], avail)         # trim buffer at the last committed word
            prev_hyp = hyp[n:]
            disp = " ".join(committed + [w for _, _, w in prev_hyp])
            displays.append(disp)
            if disp and first_text is None:
                first_text = now

    # end of speech: endpoint detector fires endpoint_s later, then one final pass over the buffer
    start = max(now, L + endpoint_s)
    if policy == "rolling":
        final, d = run_text(buf_start, L)
    else:
        words, d = run_words(buf_start, L)
        tail = [w for _, _, w in words if _norm_word(w)]
        for s, e, w in words:
            if _norm_word(w):
                lag.append(start + d - e)
        final = " ".join(committed + tail)
        if first_commit is None and final:
            first_commit = start + d
    decode_total += d; passes += 1
    ready = start + d
    if final and first_text is None:
        first_text = ready
    return {
        "final_text": final.strip(),
        "audio_s": L,
        "final_latency_s": ready - L,                 # headline: end of speech -> final text
        "decode_after_endpoint_s": d,                 # compute part of that latency
        "first_text_s": first_text,
        "first_commit_s": first_commit,
        "mean_commit_lag_s": (sum(lag) / len(lag)) if lag else None,
        "passes": passes,
        "decode_total_s": decode_total,
        "stream_rtf": decode_total / L,               # total compute / audio length
        "flicker_words": _flicker(displays),
    }
