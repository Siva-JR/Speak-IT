#!/usr/bin/env bash
# One-shot setup for the dictation tool. Safe to re-run. Reverse with ./uninstall.sh
#   Linux : venv + deps + model + systemd user service + GNOME hotkey + checks
#   macOS / Windows (Git Bash/WSL): venv + deps + model + dashboard only. The daemon is Linux-only for now.
# Env: DICTATE_HOTKEY (default <Control><Alt>space)   DICTATE_MODEL (default base.en)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="$ROOT/.venv"
MODEL="${DICTATE_MODEL:-base.en}"
HOTKEY="${DICTATE_HOTKEY:-<Control><Alt>space}"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in Linux) OS=linux ;; Darwin) OS=mac ;; *) OS=windows ;; esac
# first interpreter that RUNS and is >= 3.11 (skips the Windows Store python3 stub, which exists but fails)
find_python() {
  local c
  for c in python3.12 python3.11 python3 python "py -3"; do
    # shellcheck disable=SC2086
    if $c -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then echo "$c"; return 0; fi
  done
  return 1
}
venv_python() { for p in "$VENV/bin/python" "$VENV/Scripts/python.exe" "$VENV/Scripts/python"; do [ -f "$p" ] && { echo "$p"; return; }; done; }

say "1/5 Python environment ($OS)"
PYBIN="$(venv_python || true)"
if [ -z "$PYBIN" ]; then
  if command -v uv >/dev/null && uv venv --python 3.12 "$VENV" 2>/dev/null; then :
  else
    PYSYS="$(find_python)" || die "no working Python 3.11+ found. Install it from python.org (tick 'Add to PATH') or install uv, then re-run"
    echo "using: $PYSYS ($($PYSYS -V))"
    $PYSYS -m venv "$VENV"
  fi
  PYBIN="$(venv_python)" || die "venv was created but has no python inside $VENV"
fi
if command -v uv >/dev/null; then uv pip install --python "$PYBIN" -r "$HERE/requirements.txt"
else "$PYBIN" -m pip install -q --upgrade pip && "$PYBIN" -m pip install -q -r "$HERE/requirements.txt"; fi

say "2/5 Model (faster-whisper-$MODEL)"
"$PYBIN" - "$ROOT/models/faster-whisper-$MODEL" "$MODEL" <<'PY'
import sys
from pathlib import Path
dest, name = Path(sys.argv[1]), sys.argv[2]
if (dest / "model.bin").exists():
    print("already downloaded:", dest); sys.exit(0)
from huggingface_hub import snapshot_download
snapshot_download(f"Systran/faster-whisper-{name}", local_dir=dest)
print("downloaded:", dest)
PY
[ -f "$ROOT/models/faster-whisper-$MODEL/model.bin" ] || die "model download failed"
# the daemon's default model path is base.en; point other choices at it via config
if [ "$MODEL" != "base.en" ]; then
  mkdir -p ~/.config/dictate
  grep -q '^model *=' ~/.config/dictate/config.toml 2>/dev/null || \
    echo "model = \"$ROOT/models/faster-whisper-$MODEL\"" >> ~/.config/dictate/config.toml
fi

if [ "$OS" != linux ]; then
  say "3/5 Not Linux: skipping daemon, service and hotkey"
  warn "the dictation daemon is Linux-only for now. Environment and model are ready."
  say "5/5 Dashboard"
  echo "run:  $PYBIN $HERE/dashboard.py     (http://127.0.0.1:8765)"
  exit 0
fi

say "3/5 System tools"
MISSING=()
for t in arecord pw-play notify-send; do command -v "$t" >/dev/null || MISSING+=("$t"); done
/usr/bin/python3 -c 'import gi' 2>/dev/null || MISSING+=("python3-gi")
if command -v wl-copy >/dev/null || command -v xclip >/dev/null; then :; else warn "no clipboard fallback (optional): sudo apt install wl-clipboard"; fi
if [ ${#MISSING[@]} -gt 0 ]; then
  warn "missing: ${MISSING[*]}"
  echo "       Debian/Ubuntu: sudo apt install alsa-utils pipewire-bin libnotify-bin python3-gi gir1.2-atspi-2.0"
  die "install the above, then re-run ./install.sh"
fi
echo "all present"

say "4/5 Service + hotkey"
mkdir -p ~/.local/bin ~/.config/systemd/user
cat > ~/.local/bin/dictate <<WRAP
#!/usr/bin/env bash
exec "$PYBIN" "$HERE/dictate.py" "\$@"
WRAP
chmod +x ~/.local/bin/dictate
cat > ~/.config/systemd/user/dictate.service <<UNIT
[Unit]
Description=Local push-to-talk dictation daemon

[Service]
ExecStart=$PYBIN $HERE/dictate.py serve
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable dictate.service
systemctl --user restart dictate.service

if command -v gsettings >/dev/null && [ -n "${XDG_CURRENT_DESKTOP:-}" ] && [[ "$XDG_CURRENT_DESKTOP" == *GNOME* ]]; then
  KEYPATH=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictate/
  SCHEMA=org.gnome.settings-daemon.plugins.media-keys
  CUR="$(gsettings get $SCHEMA custom-keybindings)"
  NEW="$(python3 - "$CUR" "$KEYPATH" <<'PY'
import ast, sys
cur = ast.literal_eval(sys.argv[1].replace("@as ", ""))
if sys.argv[2] not in cur: cur.append(sys.argv[2])
print(cur)
PY
)"
  gsettings set $SCHEMA custom-keybindings "$NEW"
  K="$SCHEMA.custom-keybinding:$KEYPATH"
  gsettings set "$K" name 'Dictate'
  gsettings set "$K" command "$HOME/.local/bin/dictate toggle"
  gsettings set "$K" binding "$HOTKEY"
  echo "hotkey: $HOTKEY  ->  dictate toggle"
else
  warn "not GNOME: bind a key of your choice to:  $HOME/.local/bin/dictate toggle"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "add ~/.local/bin to your PATH to use the 'dictate' command" ;; esac

say "5/5 Checks"
sleep 2
"$HOME/.local/bin/dictate" doctor || true
cat <<DONE

Done. Press $HOTKEY to start/stop dictating (first start shows a one-time
"allow remote control / keyboard" prompt: approve it).
Debug dashboard:  dictate dashboard
DONE
