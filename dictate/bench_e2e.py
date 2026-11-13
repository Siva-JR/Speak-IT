#!/usr/bin/env python3
"""End-to-end benchmark of the real dictation pipeline: recordings are played into a private daemon IN REAL TIME, as
if spoken into the microphone, and we measure what a user waits for after pressing stop.

  dictate bench                     your saved recordings (state/audio), plus one long dictation built from them
  dictate bench --clips DIR         16 kHz mono wavs in DIR (a NAME.txt next to a wav is its reference transcript)
  dictate bench --quick             at most 6 clips, live mode only
  dictate bench --modes live,whole  compare live transcription with transcribing everything after stop (default)

It starts its own daemon (separate state folder and control socket, nothing is typed, your history is untouched), so
it is safe to run while you use dictation. Results are appended to STATE/bench.jsonl; the dashboard shows the latest.

Metrics per recording: ready_s (stop -> final text, before typing), words, WPM = words per minute of speaking, and
effective WPM = words / (speaking time + ready_s). With a reference: WER (word error rate). Without one, a saved
recording is compared with what was transcribed when you dictated it ("vs first"), which shows consistency, not
accuracy."""
import argparse, json, os, platform, shutil, statistics, subprocess, sys, tempfile, time, wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import STATE

SR = 16000


def norm(t):
    import re
    return re.sub(r"[^a-z0-9' ]", " ", (t or "").lower()).split()


def wer(ref, hyp):
    """Word error rate: word-level edit distance / reference length."""
    r, h = norm(ref), norm(hyp)
    if not r:
        return None
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return round(d[len(h)] / len(r), 4)


def wav_seconds(p):
    with wave.open(str(p)) as w:
        return w.getnframes() / w.getframerate()


