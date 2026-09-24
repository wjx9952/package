#!/bin/bash
set -euo pipefail

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STATE_DIR="$HOME/.local/state/pi500-keyboard-control-center"
mkdir -p "$STATE_DIR"
if [ -f "$APP_DIR/settings.json" ]; then
    cp -a "$APP_DIR/settings.json" "$STATE_DIR/settings.last.json"
fi

systemctl --user disable --now codex-lcd-hat.service codex-quota-server.service \
    2>/dev/null || true
rm -f "$HOME/.config/systemd/user/codex-lcd-hat.service"
rm -f "$HOME/.config/systemd/user/codex-quota-server.service"
systemctl --user daemon-reload 2>/dev/null || true
rm -f "$HOME/.local/share/applications/pi500-keyboard-control-center.desktop"
rm -f "$HOME/.config/autostart/codex-rgb-keyboard.desktop"
if command -v xdg-user-dir >/dev/null 2>&1; then
    desktop_dir=$(xdg-user-dir DESKTOP 2>/dev/null || true)
    if [ -n "$desktop_dir" ]; then
        rm -f "$desktop_dir/Pi 500+ 键盘控制中心.desktop"
    fi
fi

if command -v pkexec >/dev/null 2>&1; then
    pkexec "$APP_DIR/uninstall-root.sh"
elif command -v sudo >/dev/null 2>&1; then
    sudo "$APP_DIR/uninstall-root.sh"
fi

pkill -f "$APP_DIR/codex_rgb_keyboard.py" 2>/dev/null || true
rm -rf "$APP_DIR"
echo "卸载完成。最后的灯光设置保存在：$STATE_DIR/settings.last.json"
