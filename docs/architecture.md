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
  after the last use, then exits. It does voice-activity gating, transcription, hallucination filtering, filler
  removal, and vocabulary/replacement cleanup (`textfix.py`).
- The **dashboard** (`dashboard.py`) is separate and read-mostly; it never imports the daemon.

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

## Control commands

`toggle`, `start`, `stop`, `status`, `warm`, `workerquit`, `replay FILE`, `typetest`, `memstat`, `events N`, `last`,
`quit`, plus the dashboard's `teststart`, `level` and `teststop`. The test commands run the normal pipeline in
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

- `dictate/tests/`: manual/live scripts (`live_sim.py` feeds recorded audio through the live pipeline in real time,
  `mem_flat.py` checks memory stays flat, `worker_check.py`, `keylog.py`).
- The Windows layer was validated with unit-style checks that run on any OS (Win32 struct layouts, hotkey parser,
  UTF-16 key events, token authentication, microphone selection and conversion with fake devices, the resampler, the
  FFT-to-bar mapping in the dashboard) in addition to testing on a real Windows machine.

## Benchmark harness

`bench/`, `run.py`, `run_stream.py`, `run_mem.py`, `report.py`, `configs/` and `scripts/` compare candidate models and
engines (faster-whisper, whisper.cpp quantizations) for word error rate, latency, memory and cold start. Run
`./setup.sh` (venv, dependencies, all candidate models, test data) and then `.venv/bin/python run.py`. It is
independent of the dictation tool.
