# Performance

How fast Speak-IT turns speech into text, how to measure it on your own machine, and what has been measured.

## What matters to the user

The wait that counts is **stop → text**: from pressing stop (or letting go of the hotkey) until the words are in the
app. It has three parts:

| Part | What decides it |
|---|---|
| Transcription left after stop | With [live transcription](configuration.md#live-transcription) only the audio since the last cut; otherwise the whole recording. |
| Waiting for the keys | Typing starts when Ctrl/Alt/Shift/Super and the hotkey are up. With key state (Windows, or Linux in the `input` group) that is usually immediate; without it a fixed `type_settle_ms` (350 ms). |
| Typing | One character every `type_delay_ms` (6 ms): about 0.6 s for 100 characters. Terminal UIs mis-handle faster bursts. |

## Run the benchmark

```bash
dictate bench                 # your saved recordings + one ~60 s dictation built from them, live vs whole
dictate bench --quick         # 6 recordings, live only (about a minute)
dictate bench --clips DIR     # your own 16 kHz mono wavs; NAME.txt next to a wav = its reference transcript
```

It starts a private daemon (its own state folder and control socket, nothing typed, your history untouched) and plays
each recording into the real pipeline **in real time**, as if spoken, then presses stop. Results are appended to
`bench.jsonl` in the state folder and shown in the dashboard's Performance panel.

| Column | Meaning |
|---|---|
| ready | stop → final text ready, before typing (median, p90; by length: under 10 s, 10-30 s, over 30 s) |
| cold start | the first recording, when the model process starts with the hotkey press |
| WPM | words ÷ recording length × 60 |
| effective WPM | words ÷ (recording length + ready) × 60 |
| WER | word error rate against the reference. For saved recordings the "reference" is what was transcribed when you dictated it, so it measures consistency, not accuracy. Use `--clips` with transcripts for real accuracy. |

The dashboard also shows the same measures from your **real use** (stop → text including typing, WPM, effective WPM).

## Measured results

Author's laptop: 12 CPUs, RTX 4050 Laptop GPU, `base.en`, 33 real dictations (274 s) plus a 66 s dictation made of
them. `dictate bench`, 2026-11-13. Seconds from stop to text ready.

| Mode | median | p90 | 10-30 s | 66 s | cold start | WER vs first |
|---|---|---|---|---|---|---|
| GPU, live transcription | 0.10 | 0.25 | 0.23 | **0.46** | 0.46 | 0 |
| GPU, whole recording after stop | 0.10 | 0.34 | 0.34 | 0.81 | 0.50 | 0 |

CPU (same machine, `device = "cpu"`, measured directly on the worker):

| Dictation | whole recording after stop | live transcription |
|---|---|---|
| under 25 s | 0.1-0.45 s | same text, same time (no cut needed) |
| 98 s | 1.78 s | 0.46 s |
| 165 s | 2.75 s | ~0 s (the end was silence) |

## Before and after this round of changes

| Change | Effect |
|---|---|
| Voice detection once per audio region instead of up to three times | ~7% less transcription time across the 42-clip set, same text on 37 of 42 clips (the others differ by a word on very quiet clips) |
| Typing waits for the keys instead of a fixed 350 ms | up to 350 ms less on every dictation (real-use median stop → text before: 0.52 s, p90 1.28 s) |
| Live transcription | the wait after stop no longer grows with the length of the dictation |
| GPU settings | measured: `int8_float16` ~20% slower than `float16`, beam 1 no faster than beam 2, so the defaults stayed |

## Where the remaining time goes

- **Typing** is now the largest part for anything longer than a sentence (about 6 ms per character).
- Voice detection on the remaining audio is a noticeable share on a GPU (the model itself takes ~0.1-0.2 s for 20 s of
  speech). Reusing the detection from the live passes would shave a little more.
