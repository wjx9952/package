#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "请以普通桌面用户运行此安装程序，不要直接使用 sudo。" >&2
    exit 1
fi

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR=${PI500_INSTALL_DIR:-"$HOME/.local/share/pi500-keyboard-control-center"}
USER_SERVICE_DIR="$HOME/.config/systemd/user"
APPLICATION_DIR="$HOME/.local/share/applications"
AUTOSTART_FILE="$HOME/.config/autostart/codex-rgb-keyboard.desktop"
STATE_DIR="$HOME/.local/state/pi500-keyboard-control-center"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="$STATE_DIR/backups/$STAMP"

required_files=(
    codex_rgb_keyboard.py tray_icon.py quota_server.py lcd_hat_dashboard.py
    codex_usage_monitor.py pi500_bt_keyboard_daemon.py run.sh settings.json
    restart-keyboard-control-center.sh restart-lcd-dashboard.sh
    codex-rgb.svg codex-rgb-tray.png codex-lcd-hat.service
    codex-quota-server.service pi500-bt-keyboard.service
    "Pi 500+ 键盘控制中心.desktop" install-root.sh uninstall-root.sh
    vendor/bin/wtype vendor/bin/wlrctl
)
for file in "${required_files[@]}"; do
    if [ ! -e "$SOURCE_DIR/$file" ]; then
        echo "安装包不完整，缺少：$file" >&2
        exit 1
    fi
done

echo "[1/7] 备份现有版本…"
mkdir -p "$BACKUP_DIR" "$TARGET_DIR" "$USER_SERVICE_DIR" "$APPLICATION_DIR"
if [ -f "$TARGET_DIR/settings.json" ]; then
    cp -a "$TARGET_DIR/settings.json" "$BACKUP_DIR/settings.json"
fi
for service in codex-quota-server.service codex-lcd-hat.service; do
    if [ -f "$USER_SERVICE_DIR/$service" ]; then
        cp -a "$USER_SERVICE_DIR/$service" "$BACKUP_DIR/$service"
    fi
done

echo "[2/7] 安装应用文件…"
for file in \
    codex_rgb_keyboard.py tray_icon.py quota_server.py lcd_hat_dashboard.py \
    codex_usage_monitor.py run.sh uninstall.sh uninstall-root.sh install-root.sh \
    pi500_bt_keyboard_daemon.py restart-keyboard-control-center.sh \
    restart-lcd-dashboard.sh
do
    install -m 0755 "$SOURCE_DIR/$file" "$TARGET_DIR/$file"
done
for file in \
    codex-rgb.svg codex-rgb-tray.png README.txt PORTABLE_README.txt VERSION \
    pi500-bt-keyboard.service
do
    install -m 0644 "$SOURCE_DIR/$file" "$TARGET_DIR/$file"
done
install -d -m 0755 "$TARGET_DIR/vendor" "$TARGET_DIR/windows-lock-sync"
cp -a "$SOURCE_DIR/vendor/." "$TARGET_DIR/vendor/"
cp -a "$SOURCE_DIR/windows-lock-sync/." "$TARGET_DIR/windows-lock-sync/"
if [ -d "$SOURCE_DIR/docs" ]; then
    install -d -m 0755 "$TARGET_DIR/docs"
    cp -a "$SOURCE_DIR/docs/." "$TARGET_DIR/docs/"
fi
if [ ! -f "$TARGET_DIR/settings.json" ]; then
    install -m 0644 "$SOURCE_DIR/settings.json" "$TARGET_DIR/settings.json"
fi
printf '{"locked": false, "updated_at": 0}\n' > "$TARGET_DIR/windows_session_state.json"

echo "[3/7] 安装系统依赖和蓝牙后台（需要管理员确认）…"
if command -v pkexec >/dev/null 2>&1; then
    pkexec "$TARGET_DIR/install-root.sh" "$(id -un)" "$TARGET_DIR"
elif command -v sudo >/dev/null 2>&1; then
    sudo "$TARGET_DIR/install-root.sh" "$(id -un)" "$TARGET_DIR"
else
    echo "找不到 pkexec 或 sudo，无法安装系统服务。" >&2
    exit 1
fi

escape_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
escaped_target=$(escape_sed "$TARGET_DIR")

echo "[4/7] 注册用户服务…"
sed "s|@APP_DIR@|$escaped_target|g" "$SOURCE_DIR/codex-quota-server.service" \
    > "$USER_SERVICE_DIR/codex-quota-server.service"
sed "s|@APP_DIR@|$escaped_target|g" "$SOURCE_DIR/codex-lcd-hat.service" \
    > "$USER_SERVICE_DIR/codex-lcd-hat.service"

echo "[5/7] 创建应用菜单和桌面入口…"
generated_desktop=$(mktemp)
trap 'rm -f "$generated_desktop"' EXIT
sed "s|@APP_DIR@|$escaped_target|g" \
    "$SOURCE_DIR/Pi 500+ 键盘控制中心.desktop" > "$generated_desktop"
chmod 0755 "$generated_desktop"
install -m 0755 "$generated_desktop" \
    "$APPLICATION_DIR/pi500-keyboard-control-center.desktop"
desktop_dir=""
if command -v xdg-user-dir >/dev/null 2>&1; then
    desktop_dir=$(xdg-user-dir DESKTOP 2>/dev/null || true)
fi
if [ -n "$desktop_dir" ] && [ -d "$desktop_dir" ]; then
    install -m 0755 "$generated_desktop" \
        "$desktop_dir/Pi 500+ 键盘控制中心.desktop"
fi
if [ -f "$AUTOSTART_FILE" ]; then
    install -m 0755 "$generated_desktop" "$AUTOSTART_FILE"
fi

echo "[6/7] 启动 LCD 与额度服务…"
systemctl --user daemon-reload
systemctl --user enable codex-quota-server.service codex-lcd-hat.service
systemctl --user restart codex-quota-server.service
if ! systemctl --user restart codex-lcd-hat.service; then
    echo "LCD 服务暂未启动；注销并重新登录后会自动启动。" >&2
fi

echo "[7/7] 启动控制中心…"
"$TARGET_DIR/restart-keyboard-control-center.sh"

echo
echo "安装完成。"
echo "安装目录：$TARGET_DIR"
echo "本次备份：$BACKUP_DIR"
echo "如果安装程序刚把当前用户加入 gpio/spi/input/bluetooth 组，请注销后重新登录一次。"
echo "Windows 锁屏同步工具位于：$TARGET_DIR/windows-lock-sync"
