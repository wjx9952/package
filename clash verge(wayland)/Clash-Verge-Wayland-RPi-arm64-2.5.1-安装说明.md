# Clash Verge Wayland 树莓派安装说明

## 支持范围

此安装包已针对以下环境构建和验证：

- 64 位 Raspberry Pi OS Trixie（`aarch64`）
- labwc Wayland 桌面
- `wf-panel-pi` 1.13
- Clash Verge Rev 2.5.1

可用于运行上述系统的其他树莓派（建议 Raspberry Pi 4 或 5）。32 位系统、Bookworm、Ubuntu、X11 桌面或其他版本的 `wf-panel-pi` 不在兼容范围内；安装程序会拒绝不匹配的环境，避免破坏面板。

## 安装

1. 安装系统依赖：

   ```bash
   sudo apt update
   sudo apt install -y openssl libayatana-appindicator3-1 libwebkit2gtk-4.1-0 libgtk-3-0t64 wf-panel-pi
   ```

2. 解压并进入目录：

   ```bash
   tar -xJf Clash-Verge-Wayland-RPi-arm64-2.5.1.tar.xz
   cd Clash-Verge-Wayland-RPi-arm64-2.5.1
   ```

3. 先检查兼容性，再安装：

   ```bash
   ./install.sh --check-only
   ./install.sh
   ```

4. 从应用菜单打开“Clash Verge（Wayland 原生版）”。如果顶部面板没有立即刷新，注销并重新登录一次。

安装位置为 `~/.local/opt/clash-verge-wayland`。安装包不会携带、复制或覆盖任何订阅、节点和用户配置。

## 已包含的修复

- 强制使用原生 Wayland，并规避 wlroots/ARM 上 WebKitGTK 的 DMA-BUF 黑屏或透明问题。
- 修复 `wf-panel-pi` 无法读取 Clash 临时 PNG 路径导致的右上角透明托盘项。
- 保证最小化后右上角仍有一个可点击的 Clash 图标。
- 阻止启动时恢复为最大化窗口，默认使用约 940×700 的普通窗口。
- 安装与 Wayland `app_id` 匹配的 `clash-verge.desktop`。

## TUN 模式

普通代理功能无需额外操作。若需要 TUN 模式，可在 Clash Verge 设置中按提示安装系统服务；也可以执行：

```bash
sudo ~/.local/opt/clash-verge-wayland/bin/clash-verge-service-install
```

这是系统级变更，会要求输入管理员密码。

## 故障排查

```bash
~/.local/opt/clash-verge-wayland/diagnose.sh
```

若 `--check-only` 报告系统或面板版本不匹配，不要强制安装托盘插件。可以使用 `./install.sh --no-panel-fix` 仅安装应用，但右上角托盘修复将不会生效。

## 卸载

```bash
~/.local/opt/clash-verge-wayland/uninstall.sh
```

卸载脚本会移除菜单入口、面板服务和自动启动改动，但保留程序目录及用户配置，避免误删数据。
