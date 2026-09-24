Pi 500+ 键盘控制中心——便携安装包
==================================

适用环境
--------

- Raspberry Pi 500+，64 位 Raspberry Pi OS（Debian 系）。
- Waveshare 1.3inch LCD HAT（240×240、ST7789），按当前项目方式倒置安装。
- 若要控制显示器，默认型号为 AOC Q27V6L，HDMI=0x11、DisplayPort=0x0f。
- Codex CLI 需要已经安装并登录，才能读取 5 Hour/7 Day 用量。
- Clash Verge Rev、ChatGPT 桌面程序属于可选依赖；没有安装时，其对应长按功能不可用。

安装
----

1. 将压缩包复制到目标树莓派并解压。
2. 打开终端，进入解压后的目录。
3. 执行：

       chmod +x install.sh
       ./install.sh

4. 按提示授权安装系统依赖和蓝牙后台。
5. 安装完成后建议注销并重新登录；如果安装前 SPI/I²C 未启用，则重启一次。

程序默认安装到：

    ~/.local/share/pi500-keyboard-control-center

它不会写死用户名或特定主目录，可由任意普通桌面用户安装。重复运行 install.sh 会保留
settings.json，并把旧服务和设置备份到：

    ~/.local/state/pi500-keyboard-control-center/backups

包含的组件
------------

- Apple 风格桌面控制中心和系统托盘。
- Pi 500+ RGB 键盘状态灯及动态彩虹灯效。
- Pi 500+ 蓝牙 LE 键盘后台、配对、键盘模式和 Windows Win+L。
- Waveshare LCD HAT 的 Codex 用量、IP、TUN 公网出口 IP 与按键功能。
- AOC 显示器 DDC/CI 信号源与亮度控制。
- Codex 确认提示和 KEY3 确认。
- Windows 锁屏状态同步脚本（windows-lock-sync 文件夹）。
- 卸载脚本 uninstall.sh。

迁移前提
--------

这是“同类硬件迁移包”，不是普通 PC 通用软件。另一台设备需具备 Pi 500+ 内置键盘、
相同 LCD HAT 接线以及兼容 DDC/CI 的显示器。若 AOC 型号或输入源 VCP 编号不同，请修改
quota_server.py 顶部的 MONITOR_MFG_ID、MONITOR_MODEL、PI_INPUT_SOURCE 和
WINDOWS_INPUT_SOURCE。

额度与确认状态来自当前用户的 ~/.codex；因此目标设备需要用同一桌面用户启动 Codex，
并完成 Codex CLI 登录。安装程序会自动寻找 PATH、VS Code 和 VS Code Server 中的 Codex。

Windows 锁屏同步
----------------

把安装目录中的 windows-lock-sync 文件夹复制到 Windows，双击 install.cmd，然后输入
小屏幕显示的树莓派局域网 IP。该功能使用 TCP 8765 端口；请只在可信局域网使用，并确保
防火墙允许 Windows 访问该端口。

卸载
----

执行：

    ~/.local/share/pi500-keyboard-control-center/uninstall.sh

卸载前的最后一份灯光设置会保存在：

    ~/.local/state/pi500-keyboard-control-center/settings.last.json
