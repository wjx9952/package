# Pi 500+ 控制中心：C 语言学习版

这是现有 Python 项目的独立 C 语言教学实现，目的是学习和面试讲解。

它不会替换、安装或启动当前正在使用的软件。本目录中的程序也没有在树莓派上编译或运行。

## 学习目标

- 用 `epoll + timerfd` 编写事件驱动主循环。
- 用 `libgpiod` 读取 GPIO 按键边沿事件。
- 用 Linux `spidev` 驱动 ST7789 LCD。
- 用 `getifaddrs` 获取物理网卡 IPv4。
- 用 `ping baidu.com` 判断互联网状态。
- 用明确的状态机处理 Codex、键盘模式和灯光优先级。
- 用互斥锁保护跨线程共享状态。
- 把硬件层、业务层和 UI 层分开，方便测试与替换。

## 目录

```text
c-learning-version/
├── Makefile                 # 仅作为构建知识示例
├── INTERVIEW.md             # 面试讲解提纲
├── include/pi500.h          # 公共数据结构和接口
└── src/
    ├── main.c               # epoll 主循环
    ├── state_machine.c      # Codex/键盘灯优先级
    ├── network.c            # IPv4 与百度 Ping
    ├── codex_monitor.c      # 多会话 JSONL 状态聚合
    ├── quota_client.c       # libcurl/json-c 读取额度
    ├── buttons.c            # GPIO 按键边沿与长按
    ├── lcd_st7789.c         # SPI/ST7789 驱动框架
    └── ui.c                 # 240×240 UI 渲染框架
```

## 与当前 Python 项目的对应关系

| Python 模块 | C 学习版 |
|---|---|
| `lcd_hat_dashboard.py` | `main.c`、`buttons.c`、`lcd_st7789.c`、`ui.c` |
| `codex_usage_monitor.py` | `state_machine.c` 中的状态聚合接口 |
| `codex_rgb_keyboard.py` | `state_machine.c` 中的灯光优先级 |
| `quota_server.py` | 保留为外部 HTTP 服务；C 版可再用 libcurl 接入 |

## 重要说明

此版本刻意不包含安装脚本，也不会操作当前 systemd 服务。硬件代码展示的是标准 Linux C 实现方式，但不同 Raspberry Pi OS 版本可能使用不同的 `libgpiod` API，实际编译前需要按系统版本适配。
