#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
exec /usr/bin/python3 "$APP_DIR/codex_rgb_keyboard.py"
