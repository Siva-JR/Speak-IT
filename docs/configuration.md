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
| `gpu_compute_type` | `"float16"` | Precision on the GPU. Measured on an RTX 4050: `int8_float16` was about 20% slower and saved only ~30 MB of VRAM, so `float16` stays the default. A GPU without fast half precision falls back to `float32` automatically. |
| `beam_size` | `2` | Decoding beam width (higher: slightly more accurate, slower). On the GPU, beam 1 was no faster than 2. |
| `threads` | `4` | CPU threads for the model. |
| `adaptive_threads`, `busy_idle_cpus` | `true`, `4` | Drop to 1 thread when fewer than `busy_idle_cpus` CPUs are idle, so a busy machine does not stall dictation. |
| `pin_threads` | `true` | Linux: one engine thread per physical core. |
| `worker_linger_s` | `30` | Seconds the model process stays loaded after the last use, then exits and frees all its memory. |
| `max_record_s` | `600` | Longest single recording. |
| `output` | `"auto"` | `"auto"` types the text and falls back to the clipboard, then a file; `"portal"` only types (errors if it cannot); `"print"` only prints; any other value (e.g. `"clipboard"`) copies to the clipboard without typing. |
| `trailing_space` | `true` | Add a space after the typed text. |
| `type_delay_ms` | `6` | Pause between typed characters (terminal UIs mis-handle instant bursts). Can be set per app, see [profiles](#per-app-profiles). |
| `type_settle_ms` | `350` | Fallback only: when key state cannot be read (Linux without `/dev/input` access), wait this long after the stop key before typing. Otherwise typing starts the moment Ctrl/Alt/Shift/Super and the hotkey are released, usually with no wait at all. |
| `hotkey_mode` | `"auto"` | `"auto"`: tap the hotkey to start, tap again to stop, **or** hold it while you talk and let go to finish. `"toggle"`: taps only. `"hold"`: talk only while held. Holding needs key state (Windows: always; Linux: [`input` group](installation.md#hold-to-talk-on-linux)), otherwise the hotkey toggles. |
| `hold_threshold_s` | `0.4` | In `auto` mode, a press held at least this long counts as hold-to-talk. |
| `cancel_hotkey`, `undo_hotkey` | `"ctrl+alt+backspace"`, `"ctrl+alt+z"` | **Windows only** (Linux: GNOME shortcuts set by the installer). Cancel throws away the current recording, or the dictation being transcribed. Undo deletes the text of the last dictation. Set to `""` to disable. |
| `undo_window_s` | `120` | Undo only works this many seconds after the text was typed, only in the same app, and not after the text was sent with Enter. |
| `live_transcribe` | `true` | Transcribe while you speak, so that after you stop only the last few seconds are left. See [live transcription](#live-transcription). |
| `live_step_s`, `live_min_chunk_s`, `live_target_s`, `live_pause_s` | `1.0`, `8`, `0.3`, `0.6` | How often audio is handed to the model while recording; the shortest piece transcribed ahead; transcribe ahead only once finishing the rest after you stop would take longer than this many seconds; a pause this long is the preferred place to cut. |
| `profiles` | built in | Per-app formatting, see [profiles](#per-app-profiles). |
| `remove_fillers`, `auto_gain`, `halluc_filter` | `true` | Text/audio cleanup: drop "um/uh", normalize quiet audio, suppress phantom phrases Whisper invents on silence. |
| `min_speech_s`, `chunk_s`, `min_window_s`, `window_step_s` | `0.25`, `28`, `15`, `5` | Speech gating and chunking. |
| `save_audio`, `keep_audio` | `true`, `100` | Keep the last N recordings in the state folder for replay (set `save_audio = false` to disable). |
| `notify`, `sounds`, `tray` | `true` | Notifications, sound cues, tray icon. Windows has no notifications or tray (beeps only). |
| `hotkey` | `"ctrl+alt+space"` | **Windows only.** Global hotkey: modifiers `ctrl`, `alt`, `shift`, `win` plus one key (`space`, `a`-`z`, `0`-`9`, `f1`-`f24`, `enter`, ...). Linux uses the GNOME shortcut set by the installer. |
| `mic_device` | not set | **Windows only.** Which microphone to use: an index (`2`) or part of its name (`"realtek"`). Unset: the system default. |

## Live transcription

While you speak, the daemon appends the audio to a file and hands it to the model about once a second. When enough
audio has built up and it ends in a pause, that part is transcribed right away, with the text before it as context.
When you stop, only the words since the last cut are left, so the wait after stop stays short however long you talk.

The worker measures how fast this machine transcribes and only cuts early when transcribing everything at the end would
take longer than `live_target_s` (0.3 s). On a fast machine a normal dictation is therefore never cut and comes out word
for word as it would without live mode; on a slow laptop, or for long dictations, it cuts at pauses. Measured on the
author's recordings (CPU, base.en): identical text for every dictation under 25 s, and for a 98 s dictation the wait
after stop went from 1.78 s to 0.46 s. Cutting can change a word or a comma near the cut (in a long test, 3-9% of words
differed, some better, some worse). Set `live_transcribe = false` to always transcribe the whole recording at the end.

## Per-app profiles

The app that has focus when you start dictating picks a profile, which decides how the text is formatted and typed.

| Profile | Applies to | What it does |
|---|---|---|
| `terminal` | GNOME Terminal, Console, Ptyxis, Konsole, kitty, Alacritty, WezTerm, foot, Ghostty, Windows Terminal, cmd, PowerShell, ... | No capital on the first word ("git status", never "I" or your vocabulary terms), no full stop when the text is a single sentence. Multi-sentence prompts keep their punctuation. |
| `chat` | Slack, Discord, Telegram, Signal, WhatsApp, Teams, Element, ... | No full stop when the text is a single sentence. |
| `default` | everything else | Text as transcribed. |

Every profile accepts these keys:

| Key | Values |
|---|---|
| `apps` | app names (case and punctuation ignored). The dashboard's History and `events.log` (`target=`) show the name of each app you dictated into. |
| `capitalize` | `false`: lower-case the first word |
| `final_period` | `"keep"`, `"single"` (drop it if the text is one sentence), `"strip"` (always drop it) |
| `trailing_space` | `true` / `false` (default: the global setting) |
| `enter_phrases` | phrases that, said at the very end, are removed and press **Enter** after typing. Saying only the phrase just presses Enter. |
| `type_delay_ms` | typing speed for this app |
| `output` | e.g. `"clipboard"` for an app that rejects typed input |

```toml
[profiles.terminal]
enter_phrases = ["send it"]          # "run the tests, send it" types "run the tests" and presses Enter

[profiles.chat]
final_period = "keep"

[profiles.notes]                     # a new profile
apps = ["obsidian", "notion"]
capitalize = true
```

Keys you set are merged into the built-in profile; `apps` replaces its list. `enter_phrases` is off by default because
pressing Enter in a shell runs the command: pick a phrase you will not say by accident.

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

### Learning from corrections

You do not have to write rules by hand. Correct a dictation and Speak-IT suggests what to learn:

```bash
dictate fix "raise a PR against main"     # the corrected version of your last dictation
dictate suggestions                       # open suggestions
dictate suggestions accept 3              # or: dictate suggestions dismiss 3
```

Or press **Correct** on any dictation in the dashboard's History. For each word or phrase that changed, the suggestion
is a **vocabulary term** when adding it to `vocab.txt` would have produced your spelling (so near-misses are fixed too),
otherwise an exact **replacement rule**. Nothing changes until you accept. The same correction made again bumps the
count; a dismissed suggestion comes back after you make the same fix three times. Pure punctuation or capitalisation of
the first word is ignored.

## Where things live

| | Linux | Windows |
|---|---|---|
| Config, vocabulary, replacements | `~/.config/dictate/` | `%APPDATA%\dictate\` |
| `events.log` (what the daemon did) | `~/.local/state/dictate/` | `%LOCALAPPDATA%\dictate\state\` |
| `errors.log` (stderr, tracebacks) | same | same |
| `history.jsonl`, `last.txt`, `audio/` | same | same |
| `corrections.jsonl`, `suggestions.json` (learning from corrections) | same | same |
| `bench.jsonl` (`dictate bench` results) | same | same |
| Control channel | `$XDG_RUNTIME_DIR/dictate.sock` | `127.0.0.1:48621` + `ctl.token` in the state folder |

Logs rotate at about 1 MB (`events.log`, `events.log.1`, `events.log.2`; `errors.log`, `errors.log.1`).
