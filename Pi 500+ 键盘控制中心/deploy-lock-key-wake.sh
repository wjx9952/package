#!/bin/sh
set -eu

APP_SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=/home/w52/Python/codex_rgb_keyboard
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$TARGET/backups/before-lock-key-wake-$STAMP"

install -d -m 0755 "$BACKUP"
cp "$TARGET/codex_rgb_keyboard.py" "$BACKUP/"
cp "$TARGET/lcd_hat_dashboard.py" "$BACKUP/"
cp /opt/pi500-bt-keyboard/daemon.py "$BACKUP/pi500_bt_keyboard_daemon.py"

install -m 0755 "$APP_SOURCE/codex_rgb_keyboard.py" \
    "$TARGET/codex_rgb_keyboard.py"
install -m 0755 "$APP_SOURCE/lcd_hat_dashboard.py" \
    "$TARGET/lcd_hat_dashboard.py"
install -m 0644 "$APP_SOURCE/README.txt" "$TARGET/README.txt"

pkexec /bin/sh -c "set -eu
install -m 0755 '$APP_SOURCE/pi500_bt_keyboard_daemon.py' \
    /opt/pi500-bt-keyboard/daemon.py
systemctl restart pi500-bt-keyboard.service"

pkill -u "$(id -u)" -f \
    '/home/w52/Python/codex_rgb_keyboard/codex_rgb_keyboard.py' 2>/dev/null || true
sleep 0.5
nohup "$TARGET/run.sh" >/tmp/pi500-keyboard-control-center.log 2>&1 &

# A stale desktop session bus must not prevent the lighting controller from
# being restarted. The LCD service will start normally on the next login if
# this best-effort reload is unavailable in the current session.
"$APP_SOURCE/restart-lcd-dashboard.sh" 2>/dev/null || true

printf '部署完成。备份：%s\n' "$BACKUP"
