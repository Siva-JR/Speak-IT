#!/usr/bin/env bash
systemctl --user disable --now dictate.service 2>/dev/null
rm -f ~/.config/systemd/user/dictate.service ~/.local/bin/dictate
SCHEMA=org.gnome.settings-daemon.plugins.media-keys
for ID in dictate dictate-cancel dictate-undo; do
  KEYPATH=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/$ID/
  CUR="$(gsettings get $SCHEMA custom-keybindings)"
  NEW="$(python3 - "$CUR" "$KEYPATH" <<'PY'
import ast, sys
cur = ast.literal_eval(sys.argv[1].replace("@as ", ""))
print([c for c in cur if c != sys.argv[2]])
PY
)"
  gsettings set $SCHEMA custom-keybindings "$NEW"
  gsettings reset-recursively "$SCHEMA.custom-keybinding:$KEYPATH" 2>/dev/null
done
echo "uninstalled (your config/history in ~/.config/dictate and ~/.local/state/dictate was kept)"