def collect(args, tmp):
    """[(path, reference or None, label)]."""
    clips = []
    if args.clips:
        for p in sorted(Path(args.clips).glob("*.wav")):
            ref = p.with_suffix(".txt").read_text().strip() if p.with_suffix(".txt").exists() else None
            clips.append((p, ref, "ref" if ref else None))
    else:
        first = {}
        try:
            for l in (STATE / "history.jsonl").read_text().splitlines():
                r = json.loads(l)
                if r.get("text"):
                    first[r["ts"].replace(":", "") + ".wav"] = r["text"]
        except OSError:
            pass
        for p in sorted((STATE / "audio").glob("*.wav")):
            if p.name in first and wav_seconds(p) >= 1.0:
                clips.append((p, first[p.name], "first"))
    good = []
    for p, ref, kind in clips:
        with wave.open(str(p)) as w:
            if w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2:
                good.append((p, ref, kind))
    if args.quick:
        good = sorted(good, key=lambda c: wav_seconds(c[0]))[:: max(1, len(good) // 6)][:6]
    # one long dictation (~60 s) made of the clips with short pauses: where live transcription matters
    if good and not args.quick:
        out, refs, total = tmp / "long.wav", [], 0.0
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            for p, ref, _ in sorted(good, key=lambda c: -wav_seconds(c[0])):
                with wave.open(str(p)) as r:
                    w.writeframes(r.readframes(r.getnframes()) + b"\0\0" * int(0.4 * SR))
                refs.append(ref or ""); total += wav_seconds(p) + 0.4
                if total >= 60:
                    break
        good.append((out, " ".join(refs), "first" if not args.clips else "ref"))
    return good


class PrivateDaemon:
    def __init__(self, tmp, live):
        self.state = tmp / ("state-live" if live else "state-whole")
        env = dict(os.environ, DICTATE_STATE=str(self.state), PYTHONUNBUFFERED="1",
                   DICTATE_SOCK=str(tmp / f"bench-{int(live)}.sock"), DICTATE_PORT=str(48690 + int(live)),
                   DICTATE_CFG=json.dumps({"output": "print", "tray": False, "notify": False, "sounds": False,
                                           "save_audio": False, "hotkey": "", "cancel_hotkey": "", "undo_hotkey": "",
                                           "live_transcribe": live, "worker_linger_s": 600}))
        self.env = env
        self.p = subprocess.Popen([sys.executable, str(HERE / "dictate.py"), "serve"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if "ready" not in (self.p.stdout.readline() or ""):
            raise RuntimeError("benchmark daemon did not start")

    def cmd(self, c, timeout=300):
        r = subprocess.run([sys.executable, str(HERE / "dictate.py")] + c.split(" ", 1), env=self.env,
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()

    def close(self):
        try:
            self.cmd("quit", 10)
        except Exception:
            pass
        self.p.wait(timeout=10)


def run_clip(d, path):
    r = json.loads(d.cmd(f"teststart {path}"))
    if not r.get("ok"):
        raise RuntimeError(r.get("msg"))
    time.sleep(wav_seconds(path) + 0.15)                  # let it "speak" the whole clip, then press stop
    t = time.perf_counter()
    out = json.loads(d.cmd("teststop"))
    out["client_s"] = round(time.perf_counter() - t, 3)    # includes two process launches for the CLI round trip
    return out


def summarize(rows):
    def med(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.median(xs), 3) if xs else None
    warm = [r for r in rows if not r["cold"]]
    out = {"n": len(rows), "ready_s_median": med([r["ready_s"] for r in warm]),
           "ready_s_p90": round(sorted(r["ready_s"] for r in warm)[int(0.9 * (len(warm) - 1))], 3) if warm else None,
           "ready_s_cold": med([r["ready_s"] for r in rows if r["cold"]]),
           "wpm_median": med([r["wpm"] for r in rows]), "eff_wpm_median": med([r["eff_wpm"] for r in rows]),
           "wer_median": med([r["wer"] for r in rows if r.get("wer") is not None])}
    for lo, hi, name in ((0, 10, "short"), (10, 30, "medium"), (30, 1e9, "long")):
        b = [r["ready_s"] for r in warm if lo <= r["audio_s"] < hi]
        out[f"ready_s_{name}"] = med(b)
    return out


def main():
    ap = argparse.ArgumentParser(description="end-to-end dictation benchmark")
    ap.add_argument("--clips"); ap.add_argument("--quick", action="store_true")
    ap.add_argument("--modes", default="live,whole")
    a = ap.parse_args()
    modes = ["live"] if a.quick else [m.strip() for m in a.modes.split(",") if m.strip()]
    tmp = Path(tempfile.mkdtemp(prefix="dictate-bench-"))
    clips = collect(a, tmp)
    if not clips:
        sys.exit("no recordings to benchmark: dictate a few times first (save_audio = true), or pass --clips DIR")
    total = sum(wav_seconds(p) for p, _, _ in clips)
    print(f"{len(clips)} recordings, {total:.0f} s of audio, modes {modes}: about {total * len(modes) / 60 + 0.5:.0f} min "
          "(played in real time)")
    result = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "machine": f"{platform.system()} {platform.machine()}, "
              f"{os.cpu_count()} CPUs", "clips": len(clips), "audio_s": round(total, 1), "modes": {}}
    for mode in modes:
        d = PrivateDaemon(tmp, live=mode == "live")
        rows = []
        try:
            for i, (p, ref, kind) in enumerate(clips):
                cold = i == 0
                r = run_clip(d, p)
                words = r.get("words") or len((r.get("text") or "").split())
                row = {"clip": p.name, "audio_s": r.get("audio_s") or round(wav_seconds(p), 2), "cold": cold,
                       "ready_s": r.get("ready_s"), "client_s": r["client_s"], "asr_s": r.get("asr_s"),
                       "tail_asr_s": r.get("tail_asr_s"), "live_s": r.get("live_s"), "words": words,
                       "device": r.get("device"), "load_s": r.get("load_s"),
                       "wer": wer(ref, r.get("text")) if ref else None, "ref_kind": kind, "text": r.get("text", "")}
                row["wpm"] = round(words / row["audio_s"] * 60, 1) if row["audio_s"] else None
                row["eff_wpm"] = round(words / (row["audio_s"] + (row["ready_s"] or 0)) * 60, 1) if row["audio_s"] else None
                rows.append(row)
                print(f"  [{mode}] {p.name[:28]:<28} {row['audio_s']:6.1f}s  ready {row['ready_s']}s"
                      f"{'  (cold start)' if cold else ''}  {words} words  WER/{kind or '-'} {row['wer']}", flush=True)
        finally:
            d.close()
        result["modes"][mode] = {"summary": summarize(rows), "rows": rows,
                                 "device": next((r["device"] for r in rows if r.get("device")), None)}
    shutil.rmtree(tmp, ignore_errors=True)
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "bench.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")
    print("\nmode    ready median  p90    short  medium  long   cold   WPM   effective WPM  WER")
    for mode, m in result["modes"].items():
        s = m["summary"]
        print(f"{mode:<7} {s['ready_s_median']!s:<13} {s['ready_s_p90']!s:<6} {s['ready_s_short']!s:<6} "
              f"{s['ready_s_medium']!s:<7} {s['ready_s_long']!s:<6} {s['ready_s_cold']!s:<6} {s['wpm_median']!s:<5} "
              f"{s['eff_wpm_median']!s:<14} {s['wer_median']}")
    print(f"\nsaved to {STATE / 'bench.jsonl'}  (dictate dashboard -> Performance)")


if __name__ == "__main__":
    main()
