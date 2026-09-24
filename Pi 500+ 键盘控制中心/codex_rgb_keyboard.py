#!/usr/bin/env python3
"""Drive Raspberry Pi 500+ keyboard RGB lighting from local Codex status."""

from __future__ import annotations

import fcntl
import colorsys
import json
import math
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
# When launched as a script APP_DIR is already present, but it may no longer be
# first after adding PROJECT_DIR. Always put the bundled modules first so an
# older sibling installation cannot shadow codex_usage_monitor.py.
try:
    sys.path.remove(str(APP_DIR))
except ValueError:
    pass
sys.path.insert(0, str(APP_DIR))

import codex_usage_monitor as codex_monitor
from codex_usage_monitor import DEFAULT_CODEX_HOME, read_combined_codex_status


POLL_MS = 200
BT_STATUS_POLL_MS = 250
LOCK_STATE_POLL_MS = 250
LOCK_KEYBOARD_WAKE_SECONDS = 10.0
BRIGHTNESS_POLL_MS = 300
TRANSITION_STEPS = 18
TRANSITION_STEP_SECONDS = 0.018
KEYBOARD_TOOL = "/usr/bin/rpi-keyboard-config"
FIRMWARE_TOOL = "/usr/bin/rpi-keyboard-fw-update"
SETTINGS_FILE = APP_DIR / "settings.json"
RUNTIME_STATUS_FILE = Path("/tmp/codex-rgb-keyboard-status.json")
BT_KEYBOARD_SOCKET = "/run/pi500-bt-keyboard/control.sock"
KEYBOARD_ACTIVITY_FILE = Path("/run/pi500-bt-keyboard/activity")
LOCAL_CONTROL_API = "http://127.0.0.1:8765"
UI_FONT = "Noto Sans CJK SC"
WINDOW_BG = "#f5f5f7"
CARD_BG = "#ffffff"
SUBTLE_BG = "#f5f5f7"
TEXT = "#1d1d1f"
SECONDARY_TEXT = "#6e6e73"
BORDER = "#d2d2d7"
APPLE_BLUE = "#0071e3"
APPLE_BLUE_ACTIVE = "#0077ed"
APPLE_GREEN = "#34c759"
APPLE_RED = "#ff3b30"
DEFAULT_STATE_COLORS = {
    "keyboard_mode": "#34c759",
    "processing": "cycle_left_right",
    "idle": "#0000ff",
    "completed": "#0000ff",
    "waiting_confirmation": "#ff7e00",
}
DEFAULT_STATE_BRIGHTNESS = {
    "keyboard_mode": 180,
    "processing": 180,
    "idle": 180,
    "completed": 180,
    "waiting_confirmation": 180,
}


# Newer Codex builds wrap permission prompts inside an exec tool call. The
# shared monitor predates that event shape, so extend its predicate before
# read_codex_status() scans a session. Function-call outputs still clear the
# pending call normally once the user has answered the prompt.
_legacy_confirmation_check = codex_monitor.call_needs_confirmation


def keyboard_activity_token() -> int:
    try:
        return int(KEYBOARD_ACTIVITY_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def write_lighting_runtime_status(status: dict) -> None:
    try:
        temporary = RUNTIME_STATUS_FILE.with_name(
            RUNTIME_STATUS_FILE.name + f".{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(status, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, RUNTIME_STATUS_FILE)
    except OSError:
        pass


def call_needs_confirmation(payload: dict) -> bool:
    if _legacy_confirmation_check(payload):
        return True
    name = str(payload.get("name", ""))
    if name in {"request_permissions", "request_user_input"}:
        return True
    tool_input = payload.get("input", "")
    return isinstance(tool_input, str) and (
        "tools.request_permissions(" in tool_input
        or "tools.request_user_input(" in tool_input
    )


codex_monitor.call_needs_confirmation = call_needs_confirmation


def transition_levels(start: int, end: int, steps: int = TRANSITION_STEPS) -> list[int]:
    """Return a cosine-eased brightness ramp including both endpoints."""
    values = []
    for index in range(steps + 1):
        progress = index / steps
        eased = (1.0 - math.cos(math.pi * progress)) / 2.0
        values.append(round(start + (end - start) * eased))
    return values


def chinese_color_name(color: str) -> str:
    """Return a friendly Chinese name for an arbitrary #RRGGBB UI color."""
    try:
        red, green, blue = (
            int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)
        )
    except (TypeError, ValueError):
        return "自定义颜色"
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    degrees = hue * 360
    if value < 0.14:
        return "黑色"
    if saturation < 0.12:
        if value > 0.90:
            return "白色"
        if value > 0.68:
            return "浅灰色"
        if value > 0.36:
            return "灰色"
        return "深灰色"

    if degrees < 15 or degrees >= 345:
        name = "红色"
    elif degrees < 45:
        name = "棕色" if value < 0.55 else "橙色"
    elif degrees < 70:
        name = "黄色"
    elif degrees < 95:
        name = "黄绿色"
    elif degrees < 155:
        name = "绿色"
    elif degrees < 190:
        name = "青色"
    elif degrees < 250:
        name = "蓝色"
    elif degrees < 290:
        name = "紫色"
    elif degrees < 325:
        name = "洋红色"
    else:
        name = "粉色"

    if saturation < 0.42 and value > 0.72:
        return "浅" + name
    if value < 0.42 and name != "棕色":
        return "深" + name
    return name


def ask_rgb_color(parent: tk.Misc, initial: str, title: str) -> str | None:
    """Editable RGB/HEX picker used instead of the unreliable native dialog."""
    try:
        initial_rgb = tuple(int(initial[index:index + 2], 16) for index in (1, 3, 5))
        if len(initial) != 7 or not initial.startswith("#"):
            raise ValueError
    except (TypeError, ValueError):
        initial_rgb = (52, 199, 89)

    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.configure(bg=WINDOW_BG)
    dialog.resizable(False, False)
    dialog.transient(parent)

    result: list[str] = []
    updating = [False]
    rgb_variables = [tk.StringVar(value=str(value)) for value in initial_rgb]
    hex_variable = tk.StringVar(
        value=f"#{initial_rgb[0]:02x}{initial_rgb[1]:02x}{initial_rgb[2]:02x}"
    )
    error_variable = tk.StringVar(value="")

    card = tk.Frame(dialog, bg=CARD_BG, padx=20, pady=18)
    card.pack(fill="both", expand=True, padx=14, pady=14)
    tk.Label(
        card,
        text="RGB 数值",
        bg=CARD_BG,
        fg=TEXT,
        font=(UI_FONT, 13, "bold"),
        anchor="w",
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

    def valid_channel(candidate: str) -> bool:
        return candidate == "" or (candidate.isdigit() and int(candidate) <= 255)

    validation = (dialog.register(valid_channel), "%P")
    for row, (label, variable) in enumerate(
        zip(("红色 R", "绿色 G", "蓝色 B"), rgb_variables),
        start=1,
    ):
        tk.Label(
            card,
            text=label,
            bg=CARD_BG,
            fg=TEXT,
            font=(UI_FONT, 10),
            anchor="w",
            width=7,
        ).grid(row=row, column=0, sticky="w", pady=4)
        control = tk.Spinbox(
            card,
            from_=0,
            to=255,
            textvariable=variable,
            width=6,
            font=("DejaVu Sans Mono", 11),
            justify="right",
            validate="key",
            validatecommand=validation,
            buttonbackground=SUBTLE_BG,
            relief="solid",
            bd=1,
        )
        control.grid(row=row, column=1, sticky="w", padx=(8, 18), pady=4)

    preview = tk.Frame(
        card,
        width=112,
        height=82,
        bg=hex_variable.get(),
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    preview.grid(row=1, column=2, rowspan=3, sticky="nsew", pady=4)
    preview.grid_propagate(False)

    tk.Label(
        card,
        text="HEX",
        bg=CARD_BG,
        fg=TEXT,
        font=(UI_FONT, 10),
        anchor="w",
    ).grid(row=4, column=0, sticky="w", pady=(14, 4))
    hex_entry = tk.Entry(
        card,
        textvariable=hex_variable,
        width=11,
        font=("DejaVu Sans Mono", 11),
        relief="solid",
        bd=1,
    )
    hex_entry.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(14, 4))
    tk.Label(
        card,
        textvariable=error_variable,
        bg=CARD_BG,
        fg=APPLE_RED,
        font=(UI_FONT, 9),
        anchor="w",
    ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(2, 6))

    def update_from_rgb(*_args: object) -> None:
        if updating[0]:
            return
        try:
            channels = tuple(int(variable.get()) for variable in rgb_variables)
            if any(channel < 0 or channel > 255 for channel in channels):
                raise ValueError
        except ValueError:
            error_variable.set("请输入 0–255 的整数")
            return
        colour = f"#{channels[0]:02x}{channels[1]:02x}{channels[2]:02x}"
        updating[0] = True
        hex_variable.set(colour)
        updating[0] = False
        preview.configure(bg=colour)
        error_variable.set("")

    def apply_hex(_event: object | None = None) -> bool:
        value = hex_variable.get().strip().lower()
        if not value.startswith("#"):
            value = "#" + value
        try:
            if len(value) != 7:
                raise ValueError
            channels = tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))
        except ValueError:
            error_variable.set("请输入六位 HEX 颜色，例如 #2081ee")
            return False
        updating[0] = True
        for variable, channel in zip(rgb_variables, channels):
            variable.set(str(channel))
        hex_variable.set(value)
        updating[0] = False
        preview.configure(bg=value)
        error_variable.set("")
        return True

    for variable in rgb_variables:
        variable.trace_add("write", update_from_rgb)
    hex_entry.bind("<Return>", apply_hex)
    hex_entry.bind("<FocusOut>", apply_hex)

    actions = tk.Frame(card, bg=CARD_BG)
    actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    def accept() -> None:
        if not apply_hex():
            hex_entry.focus_set()
            return
        result.append(hex_variable.get().lower())
        dialog.destroy()

    tk.Button(
        actions,
        text="确定",
        command=accept,
        bg=APPLE_BLUE,
        fg="white",
        activebackground=APPLE_BLUE_ACTIVE,
        activeforeground="white",
        relief="flat",
        padx=24,
        pady=7,
    ).pack(side="left", fill="x", expand=True)
    tk.Button(
        actions,
        text="取消",
        command=dialog.destroy,
        bg=SUBTLE_BG,
        fg=TEXT,
        activebackground="#e8e8ed",
        relief="flat",
        padx=24,
        pady=7,
    ).pack(side="left", fill="x", expand=True, padx=(10, 0))

    dialog.bind("<Escape>", lambda _event: dialog.destroy())
    dialog.bind("<Control-Return>", lambda _event: accept())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
    dialog.grab_set()
    dialog.wait_window()
    return result[0] if result else None

