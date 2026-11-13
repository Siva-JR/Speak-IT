# Dashboard

A single local page for seeing what the tool is doing and where it breaks, on Linux and Windows alike.

```bash
dictate dashboard                       # opens http://127.0.0.1:8765
dictate dashboard --port 9000 --no-browser
```

Windows: `dictate\dictate.cmd dashboard`. You can also run `python dictate/dashboard.py` directly.

The dashboard is a separate, standard-library-only program. It reads the log files and talks to the daemon, but it
never imports the daemon, so it still works when the daemon crashed or cannot start (it then says "daemon down" and
shows why).

## The header

Pills for daemon up/down, worker running/idle, the device in use (`cuda`/`cpu`), the number of failing checks and the
number of recent problems. **Copy diagnostics** and **Download** produce the diagnostics bundle (below). "Hide dictated
text" blanks what you dictated in the page.

## Live test

The fastest way to find out why dictation fails. It drives the **same pipeline as the hotkey** (the same microphone
code, model worker and device) but nothing is typed into any window: the result appears in the card.

1. Press **Start listening** (or the hotkey), speak, then press **Stop and transcribe**.
2. Watch the **frequency bars** and the **level meter** while you speak. The "voice detected" pill lights when the
   signal is above the line marked on the meter; if almost nothing arrives for about 2.5 s the page warns that no sound
   is reaching the app.
3. Read the **stage checklist**. The first failing stage is highlighted with its reason:

| Stage | ✓ means | ✗ / ! usually means |
|---|---|---|
| Microphone opened | a device opened (name and format shown) | no microphone, permission blocked, device busy |
| Audio captured | audio reached the app (seconds, peak level, blocks) | `0 bytes`: the stream opened but delivered nothing (blocks, status flags and any callback error are shown); `!` very quiet: wrong or muted microphone |
| Speech model | model loaded (device and load time) | the worker crashed or did not start (the last lines of `errors.log` are attached) |
| Transcribed | text produced (seconds for N s of audio) | `!` no speech detected: too quiet, or only noise |

4. The **text** box shows the result (and the raw text if cleanup changed it), with timings (live transcription is on
   here too). The **player** replays
   exactly what the microphone captured, which settles "is it the mic or the model?" in seconds.

The card also shows the **hotkey configured on this machine** and its status:

| OS | Source | Status values |
|---|---|---|
| Linux | the GNOME custom shortcut named Dictate | `set`, `missing` (run `install.sh`), `unknown` |
| Windows | `hotkey` in `config.toml` (default Ctrl+Alt+Space) and the daemon's log | `active` (registered), `failed` (another program owns it), `unknown` |
| macOS | none yet | `none`: use the Start button |

If the browser tab is closed in the middle of a test, the daemon stops the recording by itself after 15 seconds so the
next hotkey press is not inverted.

## Performance

Numbers from your own real dictations (dashboard tests and benchmarks are left out):

| Tile | Meaning |
|---|---|
| Stop → text, median (p90) | From pressing stop until the whole text is in place in the app, typing included. |
| Speaking speed | Median words per minute while talking: words ÷ recording length. |
| Effective speed | Words ÷ (recording length + the wait for the text). What you really get per minute. |
| Dictated | Total words, dictations and minutes of speech. |

The chart below plots stop → text for the last 60 dictations; hover a point for the date, length, words, WPM, how much
was transcribed while you were still speaking, and the app.

Under it is the latest **`dictate bench`** run (see [Performance](performance.md)): stop → text ready (before typing)
for short, medium and long recordings, the cold start, WPM, and the error rate, for live transcription and for
transcribing everything at the end.

## Suggestions

What Speak-IT proposes to learn from your corrections: a **vocabulary** term or a **replacement** rule, with what it
heard and how many times you made that correction. **Accept** writes it to `vocab.txt` / `replacements.txt` (used from
the next dictation on); **Dismiss** hides it. See [learning from corrections](configuration.md#learning-from-corrections).

## History

Every dictation, newest first, searchable (text, raw transcript, your correction, or app name). Each entry shows the
app and profile, length, words and WPM, stop → text, and how much was transcribed live. Buttons:

- **Copy** the text.
- **Correct**: edit what it should have been and save. The entry then shows your version (with what was dictated
  below it), and the words that changed appear under Suggestions.
- **Play** the recording (when `save_audio` kept it).

"Hide dictated text" in the header also hides text here and disables Copy and Correct.

## Other panels

- **Problems**: real failures only (`job_error`, `type_fail`, `portal_down`, ...). Normal events such as the worker
  exiting after its idle timeout are not listed.
- **Checks**: model files, Python packages, per-OS tools (Linux: `arecord`, `pw-play`, `notify-send`; Windows:
  `sounddevice` and a listing of microphones), session type, hotkey permission, daemon reachable, CUDA visible.
  The dashboard lists microphones but never opens one by itself (it refreshes every 2 s, and opening the mic that often
  would trigger Windows' microphone indicator). Run `dictate doctor` to test opening the microphone for real.
- **Events** (filterable) and **Errors / stderr**: the daemon's event log and everything the daemon and worker print
  to stderr, including Python tracebacks. These are saved to `errors.log` in the state folder.
- **System / Config**: OS, Python, package versions, CUDA, effective config, environment overrides, vocabulary and
  replacements.

## Diagnostics bundle

**Copy diagnostics** (clipboard) or **Download** (a text file) gives one report with the system info, checks, config,
problems, errors, events and recent timings. **Dictated text is redacted** (only its length is kept), so it is safe to
paste into an issue or a chat. This is the first thing to send when something fails on another machine.

## Security

The page can start your microphone, so it is locked down:

- It listens on `127.0.0.1` only, and requests whose `Host` is not `127.0.0.1`/`localhost` are refused (blocks DNS
  rebinding).
- Anything that records or streams audio, saves a correction or accepts a suggestion needs a random per-run token that
  only the served page knows, and a request from another website's origin is refused, so a random web page cannot turn
  the microphone on or change your vocabulary.
- Recordings are served only from the daemon's own audio folder, by strict file-name pattern (no path traversal).
- The daemon's control channel is a `0600` Unix socket on Linux and, on Windows, a local TCP port that also requires a
  secret token kept in your user profile.
