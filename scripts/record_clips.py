#!/usr/bin/env python3
"""Record YOUR voice reading prompts (technical terms, numbers, names). Saves wav + reference text.

  .venv/bin/python scripts/record_clips.py            # records all prompts you haven't done yet
Press Enter to start each clip, Enter again to stop. Then run scripts/prepare_data.py --custom-only.
"""
import subprocess
import sys
import termios
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_data import clip_ok

D = Path(__file__).resolve().parent.parent / "data" / "custom"
D.mkdir(parents=True, exist_ok=True)

PROMPTS = [
    "Please refactor the authentication middleware and add a unit test for the token refresh path.",
    "The Kubernetes pod keeps crashing with an out of memory error after about ten minutes.",
    "Send the invoice for four thousand two hundred and fifty dollars to Priya by Friday.",
    "Set up a Rust workspace with three crates, one for audio capture, one for inference, and one for the user interface.",
    "Can you schedule a meeting with Daniel and Aisha for next Tuesday at three thirty in the afternoon.",
    "The latency dropped from eight hundred milliseconds to about two hundred after switching to int eight quantization.",
    "Open a pull request against the main branch and request a review from the platform team.",
    "I think we should use PostgreSQL with a Redis cache in front of it and deploy everything on AWS.",
    "Turn off the notifications, mute the channel, and remind me again at nine tomorrow morning.",
    "Okay so the first step is, um, to clone the repository and then run cargo build in release mode.",
    "The quick brown fox jumps over the lazy dog near the riverbank at sunrise.",
    "Next quarter we expect revenue to grow by roughly twelve percent, mostly from enterprise subscriptions.",
]

def flush_stdin():
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)   # drop stray Enter presses
    except Exception:
        pass


refs = D / "refs.tsv"
existing = {}
if refs.exists():
    for line in refs.read_text().splitlines():
        if "\t" in line:
            fn, txt = line.split("\t", 1)
            existing[fn] = txt

for i, text in enumerate(PROMPTS, 1):
    fn = f"clip{i:02d}.wav"
    if fn in existing and (D / fn).exists() and clip_ok(D / fn, existing[fn])[0]:
        continue                                       # already good, keep it
    while True:
        print(f"\n[{i}/{len(PROMPTS)}] Read aloud, at normal pace:\n  {text}")
        flush_stdin()
        input("  Enter to START, speak, then Enter to STOP... ")
        p = subprocess.Popen(["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", str(D / fn)])
        flush_stdin()
        try:
            input("  recording... ")
        finally:
            p.terminate()
            p.wait()
        ok, why = clip_ok(D / fn, text)
        if ok:
            print("  ok")
            break
        print(f"  rejected: {why}. Try again.")
    existing[fn] = text
    with open(refs, "w") as f:
        for k in sorted(existing):
            f.write(f"{k}\t{existing[k]}\n")
print("\ndone. Now run: .venv/bin/python scripts/prepare_data.py --custom-only")