# QMK/Vial uses a hue wheel from 0 through 255.
LIGHTING = {
    "processing": (14, 0, 75, "处理中 · 原生彩虹", "#a855f7"),
    "waiting_confirmation": (6, 21, 190, "需要确认 · 快速呼吸", "#ff7e00"),
    "idle": (2, 170, 128, "空闲中 · 常亮", "#0000ff"),
    "completed": (6, 170, 90, "已完成 · 呼吸", "#0000ff"),
}


def effective_lighting_state(codex_state: str, keyboard_mode: bool) -> str:
    """Keyboard-mode lighting may replace Codex lighting only while idle."""
    if keyboard_mode and codex_state == "idle":
        return "keyboard_mode"
    return codex_state
PROCESSING_UI_COLORS = (
    "#ff3b30", "#ff9500", "#ffcc00",
    "#34c759", "#007aff", "#af52de",
)

# Native Vial/QMK effects exposed by Raspberry Pi's rpi-keyboard-config.
# Values are (firmware effect id, speed, Chinese name, description).
PROCESSING_EFFECTS = {
    "cycle_left_right": (14, 75, "横向流动", "彩虹从左向右持续流动（当前默认）"),
    "cycle_all": (13, 90, "整体变色", "整块键盘同步循环变换彩虹颜色"),
    "cycle_up_down": (15, 85, "上下流动", "彩虹沿键盘纵向持续流动"),
    "moving_chevron": (16, 90, "彩虹箭头", "箭头形彩虹从左向右移动"),
    "cycle_out_in": (17, 90, "中心扩散", "接近开机动画的中心与两侧流动"),
    "cycle_out_in_dual": (18, 90, "双向扩散", "两组彩虹从中心与两侧交错流动"),
    "cycle_pinwheel": (19, 90, "中心旋转", "彩虹围绕键盘中心旋转"),
    "cycle_spiral": (20, 90, "彩虹螺旋", "彩虹以螺旋方式围绕中心移动"),
    "rainbow_beacon": (22, 95, "彩虹信标", "紧密的彩虹光束围绕中心旋转"),
    "rainbow_pinwheels": (23, 95, "双旋彩虹", "键盘左右两半分别旋转彩虹"),
    "gradient_left_right": (5, 128, "固定彩虹", "固定的横向彩虹渐变，不移动"),
}


class CodexRGBApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.results: queue.Queue[tuple[str, object]] = queue.Queue()
        self.current_state = ""
        self.latest_codex_state = "idle"
        self.lighting_runtime_status = {
            "pid": os.getpid(),
            "started_at": time.time(),
            "codex_state": "idle",
            "effective_lighting_state": "idle",
            "last_waiting_confirmation_at": "",
            "last_error": "",
        }
        self.next_lighting_status_write = 0.0
        write_lighting_runtime_status(self.lighting_runtime_status)
        self.preview_generation = 0
        self.preview_active = False
        self.processing_preview_message: tk.StringVar | None = None
        self.busy = False
        self.closed = False
        self.refresh_scheduled = False
        self.command_lock = threading.Lock()
        self.keyboard = None
        self.tray_process: subprocess.Popen | None = None
        self.show_requested = False
        self.quit_requested = False
        self.bt_busy = False
        self.bt_service_available = False
        self.bt_keyboard_active: bool | None = None
        self.bt_lighting_apply_after: str | None = None
        self.lock_poll_busy = False
        self.system_locked: bool | None = None
        self.last_keyboard_activity = keyboard_activity_token()
        self.lock_wake_until = 0.0
        self.monitor_busy = False
        self.brightness_poll_busy = False
        state_brightness, state_colors = self.load_settings()
        self.state_brightness = state_brightness
        self.state_colors = state_colors
        self.color_buttons: dict[str, tk.Button] = {}
        self.color_images: dict[str, tk.PhotoImage] = {}
        self.color_labels: dict[str, str] = {}
        self.brightness_variables = {
            state: tk.IntVar(value=value)
            for state, value in state_brightness.items()
        }

        root.title("Pi 500+ 键盘控制中心")
        root.resizable(False, False)
        root.configure(bg=WINDOW_BG)
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        shell = tk.Frame(root, bg=WINDOW_BG, padx=24, pady=18)
        shell.pack(fill="both", expand=True)
        tk.Label(
            shell,
            text="Pi 500+ 键盘控制中心",
            bg=WINDOW_BG,
            fg=TEXT,
            font=(UI_FONT, 23, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            shell,
            text="蓝牙键盘与 Codex 状态灯",
            bg=WINDOW_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 11),
            anchor="w",
        ).pack(fill="x", pady=(1, 16))

        container = tk.Frame(shell, bg=WINDOW_BG)
        container.pack(fill="both", expand=True)
        self.make_bluetooth_panel(container)
        frame = tk.Frame(
            container,
            bg=CARD_BG,
            padx=22,
            pady=16,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        frame.pack(side="left", fill="both", expand=True, padx=(0, 14))
        tk.Label(
            frame,
            text="Codex 状态灯",
            bg=CARD_BG,
            fg=TEXT,
            font=(UI_FONT, 17, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            frame,
            text="根据当前 Codex 状态自动调整键盘灯效",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(fill="x", pady=(1, 4))

        self.dot = tk.Canvas(frame, width=38, height=38, bg=CARD_BG, highlightthickness=0)
        self.dot.pack(pady=(10, 3))
        self.circle = self.dot.create_oval(5, 5, 29, 29, fill="#6b7280", outline="")
        self.processing_dot_segments = [
            self.dot.create_arc(
                5, 5, 29, 29,
                start=index * 60,
                extent=61,
                fill=color,
                outline=color,
                state="hidden",
            )
            for index, color in enumerate(PROCESSING_UI_COLORS)
        ]

        self.status = tk.StringVar(value="正在连接键盘…")
        tk.Label(
            frame,
            textvariable=self.status,
            bg=CARD_BG,
            fg=TEXT,
            font=(UI_FONT, 11, "bold"),
        ).pack()

        self.detail = tk.StringVar(value="绿：处理中  ·  蓝：空闲  ·  橙：需确认")
        tk.Label(
            frame,
            textvariable=self.detail,
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
        ).pack(pady=(7, 8))

        colours = tk.LabelFrame(
            frame,
            text=" 状态颜色 ",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            bd=1,
            relief="solid",
            font=(UI_FONT, 9),
            padx=9,
            pady=9,
        )
        colours.pack(fill="x", pady=(0, 9))
        for index, (state, label) in enumerate((
            ("processing", "处理中"),
            ("idle", "空闲"),
            ("completed", "完成"),
            ("waiting_confirmation", "需确认"),
        )):
            button = tk.Button(
                colours,
                text=label,
                command=lambda selected=state: self.choose_state_color(selected),
                relief="flat",
                padx=10,
                pady=5,
                bg=SUBTLE_BG,
                fg=TEXT,
                activebackground="#e8e8ed",
                activeforeground=TEXT,
            )
            button.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=3,
                pady=3,
            )
            colours.grid_columnconfigure(index % 2, weight=1)
            self.color_buttons[state] = button
            self.color_labels[state] = label
            self.update_color_button(state)

        brightness = tk.LabelFrame(
            frame,
            text=" 各状态亮度 ",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            bd=1,
            relief="solid",
            font=(UI_FONT, 9),
            padx=9,
            pady=6,
        )
        brightness.pack(fill="x")
        for state, label in (
            ("processing", "处理中"),
            ("idle", "空闲"),
            ("completed", "完成"),
            ("waiting_confirmation", "需确认"),
        ):
            self.make_brightness_row(
                brightness,
                label,
                self.brightness_variables[state],
                lambda value, selected=state: self.brightness_changed(selected, value),
            )

        self.firmware_actions = tk.Frame(frame, bg=CARD_BG)
        self.update_button = tk.Button(
            self.firmware_actions,
            text="升级键盘固件",
            command=self.update_firmware,
            bg=APPLE_BLUE,
            fg="white",
            activebackground=APPLE_BLUE_ACTIVE,
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=5,
        )

        self.make_monitor_panel(container)

        self.root.after(80, self.poll_results)
        self.root.after(50, self.poll_lock_state)
        self.root.after(150, self.refresh)
        self.root.after(250, self.poll_bt_status)
        self.root.after(350, self.poll_monitor_status)
        self.root.after(500, self.poll_brightness_status)
        self.install_tray()
        self.root.after(100, self.poll_tray_requests)

    def make_bluetooth_panel(self, parent: tk.Misc) -> None:
        panel = tk.Frame(
            parent,
            bg=CARD_BG,
            padx=22,
            pady=16,
            width=430,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        panel.pack(side="left", fill="both", padx=(0, 14))

        heading = tk.Frame(panel, bg=CARD_BG)
        heading.pack(fill="x", pady=(0, 5))
        tk.Label(
            heading,
            text="Pi 500+ 蓝牙键盘",
            bg=CARD_BG,
            fg=TEXT,
            font=(UI_FONT, 17, "bold"),
            anchor="w",
        ).pack(side="left")
        self.bt_connection = tk.StringVar(value="● Windows 未连接")
        self.bt_connection_label = tk.Label(
            heading,
            textvariable=self.bt_connection,
            bg=CARD_BG,
            fg=APPLE_RED,
            font=(UI_FONT, 10, "bold"),
            anchor="e",
        )
        self.bt_connection_label.pack(side="right", padx=(12, 0))
        tk.Label(
            panel,
            text="把内置机械键盘作为标准蓝牙键盘连接到 Windows",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(fill="x", pady=(0, 14))

        status_frame = tk.Frame(
            panel,
            bg=SUBTLE_BG,
            highlightbackground="#e5e5ea",
            highlightthickness=0,
            padx=14,
            pady=13,
        )
        status_frame.pack(fill="x", pady=(0, 14))
        self.bt_status = tk.StringVar(value="● 键盘模式已关闭")
        self.bt_status_label = tk.Label(
            status_frame,
            textvariable=self.bt_status,
            bg=SUBTLE_BG,
            fg=APPLE_RED,
            font=(UI_FONT, 13, "bold"),
            anchor="w",
            justify="left",
        )
        self.bt_status_label.pack(fill="x")

        lighting_row = tk.Frame(panel, bg=CARD_BG)
        lighting_row.pack(fill="x", pady=(0, 3))
        tk.Label(
            lighting_row,
            text="键盘模式灯光",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(side="left")
        self.bt_lighting_button = tk.Button(
            lighting_row,
            command=self.choose_keyboard_mode_color,
            relief="flat",
            padx=10,
            pady=5,
            bg=SUBTLE_BG,
            fg=TEXT,
            activebackground="#e8e8ed",
            activeforeground=TEXT,
        )
        self.bt_lighting_button.pack(side="right")
        self.color_buttons["keyboard_mode"] = self.bt_lighting_button
        self.color_labels["keyboard_mode"] = "键盘模式"
        self.update_color_button("keyboard_mode")

        brightness_row = tk.Frame(panel, bg=CARD_BG)
        brightness_row.pack(fill="x", pady=(0, 12))
        tk.Label(
            brightness_row,
            text="灯光亮度",
            width=7,
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(side="left")
        self.bt_lighting_brightness = tk.Scale(
            brightness_row,
            from_=1,
            to=255,
            orient="horizontal",
            variable=self.brightness_variables["keyboard_mode"],
            command=self.keyboard_mode_brightness_changed,
            showvalue=True,
            resolution=1,
            length=235,
            bg=CARD_BG,
            fg=TEXT,
            font=(UI_FONT, 8),
            troughcolor="#e5e5ea",
            activebackground=APPLE_BLUE,
            highlightthickness=0,
        )
        self.bt_lighting_brightness.pack(side="left", fill="x", expand=True)

        tk.Label(
            panel,
            text=("首次使用：允许新电脑配对，然后在 Windows 的“蓝牙和设备”"
                  "中添加 Pi500+ Keyboard。"),
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
            justify="left",
            wraplength=395,
        ).pack(fill="x", pady=(0, 14))

        self.bt_pair_button = tk.Button(
            panel,
            text="允许新电脑配对（120 秒）",
            command=lambda: self.run_bt_command("pair"),
            bg=APPLE_BLUE,
            fg="white",
            activebackground=APPLE_BLUE_ACTIVE,
            activeforeground="white",
            relief="flat",
            pady=8,
        )
        self.bt_pair_button.pack(fill="x", pady=(0, 9))

        mode_buttons = tk.Frame(panel, bg=CARD_BG)
        mode_buttons.pack(fill="x", pady=(0, 9))
        self.bt_start_button = tk.Button(
            mode_buttons,
            text="开启键盘模式",
            command=lambda: self.run_bt_command("start"),
            bg=APPLE_BLUE,
            fg="white",
            activebackground=APPLE_BLUE_ACTIVE,
            activeforeground="white",
            relief="flat",
            pady=8,
        )
        self.bt_start_button.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.bt_stop_button = tk.Button(
            mode_buttons,
            text="关闭键盘模式",
            command=lambda: self.run_bt_command("stop"),
            bg=SUBTLE_BG,
            fg=APPLE_BLUE,
            activebackground="#e8e8ed",
            activeforeground=APPLE_BLUE,
            highlightbackground=BORDER,
            highlightthickness=1,
            relief="flat",
            pady=8,
        )
        self.bt_stop_button.pack(side="left", fill="x", expand=True, padx=(5, 0))

        self.bt_clear_button = tk.Button(
            panel,
            text="清除已连接设备",
            command=self.clear_bt_devices,
            bg=CARD_BG,
            fg=APPLE_RED,
            activebackground="#fff2f1",
            activeforeground=APPLE_RED,
            highlightbackground=BORDER,
            highlightthickness=1,
            relief="flat",
            pady=8,
        )
        self.bt_clear_button.pack(fill="x")

        tk.Label(
            panel,
            text="KEY1：切换键盘模式  ·  Ctrl+Alt+F12：紧急退出",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(side="bottom", fill="x", pady=(12, 0))

        for button in (
            self.bt_pair_button,
            self.bt_start_button,
            self.bt_stop_button,
            self.bt_clear_button,
        ):
            button.configure(state="disabled")

    @staticmethod
    def bt_request(command: str) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(4)
            client.connect(BT_KEYBOARD_SOCKET)
            client.sendall(json.dumps({"command": command}).encode("utf-8"))
            data = b""
            while b"\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode("utf-8"))

    def poll_bt_status(self) -> None:
        if not self.closed and not self.bt_busy:
            self.bt_busy = True
            threading.Thread(
                target=self.bt_command_worker, args=("status",), daemon=True
            ).start()
        if not self.closed:
            self.root.after(BT_STATUS_POLL_MS, self.poll_bt_status)

    def poll_lock_state(self) -> None:
        if not self.closed and not self.lock_poll_busy:
            self.lock_poll_busy = True
            threading.Thread(target=self.lock_state_worker, daemon=True).start()
        if not self.closed:
            self.root.after(LOCK_STATE_POLL_MS, self.poll_lock_state)

    def lock_state_worker(self) -> None:
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-x", "swaylock"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=False,
            )
            self.results.put(("lock_state", result.returncode == 0))
        except Exception as error:
            self.results.put(("lock_state_error", str(error)))

    def apply_lock_state(self, locked: bool) -> None:
        previous = self.system_locked
        self.system_locked = locked
        activity = keyboard_activity_token()
        if locked and previous is not True:
            # Ignore the key that initiated locking. Only activity observed
            # after the lock screen is active is allowed to wake the lights.
            self.last_keyboard_activity = activity
            self.lock_wake_until = 0.0
            self.preview_generation += 1
            self.preview_active = False
            self.current_state = ""
            threading.Thread(
                target=self.turn_off_keyboard_lighting_worker,
                daemon=True,
            ).start()
        elif locked:
            now = time.monotonic()
            was_awake = self.lock_lighting_allowed()
            if activity and activity != self.last_keyboard_activity:
                self.last_keyboard_activity = activity
                self.lock_wake_until = now + LOCK_KEYBOARD_WAKE_SECONDS
                if not was_awake:
                    self.current_state = ""
                    if self.keyboard_mode_lighting_should_apply():
                        self.activate_keyboard_mode_lighting()
                    else:
                        self.restore_codex_lighting()
            elif self.lock_wake_until and now >= self.lock_wake_until:
                self.lock_wake_until = 0.0
                self.current_state = ""
                threading.Thread(
                    target=self.turn_off_keyboard_lighting_worker,
                    daemon=True,
                ).start()
        elif not locked and previous is not False:
            self.last_keyboard_activity = activity
            self.lock_wake_until = 0.0
            if self.keyboard_mode_lighting_should_apply():
                self.activate_keyboard_mode_lighting()
            else:
                self.restore_codex_lighting()

    def lock_lighting_allowed(self) -> bool:
        if self.system_locked is False:
            return True
        return (
            self.system_locked is True
            and self.lock_wake_until > time.monotonic()
        )

    def turn_off_keyboard_lighting_worker(self) -> None:
        try:
            with self.command_lock:
                if self.system_locked and not self.lock_lighting_allowed():
                    self.get_keyboard().set_brightness(0)
        except Exception as error:
            self.results.put(("lock_lighting_error", str(error)))

    def run_bt_command(self, command: str) -> None:
        if self.bt_busy:
            return
        self.bt_busy = True
        self.bt_status.set("正在执行…")
        threading.Thread(
            target=self.bt_command_worker, args=(command,), daemon=True
        ).start()

    def bt_command_worker(self, command: str) -> None:
        try:
            result = self.bt_request(command)
            self.results.put(("bt_result", (command, result)))
        except Exception as error:
            self.results.put(("bt_error", str(error)))

    def clear_bt_devices(self) -> None:
        if not messagebox.askyesno(
            "清除已连接设备",
            "确定删除树莓派上的全部蓝牙键盘配对记录吗？\n之后需要在 Windows 中删除旧设备并重新配对。",
            parent=self.root,
        ):
            return
        self.run_bt_command("clear")

    def apply_bt_status(self, result: dict) -> None:
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error", "后台服务返回错误")))
        self.bt_service_available = True
        active = bool(result.get("active"))
        previous_active = self.bt_keyboard_active
        self.bt_keyboard_active = active
        connected = bool(result.get("connected"))
        pairing = bool(result.get("pairing"))
        if connected:
            connection_text = "● Windows 已连接"
            connection_color = APPLE_GREEN
        elif pairing:
            connection_text = "● 等待 Windows 配对"
            connection_color = APPLE_BLUE
        else:
            connection_text = "● Windows 未连接"
            connection_color = APPLE_RED
        self.bt_connection.set(connection_text)
        self.bt_connection_label.configure(fg=connection_color)

        if active:
            headline = "● 键盘模式已开启"
            status_color = APPLE_GREEN
        else:
            headline = "● 键盘模式已关闭"
            status_color = APPLE_RED
        devices = result.get("paired_devices", [])
        self.bt_status.set(headline)
        self.bt_status_label.configure(fg=status_color)
        self.bt_pair_button.configure(state="disabled" if active or pairing else "normal")
        self.bt_start_button.configure(state="normal" if connected and not active else "disabled")
        self.bt_stop_button.configure(state="normal" if active else "disabled")
        self.bt_clear_button.configure(state="normal" if devices and not active else "disabled")
        if active and previous_active is not True:
            if self.keyboard_mode_lighting_should_apply():
                self.activate_keyboard_mode_lighting()
            else:
                self.restore_codex_lighting()
        elif not active and previous_active is True:
            self.restore_codex_lighting()

    def mark_bt_unavailable(self, message: str) -> None:
        self.bt_service_available = False
        self.bt_connection.set("● Windows 未连接")
        self.bt_connection_label.configure(fg=APPLE_RED)
        self.bt_status.set("● 键盘模式已关闭")
        self.bt_status_label.configure(fg=APPLE_RED)
        for button in (
            self.bt_pair_button,
            self.bt_start_button,
            self.bt_stop_button,
            self.bt_clear_button,
        ):
            button.configure(state="disabled")

    def make_monitor_panel(self, parent: tk.Misc) -> None:
        panel = tk.Frame(
            parent,
            bg=CARD_BG,
            padx=22,
            pady=16,
            width=300,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        panel.pack(side="left", fill="both")
        tk.Label(
            panel,
            text="用量与显示器",
            bg=CARD_BG,
            fg=TEXT,
            font=(UI_FONT, 17, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            panel,
            text="Codex 额度、LCD HAT 与 AOC 控制",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(fill="x", pady=(1, 12))

        quota_card = tk.Frame(panel, bg=SUBTLE_BG, padx=13, pady=12)
        quota_card.pack(fill="x", pady=(0, 10))
        self.quota_5h_text = tk.StringVar(value="--%")
        self.quota_7d_text = tk.StringVar(value="--%")
        self.quota_5h_reset = tk.StringVar(value="重置时间 --")
        self.quota_7d_reset = tk.StringVar(value="重置时间 --")
        self.quota_updated = tk.StringVar(value="正在读取 Codex 用量…")
        self.quota_5h_bar = self.make_quota_row(
            quota_card, "5 小时", self.quota_5h_text, APPLE_BLUE
        )
        tk.Label(
            quota_card,
            textvariable=self.quota_5h_reset,
            bg=SUBTLE_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 8),
            anchor="w",
        ).pack(fill="x", pady=(2, 7))
        self.quota_7d_bar = self.make_quota_row(
            quota_card, "7 天", self.quota_7d_text, "#8e5af7"
        )
        tk.Label(
            quota_card,
            textvariable=self.quota_7d_reset,
            bg=SUBTLE_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 8),
            anchor="w",
        ).pack(fill="x", pady=(2, 6))
        tk.Label(
            quota_card,
            textvariable=self.quota_updated,
            bg=SUBTLE_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 8),
            anchor="w",
        ).pack(fill="x")

        monitor_card = tk.Frame(panel, bg=SUBTLE_BG, padx=13, pady=12)
        monitor_card.pack(fill="x", pady=(0, 10))
        self.monitor_input = tk.StringVar(value="信号源  正在读取…")
        self.monitor_brightness = tk.StringVar(value="亮度  --%")
        self.lcd_status = tk.StringVar(value="LCD HAT  正在连接…")
        for variable, weight in (
            (self.monitor_input, "bold"),
            (self.monitor_brightness, "normal"),
            (self.lcd_status, "normal"),
        ):
            tk.Label(
                monitor_card,
                textvariable=variable,
                bg=SUBTLE_BG,
                fg=TEXT if weight == "bold" else SECONDARY_TEXT,
                font=(UI_FONT, 10, weight),
                anchor="w",
            ).pack(fill="x", pady=2)

        self.monitor_toggle_button = tk.Button(
            panel,
            text="切换信号源",
            command=lambda: self.run_monitor_action("toggle"),
            bg=APPLE_BLUE,
            fg="white",
            activebackground=APPLE_BLUE_ACTIVE,
            activeforeground="white",
            relief="flat",
            pady=8,
        )
        self.monitor_toggle_button.pack(fill="x", pady=(0, 9))
        brightness_buttons = tk.Frame(panel, bg=CARD_BG)
        brightness_buttons.pack(fill="x")
        self.monitor_down_button = tk.Button(
            brightness_buttons,
            text="亮度 −5%",
            command=lambda: self.run_monitor_action("brightness_down"),
            bg=SUBTLE_BG,
            fg=APPLE_BLUE,
            activebackground="#e8e8ed",
            activeforeground=APPLE_BLUE,
            relief="flat",
            highlightbackground=BORDER,
            highlightthickness=1,
            pady=7,
        )
        self.monitor_down_button.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.monitor_up_button = tk.Button(
            brightness_buttons,
            text="亮度 +5%",
            command=lambda: self.run_monitor_action("brightness_up"),
            bg=SUBTLE_BG,
            fg=APPLE_BLUE,
            activebackground="#e8e8ed",
            activeforeground=APPLE_BLUE,
            relief="flat",
            highlightbackground=BORDER,
            highlightthickness=1,
            pady=7,
        )
        self.monitor_up_button.pack(side="left", fill="x", expand=True, padx=(5, 0))
        tk.Label(
            panel,
            text="KEY3：确认 Codex 请求  ·  摇杆左 DisplayPort / 右 HDMI / 上下亮度 ±5%",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 8),
            anchor="w",
        ).pack(side="bottom", fill="x", pady=(12, 0))
        self.set_monitor_buttons("disabled")

    def make_quota_row(
        self,
        parent: tk.Misc,
        label: str,
        variable: tk.StringVar,
        color: str,
    ) -> tk.Canvas:
        heading = tk.Frame(parent, bg=SUBTLE_BG)
        heading.pack(fill="x")
        tk.Label(
            heading,
            text=label,
            bg=SUBTLE_BG,
            fg=TEXT,
            font=(UI_FONT, 10, "bold"),
        ).pack(side="left")
        tk.Label(
            heading,
            textvariable=variable,
            bg=SUBTLE_BG,
            fg=TEXT,
            font=(UI_FONT, 12, "bold"),
        ).pack(side="right")
        canvas = tk.Canvas(
            parent,
            width=248,
            height=7,
            bg=SUBTLE_BG,
            highlightthickness=0,
        )
        canvas.pack(fill="x", pady=(4, 0))
        canvas.bar_color = color
        self.draw_quota_bar(canvas, 0)
        return canvas

    @staticmethod
    def draw_quota_bar(canvas: tk.Canvas, percent: int) -> None:
        canvas.delete("all")
        canvas.create_rectangle(0, 0, 248, 7, fill="#dedee3", outline="")
        width = round(248 * max(0, min(100, percent)) / 100)
        if width:
            canvas.create_rectangle(
                0, 0, width, 7, fill=canvas.bar_color, outline=""
            )

    @staticmethod
    def format_reset(timestamp: int | None) -> str:
        if not timestamp:
            return "重置时间 --"
        return "重置 " + time.strftime("%m-%d %H:%M", time.localtime(timestamp))

    @staticmethod
    def control_api(path: str, method: str = "GET") -> dict:
        request = Request(
            LOCAL_CONTROL_API + path,
            data=b"" if method == "POST" else None,
            method=method,
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def poll_monitor_status(self) -> None:
        if not self.closed and not self.monitor_busy:
            self.monitor_busy = True
            threading.Thread(
                target=self.monitor_worker, args=(None,), daemon=True
            ).start()
        if not self.closed:
            self.root.after(15000, self.poll_monitor_status)

    def poll_brightness_status(self) -> None:
        if not self.closed and not self.brightness_poll_busy:
            self.brightness_poll_busy = True
            threading.Thread(
                target=self.brightness_status_worker,
                daemon=True,
            ).start()
        if not self.closed:
            self.root.after(BRIGHTNESS_POLL_MS, self.poll_brightness_status)

    def brightness_status_worker(self) -> None:
        try:
            result = self.control_api("/monitor/brightness/cached")
            self.results.put(("brightness_status", result))
        except Exception as error:
            self.results.put(("brightness_status_error", str(error)))

    def run_monitor_action(self, action: str) -> None:
        if self.monitor_busy:
            return
        self.monitor_busy = True
        self.set_monitor_buttons("disabled")
        self.lcd_status.set("正在执行显示器操作…")
        threading.Thread(
            target=self.monitor_worker, args=(action,), daemon=True
        ).start()

    def monitor_worker(self, action: str | None) -> None:
        paths = {
            "toggle": "/monitor/toggle",
            "brightness_up": "/monitor/brightness/up",
            "brightness_down": "/monitor/brightness/down",
        }
        result: dict[str, object] = {"action": action}
        try:
            if action:
                result["action_result"] = self.control_api(paths[action], "POST")
            for name, path in (
                ("quota", "/quota"),
                ("monitor", "/monitor/status"),
                ("brightness", "/monitor/brightness"),
            ):
                try:
                    result[name] = self.control_api(path)
                except Exception as error:
                    result[name] = {"ok": False, "error": str(error)}
            self.results.put(("monitor_result", result))
        except Exception as error:
            self.results.put(("monitor_error", str(error)))

    def set_monitor_buttons(self, state: str) -> None:
        for button in (
            self.monitor_toggle_button,
            self.monitor_down_button,
            self.monitor_up_button,
        ):
            button.configure(state=state)

    def apply_monitor_status(self, bundle: dict) -> None:
        quota = bundle.get("quota", {})
        if quota.get("ok"):
            windows = [quota.get("primary"), quota.get("secondary")]
            short = next(
                (window for window in windows if window and (window.get("window_minutes") or 0) < 1440),
                None,
            )
            long = next(
                (window for window in windows if window and (window.get("window_minutes") or 0) >= 1440),
                None,
            )
            short_value = int((short or {}).get("remaining_percent", 0))
            long_value = int((long or {}).get("remaining_percent", 0))
            self.quota_5h_text.set(f"{short_value}%")
            self.quota_7d_text.set(f"{long_value}%")
            self.draw_quota_bar(self.quota_5h_bar, short_value)
            self.draw_quota_bar(self.quota_7d_bar, long_value)
            self.quota_5h_reset.set(self.format_reset((short or {}).get("resets_at")))
            self.quota_7d_reset.set(self.format_reset((long or {}).get("resets_at")))
            fetched = quota.get("fetched_at")
            updated = time.strftime("%H:%M", time.localtime(fetched)) if fetched else "--:--"
            self.quota_updated.set(f"额度刷新 {updated}")
            self.lcd_status.set("● LCD HAT 与额度服务在线")
        else:
            self.quota_updated.set("额度服务不可用")
            self.lcd_status.set("○ LCD HAT 服务不可用")

        monitor = bundle.get("monitor", {})
        brightness = bundle.get("brightness", {})
        if monitor.get("ok"):
            self.monitor_input.set("信号源  " + str(monitor.get("current_name", "--")))
        else:
            self.monitor_input.set("信号源  不可用")
        if brightness.get("ok"):
            self.monitor_brightness.set(
                f"亮度  {int(brightness.get('brightness', 0))}%"
            )
        else:
            self.monitor_brightness.set("亮度  --%")
        self.set_monitor_buttons(
            "normal" if monitor.get("ok") and brightness.get("ok") else "disabled"
        )

    def make_brightness_row(
        self,
        parent: tk.Misc,
        label: str,
        variable: tk.IntVar,
        callback,
    ) -> None:
        row = tk.Frame(parent, bg=CARD_BG)
        row.pack(fill="x")
        tk.Label(
            row,
            text=label,
            width=5,
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(side="left")
        scale = tk.Scale(
            row,
            from_=1,
            to=255,
            orient="horizontal",
            variable=variable,
            command=callback,
            showvalue=True,
            resolution=1,
            length=190,
            bg=CARD_BG,
            fg=TEXT,
            font=(UI_FONT, 8),
            troughcolor="#e5e5ea",
            activebackground=APPLE_BLUE,
            highlightthickness=0,
        )
        scale.pack(side="left", fill="x", expand=True)
    @classmethod
    def load_settings(cls) -> tuple[dict[str, int], dict[str, str]]:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            legacy_brightness = min(
                255, max(1, int(data.get("maximum_brightness", 180)))
            )
            saved_brightness = data.get("state_brightness", {})
            brightness = {
                state: min(
                    255,
                    max(1, int(saved_brightness.get(state, legacy_brightness))),
                )
                for state in DEFAULT_STATE_BRIGHTNESS
            }
            saved_colors = data.get("state_colors", {})
            colors = {}
            for state, default in DEFAULT_STATE_COLORS.items():
                saved = str(saved_colors.get(state, default))
                # Migrate the first version of the picker, where every native
                # rainbow effect was represented by the single word "rainbow".
                if state == "processing" and saved == "rainbow":
                    saved = "cycle_left_right"
                colors[state] = (
                    saved if cls.valid_state_color(state, saved) else default
                )
            return brightness, colors
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return DEFAULT_STATE_BRIGHTNESS.copy(), DEFAULT_STATE_COLORS.copy()

    @staticmethod
    def valid_color(value: str) -> bool:
        if len(value) != 7 or not value.startswith("#"):
            return False
        try:
            int(value[1:], 16)
            return True
        except ValueError:
            return False

    @classmethod
    def valid_state_color(cls, state: str, value: str) -> bool:
        return (
            state == "processing" and value in PROCESSING_EFFECTS
        ) or cls.valid_color(value)

    def save_settings(self) -> None:
        data = {
            "state_brightness": self.state_brightness,
            "state_colors": self.state_colors,
        }
        try:
            SETTINGS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def brightness_changed(self, state: str, value: str) -> None:
        self.state_brightness[state] = int(float(value))
        self.save_settings()
        if self.current_state == state:
            self.current_state = ""

    def choose_state_color(self, state: str) -> None:
        if state == "processing":
            self.choose_processing_effect()
            return
        selected = ask_rgb_color(
            self.root,
            self.state_colors[state],
            title="选择状态灯光颜色",
        )
        if not selected:
            return
        self.state_colors[state] = selected.lower()
        self.update_color_button(state)
        self.save_settings()
        self.current_state = ""
        if state in {"completed", "idle"}:
            self.detail.set(self.completion_lighting_detail())

    def choose_keyboard_mode_color(self) -> None:
        selected = ask_rgb_color(
            self.root,
            self.state_colors["keyboard_mode"],
            title="选择键盘模式灯光颜色",
        )
        if not selected:
            return
        self.state_colors["keyboard_mode"] = selected.lower()
        self.update_color_button("keyboard_mode")
        self.save_settings()
        if self.keyboard_mode_lighting_should_apply():
            self.activate_keyboard_mode_lighting()

    def keyboard_mode_brightness_changed(self, value: str) -> None:
        self.state_brightness["keyboard_mode"] = int(float(value))
        self.save_settings()
        if self.bt_lighting_apply_after is not None:
            try:
                self.root.after_cancel(self.bt_lighting_apply_after)
            except tk.TclError:
                pass
            self.bt_lighting_apply_after = None
        if self.keyboard_mode_lighting_should_apply():
            self.bt_lighting_apply_after = self.root.after(
                80,
                self.apply_scheduled_keyboard_mode_lighting,
            )

    def apply_scheduled_keyboard_mode_lighting(self) -> None:
        self.bt_lighting_apply_after = None
        if self.keyboard_mode_lighting_should_apply():
            self.activate_keyboard_mode_lighting()

    def keyboard_mode_lighting_should_apply(self) -> bool:
        return bool(
            self.bt_keyboard_active and self.latest_codex_state == "idle"
        )

    def activate_keyboard_mode_lighting(self) -> None:
        """Apply keyboard-mode lighting only when Codex is genuinely idle."""
        if self.bt_lighting_apply_after is not None:
            try:
                self.root.after_cancel(self.bt_lighting_apply_after)
            except tk.TclError:
                pass
            self.bt_lighting_apply_after = None
        if not self.lock_lighting_allowed():
            return
        if not self.keyboard_mode_lighting_should_apply():
            self.restore_codex_lighting()
            return
        self.preview_generation += 1
        self.preview_active = False
        if self.processing_preview_message is not None:
            self.processing_preview_message.set("键盘模式已开启，灯效预览已结束")
        self.current_state = ""
        threading.Thread(
            target=self.apply_keyboard_mode_lighting_worker,
            daemon=True,
        ).start()

    def apply_keyboard_mode_lighting_worker(self) -> None:
        try:
            if not self.keyboard_mode_lighting_should_apply():
                return
            hue, saturation = self.color_to_hs(
                self.state_colors["keyboard_mode"]
            )
            self.apply_effect(
                2,
                hue,
                saturation,
                128,
                self.state_brightness["keyboard_mode"],
                require_bt_active=True,
            )
        except Exception as error:
            self.results.put(("keyboard_lighting_error", str(error)))

    def restore_codex_lighting(self) -> None:
        self.current_state = ""
        self.root.after(0, lambda: self.refresh_when_ready(0))

    def refresh_when_ready(self, attempt: int) -> None:
        if (
            self.closed
            or not self.lock_lighting_allowed()
        ):
            return
        if self.busy and attempt < 20:
            self.root.after(50, lambda: self.refresh_when_ready(attempt + 1))
            return
        self.refresh()

    def choose_processing_effect(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("处理中灯效")
        dialog.configure(bg=WINDOW_BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        card = tk.Frame(
            dialog,
            bg=CARD_BG,
            padx=22,
            pady=18,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        card.pack(padx=14, pady=14)
        tk.Label(
            card,
            text="处理中状态灯效",
            bg=CARD_BG,
            fg=TEXT,
            font=(UI_FONT, 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            card,
            text="选择键盘固件原生动态灯效，或使用一种纯色常亮。",
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(2, 12))

        current = self.state_colors.get("processing", "cycle_left_right")
        if current not in PROCESSING_EFFECTS:
            current = "cycle_left_right"
        selected_effect = tk.StringVar(value=current)
        description = tk.StringVar(value=PROCESSING_EFFECTS[current][3])
        preview_message = tk.StringVar(value="点击模式可在键盘上预览 5 秒")
        self.processing_preview_message = preview_message
        effect_grid = tk.Frame(card, bg=CARD_BG)
        effect_grid.pack(fill="x")
        effect_images: dict[str, tk.PhotoImage] = {}
        for index, (key, (_effect, _speed, name, detail)) in enumerate(
            PROCESSING_EFFECTS.items()
        ):
            preview = self.color_swatch(key, width=44, height=14)
            effect_images[key] = preview
            tk.Radiobutton(
                effect_grid,
                text=name,
                image=preview,
                compound="left",
                variable=selected_effect,
                value=key,
                command=lambda key=key, name=name, detail=detail: (
                    description.set(detail),
                    self.preview_processing_effect(key, name),
                ),
                indicatoron=False,
                selectcolor="#dbeafe",
                bg=SUBTLE_BG,
                fg=TEXT,
                activebackground="#e8e8ed",
                activeforeground=TEXT,
                relief="flat",
                padx=8,
                pady=6,
                anchor="w",
                width=116,
            ).grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=3,
                pady=3,
            )
            effect_grid.grid_columnconfigure(index % 2, weight=1)
        # Keep PhotoImage instances alive for as long as the dialog exists.
        dialog.effect_images = effect_images

        tk.Label(
            card,
            textvariable=description,
            bg=CARD_BG,
            fg=SECONDARY_TEXT,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(fill="x", pady=(10, 8))

        tk.Label(
            card,
            textvariable=preview_message,
            bg=CARD_BG,
            fg=APPLE_BLUE,
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        actions = tk.Frame(card, bg=CARD_BG)
        actions.pack(fill="x")
        tk.Button(
            actions,
            text="应用所选动态灯效",
            command=lambda: self.set_processing_effect(selected_effect.get(), dialog),
            bg=APPLE_BLUE,
            fg="white",
            activebackground=APPLE_BLUE_ACTIVE,
            activeforeground="white",
            relief="flat",
            padx=14,
            pady=7,
        ).pack(side="left", fill="x", expand=True)
        tk.Button(
            actions,
            text="选择纯色…",
            command=lambda: self.choose_processing_solid(dialog),
            bg=SUBTLE_BG,
            fg=TEXT,
            activebackground="#e8e8ed",
            activeforeground=TEXT,
            relief="flat",
            padx=14,
            pady=7,
        ).pack(side="left", fill="x", expand=True, padx=(8, 0))

        dialog.protocol(
            "WM_DELETE_WINDOW", lambda: self.cancel_processing_preview(dialog)
        )

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (
            self.root.winfo_width() - dialog.winfo_width()
        ) // 2
        y = self.root.winfo_rooty() + (
            self.root.winfo_height() - dialog.winfo_height()
        ) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")

    def choose_processing_solid(self, dialog: tk.Toplevel) -> None:
        self.cancel_processing_preview(dialog)
        current = self.state_colors.get("processing", "cycle_left_right")
        initial = current if self.valid_color(current) else "#34c759"
        selected = ask_rgb_color(
            self.root,
            initial,
            title="选择处理中纯色",
        )
        if selected:
            self.set_processing_effect(selected.lower())

    def set_processing_effect(
        self, value: str, dialog: tk.Toplevel | None = None
    ) -> None:
        self.preview_generation += 1
        self.preview_active = False
        self.processing_preview_message = None
        if dialog is not None:
            dialog.grab_release()
            dialog.destroy()
        self.state_colors["processing"] = value
        self.update_color_button("processing")
        self.save_settings()
        self.current_state = ""
        self.root.after(0, self.refresh)

    def preview_processing_effect(self, key: str, name: str) -> None:
        if key not in PROCESSING_EFFECTS:
            return
        self.preview_generation += 1
        token = self.preview_generation
        self.preview_active = True
        if self.processing_preview_message is not None:
            self.processing_preview_message.set(f"正在预览：{name}（5 秒）")
        threading.Thread(
            target=self.processing_preview_worker,
            args=(key, token),
            daemon=True,
        ).start()

    def processing_preview_worker(self, key: str, token: int) -> None:
        try:
            effect, speed, _name, _description = PROCESSING_EFFECTS[key]
            applied = self.apply_effect(
                effect,
                0,
                255,
                speed,
                self.state_brightness["processing"],
                preview_token=token,
            )
            if not applied:
                return
            time.sleep(5.0)
            self.results.put(("preview_done", token))
        except Exception as error:
            self.results.put(("preview_error", (token, str(error))))

    def cancel_processing_preview(self, dialog: tk.Toplevel | None = None) -> None:
        was_active = self.preview_active
        self.preview_generation += 1
        self.preview_active = False
        self.processing_preview_message = None
        if dialog is not None:
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()
        if was_active:
            self.current_state = ""
            self.root.after(0, self.refresh)

    @staticmethod
    def color_swatch(color: str, width: int = 28, height: int = 14) -> tk.PhotoImage:
        image = tk.PhotoImage(width=width, height=height)
        if color in PROCESSING_EFFECTS:
            for y in range(height):
                for x in range(width):
                    if color == "cycle_all":
                        hue = 0.58
                    elif color == "cycle_up_down":
                        hue = y / max(1, height - 1)
                    elif color in {"cycle_out_in", "cycle_out_in_dual"}:
                        distance = abs(x - (width - 1) / 2) / max(1, width / 2)
                        hue = distance + (0.28 if color.endswith("dual") and x > width / 2 else 0)
                    elif color == "moving_chevron":
                        hue = (x / max(1, width - 1) + abs(y - height / 2) / height) % 1
                    elif color in {"cycle_pinwheel", "rainbow_pinwheels"}:
                        centre = width * (0.25 if color == "rainbow_pinwheels" and x < width / 2 else 0.75 if color == "rainbow_pinwheels" else 0.5)
                        hue = (math.atan2(y - height / 2, x - centre) / (2 * math.pi)) % 1
                    elif color in {"cycle_spiral", "rainbow_beacon"}:
                        dx, dy = x - width / 2, y - height / 2
                        angle = math.atan2(dy, dx) / (2 * math.pi)
                        radius = math.hypot(dx, dy) / max(1, width / 2)
                        hue = (angle + radius * (0.7 if color == "cycle_spiral" else 0.25)) % 1
                    else:
                        hue = x / max(1, width - 1)
                    red, green, blue = colorsys.hsv_to_rgb(hue % 1, 0.82, 1.0)
                    value = (
                        f"#{round(red * 255):02x}{round(green * 255):02x}"
                        f"{round(blue * 255):02x}"
                    )
                    image.put(value, to=(x, y, x + 1, y + 1))
        else:
            image.put(color, to=(0, 0, width, height))
        return image

    def update_color_button(self, state: str) -> None:
        color = self.state_colors[state]
        preview = self.color_swatch(color)
        self.color_images[state] = preview
        color_name = (
            PROCESSING_EFFECTS[color][2]
            if color in PROCESSING_EFFECTS else chinese_color_name(color)
        )
        self.color_buttons[state].configure(
            text=f"{self.color_labels[state]} · {color_name}",
            image=preview,
            compound="left",
            bg=SUBTLE_BG,
            fg=TEXT,
            activebackground="#e8e8ed",
            activeforeground=TEXT,
        )

    def update_status_dot(self, state: str, color: str) -> None:
        """Mirror the selected processing effect in the UI status preview."""
        processing = (
            state == "processing"
            and self.state_colors.get("processing") in PROCESSING_EFFECTS
        )
        self.dot.itemconfigure(
            self.circle,
            fill=color,
            state="hidden" if processing else "normal",
        )
        for segment in self.processing_dot_segments:
            self.dot.itemconfigure(segment, state="normal" if processing else "hidden")

    def completion_lighting_detail(self) -> str:
        completed = chinese_color_name(self.state_colors["completed"])
        idle = chinese_color_name(self.state_colors["idle"])
        return f"完成后{completed}呼吸 1 分钟，再转为{idle}常亮"

    @staticmethod
    def color_to_hs(color: str) -> tuple[int, int]:
        red, green, blue = (int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))
        hue, saturation, _value = colorsys.rgb_to_hsv(red, green, blue)
        return round(hue * 255) % 256, round(saturation * 255)

    def lighting_for(self, state: str) -> tuple[int, int, int, int, str, str]:
        if state == "keyboard_mode":
            color = self.state_colors["keyboard_mode"]
            hue, saturation = self.color_to_hs(color)
            return 2, hue, saturation, 128, "键盘模式", color
        effect, hue, speed, label, color = LIGHTING[state]
        saturation = 255
        if state == "processing":
            selected = self.state_colors.get("processing", "cycle_left_right")
            if selected in PROCESSING_EFFECTS:
                effect, speed, effect_name, _description = PROCESSING_EFFECTS[selected]
                return effect, 0, saturation, speed, f"处理中 · {effect_name}", color
            hue, saturation = self.color_to_hs(selected)
            color_name = chinese_color_name(selected)
            return 2, hue, saturation, 128, f"处理中 · {color_name}常亮", selected
        if state in self.state_colors:
            color = self.state_colors[state]
            hue, saturation = self.color_to_hs(color)
        return effect, hue, saturation, speed, label, color

    def install_tray(self) -> None:
        signal.signal(signal.SIGUSR1, self.request_show)
        signal.signal(signal.SIGUSR2, self.request_quit)
        try:
            self.tray_process = subprocess.Popen(
                [
                    sys.executable,
                    str(APP_DIR / "tray_icon.py"),
                    str(os.getpid()),
                    str(APP_DIR / "codex-rgb-tray.png"),
                ]
            )
        except OSError as error:
            self.detail.set(f"系统托盘启动失败：{error}")

    def request_show(self, _signum=None, _frame=None) -> None:
        self.show_requested = True

    def request_quit(self, _signum=None, _frame=None) -> None:
        self.quit_requested = True

    def poll_tray_requests(self) -> None:
        if self.quit_requested:
            self.close()
            return
        if self.show_requested:
            self.show_requested = False
            self.show_window()
        if not self.closed:
            self.root.after(100, self.poll_tray_requests)

    def hide_to_tray(self) -> None:
        if self.tray_process is not None and self.tray_process.poll() is None:
            self.root.withdraw()
        else:
            self.root.iconify()
            self.detail.set("系统托盘不可用，已改为最小化到任务栏")

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def refresh(self) -> None:
        self.refresh_scheduled = False
        if self.closed or self.busy:
            self.schedule_refresh()
            return
        self.busy = True
        threading.Thread(target=self.read_and_apply, daemon=True).start()

    def schedule_refresh(self) -> None:
        if not self.closed and not self.refresh_scheduled:
            self.refresh_scheduled = True
            self.root.after(POLL_MS, self.refresh)

    def read_and_apply(self) -> None:
        try:
            state = read_combined_codex_status(DEFAULT_CODEX_HOME).code
            if state not in LIGHTING:
                state = "idle"
            previous_codex_state = self.latest_codex_state
            self.latest_codex_state = state
            lighting_state = effective_lighting_state(
                state, bool(self.bt_keyboard_active)
            )
            self.lighting_runtime_status["codex_state"] = state
            self.lighting_runtime_status["effective_lighting_state"] = lighting_state
            self.lighting_runtime_status["updated_at"] = time.time()
            self.lighting_runtime_status["last_error"] = ""
            if state == "waiting_confirmation" and previous_codex_state != state:
                self.lighting_runtime_status["last_waiting_confirmation_at"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )
            now = time.monotonic()
            if previous_codex_state != state or now >= self.next_lighting_status_write:
                self.next_lighting_status_write = now + 1.0
                write_lighting_runtime_status(self.lighting_runtime_status)
            if (
                self.preview_active
                or not self.lock_lighting_allowed()
            ):
                self.results.put(("state", state))
                return
            if lighting_state != self.current_state:
                effect, hue, saturation, speed, _label, _colour = self.lighting_for(
                    lighting_state
                )
                target_brightness = self.state_brightness[lighting_state]
                if self.current_state:
                    previous_brightness = self.state_brightness.get(
                        self.current_state, target_brightness
                    )
                    if not self.fade_brightness(previous_brightness, 1):
                        self.results.put(("state", state))
                        return
                    if not self.apply_effect(
                        effect,
                        hue,
                        saturation,
                        speed,
                        1,
                    ):
                        self.results.put(("state", state))
                        return
                    if not self.fade_brightness(1, target_brightness):
                        self.results.put(("state", state))
                        return
                else:
                    if not self.apply_effect(
                        effect,
                        hue,
                        saturation,
                        speed,
                        target_brightness,
                    ):
                        self.results.put(("state", state))
                        return
                self.current_state = lighting_state
                self.lighting_runtime_status["applied_lighting_state"] = lighting_state
                self.lighting_runtime_status["applied_at"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )
                write_lighting_runtime_status(self.lighting_runtime_status)
            self.results.put(("state", state))
        except Exception as error:
            self.lighting_runtime_status["last_error"] = repr(error)
            self.lighting_runtime_status["updated_at"] = time.time()
            write_lighting_runtime_status(self.lighting_runtime_status)
            self.results.put(("error", str(error)))

    def get_keyboard(self):
        if self.keyboard is None:
            from RPiKeyboardConfig.keyboard import RPiKeyboardConfig

            self.keyboard = RPiKeyboardConfig()
        return self.keyboard

    def apply_effect(
        self,
        effect: int,
        hue: int,
        saturation: int,
        speed: int,
        brightness: int,
        preview_token: int | None = None,
        require_bt_active: bool = False,
        require_bt_inactive: bool = False,
    ) -> bool:
        from RPiKeyboardConfig.keyboard import Preset

        with self.command_lock:
            if not self.lock_lighting_allowed() and brightness > 0:
                return False
            if preview_token is not None and (
                preview_token != self.preview_generation or not self.preview_active
            ):
                return False
            if require_bt_active and not self.bt_keyboard_active:
                return False
            if require_bt_inactive and self.bt_keyboard_active:
                return False
            keyboard = self.get_keyboard()
            keyboard.set_temp_effect(
                preset=Preset(
                    effect=effect,
                    speed=speed,
                    fixed_hue=True,
                    hue=hue,
                    sat=saturation,
                )
            )
            keyboard.set_brightness(brightness)
        return True

    def fade_brightness(self, start: int, end: int) -> bool:
        for brightness in transition_levels(start, end):
            if (
                self.closed
                or not self.lock_lighting_allowed()
            ):
                return False
            with self.command_lock:
                if not self.lock_lighting_allowed():
                    return False
                self.get_keyboard().set_brightness(brightness)
            time.sleep(TRANSITION_STEP_SECONDS)
        return True

    def poll_results(self) -> None:
        try:
            while True:
                kind, value = self.results.get_nowait()
                if kind == "monitor_result":
                    self.monitor_busy = False
                    self.apply_monitor_status(value)
                elif kind == "monitor_error":
                    self.monitor_busy = False
                    self.lcd_status.set("显示器控制失败：" + str(value)[:45])
                    self.set_monitor_buttons("disabled")
                elif kind == "brightness_status":
                    self.brightness_poll_busy = False
                    if value.get("ok") and value.get("brightness") is not None:
                        self.monitor_brightness.set(
                            f"亮度  {int(value['brightness'])}%"
                        )
                elif kind == "brightness_status_error":
                    self.brightness_poll_busy = False
                elif kind == "bt_result":
                    self.bt_busy = False
                    command, result = value
                    try:
                        self.apply_bt_status(result)
                        if command == "clear":
                            removed = result.get("removed_devices", [])
                            message = (
                                "已清除：" + "、".join(removed)
                                if removed else "没有找到已配对设备。"
                            )
                            messagebox.showinfo(
                                "清除完成",
                                message + "\n请同时在 Windows 中删除旧设备，然后重新配对。",
                                parent=self.root,
                            )
                    except Exception as error:
                        self.mark_bt_unavailable(str(error))
                elif kind == "bt_error":
                    self.bt_busy = False
                    self.mark_bt_unavailable(str(value))
                elif kind == "lock_state":
                    self.lock_poll_busy = False
                    self.apply_lock_state(bool(value))
                elif kind == "lock_state_error":
                    self.lock_poll_busy = False
                elif kind == "lock_lighting_error":
                    pass
                elif kind == "keyboard_lighting_error":
                    self.bt_status.set("● 键盘模式灯光设置失败")
                    self.bt_status_label.configure(fg=APPLE_RED)
                elif kind == "preview_done":
                    token = int(value)
                    if token == self.preview_generation and self.preview_active:
                        self.preview_active = False
                        self.current_state = ""
                        if self.processing_preview_message is not None:
                            self.processing_preview_message.set(
                                "预览结束，已恢复当前 Codex 状态"
                            )
                        self.root.after(0, self.refresh)
                elif kind == "preview_error":
                    token, error = value
                    if token == self.preview_generation and self.preview_active:
                        self.preview_active = False
                        self.current_state = ""
                        if self.processing_preview_message is not None:
                            self.processing_preview_message.set(
                                "预览失败：" + str(error)[:45]
                            )
                        self.root.after(0, self.refresh)
                elif kind == "state":
                    self.busy = False
                    _effect, _hue, _sat, _speed, label, colour = self.lighting_for(value)
                    self.status.set(label)
                    self.update_status_dot(value, colour)
                    self.detail.set(self.completion_lighting_detail())
                    self.update_button.pack_forget()
                    self.firmware_actions.pack_forget()
                elif kind == "firmware_button":
                    self.update_button.configure(state=value)
                else:
                    self.busy = False
                    self.status.set("键盘尚未就绪")
                    self.update_status_dot("error", "#ef4444")
                    if "compatible firmware" in value.lower():
                        self.detail.set("首次使用需要升级键盘固件")
                        if not self.firmware_actions.winfo_manager():
                            self.firmware_actions.pack(fill="x", pady=(3, 0))
                        if not self.update_button.winfo_manager():
                            self.update_button.pack(side="left")
                    else:
                        self.detail.set(value.splitlines()[-1][:80])
                self.schedule_refresh()
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(100, self.poll_results)

    def update_firmware(self) -> None:
        if not messagebox.askokcancel(
            "升级键盘固件",
            "将调用树莓派官方固件升级工具，并弹出管理员密码确认。\n"
            "升级过程中请勿关机。完成后键盘会短暂重新连接。",
        ):
            return
        self.update_button.configure(state="disabled")
        self.status.set("正在升级固件…")
        self.detail.set("请完成管理员授权，期间不要关机")
        threading.Thread(target=self.run_firmware_update, daemon=True).start()

    def run_firmware_update(self) -> None:
        try:
            result = subprocess.run(
                ["/usr/bin/pkexec", FIRMWARE_TOOL],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode:
                error = (result.stderr or result.stdout).strip()
                raise RuntimeError(error or "固件升级未完成")
            self.keyboard = None
            self.current_state = ""
            self.results.put(("state", "idle"))
        except Exception as error:
            self.results.put(("error", str(error)))
        finally:
            self.results.put(("firmware_button", "normal"))

    def close(self) -> None:
        self.closed = True
        self.preview_generation += 1
        self.preview_active = False
        if self.tray_process is not None and self.tray_process.poll() is None:
            self.tray_process.terminate()
        self.root.destroy()


def acquire_single_instance() -> object:
    lock_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "codex-rgb-keyboard.lock"
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        try:
            running_pid = int(lock.read().strip())
            os.kill(running_pid, signal.SIGUSR1)
        except (ValueError, ProcessLookupError, PermissionError):
            subprocess.run(
                ["notify-send", "Codex RGB 键盘灯", "控制器已经在运行"],
                check=False,
            )
        raise SystemExit(0)
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def main() -> None:
    lock = acquire_single_instance()
    root = tk.Tk()
    CodexRGBApp(root)
    root.mainloop()
    lock.close()


if __name__ == "__main__":
    main()
