# Installation

The installers are idempotent: run them again any time to repair or update an installation.

## What gets installed

| | Linux | Windows |
|---|---|---|
| Python environment | `.venv/` in the repo | `.venv\` in the repo |
| Model | `models/faster-whisper-base.en` (about 150 MB) | same |
| Background service | systemd **user** service `dictate.service` | hidden process, started at login by `Dictate.lnk` in your Startup folder |
| Command | `~/.local/bin/dictate` | `dictate\dictate.cmd` (not added to PATH) |
| Hotkeys | GNOME custom shortcuts: Ctrl+Alt+Space (tap or hold), Ctrl+Alt+Backspace (cancel), Ctrl+Alt+Z (undo) | registered by the daemon itself, same keys |
| Config | `~/.config/dictate/` | `%APPDATA%\dictate\` |
| State (logs, history, audio) | `~/.local/state/dictate/` | `%LOCALAPPDATA%\dictate\state\` |

Nothing needs administrator rights or `sudo`, except installing missing Linux packages (the installer prints the
command, it does not run it).

## Requirements

- Python **3.11 or newer** (the installer finds a working one; on Windows install it from python.org and tick
  "Add python.exe to PATH").
- About 1 GB of free disk space and an internet connection for the first install (packages and the model).
- A microphone.

## Linux

```bash
./dictate/install.sh
```

Steps it performs: Python environment, model download, system-tool check, systemd service + GNOME hotkey, `dictate doctor`.

System packages (Debian/Ubuntu). If something is missing, the installer stops and prints this line:

```bash
sudo apt install alsa-utils pipewire-bin libnotify-bin python3-gi gir1.2-atspi-2.0
sudo apt install wl-clipboard      # optional: clipboard fallback if typing is ever blocked
```

Options (environment variables):

- `DICTATE_HOTKEY='<Control><Shift>space' ./dictate/install.sh` : use a different GNOME shortcut.
- `DICTATE_CANCEL_HOTKEY=...`, `DICTATE_UNDO_HOTKEY=...` : different cancel / undo shortcuts (GNOME key syntax).
- `DICTATE_MODEL=small.en ./dictate/install.sh` : a larger, more accurate model (slower, more memory).

On other desktops than GNOME, bind keys to `~/.local/bin/dictate toggle`, `dictate cancel` and `dictate undo` yourself;
the installer tells you if it could not set the shortcuts.

### Hold-to-talk on Linux

Tapping the hotkey always works. To also **hold** it while you talk (and to type the instant you let go of the keys,
instead of after a fixed 350 ms), the daemon has to read the keyboard state, which on Linux needs the `input` group:

```bash
sudo usermod -aG input $USER       # then log out and back in
```

It only asks the kernel which keys are down at that moment, while a dictation is running; it never reads the stream of
key presses. `dictate doctor` reports whether it works. The installer prints this hint when you are not in the group.

First start: GNOME shows a one-time "allow remote control / keyboard" prompt. Approve it, or typing will not work.

## Windows

```powershell
powershell -ExecutionPolicy Bypass -File dictate\install.ps1
```

From Git Bash or MSYS2, `./dictate/install.sh` does the same thing: it hands over to `install.ps1`.

Steps it performs: Python environment and packages (including `sounddevice` for the microphone), CUDA runtime wheels if an
NVIDIA GPU is present, model download, stop any running daemon, create the startup shortcut and start the daemon,
`dictate doctor`.

After it finishes, `doctor` should report the model, packages, a microphone that really opens and delivers audio, the
hotkey, and the daemon. Then click into any text field and press **Ctrl+Alt+Space**.

Good to know:

- Windows blocks synthetic input into windows running **as Administrator**. Text will not be typed into an elevated
  window unless the daemon is also elevated.
- While listening, a small **red dot** shows at the top centre of the screen (amber while transcribing). There are no
  pop-up notifications on Windows; sound cues and the dashboard replace them.
- Tap Ctrl+Alt+Space to start and stop, or hold it while you talk. Ctrl+Alt+Backspace cancels, Ctrl+Alt+Z removes
  the last dictated text.
- If Ctrl+Alt+Space is used by another program, the daemon logs `hotkey_fail`. Set another one in
  `%APPDATA%\dictate\config.toml` (`hotkey = "ctrl+shift+space"`), then restart the daemon (below).

Restarting the daemon on Windows:

```powershell
dictate\dictate.cmd quit
powershell -ExecutionPolicy Bypass -File dictate\install.ps1
```

## macOS

`./dictate/install.sh` sets up the Python environment and model and tells you how to start the dashboard. The
dictation daemon is not ported to macOS yet (see [Architecture](architecture.md#adding-a-new-os)).

## GPU (NVIDIA)

`device = "auto"` (the default) uses the GPU when CUDA is usable and otherwise the CPU, and it falls back to the CPU
if the GPU fails to load, so it is safe to leave on. Check which one is active with `dictate status` (`"device"`) or
the dashboard header. For the small `base.en` model the GPU is roughly 4× faster once warm (about 0.07 s against 0.3 s
for a 6.5 s clip), at the cost of VRAM while the worker is running (about 290 MB) and a slightly longer first start.
`float16` with beam 2 was the fastest setting measured (see `gpu_compute_type` in [configuration](configuration.md)).

- Linux needs cuBLAS 12 on the system library path (the NVIDIA CUDA toolkit provides it).
- Windows: the installer adds `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` when `nvidia-smi` is present.
- Force CPU: `device = "cpu"` in the config, or `DICTATE_DEVICE=cpu`.

## Updating

```bash
git pull
./dictate/install.sh          # Windows: dictate\install.ps1
```

The installer restarts the daemon so new code takes effect.

## Uninstalling

```bash
./dictate/uninstall.sh        # Windows: powershell -File dictate\uninstall.ps1
```

This stops the daemon and removes the service/shortcut and (Linux) the command and the three hotkeys. Your config, history and
audio, and the repo, `.venv` and models, are kept; delete them yourself for a full removal.
