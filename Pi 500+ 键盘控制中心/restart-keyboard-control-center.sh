#!/bin/sh
set -eu

TARGET=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
LOCK=$RUNTIME_DIR/codex-rgb-keyboard.lock
LOG=/tmp/pi500-keyboard-control-center.log
old_pid=""

if [ -f "$LOCK" ]; then
    old_pid=$(cat "$LOCK" 2>/dev/null || true)
    case "$old_pid" in
        ''|*[!0-9]*) ;;
        *) kill "$old_pid" 2>/dev/null || true ;;
    esac
fi

attempt=0
while [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 50 ]; then
        printf '旧控制中心未能退出，PID：%s\n' "$old_pid" >&2
        exit 1
    fi
    sleep 0.1
done

rm -f "$LOCK"
nohup "$TARGET/run.sh" >"$LOG" 2>&1 &

attempt=0
while [ "$attempt" -lt 50 ]; do
    new_pid=$(cat "$LOCK" 2>/dev/null || true)
    case "$new_pid" in
        ''|*[!0-9]*) ;;
        *)
            if [ "$new_pid" != "$old_pid" ] && kill -0 "$new_pid" 2>/dev/null; then
                printf '控制中心已重新启动，PID：%s\n' "$new_pid"
                exit 0
            fi
            ;;
    esac
    attempt=$((attempt + 1))
    sleep 0.1
done

printf '控制中心启动失败，请检查 %s\n' "$LOG" >&2
tail -20 "$LOG" >&2 2>/dev/null || true
exit 1
