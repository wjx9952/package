#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "此更新需要管理员权限" >&2
    exit 1
fi

SOURCE=/home/w52/Desktop/Pi500蓝牙键盘/daemon.py
TARGET=/opt/pi500-bt-keyboard/daemon.py
PATCH_FILE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/pi500-bt-keyboard-winlock.patch

if [ ! -f "$SOURCE" ]; then
    echo "找不到蓝牙键盘后台程序：$SOURCE" >&2
    exit 1
fi

if ! grep -q 'def lock_windows' "$SOURCE"; then
    patch --batch --forward -d "$(dirname -- "$SOURCE")" -p0 < "$PATCH_FILE"
fi

install -d -m 755 "$(dirname -- "$TARGET")"
install -m 755 "$SOURCE" "$TARGET"
systemctl restart pi500-bt-keyboard.service

i=0
while [ "$i" -lt 20 ] && [ ! -S /run/pi500-bt-keyboard/control.sock ]; do
    sleep 0.25
    i=$((i + 1))
done

if [ ! -S /run/pi500-bt-keyboard/control.sock ]; then
    echo "蓝牙键盘后台服务启动失败" >&2
    exit 1
fi

echo "Windows Win+L 蓝牙命令已安装"
