#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DURATION=${1:-1800}

cleanup() {
    systemctl --user start codex-lcd-hat.service || true
}
trap cleanup EXIT HUP INT TERM

systemctl --user stop codex-lcd-hat.service
/usr/bin/python3 "$APP_DIR/lcd_retention_recovery.py" --duration "$DURATION"
