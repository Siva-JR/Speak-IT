# Troubleshooting

Start here, in this order:

1. `dictate doctor` : checks the model, packages, microphone (it really records for one second), hotkey and daemon.
2. `dictate dashboard`, then the **Live test** card: it shows which stage fails and why. See [dashboard](dashboard.md).
3. **Copy diagnostics** in the dashboard (dictated text is redacted) and send it along with the problem.

Logs: `events.log` (what the daemon did) and `errors.log` (tracebacks), in the state folder listed in
[configuration](configuration.md#where-things-live).

## Install problems

| Symptom | Cause | Fix |
|---|---|---|
| Windows: `Python was not found; run without arguments to install from the Microsoft Store` | Windows ships a `python3.exe` stub that exists but does not run. A shell `alias` does not apply inside scripts. | The installer now tries `python3.12`, `python3.11`, `python3`, `python` and `py -3` and uses the first that really runs on 3.11+. Install Python from python.org if none does. |
| `no working Python 3.11+ found` | Python missing or too old | Install Python 3.11+ and re-run. |
| Linux installer stops with `missing: arecord ...` | System packages not installed | Run the `sudo apt install ...` line it prints, then re-run. |
| Windows installer: pip fails | Network or an old pip | Re-run (it upgrades pip first); check the proxy/network. |

## Nothing happens when I press the hotkey

| Check | Meaning | Fix |
|---|---|---|
| Dashboard Live test hotkey status `failed` / log has `hotkey_fail` (Windows) | Another program already registered the combination | Set `hotkey = "ctrl+shift+space"` (any free combo) in `config.toml`, restart the daemon |
| Status `missing` (Linux) | The GNOME shortcut is not set | Re-run `./dictate/install.sh`, or bind a key to `~/.local/bin/dictate toggle` |
| Daemon down | The service is not running | Linux: `systemctl --user start dictate`; Windows: re-run `install.ps1` |
| Works in the dashboard but not with the hotkey | Hotkey only | Use the dashboard's Start button meanwhile, and check the status above |

Pressing the hotkey twice within 0.35 s is treated as one press (key bounce protection), so double-tapping it is ignored.

## Hold-to-talk, cancel and undo

| Symptom | Cause | Fix |
|---|---|---|
| Linux: holding the hotkey does not stop when I let go; it only toggles | The daemon cannot read the keyboard state. `dictate doctor` shows `hold-to-talk / key state` as `warn` | `sudo usermod -aG input $USER`, log out and in, restart the daemon. Without it, typing also waits the fixed `type_settle_ms` |
| It stops as soon as I start talking | `hotkey_mode = "hold"` and you tapped instead of holding | Use `"auto"` (tap or hold) |
| A quick press is taken as hold | `hold_threshold_s` is too low for how you press | Raise it, e.g. `0.6` |
| Cancel / undo hotkeys do nothing (Linux) | The GNOME shortcuts were added by a newer installer | Re-run `./dictate/install.sh`, or bind keys to `dictate cancel` and `dictate undo` |
| Undo says `focus moved from X to Y` | Undo only deletes in the app it typed into | Click back into that app and undo again |
| Undo says `already sent with Enter` / `too old` | The text was submitted, or more than `undo_window_s` passed | Delete it by hand |
| Undo deleted more or less than the dictation | You typed in the same field after dictating; undo presses Backspace once per character it typed | Undo right after dictating |

## Formatting

| Symptom | Cause | Fix |
|---|---|---|
| No capital letter / no full stop in my terminal | The `terminal` profile does that for shells and agents | `[profiles.terminal]` with `capitalize = true` and/or `final_period = "keep"` |
| An app gets the wrong profile | Its name is not (or wrongly) listed | Find its name in the dashboard's History (or `target=` in `events.log`) and add it to a profile's `apps` |
| Enter was pressed unexpectedly | A phrase in `enter_phrases` was said at the end | Choose a phrase you do not say naturally, or remove it |

## The microphone

| Symptom | Cause | Fix |
|---|---|---|
| `microphone: Error querying device -1` (Windows) | Windows reports no default input device even though microphones exist | Fixed: the daemon now selects a real input itself. Update and restart the daemon. |
| Stage "Audio captured" says `0 bytes` | The stream opened but the driver delivered nothing | Windows: Settings → Privacy & security → Microphone, allow desktop apps; check the default recording device in Sound settings. The stage line shows blocks, PortAudio status flags and any callback error. |
| "very quiet" / "No sound is reaching the app" | Wrong or muted microphone | Unmute; pick the right one with `mic_device = "name part"`; speak closer |
| It recorded "Stereo Mix" and nothing useful came out | "Stereo Mix" captures the speakers, not you | Fixed: it is never chosen automatically. If you set `mic_device` to it, you get it. |
| `could not open any microphone: ... @ 48000 Hz x2: ...` | Every format failed on every default device | The error lists each attempt; usually another program holds the device exclusively, or privacy blocks it |

Play back the recording in the Live test card: if you can hear yourself, the microphone is fine and the problem is
later in the chain.

## The model / transcription

| Symptom | Cause | Fix |
|---|---|---|
| `model worker did not start` | The worker process crashed while starting (the dashboard attaches the last `errors.log` lines) | Read the traceback. Seen causes: missing packages (`ModuleNotFoundError: numpy` → re-run the installer) and, on Windows, a Linux-only memory call (`'pfullmem' object has no attribute 'pss'`, fixed). |
| `GPU unavailable (no CUDA device); falling back to CPU` | No NVIDIA GPU or CUDA libraries | Informational: it runs on the CPU. Install the NVIDIA driver / CUDA wheels to use the GPU, or set `device = "cpu"` to silence it. |
| "no speech detected" | The speech gate found only quiet noise (shown with peak/rms) | Speak louder/closer; check the microphone as above |
| The first dictation takes several seconds | The model loads when you first press the hotkey; it stays loaded for `worker_linger_s` (30 s) | Normal; `dictate warm` pre-loads it |
| Long dictations: a word near the middle differs from what a re-run gives | Live transcription cut the audio at a pause there | Expected occasionally; `live_transcribe = false` transcribes the whole recording at the end (slower after stop) |
| `live_error` in Problems | A live transcription step failed | The final transcription still covers the whole recording; check `errors.log` |
| `transcription timed out` | The machine was fully busy | Adaptive threads handle most cases; close heavy programs or raise `worker_linger_s` |

## Text is not typed

| Symptom | Cause | Fix |
|---|---|---|
| Windows: nothing typed into one particular app | That window runs as Administrator; Windows blocks synthetic input from a normal process | Run the daemon as Administrator too, or use a non-elevated window |
| Linux: first start shows a prompt | GNOME asks to allow keyboard control | Approve it (once). `dictate doctor` shows `keyboard-control permission granted` afterwards |
| Linux: `portal_down` in the log after unlocking the screen | GNOME closes the typing session while locked | The daemon re-opens it automatically within about 10 s |
| `⚠ could not type it` notification | Typing and the clipboard both failed | The text is saved: run `dictate last`. On Linux install `wl-clipboard` for the clipboard fallback |
| Words like "commit" typed as "comment" | Whisper misheard | Add a narrow rule: `dictate replace add "comment and push => commit and push"` |

## Reading the logs

`events.log` lines are `time kind key=value ...`. The ones that matter:

| Event | Meaning |
|---|---|
| `daemon_start`, `hotkey_ok` / `hotkey_fail` | daemon started; Windows hotkey registered or refused |
| `rec_start mic=...`, `rec_stop audio_s=... mic_blocks=... mic_bytes=...` | recording began/ended; on Windows the counters show whether the microphone delivered audio |
| `worker_spawn`, `worker_ready load_s=...`, `worker_exit` | model process lifecycle (`worker_exit` after the idle timeout is normal) |
| `job_done asr_s=... tail_asr_s=... live_s=...`, `job_error error=...` | transcription finished (`live_s`: seconds transcribed while you spoke, `tail_asr_s`: work left after stop) or failed |
| `hold_release held_s=...` | hold-to-talk: the hotkey was let go and the recording stopped |
| `cancel`, `job_cancelled`, `undo chars=...`, `undo_refused why=...` | cancel and undo |
| `type_done`, `type_fail`, `deliver_fallback` | text typed, typing failed, fell back to clipboard/file |
| `test_start`, `test_stop`, `test_abandoned` | dashboard live test |

## Still stuck

Open the dashboard, run the Live test, then **Copy diagnostics** and share it with a description of what you did and
what you expected.
