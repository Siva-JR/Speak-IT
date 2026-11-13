# Architecture

## Processes

```
 hotkey ──► daemon (dictate.py serve) ──► model worker (worker.py)
 (GNOME shortcut                │   ▲            loads faster-whisper on CPU/GPU, exits after
  or Windows RegisterHotKey)    │   │            worker_linger_s idle and returns ALL its memory
                                │   └──── JSON lines over stdin/stdout
                                ├──► records the microphone, delivers the text (types / clipboard / file)
                                └──► control channel ◄── `dictate toggle|status|doctor`, dashboard live test
```

- The **daemon** is small and always on. It imports no numpy and no model code (about 10 MB), records audio, talks to
  the worker, and delivers the text. It logs to `events.log` and tees stderr to `errors.log`.
- The **worker** is started when you press the hotkey (the model loads while you speak), stays for `worker_linger_s`
  after the last use, then exits. It does voice-activity gating, transcription (live, while you speak), hallucination
  filtering, filler removal, and vocabulary/replacement cleanup (`textfix.py`).
- The **dashboard** (`dashboard.py`) is separate and read-mostly; it never imports the daemon.

## One dictation, step by step

1. **Hotkey pressed** → `toggle` → `start_rec(hotkey=True)`: the microphone starts, the worker is spawned if needed,
   the focused app is looked up in the background (it picks the [profile](configuration.md#per-app-profiles)), and
   `keys.KeyWatch` opens the keyboard state.
2. **While recording**, a live thread appends new audio to `RUNTIME/dictate-<id>.raw` every `live_step_s` and sends the
   worker `{"feed": id, "upto": bytes}`. The worker transcribes ahead whenever a piece ends in a pause and it is worth
   it (see below). A hold-watch thread polls the hotkey's key: released after `hold_threshold_s` → stop.
3. **Stop** (second tap, key released, or `stop`): the last audio is flushed and the worker gets
   `{"job": n, "stream": id, "final": true}`. It transcribes only what is left after the last cut and returns the
   whole text plus timings (`live_s`, `tail_s`, `tail_asr_s`, ...).
4. The daemon applies the profile (`profiles.apply`: capitalisation, final full stop, Enter phrases), waits until
   Ctrl/Alt/Shift/Super and the hotkey are released (`KeyWatch.wait_released`; without key state it waits
   `type_settle_ms`), types the text, presses Enter if asked, and remembers what it typed for `undo`.
5. `history.jsonl` gets the record (timings, words, WPM, app, profile), which the dashboard reads.

### Live transcription in the worker

- Voice activity detection runs **once per audio region** (`speech_spans`, unpadded, 250 ms minimum silence). The same
  spans decide gating, where to cut, and what the model hears (padded by 400 ms and merged, exactly what
  faster-whisper's own `vad_filter` would feed it), so the model is called with `vad_filter=False`. Before, the VAD
  ran up to three times per dictation.
- `live_cut` picks the cut: the latest pause ≥ `live_pause_s` after at least the minimum chunk; for people who never
  pause that long, the longest short breath (≥ 0.25 s) once twice the minimum has built up; past `chunk_s` (28 s) the
  longest pause in the second half, and only without any pause a hard cut.
- The minimum chunk adapts: the worker keeps a running estimate of compute seconds per second of audio and waits until
  finishing the rest after stop would exceed `live_target_s`. Fast machines therefore cut rarely (output identical to a
  whole-file transcription), slow ones often.
- Stream state lives only in the worker. If the worker is restarted mid-dictation (busy machine), the final request
  finds no state and simply transcribes the file from the start, so nothing is lost.

## One flow, two platforms

The control logic (`Daemon`, `Worker`, config, logging) is shared. Everything that touches the operating system is
behind a small seam:

| Job | Linux (inside `dictate.py`) | Windows (`platform_win.py`) |
|---|---|---|
| Global hotkey | GNOME custom shortcut runs `dictate toggle` | `RegisterHotKey` (ctypes) in a message-loop thread, 0.35 s debounce |
| Record microphone | `arecord` subprocess | `sounddevice` stream, device/format fallback, resampler to 16 kHz mono |
| Type text | `portal_typer.py` over the GNOME RemoteDesktop portal | `SendInput` with Unicode key events (surrogate pairs, Enter/Tab) |
| Clipboard fallback | `wl-copy` / `xclip` | Win32 clipboard (`CF_UNICODETEXT`) |
| Focused app name (for logs) | AT-SPI helper | `GetForegroundWindow` + process image name |
| Sound cues / notifications | `pw-play`, `notify-send` | `winsound` beeps; notifications are a no-op |
| Recording indicator | top-bar icon (`tray.py`, StatusNotifierItem) | red dot at the top of the screen (`Indicator`: topmost, click-through layered window; amber while transcribing) |
| Hold-to-talk / modifier state | `keys.py`: `EVIOCGKEY` on `/dev/input` keyboards (current state only, never the key stream; needs the `input` group) | `keys.py`: `GetAsyncKeyState` |
| Cancel / undo hotkeys | GNOME shortcuts → `dictate cancel` / `dictate undo` | `RegisterHotKey` (`start_hotkeys`) |
| Enter / Backspace | portal `NotifyKeyboardKeysym` (`{"key": ...}` message) | `SendInput` with `VK_RETURN` / `VK_BACK` |
| CPU idle / process memory | `/proc` | `psutil` |
| Autostart | systemd user service | Startup-folder shortcut |
| CUDA libraries | system `libcublas` | pip `nvidia-*` wheels, DLL directories added at worker start |

`dictate.py` swaps the Linux helpers for the Windows ones at import time when running on Windows
(`if IS_WIN: ...`), so the Linux paths are untouched.

## Shared modules

- `common.py`: OS flags, state/config paths, and the **control channel**. On Linux/macOS the daemon listens on a `0600`
  Unix socket. Windows has no Unix sockets in Python, so it listens on `127.0.0.1:48621` with `SO_EXCLUSIVEADDRUSE`,
  and every request must start with the secret in `STATE/ctl.token` (compared in constant time); other local programs
  cannot drive the daemon.
- `dashboard.py`, `dictate.py`, `worker.py` and `platform_win.py` all take their paths from `common.py`, so they cannot
  disagree about where state lives.
- `keys.py`: key state for hold-to-talk and "type only after the modifiers are up" (stdlib only).
- `profiles.py`: built-in and user profiles, app matching, formatting (stdlib only).
- `learn.py`: corrections → suggestions, and the `vocab.txt` / `replacements.txt` helpers shared by the CLI and the
  dashboard (stdlib + `textfix.py`).
- `bench_e2e.py`: `dictate bench`, the end-to-end benchmark ([Performance](performance.md)).

## Control commands

`toggle`, `start`, `stop`, `cancel`, `undo`, `status`, `warm`, `workerquit`, `replay FILE`, `typetest`, `memstat`,
`events N`, `last`, `quit`, plus the dashboard's `teststart [WAV]`, `level` and `teststop`. `teststart WAV` plays a
16 kHz mono wav into the pipeline in real time instead of the microphone (used by `dictate bench`). A `toggle` that
arrives while the same key press is still held (key auto-repeat) is ignored. The test commands run the normal pipeline in
"dry" mode (text is returned, not typed); `level` returns the last ~64 ms of raw samples so the browser can draw the
spectrum, and it doubles as a heartbeat (no poll for 15 s ends an abandoned test).

## Robustness decisions

- **Logging never breaks dictation**: `elog` swallows its own errors; stats calls (`mem()`) cannot kill the worker.
- **The microphone callback never raises** (an exception in the audio thread aborts the stream); errors are recorded
  and shown in the log and the dashboard instead.
- **No silent wrong-source fallback**: automatic microphone choice only considers the OS's default inputs.
- **Memory returns to the OS**: the worker is a separate process that exits when idle.
- **GPU is optional**: any failure to use CUDA falls back to the CPU with a logged warning.

## Adding a new OS

macOS is the next candidate: the environment, model and dashboard already work. A daemon port needs a
`platform_mac.py` with the same seam as `platform_win.py` (`Typer`, `Recorder`, `start_hotkey`, `clipboard_set`,
`focus_app`, `cue`, `notify`, `idle_cpus`, `proc_mem`), an installer step that creates a LaunchAgent, and handling of
the macOS microphone and accessibility permissions. Select it next to the `IS_WIN` block in `dictate.py`.

## Tests and tooling

- `dictate/tests/test_units.py`: automated tests that run on any OS without a microphone, GPU or desktop: profiles,
  correction suggestions, live-cut planning, key codes and ioctl numbers, Windows hotkey parsing, and daemon flows with
  fakes (terminal formatting, "send it" → Enter, undo and its refusals, cancel during transcription, hold vs tap,
  auto-repeat). Run: `.venv/bin/python -m unittest discover -s dictate/tests -p 'test_*.py'`.
- `dictate bench`: end-to-end latency/WPM/accuracy on real recordings through a private daemon.
- Manual scripts: `mem_flat.py` checks memory stays flat, `worker_check.py`, `keylog.py`.
- The Windows layer was validated with unit-style checks that run on any OS (Win32 struct layouts, hotkey parser,
  UTF-16 key events, token authentication, microphone selection and conversion with fake devices, the resampler, the
  FFT-to-bar mapping in the dashboard) in addition to testing on a real Windows machine.

## Benchmark harness

`bench/`, `run.py`, `run_stream.py`, `run_mem.py`, `report.py`, `configs/` and `scripts/` compare candidate models and
engines (faster-whisper, whisper.cpp quantizations) for word error rate, latency, memory and cold start. Run
`./setup.sh` (venv, dependencies, all candidate models, test data) and then `.venv/bin/python run.py`. It is
independent of the dictation tool.
