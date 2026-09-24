#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "系统卸载步骤必须以 root 运行。" >&2
    exit 1
fi
systemctl disable --now pi500-bt-keyboard.service 2>/dev/null || true
rm -f /etc/systemd/system/pi500-bt-keyboard.service
rm -f /etc/sudoers.d/pi500-display-manager-restart
rm -rf /opt/pi500-bt-keyboard
systemctl daemon-reload
