#!/usr/bin/env bash
# Hearth installer — local AI chat for Ollama.
#   ./install.sh              install or update (keeps your chats, memory and settings)
#   ./install.sh --uninstall  remove the app (keeps chats in ~/.local/share/hearth)
set -Eeuo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APP="${XDG_DATA_HOME:-$HOME/.local/share}/hearth-app"
BIN="$HOME/.local/bin"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/org.homelab.Hearth.svg"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
PORT=8410
ok() { printf '  \e[32m✓\e[0m %s\n' "$*"; }
trap 'echo "✗ Stopped at line $LINENO: $BASH_COMMAND" >&2' ERR
[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user, not root."; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now hearth.service 2>/dev/null || true
    rm -rf "$APP" "$BIN/hearth" "$UNITS/hearth.service" "$APPS/org.homelab.Hearth.desktop" "$DESK/org.homelab.Hearth.desktop" "$ICON"
    systemctl --user daemon-reload
    echo "Hearth removed. Your chats and memory are still in ~/.local/share/hearth."
    exit 0
fi

echo "Installing Hearth"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ is needed"; exit 1; }
command -v ollama >/dev/null || echo "  ! Ollama isn't installed yet: curl -fsSL https://ollama.com/install.sh | sh"
command -v pdftotext >/dev/null || echo "  ! pdftotext missing — PDFs can't be read (sudo apt install poppler-utils)"

mkdir -p "$APP" "$BIN" "$UNITS" "$APPS" "$(dirname "$ICON")"
rm -rf "$APP.new" && mkdir -p "$APP.new" && cp -r "$SRC/hearth" "$SRC/web" "$APP.new/"
find "$APP.new" -name __pycache__ -prune -exec rm -rf {} +
python3 -m compileall -q "$APP.new/hearth" >/dev/null
rm -rf "$APP.old"; [ -d "$APP" ] && mv "$APP" "$APP.old"; mv "$APP.new" "$APP"; rm -rf "$APP.old"
ok "app files in $APP"

cat > "$UNITS/hearth.service" <<UNIT
[Unit]
Description=Hearth local AI chat server
After=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=/usr/bin/python3 -m hearth.server
Environment=HEARTH_PORT=$PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable hearth.service >/dev/null 2>&1
systemctl --user restart hearth.service
ok "background server (starts at login, port $PORT, this PC only)"

cat > "$BIN/hearth" <<'LAUNCH'
#!/usr/bin/env bash
# Open Hearth in its own window.
URL="http://127.0.0.1:8410/"
systemctl --user start hearth.service 2>/dev/null
for _ in $(seq 40); do curl -s -o /dev/null "$URL" && break; sleep 0.25; done
for b in google-chrome google-chrome-stable chromium chromium-browser brave-browser microsoft-edge; do
    if command -v "$b" >/dev/null; then exec "$b" --app="$URL" --class=Hearth --window-size=1280,860; fi
done
exec xdg-open "$URL"
LAUNCH
chmod +x "$BIN/hearth"
ok "launcher: $BIN/hearth"


cp "$SRC/web/hearth.svg" "$ICON"
gtk-update-icon-cache -q -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
cat > "$APPS/org.homelab.Hearth.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Hearth
GenericName=AI Chat
Comment=Chat with your local AI models — with web access, tools, memory and files
Exec=$BIN/hearth
Icon=org.homelab.Hearth
Terminal=false
Categories=Utility;Network;Chat;
Keywords=ai;chat;ollama;llm;assistant;
StartupWMClass=Hearth
DESKTOP
update-desktop-database "$APPS" >/dev/null 2>&1 || true
if [ -d "$DESK" ]; then
    cp "$APPS/org.homelab.Hearth.desktop" "$DESK/"
    chmod +x "$DESK/org.homelab.Hearth.desktop"
    gio set "$DESK/org.homelab.Hearth.desktop" metadata::trusted true 2>/dev/null || true
    gio set "$DESK/org.homelab.Hearth.desktop" metadata::xfce-exe-checksum "$(sha256sum "$DESK/org.homelab.Hearth.desktop" | cut -d' ' -f1)" 2>/dev/null || true
fi
ok "app menu entry and desktop icon"

for _ in $(seq 20); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.25; done
curl -s "http://127.0.0.1:$PORT/api/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  ✓ running — Ollama", "connected," if d["ollama"] else "NOT reachable,", len(d["models"]), "models")'
echo "Done. Open Hearth from the menu, the desktop icon, or by running: hearth"
