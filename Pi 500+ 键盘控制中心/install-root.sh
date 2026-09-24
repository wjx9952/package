#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "系统安装步骤必须以 root 运行。" >&2
    exit 1
fi
if [ "$#" -ne 2 ]; then
    echo "用法：install-root.sh 用户名 安装目录" >&2
    exit 2
fi

TARGET_USER=$1
APP_DIR=$2
if ! id "$TARGET_USER" >/dev/null 2>&1; then
    echo "用户不存在：$TARGET_USER" >&2
    exit 1
fi
if [ ! -f "$APP_DIR/pi500_bt_keyboard_daemon.py" ]; then
    echo "找不到蓝牙键盘后台程序。" >&2
    exit 1
fi

packages="python3 python3-tk python3-numpy python3-pil python3-gpiozero python3-spidev python3-dbus python3-gi gir1.2-gtk-3.0 libayatana-appindicator3-1 libwayland-client0 libxkbcommon0 bluez curl ddcutil swaylock fonts-dejavu-core fonts-noto-cjk iputils-ping sudo rpi-keyboard-config rpi-keyboard-fw-update"
missing=""
if command -v dpkg-query >/dev/null 2>&1; then
    for package in $packages; do
        if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
            missing="$missing $package"
        fi
    done
fi
if [ -n "$missing" ]; then
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "缺少依赖且系统没有 apt-get：$missing" >&2
        exit 1
    fi
    echo "正在安装系统依赖：$missing"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y $missing
fi

if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_spi 0 || true
    raspi-config nonint do_i2c 0 || true
fi
for group in bluetooth input spi gpio i2c; do
    if getent group "$group" >/dev/null 2>&1; then
        usermod -aG "$group" "$TARGET_USER"
    fi
done

install -d -m 0755 /opt/pi500-bt-keyboard
install -m 0755 "$APP_DIR/pi500_bt_keyboard_daemon.py" \
    /opt/pi500-bt-keyboard/daemon.py
install -m 0644 "$APP_DIR/pi500-bt-keyboard.service" \
    /etc/systemd/system/pi500-bt-keyboard.service

sudoers_tmp=$(mktemp)
trap 'rm -f "$sudoers_tmp"' EXIT
printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart display-manager\n' \
    "$TARGET_USER" > "$sudoers_tmp"
chmod 0440 "$sudoers_tmp"
visudo -cf "$sudoers_tmp" >/dev/null
install -m 0440 "$sudoers_tmp" /etc/sudoers.d/pi500-display-manager-restart

systemctl daemon-reload
systemctl enable --now bluetooth.service pi500-bt-keyboard.service
i=0
while [ "$i" -lt 30 ] && [ ! -S /run/pi500-bt-keyboard/control.sock ]; do
    sleep 0.5
    i=$((i + 1))
done
if [ ! -S /run/pi500-bt-keyboard/control.sock ]; then
    echo "蓝牙键盘后台启动失败：" >&2
    journalctl -u pi500-bt-keyboard.service -n 20 --no-pager -o cat >&2 || true
    exit 1
fi
