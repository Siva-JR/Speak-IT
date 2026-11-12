# Speak-IT

Local, private push-to-talk dictation. Press a hotkey, speak, press it again: the text is typed into whatever
window has focus. Speech recognition runs on your own machine with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper); no audio leaves it.

The repo has two parts:

| Part | Where | What it is |
|---|---|---|
| **Dictation tool** | [`dictate/`](dictate/) | The hotkey daemon, the model worker, the installer and the debug dashboard. This is what you install. |
| **Benchmark harness** | `bench/`, `run.py`, `configs/`, `scripts/` | The code used to compare speech models for accuracy, speed and memory, which led to the choice of `base.en`. You do not need it to dictate. |

## Platform support

| | Linux (GNOME) | Windows | macOS |
|---|---|---|---|
| Hotkey → record → transcribe → type | yes | yes | not yet |
| GPU (NVIDIA CUDA) | yes | yes (falls back to CPU) | n/a |
| Debug dashboard + live test | yes | yes | dashboard only |
| One-command installer | `install.sh` | `install.ps1` (or `install.sh` from Git Bash) | environment and model only |
| Start at login | systemd user service | Startup-folder shortcut | no |
| Desktop notifications / tray icon | yes | no (beeps instead) | no |

Linux has been tested on GNOME/Wayland. Windows has been tested on a real machine with Python 3.12 and a Realtek
microphone; see [Troubleshooting](docs/troubleshooting.md) for the problems that turned up and how they are handled.

## Quick start

### Linux

```bash
git clone git@github.com:Siva-JR/Speak-IT.git && cd Speak-IT
./dictate/install.sh
```

Press **Ctrl+Alt+Space**, speak, press it again. The first time, GNOME asks you to allow keyboard control: approve it.

### Windows

```powershell
git clone https://github.com/Siva-JR/Speak-IT.git; cd Speak-IT
powershell -ExecutionPolicy Bypass -File dictate\install.ps1
```

Click into a text field, press **Ctrl+Alt+Space**, speak, press it again. You hear one beep when listening starts, two
when the text is typed, and a low one on error.

Details, requirements and uninstalling: [docs/installation.md](docs/installation.md).

## Using it

```text
dictate toggle        start/stop (what the hotkey runs)         dictate status    daemon, worker, device
dictate doctor        check the whole setup                     dictate last      the last dictated text
dictate dashboard     open the debug dashboard                  dictate events    recent log lines
dictate vocab add X   teach it a word or name                   dictate replace add "heard => Correct"
```

On Windows use `dictate\dictate.cmd` in place of `dictate`.

## The dashboard

`dictate dashboard` opens a local page (http://127.0.0.1:8765) with everything needed to debug a problem on any
machine: a **live test** (start/stop button, microphone level and frequency bars, a stage-by-stage checklist, the
transcribed text and playback of what the microphone captured), the hotkey configured on the machine, system checks,
recent dictations with timings, and the full event and error logs. One button copies a redacted diagnostics report.
See [docs/dashboard.md](docs/dashboard.md).

## Documentation

- [Installation](docs/installation.md): requirements, Linux and Windows setup, GPU, updating, uninstalling
- [Dashboard](docs/dashboard.md): the live test, reading the stages, diagnostics bundle, security model
- [Configuration](docs/configuration.md): config file, every setting, environment variables, vocabulary and replacements
- [Troubleshooting](docs/troubleshooting.md): symptom → cause → fix, including every problem we hit on Windows
- [Architecture](docs/architecture.md): processes, the per-OS layer, the control channel, files, how to port to a new OS

## Privacy

Audio is recorded to memory, transcribed locally, and (by default) saved to the state folder so you can replay it; set
`save_audio = false` to disable that. The dashboard listens on `127.0.0.1` only and requires a per-run token for
anything that touches the microphone. Diagnostics bundles redact the dictated text.
