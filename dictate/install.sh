#!/usr/bin/env bash
# Installs: ~/.local/bin/dictate, a systemd *user* service, and a GNOME hotkey. Reversible: ./uninstall.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../.venv/bin/python"
HOTKEY="${DICTATE_HOTKEY:-<Control><Alt>space}"

mkdir -p ~/.local/bin ~/.config/systemd/user
cat > ~/.local/bin/dictate <<WRAP
#!/usr/bin/env bash
exec "$PY" "$HERE/dictate.py" "\$@"
WRAP
chmod +x ~/.local/bin/dictate

cat > ~/.config/systemd/user/dictate.service <<UNIT
[Unit]
Description=Local push-to-talk dictation daemon

[Service]
ExecStart=$PY $HERE/dictate.py serve
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now dictate.service

# GNOME custom shortcut (no sudo)
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
echo "installed. hotkey: $HOTKEY   command: dictate toggle"
echo "first start shows a one-time 'allow remote control / keyboard' prompt: approve it."
