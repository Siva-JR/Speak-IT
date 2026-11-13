# Configuration

Defaults work out of the box. To change something, create a TOML file:

| OS | Config file |
|---|---|
| Linux | `~/.config/dictate/config.toml` |
| Windows | `%APPDATA%\dictate\config.toml` |

```toml
device = "cpu"                 # example
hotkey = "ctrl+shift+space"    # Windows only
```

Restart the daemon after editing (Linux: `systemctl --user restart dictate`; Windows: see
[installation](installation.md#windows)). The dashboard's Config panel shows what is in effect.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `model` | `models/faster-whisper-base.en` | Path to a faster-whisper model folder. |
| `device` | `"auto"` | `"cuda"`, `"cpu"` or `"auto"` (GPU if usable, else CPU; falls back to CPU if the GPU fails to load). |
| `compute_type` | `"int8"` | Precision on the CPU. |
| `gpu_compute_type` | `"float16"` | Precision on the GPU. |
| `beam_size` | `2` | Decoding beam width (higher: slightly more accurate, slower). |
| `threads` | `4` | CPU threads for the model. |
| `adaptive_threads`, `busy_idle_cpus` | `true`, `4` | Drop to 1 thread when fewer than `busy_idle_cpus` CPUs are idle, so a busy machine does not stall dictation. |
| `pin_threads` | `true` | Linux: one engine thread per physical core. |
| `worker_linger_s` | `30` | Seconds the model process stays loaded after the last use, then exits and frees all its memory. |
| `max_record_s` | `600` | Longest single recording. |
| `output` | `"auto"` | `"auto"` types the text and falls back to the clipboard, then a file; `"portal"` only types (errors if it cannot); `"print"` only prints; any other value (e.g. `"clipboard"`) copies to the clipboard without typing. |
| `trailing_space` | `true` | Add a space after the typed text. |
| `type_delay_ms`, `type_settle_ms` | `6`, `350` | Pause between typed characters; minimum wait after the stop key before typing (so held modifier keys are released). |
| `remove_fillers`, `auto_gain`, `halluc_filter` | `true` | Text/audio cleanup: drop "um/uh", normalize quiet audio, suppress phantom phrases Whisper invents on silence. |
| `min_speech_s`, `chunk_s`, `min_window_s`, `window_step_s` | `0.25`, `28`, `15`, `5` | Speech gating and chunking. |
| `save_audio`, `keep_audio` | `true`, `100` | Keep the last N recordings in the state folder for replay (set `save_audio = false` to disable). |
| `notify`, `sounds`, `tray` | `true` | Notifications, sound cues, tray icon. Windows has no notifications or tray (beeps only). |
| `hotkey` | `"ctrl+alt+space"` | **Windows only.** Global hotkey: modifiers `ctrl`, `alt`, `shift`, `win` plus one key (`space`, `a`-`z`, `0`-`9`, `f1`-`f24`, `enter`, ...). Linux uses the GNOME shortcut set by the installer. |
| `mic_device` | not set | **Windows only.** Which microphone to use: an index (`2`) or part of its name (`"realtek"`). Unset: the system default. |

## Microphone selection (Windows)

With `mic_device` unset, the daemon tries the system default input, then each audio API's own default input, and uses
the first that opens. For each it tries 16 kHz mono first, then the device's native rate and channel count, converting
to 16 kHz mono itself. It does **not** fall back to arbitrary other inputs (for example "Stereo Mix", which records the
speakers); if no default works it stops with an error naming every device and format it tried. Set `mic_device` to
choose one deliberately. `dictate doctor` records one second and reports whether audio really arrives.

## Environment variables

| Variable | Effect |
|---|---|
| `DICTATE_DEVICE` | Overrides `device` (`cpu`, `cuda`, `auto`). |
| `DICTATE_CFG` | JSON object merged over the config (useful in scripts and tests). |
| `DICTATE_STATE` | Alternative state folder (logs, history, audio). |
| `DICTATE_SOCK` | Linux: alternative control-socket path. |
| `DICTATE_PORT` | Windows: control port (default `48621`). |
| `DICTATE_HOTKEY`, `DICTATE_MODEL` | Read by the installer only (see [installation](installation.md)). |

## Teaching it words

Two plain-text lists in the config folder, re-read on every dictation, no restart needed.

```bash
dictate vocab add Nuvai                              # near-miss transcriptions snap to this spelling
dictate vocab add faster-whisper
dictate replace add "comment and push => commit and push"   # exact fix for a phrase it keeps mishearing
dictate fixtest "comment and push this"              # try the rules without recording
dictate vocab list | dictate replace list | dictate vocab rm TERM | dictate replace rm "rule"
```

Keep replacement rules narrow: a rule replaces the whole matching phrase, so write the part you want to keep into the
right-hand side, and avoid phrases you might genuinely say ("comment" alone should not become "commit").

## Where things live

| | Linux | Windows |
|---|---|---|
| Config, vocabulary, replacements | `~/.config/dictate/` | `%APPDATA%\dictate\` |
| `events.log` (what the daemon did) | `~/.local/state/dictate/` | `%LOCALAPPDATA%\dictate\state\` |
| `errors.log` (stderr, tracebacks) | same | same |
| `history.jsonl`, `last.txt`, `audio/` | same | same |
| Control channel | `$XDG_RUNTIME_DIR/dictate.sock` | `127.0.0.1:48621` + `ctl.token` in the state folder |

Logs rotate at about 1 MB (`events.log`, `events.log.1`, `events.log.2`; `errors.log`, `errors.log.1`).
