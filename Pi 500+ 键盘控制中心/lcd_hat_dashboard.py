#!/usr/bin/env python3
"""Codex quota dashboard for the Waveshare 1.3inch LCD HAT.

KEY1 (BCM GPIO 21) toggles Bluetooth keyboard mode.
KEY2 (BCM GPIO 20) toggles Clash Verge virtual NIC (TUN) mode.
KEY3 (BCM GPIO 16) confirms a pending Codex request.
Joystick press (BCM GPIO 13) locks the Raspberry Pi and Windows; holding it
for five seconds restarts the display manager.
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image, ImageDraw, ImageFont


WIDTH = 240
HEIGHT = 240
DC_PIN = 25
RESET_PIN = 27
BACKLIGHT_PIN = 24
KEY1_PIN = 21
KEY2_PIN = 20
KEY3_PIN = 16
JOYSTICK_UP_PIN = 6
JOYSTICK_DOWN_PIN = 19
JOYSTICK_LEFT_PIN = 5
JOYSTICK_RIGHT_PIN = 26
JOYSTICK_PRESS_PIN = 13
BT_KEYBOARD_SOCKET = "/run/pi500-bt-keyboard/control.sock"
KEYBOARD_ACTIVITY_FILE = Path("/run/pi500-bt-keyboard/activity")
LOCK_KEYBOARD_WAKE_SECONDS = 10.0
APP_DIR = Path(__file__).resolve().parent
WINDOWS_SESSION_FILE = APP_DIR / "windows_session_state.json"
CLASH_VERGE_DATA_DIR = Path.home() / ".local/share/io.github.clash-verge-rev.clash-verge-rev"
CLASH_VERGE_SETTINGS = CLASH_VERGE_DATA_DIR / "verge.yaml"
CODEX_MONITOR_DIR = APP_DIR
WTYPE_PATH = APP_DIR / "vendor" / "bin" / "wtype"
WLRCTL_PATH = APP_DIR / "vendor" / "bin" / "wlrctl"
CODEX_WINDOW_MATCH = "title:ChatGPT"
FOCUS_SETTLE_SECONDS = 0.18
CODEX_THREAD_SETTLE_SECONDS = 0.8
CONFIRMATION_RESULT_TIMEOUT_SECONDS = 3.0
CONFIRMATION_CHECK_SECONDS = 0.2
REFRESH_SECONDS = 60
QUOTA_STALE_SECONDS = 5 * 60
DISPLAY_RECOVERY_SECONDS = 5
NETWORK_CHECK_SECONDS = 5
NETWORK_FAILURE_THRESHOLD = 2
HTTP_TIMEOUT_SECONDS = 30
TUN_STATE_CHECK_SECONDS = 1
TUN_IP_REFRESH_SECONDS = 60
TUN_NETWORK_CHECK_SECONDS = 5
WEBSITE_CONNECT_TIMEOUT_SECONDS = 4
WEBSITE_CHECK_TIMEOUT_SECONDS = 7
PUBLIC_IP_TIMEOUT_SECONDS = 5
BRIGHTNESS_REPEAT_DELAY = 0.20
BRIGHTNESS_REPEAT_INTERVAL = 0.08
KEY1_COOLDOWN_SECONDS = 0.6
LOCK_COOLDOWN_SECONDS = 0.6
TUN_TOGGLE_COOLDOWN_SECONDS = 0.6
KEY3_COOLDOWN_SECONDS = 0.6
KEY_APP_HOLD_SECONDS = 2.0
JOYSTICK_RESTART_HOLD_SECONDS = 5.0
JOYSTICK_STARTUP_RELEASE_SECONDS = 0.5
LCD_BUILD_ID = "2026-09-23-web-connectivity-v2"
LCD_RUNTIME_STATUS_FILE = Path("/tmp/codex-lcd-hat-status.json")

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_MONO if mono else (FONT_BOLD if bold else FONT_REGULAR)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def executable_path(*candidates: str) -> str:
    """Return the first installed executable, retaining a useful fallback."""
    for candidate in candidates:
        if "/" in candidate:
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return candidates[0]


def write_runtime_status(status: dict) -> None:
    """Publish enough live state to diagnose the service without its D-Bus."""
    try:
        temporary = LCD_RUNTIME_STATUS_FILE.with_name(
            LCD_RUNTIME_STATUS_FILE.name + f".{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(status, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, LCD_RUNTIME_STATUS_FILE)
    except OSError:
        pass


F_TITLE = font(22, bold=True)
F_PERCENT = font(31, bold=True)
F_LABEL = font(14, bold=True)
F_SMALL = font(12)
F_BUTTON = font(13, bold=True)
F_ACTION = font(27, bold=True)
F_ACTION_DETAIL = font(17, bold=True)
F_META = font(10)
F_TIME = font(15, bold=True)
F_CARD_LABEL = font(17, bold=True)
F_RESET = font(11)


def cjk_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
        if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    )
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return font(size, bold=bold)


F_CONFIRM_TITLE = cjk_font(22, bold=True)
F_CONFIRM_BODY = cjk_font(15)
F_CONFIRM_FOOTER = cjk_font(13, bold=True)


def get_json(url: str) -> dict:
    with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def post_json(url: str) -> dict:
    request = Request(url, data=b"", method="POST")
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def bt_keyboard_request(command: str) -> dict:
    """Send a command to the Pi 500+ BLE keyboard daemon."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(4)
        client.connect(BT_KEYBOARD_SOCKET)
        client.sendall(json.dumps({"command": command}).encode("utf-8"))
        payload = b""
        while b"\n" not in payload:
            chunk = client.recv(4096)
            if not chunk:
                break
            payload += chunk
    return json.loads(payload.decode("utf-8"))


def keyboard_activity_token() -> int:
    try:
        return int(KEYBOARD_ACTIVITY_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def _gdbus_call(destination: str, object_path: str, method: str, *args: str) -> str:
    """Call a session-bus method from the LCD user service."""
    environment = os.environ.copy()
    environment.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    result = subprocess.run(
        [
            "/usr/bin/gdbus", "call", "--session",
            "--dest", destination,
            "--object-path", object_path,
            "--method", method,
            *args,
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Clash Verge DBus call failed")
    return result.stdout.strip()


def _clash_verge_tun_setting() -> bool:
    try:
        text = CLASH_VERGE_SETTINGS.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("Clash Verge settings unavailable") from exc
    match = re.search(r"(?m)^enable_tun_mode:\s*(true|false)\s*$", text)
    if not match:
        raise RuntimeError("Clash Verge TUN setting not found")
    return match.group(1) == "true"


def public_egress_ipv4() -> str:
    """Return the public IPv4 currently used for outbound traffic."""
    endpoints = (
        ("https://api.ipify.org?format=json", True),
        ("https://v4.ident.me/", False),
        ("https://ipv4.icanhazip.com/", False),
    )
    last_error: Exception | None = None
    for url, is_json in endpoints:
        try:
            request = Request(url, headers={"User-Agent": "pi500-lcd/1.0"})
            with urlopen(request, timeout=PUBLIC_IP_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", errors="replace").strip()
            candidate = str(json.loads(body).get("ip", "")) if is_json else body
            address = ipaddress.ip_address(candidate.strip())
            if address.version == 4 and not address.is_unspecified:
                return str(address)
        except Exception as exc:
            last_error = exc
    raise RuntimeError("Public IPv4 unavailable") from last_error


def clash_verge_tun_display() -> tuple[str, bool]:
    """Return the public egress IPv4 and Clash Verge TUN state."""
    try:
        enabled = _clash_verge_tun_setting()
    except Exception:
        return "--", False
    if not enabled:
        return "--", False
    try:
        return public_egress_ipv4(), True
    except Exception:
        return "--", True


def _clash_verge_tun_menu() -> tuple[str, str, int]:
    """Find Clash Verge's native TUN item on its exported DBusMenu."""
    watcher = _gdbus_call(
        "org.kde.StatusNotifierWatcher",
        "/StatusNotifierWatcher",
        "org.freedesktop.DBus.Properties.Get",
        "org.kde.StatusNotifierWatcher",
        "RegisteredStatusNotifierItems",
    )
    for item in re.findall(r"'([^']+)'", watcher):
        if "/" not in item:
            continue
        destination, relative_path = item.split("/", 1)
        item_path = f"/{relative_path}"
        try:
            title = _gdbus_call(
                destination,
                item_path,
                "org.freedesktop.DBus.Properties.Get",
                "org.kde.StatusNotifierItem",
                "Title",
            )
            if "clash-verge" not in title.lower():
                continue
            menu_value = _gdbus_call(
                destination,
                item_path,
                "org.freedesktop.DBus.Properties.Get",
                "org.kde.StatusNotifierItem",
                "Menu",
            )
            menu_match = re.search(r"objectpath '([^']+)'", menu_value)
            if not menu_match:
                continue
            menu_path = menu_match.group(1)
            layout = _gdbus_call(
                destination,
                menu_path,
                "com.canonical.dbusmenu.GetLayout",
                "--", "0", "-1", "[]",
            )
        except Exception:
            continue
        for menu_item in re.finditer(r"\((\d+), \{([^{}]*)\}", layout):
            properties = menu_item.group(2)
            label_match = re.search(r"'label': <'([^']*)'>", properties)
            if not label_match:
                continue
            label = label_match.group(1).strip().upper()
            if label.startswith("TUN") or "虚拟网卡" in label:
                return destination, menu_path, int(menu_item.group(1))
    raise RuntimeError("Clash Verge native TUN menu not found")


def toggle_clash_verge_tun() -> bool:
    """Activate Clash Verge's own TUN menu so UI and runtime stay in sync."""
    current = _clash_verge_tun_setting()
    destination, menu_path, menu_id = _clash_verge_tun_menu()
    _gdbus_call(
        destination,
        menu_path,
        "com.canonical.dbusmenu.Event",
        str(menu_id),
        "clicked",
        "<int32 0>",
        str(int(time.time() * 1000) & 0xFFFFFFFF),
    )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        time.sleep(0.1)
        updated = _clash_verge_tun_setting()
        if updated != current:
            return updated
    raise RuntimeError("Clash Verge did not change TUN state")


def lock_raspberry_pi() -> None:
    """Run the same lock screen command as labwc's Ctrl+Alt+L binding."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment = os.environ.copy()
    environment["XDG_RUNTIME_DIR"] = runtime_dir
    if not environment.get("WAYLAND_DISPLAY"):
        sockets = sorted(
            path.name for path in Path(runtime_dir).glob("wayland-*")
            if not path.name.endswith(".lock")
        )
        if not sockets:
            raise RuntimeError("Wayland display not found")
        environment["WAYLAND_DISPLAY"] = sockets[0]
    process = subprocess.Popen(
        ["/usr/bin/swaylock", "-p"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Detect immediate startup errors while leaving the locker independent of
    # the LCD event loop until the user unlocks the desktop.
    time.sleep(0.15)
    if process.poll() not in (None, 0):
        raise RuntimeError("system lock screen failed to start")


def restart_display_manager() -> None:
    """Restart the login/display service through one narrowly allowed sudo rule."""
    process = subprocess.Popen(
        [
            "/usr/bin/sudo", "-n",
            "/usr/bin/systemctl", "restart", "display-manager",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Catch a missing sudo rule or malformed command without waiting for the
    # display-manager restart, which may terminate this graphical session.
    time.sleep(0.15)
    if process.poll() not in (None, 0):
        raise RuntimeError("display manager restart was rejected")


def interface_ipv4(interface: str) -> str | None:
    """Read an interface's IPv4 address without consulting the route table."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            request = struct.pack("256s", interface[:15].encode("utf-8"))
            result = fcntl.ioctl(sock.fileno(), 0x8915, request)  # SIOCGIFADDR
        address = socket.inet_ntoa(result[20:24])
        if not address.startswith(("127.", "169.254.")):
            return address
    except (OSError, struct.error):
        pass
    return None


def local_ipv4() -> str:
    """Return only an active physical LAN address, never a virtual IP."""
    preferred = ["wlan0", "eth0", "end0"]
    try:
        physical = sorted(
            path.name
            for path in Path("/sys/class/net").iterdir()
            if (path / "device").exists()
        )
    except OSError:
        physical = []
    interfaces = preferred + [name for name in physical if name not in preferred]
    for interface in interfaces:
        try:
            state = Path(f"/sys/class/net/{interface}/operstate").read_text().strip()
        except OSError:
            continue
        if state != "up":
            continue
        address = interface_ipv4(interface)
        if address:
            return address
    return "--"


def windows_session_locked() -> bool:
    try:
        data = json.loads(WINDOWS_SESSION_FILE.read_text(encoding="utf-8"))
        return bool(data.get("locked", False))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def set_windows_session_locked(locked: bool) -> None:
    payload = {"locked": bool(locked), "updated_at": int(time.time())}
    temporary = WINDOWS_SESSION_FILE.with_name(
        WINDOWS_SESSION_FILE.name + f".{os.getpid()}.tmp"
    )
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, WINDOWS_SESSION_FILE)


def raspberry_pi_locked() -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-x", "swaylock"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def codex_monitor_module():
    """Load the shared monitor with support for current desktop event shapes."""
    if str(CODEX_MONITOR_DIR) not in sys.path:
        sys.path.insert(0, str(CODEX_MONITOR_DIR))
    import codex_usage_monitor as monitor

    if not getattr(monitor, "_pi500_confirmation_extended", False):
        original_check = monitor.call_needs_confirmation

        def extended_check(payload: dict) -> bool:
            name = str(payload.get("name", ""))
            if name in {"request_permissions", "request_user_input"}:
                return True
            tool_input = payload.get("input", payload.get("arguments", ""))
            if not isinstance(tool_input, str):
                return original_check(payload)
            call = re.search(
                r"\bawait\s+tools\."
                r"(request_permissions|request_user_input|exec_command)\s*\(",
                tool_input,
            )
            if not call:
                return original_check(payload)
            if call.group(1) in {"request_permissions", "request_user_input"}:
                return True
            return bool(re.search(
                r"\bsandbox_permissions\s*:\s*[\"']require_escalated[\"']",
                tool_input,
            ))

        monitor.call_needs_confirmation = extended_check
        monitor._pi500_confirmation_extended = True

    return monitor


CONFIRMATION_SESSION_LIMIT = 12
CONFIRMATION_SESSION_MAX_AGE_SECONDS = 6 * 60 * 60
_confirmation_sessions: dict[Path, dict] = {}


def recent_codex_sessions(codex_home: Path) -> list[Path]:
    """Return every recently active task, not only the newest task."""
    sessions_dir = codex_home / "sessions"
    try:
        candidates = sorted(
            sessions_dir.glob("**/rollout-*.jsonl"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:CONFIRMATION_SESSION_LIMIT]
    except OSError:
        return []
    if not candidates:
        return []
    cutoff = time.time() - CONFIRMATION_SESSION_MAX_AGE_SECONDS
    recent = []
    for path in candidates:
        try:
            if path.stat().st_mtime >= cutoff:
                recent.append(path)
        except OSError:
            continue
    return recent or candidates[:1]


def pending_codex_confirmation() -> dict | None:
    """Incrementally return the newest unresolved confirmation payload."""
    monitor = codex_monitor_module()
    sessions = recent_codex_sessions(monitor.DEFAULT_CODEX_HOME)
    if not sessions:
        return None

    active_paths = set(sessions)
    for stale_path in set(_confirmation_sessions) - active_paths:
        _confirmation_sessions.pop(stale_path, None)

    for session in sessions:
        try:
            stat = session.stat()
        except OSError:
            continue
        state = _confirmation_sessions.setdefault(
            session,
            {"offset": 0, "calls": {}},
        )
        if stat.st_size < int(state["offset"]):
            state["offset"] = 0
            state["calls"] = {}
        try:
            with session.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(int(state["offset"]))
                while True:
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        handle.seek(line_start)
                        break
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    if event.get("type") == "event_msg":
                        if payload.get("type") in {
                            "task_started", "turn_started",
                            "task_complete", "turn_complete",
                        }:
                            state["calls"].clear()
                        continue
                    if event.get("type") != "response_item":
                        continue
                    call_id = str(payload.get("call_id", ""))
                    if payload.get("type") in {
                        "function_call", "custom_tool_call"
                    }:
                        if call_id and monitor.call_needs_confirmation(payload):
                            order = (
                                str(event.get("timestamp", "")),
                                stat.st_mtime_ns,
                                handle.tell(),
                            )
                            contextual_payload = dict(payload)
                            contextual_payload["_codex_session_id"] = (
                                monitor.session_id_from_path(session)
                            )
                            contextual_payload["_codex_session_path"] = str(session)
                            state["calls"][call_id] = (order, contextual_payload)
                    elif payload.get("type") in {
                        "function_call_output", "custom_tool_call_output"
                    }:
                        state["calls"].pop(call_id, None)
                state["offset"] = handle.tell()
        except OSError:
            continue

    pending = [
        item
        for state in _confirmation_sessions.values()
        for item in state["calls"].values()
    ]
    return max(pending, key=lambda item: item[0])[1] if pending else None


def codex_confirmation_pending() -> bool:
    return pending_codex_confirmation() is not None


def quoted_field(text: str, field: str) -> str:
    """Extract a JSON/JavaScript-style quoted user-facing field."""
    match = re.search(
        rf"\b{re.escape(field)}\s*:\s*(\"(?:\\.|[^\"\\])*\")",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return ""
    try:
        return str(json.loads(match.group(1)))
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""


def confirmation_text(payload: dict) -> tuple[str, str]:
    """Create a compact Chinese title and user-facing request description."""
    name = str(payload.get("name", ""))
    raw = payload.get("arguments", payload.get("input", ""))
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False)

    parsed: dict = {}
    try:
        candidate = json.loads(raw)
        if isinstance(candidate, dict):
            parsed = candidate
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    is_question = name == "request_user_input" or "request_user_input" in raw
    if is_question:
        detail = ""
        questions = parsed.get("questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            if isinstance(first, dict):
                detail = str(first.get("question", ""))
        detail = detail or quoted_field(raw, "question")
        return "Codex 需要选择", detail or "请查看 Codex 中的选项"

    detail = str(parsed.get("reason") or parsed.get("justification") or "")
    detail = (
        detail
        or quoted_field(raw, "reason")
        or quoted_field(raw, "justification")
    )
    if not detail:
        if "network" in raw:
            detail = "请求访问网络"
        elif "file_system" in raw:
            detail = "请求访问受保护的文件"
        else:
            detail = "请求执行需要你授权的操作"
    return "Codex 需要确认", detail


def desktop_environment() -> dict[str, str]:
    """Build the Wayland environment used by the bundled desktop helpers."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment = os.environ.copy()
    environment["XDG_RUNTIME_DIR"] = runtime_dir
    if not environment.get("WAYLAND_DISPLAY"):
        sockets = sorted(
            path.name for path in Path(runtime_dir).glob("wayland-*")
            if not path.name.endswith(".lock")
        )
        if not sockets:
            raise RuntimeError("Wayland display not found")
        environment["WAYLAND_DISPLAY"] = sockets[0]
    return environment


def run_desktop_command(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        env=desktop_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=3,
        check=False,
    )


def focus_or_launch_window(match: str, command: list[str]) -> str:
    """Focus an existing Wayland window, otherwise launch its single instance."""
    if not WLRCTL_PATH.is_file():
        raise RuntimeError("Wayland window helper is not installed")
    focused = run_desktop_command(
        [str(WLRCTL_PATH), "toplevel", "focus", match]
    )
    if focused.returncode == 0:
        return "Focused"
    subprocess.Popen(
        command,
        env=desktop_environment(),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return "Opened"


def confirm_codex_request(payload: dict) -> None:
    """Open the exact pending task, confirm it, then restore the prior app."""
    if not WTYPE_PATH.is_file() or not WLRCTL_PATH.is_file():
        raise RuntimeError("Wayland helpers are not installed")

    was_focused = run_desktop_command(
        [str(WLRCTL_PATH), "toplevel", "find", CODEX_WINDOW_MATCH, "state:active"]
    ).returncode == 0
    session_id = str(payload.get("_codex_session_id", ""))
    if session_id:
        opened = run_desktop_command(
            [
                executable_path("chatgpt", "/usr/bin/chatgpt"),
                f"codex://threads/{session_id}",
            ]
        )
        if opened.returncode != 0:
            raise RuntimeError("could not open the pending Codex task")
        time.sleep(CODEX_THREAD_SETTLE_SECONDS)

    codex_is_active = run_desktop_command(
        [str(WLRCTL_PATH), "toplevel", "find", CODEX_WINDOW_MATCH, "state:active"]
    ).returncode == 0
    if not codex_is_active:
        focused = run_desktop_command(
            [str(WLRCTL_PATH), "toplevel", "focus", CODEX_WINDOW_MATCH]
        )
        if focused.returncode != 0:
            raise RuntimeError("Codex window not found")
        time.sleep(FOCUS_SETTLE_SECONDS)

    result = run_desktop_command([str(WTYPE_PATH), "-k", "Return"])
    if result.returncode != 0:
        raise RuntimeError("could not send Codex confirmation key")

    call_id = str(payload.get("call_id", ""))
    deadline = time.monotonic() + CONFIRMATION_RESULT_TIMEOUT_SECONDS
    confirmed = False
    while time.monotonic() < deadline:
        pending = pending_codex_confirmation()
        if pending is None or str(pending.get("call_id", "")) != call_id:
            confirmed = True
            break
        time.sleep(0.1)

    if not was_focused:
        time.sleep(FOCUS_SETTLE_SECONDS)
        run_desktop_command(
            [str(WTYPE_PATH), "-M", "alt", "-k", "Tab", "-m", "alt"]
        )
    if not confirmed:
        raise RuntimeError("Codex prompt did not accept the confirmation")


def website_reachable(url: str) -> bool:
    """Check DNS, TLS and HTTP reachability without downloading the page."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--ipv4",
                "--head",
                "--location",
                "--silent",
                "--show-error",
                "--output", "/dev/null",
                "--connect-timeout", str(WEBSITE_CONNECT_TIMEOUT_SECONDS),
                "--max-time", str(WEBSITE_CHECK_TIMEOUT_SECONDS),
                "--user-agent", "Mozilla/5.0",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=WEBSITE_CHECK_TIMEOUT_SECONDS + 1,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def internet_reachable() -> bool:
    """The green LAN indicator requires an HTTP response from baidu.com."""
    return website_reachable("https://baidu.com")


def tun_internet_reachable() -> bool:
    """The purple TUN indicator requires both Google and ChatGPT responses."""
    urls = ("https://google.com.hk", "https://chatgpt.com")
    with ThreadPoolExecutor(max_workers=len(urls)) as executor:
        return all(executor.map(website_reachable, urls))


def reset_text(timestamp: int | None) -> str:
    if not timestamp:
        return "Reset --"
    now = datetime.now().astimezone()
    target = datetime.fromtimestamp(timestamp).astimezone()
    remaining = int((target - now).total_seconds())
    if remaining <= 0:
        return "Reset updating"
    if remaining < 3600:
        return f"Reset in {max(1, remaining // 60)}m"
    if target.date() == now.date():
        return "Reset " + target.strftime("%H:%M")
    return "Reset " + target.strftime("%m-%d %H:%M")


def quota_windows(data: dict) -> tuple[dict | None, dict | None]:
    short = None
    long = None
    for window in (data.get("primary"), data.get("secondary")):
        if not window:
            continue
        minutes = window.get("window_minutes")
        if minutes and minutes < 1440:
            short = short or window
        elif minutes and minutes >= 1440:
            long = long or window
        elif short is None:
            short = window
        else:
            long = long or window
    return short, long


def quota_is_stale(data: dict, now: float | None = None) -> bool:
    """Return true when the last successful quota update is over five minutes old."""
    if not data.get("ok"):
        return False
    try:
        fetched_at = float(data.get("fetched_at", 0))
    except (TypeError, ValueError):
        return False
    if fetched_at <= 0:
        return False
    return (time.time() if now is None else now) - fetched_at > QUOTA_STALE_SECONDS


def draw_rounded_gradient(
    image: Image.Image,
    box: tuple[int, int, int, int],
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    radius: int,
) -> None:
    left, top, right, bottom = box
    width = right - left + 1
    height = bottom - top + 1
    gradient = Image.new("RGB", (width, height))
    gradient_draw = ImageDraw.Draw(gradient)
    for x in range(width):
        amount = x / max(1, width - 1)
        colour = tuple(round(a + (b - a) * amount) for a, b in zip(start, end))
        gradient_draw.line((x, 0, x, height), fill=colour)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    image.paste(gradient, (left, top), mask)


def draw_card_icon(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    accent: tuple[int, int, int],
    kind: str,
) -> None:
    left, top, right, bottom = box
    tint = tuple(round(7 + channel * 0.17) for channel in accent)
    draw.rounded_rectangle(box, radius=10, fill=tint)
    if kind == "clock":
        draw.ellipse((left + 7, top + 7, right - 7, bottom - 7), outline=accent, width=3)
        centre_x = (left + right) // 2
        centre_y = (top + bottom) // 2
        draw.line((centre_x, centre_y, centre_x, top + 11), fill=accent, width=3)
        draw.line((centre_x, centre_y, right - 11, centre_y), fill=accent, width=3)
    else:
        draw.rounded_rectangle(
            (left + 8, top + 9, right - 8, bottom - 7),
            radius=3,
            outline=accent,
            width=2,
        )
        draw.line((left + 8, top + 15, right - 8, top + 15), fill=accent, width=2)
        draw.line((left + 13, top + 6, left + 13, top + 12), fill=accent, width=3)
        draw.line((right - 13, top + 6, right - 13, top + 12), fill=accent, width=3)


def draw_quota_card(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    window: dict | None,
    accent: tuple[int, int, int],
    card_fill: tuple[int, int, int],
    border: tuple[int, int, int],
    gradient_start: tuple[int, int, int],
    gradient_end: tuple[int, int, int],
    icon: str,
    *,
    stale: bool = False,
) -> None:
    left, top, right, bottom = box
    compact = bottom - top + 1 < 90
    draw.rounded_rectangle(box, radius=15, fill=card_fill, outline=border, width=1)
    remaining = int((window or {}).get("remaining_percent", 0))
    icon_top = top + (9 if compact else 12)
    draw_card_icon(draw, (left + 11, icon_top, left + 46, icon_top + 35), accent, icon)
    draw.text(
        (left + 54, top + (15 if compact else 18)),
        label,
        font=F_CARD_LABEL,
        fill=(166, 171, 181) if stale else (246, 248, 252),
    )
    percent = f"{remaining}%" if window else "--"
    percent_box = draw.textbbox((0, 0), percent, font=F_PERCENT)
    draw.text((right - 10 - (percent_box[2] - percent_box[0]), top + (2 if compact else 5)), percent,
              font=F_PERCENT, fill=(166, 171, 181) if stale else (250, 251, 253))
    bar_top = top + (50 if compact else 57)
    bar = (left + 11, bar_top, right - 11, bar_top + 9)
    draw.rounded_rectangle(bar, radius=5, fill=border)
    if remaining:
        fill_right = bar[0] + max(8, int((bar[2] - bar[0]) * remaining / 100))
        draw_rounded_gradient(
            image,
            (bar[0], bar[1], fill_right, bar[3]),
            gradient_start,
            gradient_end,
            5,
        )
    draw.text((left + 11, bottom - (18 if compact else 20)), reset_text((window or {}).get("resets_at")),
              font=F_RESET, fill=(112, 118, 129) if stale else (135, 146, 162))


def render(
    data: dict,
    network_online: bool = False,
    *,
    local_ip: str = "--",
    tun_ip: str = "--",
    tun_enabled: bool = False,
    tun_online: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    title = "Codex Usage"
    title_box = draw.textbbox((0, 0), title, font=F_TITLE)
    draw.text(((WIDTH - (title_box[2] - title_box[0])) // 2, 2), title,
              font=F_TITLE, fill=(250, 250, 252))
    updated_at = data.get("fetched_at")
    refreshed = (
        datetime.fromtimestamp(updated_at).astimezone().strftime("%H:%M:%S")
        if updated_at else "--:--:--"
    )
    meta_text_colour = (250, 250, 252)
    status_colour = (
        (52, 224, 91)
        if network_online and local_ip != "--"
        else (255, 69, 58)
    )
    draw.ellipse((10, 32, 18, 40), fill=status_colour)
    draw.text((23, 30), "IP: " + local_ip, font=F_META, fill=meta_text_colour)
    updated = "Updated " + refreshed
    updated_box = draw.textbbox((0, 0), updated, font=F_META)
    updated_x = 231 - (updated_box[2] - updated_box[0])
    draw.text((updated_x - 10, 30), "|", font=F_META, fill=(74, 104, 139))
    draw.text((updated_x, 30), updated, font=F_META, fill=meta_text_colour)

    tun_accent = (174, 112, 255) if tun_online else (255, 69, 58)
    draw.ellipse((10, 46, 18, 54), fill=tun_accent)
    draw.text(
        (23, 44),
        "TUN IP: " + tun_ip,
        font=F_META,
        fill=meta_text_colour,
    )
    tun_status = "TUN: " + ("ON" if tun_enabled else "OFF")
    tun_box = draw.textbbox((0, 0), tun_status, font=F_META)
    tun_x = 231 - (tun_box[2] - tun_box[0])
    draw.text((tun_x - 10, 44), "|", font=F_META, fill=(74, 104, 139))
    draw.text((tun_x, 44), tun_status, font=F_META, fill=meta_text_colour)

    if data.get("ok"):
        short, long = quota_windows(data)
        stale = quota_is_stale(data)
        blue_palette = (
            (108, 114, 124), (18, 20, 24), (62, 67, 76),
            (126, 132, 142), (82, 88, 98),
        ) if stale else (
            (70, 198, 255), (10, 15, 21), (39, 49, 63),
            (54, 196, 255), (32, 128, 244),
        )
        purple_palette = (
            (108, 114, 124), (18, 20, 24), (62, 67, 76),
            (126, 132, 142), (82, 88, 98),
        ) if stale else (
            (174, 112, 255), (10, 15, 21), (39, 49, 63),
            (176, 93, 255), (124, 77, 246),
        )
        draw_quota_card(
            image, draw, (7, 61, 233, 146), "5 Hour", short,
            *blue_palette, "clock", stale=stale,
        )
        draw_quota_card(
            image, draw, (7, 152, 233, 236), "7 Day", long,
            *purple_palette, "calendar", stale=stale,
        )
    else:
        draw.rounded_rectangle((14, 61, 225, 237), radius=15, fill=(17, 12, 15),
                               outline=(105, 45, 45))
        draw.text((30, 103), "Quota unavailable", font=F_LABEL, fill=(255, 99, 91))
        message = str(data.get("error", "service unavailable"))[:27]
        draw.text((30, 131), message, font=F_SMALL, fill=(180, 137, 137))
    # The HAT is mounted upside down; rotate the complete UI by the opposite
    # landscape orientation so it remains readable.
    return image.rotate(90)


def render_action(title: str, detail: str = "", *, failed: bool = False) -> Image.Image:
    """Render transient button feedback as a full-screen page."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    accent = (255, 59, 48) if failed else (31, 111, 235)
    pale = (55, 19, 20) if failed else (9, 39, 70)
    draw.rounded_rectangle((13, 12, 227, 226), radius=25, fill=(10, 15, 21),
                           outline=accent, width=2)
    draw.rounded_rectangle((96, 43, 144, 91), radius=14, fill=pale)
    draw.ellipse((108, 55, 132, 79), fill=accent)

    title_box = draw.textbbox((0, 0), title, font=F_ACTION)
    draw.text(((WIDTH - (title_box[2] - title_box[0])) // 2, 98), title,
              font=F_ACTION, fill=(250, 251, 253))
    if detail:
        detail_box = draw.textbbox((0, 0), detail, font=F_ACTION_DETAIL)
        draw.text(((WIDTH - (detail_box[2] - detail_box[0])) // 2, 143), detail,
                  font=F_ACTION_DETAIL, fill=(145, 156, 173))
    return image.rotate(90)


def wrap_by_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    text_font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Wrap mixed Chinese/Latin text by rendered pixel width."""
    clean = " ".join(str(text).split())
    lines: list[str] = []
    current = ""
    for character in clean:
        candidate = current + character
        bounds = draw.textbbox((0, 0), candidate, font=text_font)
        if current and bounds[2] - bounds[0] > max_width:
            lines.append(current.rstrip())
            current = character.lstrip()
            if len(lines) == max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current.rstrip())
    truncated = len("".join(lines).replace(" ", "")) < len(clean.replace(" ", ""))
    if truncated and lines:
        while lines[-1]:
            candidate = lines[-1].rstrip() + "…"
            bounds = draw.textbbox((0, 0), candidate, font=text_font)
            if bounds[2] - bounds[0] <= max_width:
                lines[-1] = candidate
                break
            lines[-1] = lines[-1][:-1]
    return lines


def render_confirmation(payload: dict) -> Image.Image:
    """Render the pending Codex request as a dedicated full-screen page."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    accent = (255, 149, 0)
    title, detail = confirmation_text(payload)

    draw.rounded_rectangle(
        (8, 8, 232, 232), radius=22, fill=(14, 16, 21),
        outline=(92, 66, 30), width=2,
    )
    draw.ellipse((20, 22, 32, 34), fill=accent)
    draw.text((40, 16), title, font=F_CONFIRM_TITLE, fill=(250, 250, 252))
    draw.line((20, 53, 220, 53), fill=(57, 61, 70), width=1)

    lines = wrap_by_width(draw, detail, F_CONFIRM_BODY, 194, 5)
    y = 69
    for line in lines:
        draw.text((23, y), line, font=F_CONFIRM_BODY, fill=(220, 224, 232))
        y += 23

    draw.rounded_rectangle((20, 196, 220, 220), radius=12, fill=(78, 48, 8))
    footer = "按 KEY3 确认"
    footer_box = draw.textbbox((0, 0), footer, font=F_CONFIRM_FOOTER)
    draw.text(
        ((WIDTH - (footer_box[2] - footer_box[0])) // 2, 198), footer,
        font=F_CONFIRM_FOOTER, fill=(255, 190, 76),
    )
    return image.rotate(90)


class ST7789:
    """Small userspace driver for the HAT's ST7789 over SPI0 CE0."""

    def __init__(self) -> None:
        import spidev
        from gpiozero import OutputDevice

        self.dc = OutputDevice(DC_PIN, active_high=True, initial_value=False)
        self.reset = OutputDevice(RESET_PIN, active_high=True, initial_value=True)
        self.backlight = OutputDevice(BACKLIGHT_PIN, active_high=True, initial_value=False)
        self.standby = False
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = 40_000_000
        self.spi.mode = 0
        self._initialize()

    def _command(self, command: int, data: bytes = b"") -> None:
        self.dc.off()
        self.spi.xfer2([command])
        if data:
            self.dc.on()
            self.spi.writebytes2(data)

    def _initialize(self) -> None:
        self.reset.on()
        time.sleep(0.12)
        self.reset.off()
        time.sleep(0.12)
        self.reset.on()
        time.sleep(0.12)
        # A cold ST7789 powers up with undefined display RAM. Keep both the
        # panel output and backlight off until a complete black frame and then
        # the first dashboard frame have been transferred.
        self._wake_controller(show_backlight=False)
        self._write_payload(bytes(WIDTH * HEIGHT * 2))

    def _wake_controller(self, *, show_backlight: bool = True) -> None:
        """Fully restore ST7789 state after the HAT loses power."""
        self._command(0x11)  # Sleep out
        time.sleep(0.12)
        self._command(0x36, b"\x00")  # Native orientation, without horizontal mirroring
        self._command(0x3A, b"\x05")  # RGB565
        self._command(0xB2, b"\x0c\x0c\x00\x33\x33")  # Porch control
        self._command(0xB7, b"\x35")  # Gate control
        self._command(0xBB, b"\x19")  # VCOM setting
        self._command(0xC0, b"\x2c")  # LCM control
        self._command(0xC2, b"\x01")  # VDV/VRH enable
        self._command(0xC3, b"\x12")  # VRH setting
        self._command(0xC4, b"\x20")  # VDV setting
        self._command(0xC6, b"\x0f")  # Frame-rate control
        self._command(0xD0, b"\xa4\xa1")  # Power control
        self._command(
            0xE0,
            b"\xd0\x04\x0d\x11\x13\x2b\x3f\x54\x4c\x18\x0d\x0b\x1f\x23",
        )
        self._command(
            0xE1,
            b"\xd0\x04\x0c\x11\x13\x2c\x3f\x44\x51\x2f\x1f\x1f\x20\x23",
        )
        self._command(0x21)  # Display inversion on
        self._command(0x13)  # Normal display mode
        if self.standby or not show_backlight:
            self._command(0x28)  # Keep random/partial display RAM hidden.
            self.backlight.off()
        else:
            self._command(0x29)
            self.backlight.on()

    def _write_payload(self, payload: bytes) -> None:
        self._command(0x2A, b"\x00\x00\x00\xef")
        self._command(0x2B, b"\x00\x00\x00\xef")
        self._command(0x2C)
        self.dc.on()
        self.spi.writebytes2(payload)

    def set_standby(self, standby: bool) -> None:
        standby = bool(standby)
        if standby == self.standby:
            return
        self.standby = standby
        if standby:
            self.backlight.off()
            self._command(0x28)  # Display off
        else:
            self._command(0x29)  # Display on
            self.backlight.on()

    def show(self, image: Image.Image, *, recover: bool = False) -> None:
        # SPI has no reliable presence/readback signal on this HAT. Only the
        # periodic recovery frame replays the slower wake sequence; ordinary
        # button feedback stays immediate.
        if recover:
            self._wake_controller()
        pixels = np.asarray(image.convert("RGB"), dtype=np.uint16)
        rgb565 = ((pixels[:, :, 0] & 0xF8) << 8) | ((pixels[:, :, 1] & 0xFC) << 3) | (pixels[:, :, 2] >> 3)
        payload = rgb565.astype(">u2", copy=False).tobytes()
        self._write_payload(payload)
        if not self.standby and not self.backlight.value:
            self._command(0x29)  # Reveal only the completed first frame.
            self.backlight.on()

    def close(self) -> None:
        self.backlight.off()
        self.spi.close()
        self.dc.close()
        self.reset.close()
        self.backlight.close()


def run(
    quota_url: str,
    displayport_url: str,
    hdmi_url: str,
    brightness_up_url: str,
    brightness_down_url: str,
) -> None:
    from gpiozero import Button

    display = ST7789()
    # The HAT is mounted upside down: physical KEY1/2/3 map to the former
    # KEY3/2/1 functions. A smaller hardware debounce
    # catches quick taps; the explicit cooldown below prevents double toggles.
    key1 = Button(KEY1_PIN, pull_up=True, bounce_time=0.05)
    key2 = Button(KEY2_PIN, pull_up=True, bounce_time=0.05)
    key3 = Button(KEY3_PIN, pull_up=True, bounce_time=0.05)
    joystick_up = Button(JOYSTICK_UP_PIN, pull_up=True, bounce_time=0.05)
    joystick_down = Button(JOYSTICK_DOWN_PIN, pull_up=True, bounce_time=0.05)
    joystick_left = Button(JOYSTICK_LEFT_PIN, pull_up=True, bounce_time=0.05)
    joystick_right = Button(JOYSTICK_RIGHT_PIN, pull_up=True, bounce_time=0.05)
    joystick_press = Button(JOYSTICK_PRESS_PIN, pull_up=True, bounce_time=0.05)
    confirm_pressed = threading.Event()
    lock_pressed = threading.Event()
    tun_toggle_pressed = threading.Event()
    keyboard_toggle_pressed = threading.Event()
    displayport_pressed = threading.Event()
    hdmi_pressed = threading.Event()
    stopping = threading.Event()
    # With the HAT rotated 180 degrees, the user's left direction is the
    # board's original RIGHT switch, and the user's right is original LEFT.
    joystick_right.when_pressed = displayport_pressed.set
    joystick_left.when_pressed = hdmi_pressed.set

    def stop(_signum, _frame) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    data: dict = {"ok": False, "error": "Starting quota service"}
    next_fetch = 0.0
    next_network_check = 0.0
    next_tun_state_check = 0.0
    next_tun_ip_check = 0.0
    next_tun_network_check = 0.0
    next_lock_state_check = 0.0
    next_confirmation_check = 0.0
    next_display_recovery = time.monotonic() + DISPLAY_RECOVERY_SECONDS
    network_state = {
        "online": False,
        "checking": False,
        "lan_ip": "--",
        "consecutive_failures": 0,
    }
    quota_state = {"fetching": False}
    quota_results: queue.SimpleQueue[dict] = queue.SimpleQueue()
    try:
        initial_tun_enabled = _clash_verge_tun_setting()
    except Exception:
        initial_tun_enabled = False
    tun_state = {
        "enabled": initial_tun_enabled,
        "ip": "--",
        "checking": False,
        "online": False,
        "network_checking": False,
    }
    tun_results: queue.SimpleQueue[tuple[bool, str]] = queue.SimpleQueue()
    tun_network_results: queue.SimpleQueue[tuple[bool, bool]] = queue.SimpleQueue()
    displayed_network_online: bool | None = None
    displayed_lan_ip: str | None = None
    confirmation: dict | None = None
    confirmation_signature = ""
    overlay: Image.Image | None = None
    overlay_until = 0.0
    dirty = True
    brightness_held = {1: False, -1: False}
    brightness_repeat_at = {1: 0.0, -1: 0.0}
    brightness_last_value: dict[int, int | None] = {1: None, -1: None}
    last_confirm_at = -KEY3_COOLDOWN_SECONDS
    last_lock_at = -LOCK_COOLDOWN_SECONDS
    last_tun_toggle_at = -TUN_TOGGLE_COOLDOWN_SECONDS
    last_keyboard_toggle_at = -KEY3_COOLDOWN_SECONDS
    last_input_select_at = -KEY1_COOLDOWN_SECONDS
    key_hold_states = [
        {
            "button": key1,
            "started": None,
            "fired": False,
            "short_event": keyboard_toggle_pressed,
            "title": "Pi 500+",
            "match": "title:Pi 500+ 键盘控制中心",
            "command": [str(APP_DIR / "run.sh")],
        },
        {
            "button": key2,
            "started": None,
            "fired": False,
            "short_event": tun_toggle_pressed,
            "title": "Clash Verge",
            "match": "app_id:clash-verge",
            "command": [executable_path(
                "/usr/lib/Clash Verge/wayland-compat/clash-verge-wayland",
                "clash-verge",
                "clash-verge-rev",
                "/usr/bin/clash-verge",
            )],
        },
        {
            "button": key3,
            "started": None,
            "fired": False,
            "short_event": confirm_pressed,
            "title": "ChatGPT",
            "match": CODEX_WINDOW_MATCH,
            "command": [executable_path("chatgpt", "/usr/bin/chatgpt")],
        },
    ]
    # Capture GPIO edges in gpiozero's callback threads. Polling button levels
    # from the main rendering loop can miss a quick tap while SPI or a desktop
    # command is busy, which was especially visible on KEY3.
    key_edge_events: queue.SimpleQueue[tuple[int, bool, float]] = queue.SimpleQueue()
    for key_index, key_state in enumerate(key_hold_states):
        button = key_state["button"]
        button.when_pressed = (
            lambda index=key_index: key_edge_events.put(
                (index, True, time.monotonic())
            )
        )
        button.when_released = (
            lambda index=key_index: key_edge_events.put(
                (index, False, time.monotonic())
            )
        )
    joystick_press_started_at: float | None = None
    joystick_restart_fired = False
    joystick_press_armed = False
    joystick_release_started_at: float | None = None
    screen_standby = False
    both_sessions_locked = False
    last_keyboard_activity = keyboard_activity_token()
    lock_wake_until = 0.0
    runtime_status = {
        "build_id": LCD_BUILD_ID,
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "confirmation_pending": False,
        "last_confirmation_error": "",
        "last_key3_event": "",
        "last_key3_result": "",
    }
    next_runtime_status_write = 0.0
    write_runtime_status(runtime_status)

    def probe_network() -> None:
        lan_ip = local_ipv4()
        reachable = lan_ip != "--" and internet_reachable()
        network_state["lan_ip"] = lan_ip
        if reachable:
            network_state["consecutive_failures"] = 0
            network_state["online"] = True
        else:
            network_state["consecutive_failures"] += 1
            # A detached Wi-Fi/Ethernet link is definitive. While a physical
            # address still exists, require two failed Baidu probes to avoid a
            # one-packet hiccup making the quota cards disappear/flicker.
            if (
                lan_ip == "--"
                or network_state["consecutive_failures"]
                >= NETWORK_FAILURE_THRESHOLD
            ):
                network_state["online"] = False
        network_state["checking"] = False

    def fetch_quota() -> None:
        try:
            result = get_json(quota_url)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        quota_results.put(result)

    def fetch_tun_ip(expected_enabled: bool) -> None:
        address = "--"
        if expected_enabled:
            try:
                address = public_egress_ipv4()
            except Exception:
                pass
        tun_results.put((expected_enabled, address))

    def probe_tun_network(expected_enabled: bool) -> None:
        reachable = expected_enabled and tun_internet_reachable()
        tun_network_results.put((expected_enabled, reachable))

    try:
        while not stopping.is_set():
            now = time.monotonic()
            if now >= next_network_check and not network_state["checking"]:
                network_state["checking"] = True
                next_network_check = now + NETWORK_CHECK_SECONDS
                threading.Thread(target=probe_network, daemon=True).start()

            network_online = bool(network_state["online"])
            lan_ip = str(network_state["lan_ip"])
            if (
                network_online != displayed_network_online
                or lan_ip != displayed_lan_ip
            ):
                displayed_network_online = network_online
                displayed_lan_ip = lan_ip
                if network_online:
                    # Refresh immediately when connectivity returns.
                    next_fetch = 0.0
                dirty = True

            if now >= next_tun_state_check:
                next_tun_state_check = now + TUN_STATE_CHECK_SECONDS
                try:
                    current_tun_enabled = _clash_verge_tun_setting()
                except Exception:
                    current_tun_enabled = False
                if current_tun_enabled != tun_state["enabled"]:
                    tun_state["enabled"] = current_tun_enabled
                    tun_state["ip"] = "--"
                    tun_state["online"] = False
                    next_tun_ip_check = 0.0
                    next_tun_network_check = 0.0
                    dirty = True

            try:
                while True:
                    result_enabled, result_ip = tun_results.get_nowait()
                    tun_state["checking"] = False
                    # Ignore a late result from before a TUN state change.
                    if result_enabled == tun_state["enabled"]:
                        if tun_state["ip"] != result_ip:
                            tun_state["ip"] = result_ip
                            dirty = True
                    else:
                        next_tun_ip_check = 0.0
            except queue.Empty:
                pass

            try:
                while True:
                    result_enabled, result_online = tun_network_results.get_nowait()
                    tun_state["network_checking"] = False
                    if result_enabled == tun_state["enabled"]:
                        if tun_state["online"] != result_online:
                            tun_state["online"] = result_online
                            dirty = True
                    else:
                        next_tun_network_check = 0.0
            except queue.Empty:
                pass

            if (
                tun_state["enabled"]
                and now >= next_tun_ip_check
                and not tun_state["checking"]
            ):
                tun_state["checking"] = True
                next_tun_ip_check = now + TUN_IP_REFRESH_SECONDS
                threading.Thread(
                    target=fetch_tun_ip,
                    args=(True,),
                    daemon=True,
                ).start()

            if (
                tun_state["enabled"]
                and now >= next_tun_network_check
                and not tun_state["network_checking"]
            ):
                tun_state["network_checking"] = True
                next_tun_network_check = now + TUN_NETWORK_CHECK_SECONDS
                threading.Thread(
                    target=probe_tun_network,
                    args=(True,),
                    daemon=True,
                ).start()
            elif not tun_state["enabled"] and tun_state["online"]:
                tun_state["online"] = False
                dirty = True

            try:
                while True:
                    quota_result = quota_results.get_nowait()
                    quota_state["fetching"] = False
                    # Keep the last known-good quota through a transient API
                    # failure; its Updated timestamp makes staleness visible.
                    if network_online:
                        if quota_result.get("ok") or not data.get("ok"):
                            data = quota_result
                            dirty = True
                        runtime_status["last_quota_error"] = (
                            "" if quota_result.get("ok")
                            else str(quota_result.get("error", "unknown error"))
                        )
            except queue.Empty:
                pass

            if now >= next_lock_state_check:
                next_lock_state_check = now + 0.25
                sessions_locked = raspberry_pi_locked() and windows_session_locked()
                activity = keyboard_activity_token()
                if sessions_locked and not both_sessions_locked:
                    # Ignore the key sequence that initiated locking. A new
                    # key event after both sessions are locked wakes the LCD.
                    last_keyboard_activity = activity
                    lock_wake_until = 0.0
                elif sessions_locked and activity and activity != last_keyboard_activity:
                    last_keyboard_activity = activity
                    lock_wake_until = now + LOCK_KEYBOARD_WAKE_SECONDS
                elif not sessions_locked:
                    last_keyboard_activity = activity
                    lock_wake_until = 0.0
                both_sessions_locked = sessions_locked
                should_standby = sessions_locked and now >= lock_wake_until
                if should_standby != screen_standby:
                    screen_standby = should_standby
                    display.set_standby(screen_standby)
                    if not screen_standby:
                        dirty = True

            if now >= next_confirmation_check:
                next_confirmation_check = now + CONFIRMATION_CHECK_SECONDS
                try:
                    latest_confirmation = pending_codex_confirmation()
                    runtime_status["last_confirmation_error"] = ""
                except Exception as exc:
                    latest_confirmation = None
                    runtime_status["last_confirmation_error"] = repr(exc)
                latest_signature = json.dumps(
                    latest_confirmation, ensure_ascii=False, sort_keys=True
                ) if latest_confirmation else ""
                runtime_status["confirmation_pending"] = bool(latest_confirmation)
                runtime_status["confirmation_call_id"] = (
                    str(latest_confirmation.get("call_id", ""))
                    if latest_confirmation else ""
                )
                if latest_signature != confirmation_signature:
                    confirmation = latest_confirmation
                    confirmation_signature = latest_signature
                    event_time = datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    )
                    if latest_confirmation:
                        runtime_status["last_confirmation_detected_at"] = event_time
                    else:
                        runtime_status["last_confirmation_cleared_at"] = event_time
                    dirty = True

            if (
                network_online
                and now >= next_fetch
                and not quota_state["fetching"]
            ):
                quota_state["fetching"] = True
                next_fetch = now + REFRESH_SECONDS
                threading.Thread(target=fetch_quota, daemon=True).start()

            try:
                while True:
                    key_index, pressed, edge_at = key_edge_events.get_nowait()
                    key_state = key_hold_states[key_index]
                    if pressed:
                        key_state["started"] = edge_at
                        key_state["fired"] = False
                    elif key_state["started"] is not None:
                        if not key_state["fired"]:
                            key_state["short_event"].set()
                        key_state["started"] = None
                        key_state["fired"] = False
            except queue.Empty:
                pass

            # KEY1/2/3 keep their original short-press actions, but only fire
            # those actions after release. Holding for two seconds opens or
            # focuses the matching desktop application instead.
            for key_state in key_hold_states:
                started = key_state["started"]
                if (
                    started is not None
                    and not key_state["fired"]
                    and now - started >= KEY_APP_HOLD_SECONDS
                ):
                    key_state["fired"] = True
                    title = str(key_state["title"])
                    try:
                        result = focus_or_launch_window(
                            str(key_state["match"]),
                            list(key_state["command"]),
                        )
                        overlay = render_action(title, result)
                    except Exception:
                        overlay = render_action(
                            title, "Open Failed", failed=True
                        )
                    overlay_until = time.monotonic() + 2.0
                    dirty = True

            # Do not accept the startup GPIO level as a press. Deployment
            # restarts this service, and the input can briefly read low while
            # GPIO/pull-up state is settling. Require a stable released state
            # before arming short-press lock and five-second restart actions.
            if not joystick_press_armed:
                joystick_press_started_at = None
                joystick_restart_fired = False
                if joystick_press.is_pressed:
                    joystick_release_started_at = None
                elif joystick_release_started_at is None:
                    joystick_release_started_at = now
                elif (
                    now - joystick_release_started_at
                    >= JOYSTICK_STARTUP_RELEASE_SECONDS
                ):
                    joystick_press_armed = True

            # A short press locks on release. Holding for five seconds instead
            # restarts the display manager once, including while both sessions
            # are locked and the LCD backlight is in standby.
            elif joystick_press.is_pressed:
                if joystick_press_started_at is None:
                    joystick_press_started_at = now
                    joystick_restart_fired = False
                elif (
                    not joystick_restart_fired
                    and now - joystick_press_started_at
                    >= JOYSTICK_RESTART_HOLD_SECONDS
                ):
                    joystick_restart_fired = True
                    if not screen_standby:
                        display.show(render_action("Restarting", "Display Manager"))
                    try:
                        restart_display_manager()
                        overlay = render_action("Restarting", "Display Manager")
                    except Exception:
                        overlay = render_action(
                            "Restart Failed", "Check Permission", failed=True
                        )
                    overlay_until = time.monotonic() + 2.5
                    dirty = True
            elif joystick_press_started_at is not None:
                if not joystick_restart_fired:
                    lock_pressed.set()
                joystick_press_started_at = None
                joystick_restart_fired = False

            if confirm_pressed.is_set():
                confirm_pressed.clear()
                runtime_status["last_key3_event"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )
                if now - last_confirm_at >= KEY3_COOLDOWN_SECONDS:
                    last_confirm_at = now
                    try:
                        confirmation = pending_codex_confirmation()
                        if confirmation is None:
                            overlay = render_action("Codex", "No Prompt", failed=True)
                            runtime_status["last_key3_result"] = "no_prompt"
                        else:
                            confirm_codex_request(confirmation)
                            overlay = render_action("Codex", "Confirmed")
                            runtime_status["last_key3_result"] = "confirmed"
                            confirmation = None
                            confirmation_signature = ""
                            next_confirmation_check = time.monotonic() + 0.5
                    except Exception as exc:
                        overlay = render_action("Codex", "Confirm Failed", failed=True)
                        runtime_status["last_key3_result"] = "failed"
                        runtime_status["last_key3_error"] = repr(exc)
                    overlay_until = time.monotonic() + 2.0
                    dirty = True

            if lock_pressed.is_set():
                lock_pressed.clear()
                if now - last_lock_at >= LOCK_COOLDOWN_SECONDS:
                    last_lock_at = now
                    display.show(render_action("Locking", "Pi + Windows"))
                    windows_locked = False
                    try:
                        result = bt_keyboard_request("lock_windows")
                        if not result.get("ok"):
                            raise RuntimeError(result.get("error", "unknown error"))
                        windows_locked = True
                        set_windows_session_locked(True)
                    except Exception:
                        pass
                    try:
                        lock_raspberry_pi()
                        overlay = render_action(
                            "Locked",
                            "Pi + Windows" if windows_locked else "Pi Only",
                            failed=not windows_locked,
                        )
                    except Exception:
                        overlay = render_action(
                            "Lock Failed",
                            "Windows Locked" if windows_locked else "Check Desktop",
                            failed=True,
                        )
                    overlay_until = time.monotonic() + 2.5
                    dirty = True

            if tun_toggle_pressed.is_set():
                tun_toggle_pressed.clear()
                if now - last_tun_toggle_at >= TUN_TOGGLE_COOLDOWN_SECONDS:
                    last_tun_toggle_at = now
                    display.show(render_action("Clash Verge", "Switching TUN"))
                    try:
                        tun_enabled = toggle_clash_verge_tun()
                        tun_state["enabled"] = tun_enabled
                        tun_state["ip"] = "--"
                        next_tun_ip_check = 0.0
                        overlay = render_action(
                            "Clash Verge",
                            "TUN ON" if tun_enabled else "TUN OFF",
                        )
                    except Exception:
                        overlay = render_action(
                            "TUN Failed", "Check Clash Verge", failed=True
                        )
                    overlay_until = time.monotonic() + 2.5
                    dirty = True

            if keyboard_toggle_pressed.is_set():
                keyboard_toggle_pressed.clear()
                if now - last_keyboard_toggle_at >= KEY3_COOLDOWN_SECONDS:
                    last_keyboard_toggle_at = now
                    try:
                        current = bt_keyboard_request("status")
                        command = "stop" if current.get("active") else "start"
                        result = bt_keyboard_request(command)
                        if not result.get("ok"):
                            raise RuntimeError(result.get("error", "unknown error"))
                        overlay = render_action(
                            "Keyboard",
                            "ON" if result.get("active") else "OFF",
                        )
                    except Exception as exc:
                        message = str(exc).lower()
                        detail = (
                            "Not Connected"
                            if "尚未连接" in str(exc) or "connect" in message
                            else "Operation Failed"
                        )
                        overlay = render_action("Keyboard", detail, failed=True)
                    overlay_until = time.monotonic() + 2.5
                    dirty = True

            for input_event, input_url, input_name in (
                (displayport_pressed, displayport_url, "DisplayPort"),
                (hdmi_pressed, hdmi_url, "HDMI"),
            ):
                if input_event.is_set():
                    input_event.clear()
                    if now - last_input_select_at >= KEY1_COOLDOWN_SECONDS:
                        last_input_select_at = now
                        display.show(render_action("Switching", input_name))
                        try:
                            result = post_json(input_url)
                            destination = result.get("to", {}).get("name", input_name)
                            overlay = render_action("Switched", destination)
                        except Exception:
                            overlay = render_action("Failed", "Check AOC", failed=True)
                        overlay_until = time.monotonic() + 2.0
                        dirty = True

            # Physical up/down are reversed by the 180-degree HAT mounting.
            for direction, button in ((1, joystick_down), (-1, joystick_up)):
                if button.is_pressed:
                    adjustment_started = not brightness_held[direction]
                    should_adjust = adjustment_started
                    if adjustment_started:
                        brightness_held[direction] = True
                        brightness_last_value[direction] = None
                        brightness_repeat_at[direction] = now + BRIGHTNESS_REPEAT_DELAY
                        display.show(render_action("Adjusting", "Brightness"))
                    elif now >= brightness_repeat_at[direction]:
                        should_adjust = True
                        brightness_repeat_at[direction] = now + BRIGHTNESS_REPEAT_INTERVAL
                    if should_adjust:
                        try:
                            result = post_json(
                                brightness_up_url if direction > 0 else brightness_down_url
                            )
                            brightness_last_value[direction] = int(result["brightness"])
                        except Exception:
                            overlay = render_action(
                                "Failed", "Check AOC", failed=True
                            )
                            overlay_until = time.monotonic() + 1.5
                            dirty = True
                else:
                    if brightness_held[direction]:
                        brightness_held[direction] = False
                        final_brightness = brightness_last_value[direction]
                        brightness_last_value[direction] = None
                        if final_brightness is not None:
                            overlay = render_action(
                                "Adjusted", f"Brightness {final_brightness}%"
                            )
                            overlay_until = time.monotonic() + 1.5
                            dirty = True

            if overlay is not None and time.monotonic() >= overlay_until:
                overlay = None
                dirty = True
            if dirty:
                if overlay is None and confirmation is not None:
                    runtime_status["last_confirmation_rendered_at"] = (
                        datetime.now().astimezone().isoformat(timespec="seconds")
                    )
                display.show(
                    overlay if overlay is not None
                    else render_confirmation(confirmation) if confirmation is not None
                    else render(
                        data,
                        network_online=network_online,
                        local_ip=lan_ip,
                        tun_ip=str(tun_state["ip"]),
                        tun_enabled=bool(tun_state["enabled"]),
                        tun_online=bool(tun_state["online"]),
                    )
                )
                dirty = False
            elif now >= next_display_recovery:
                # Re-send the current page periodically. SPI writes do not
                # fail when the panel is absent, so this is also our hot-plug
                # recovery mechanism.
                display.show(
                    overlay if overlay is not None
                    else render_confirmation(confirmation) if confirmation is not None
                    else render(
                        data,
                        network_online=network_online,
                        local_ip=lan_ip,
                        tun_ip=str(tun_state["ip"]),
                        tun_enabled=bool(tun_state["enabled"]),
                        tun_online=bool(tun_state["online"]),
                    ),
                    recover=True,
                )
                next_display_recovery = time.monotonic() + DISPLAY_RECOVERY_SECONDS
            if now >= next_runtime_status_write:
                next_runtime_status_write = now + 1.0
                runtime_status["heartbeat_at"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )
                runtime_status["network_online"] = network_online
                runtime_status["local_ip"] = lan_ip
                runtime_status["quota_ok"] = bool(data.get("ok"))
                runtime_status["quota_stale"] = quota_is_stale(data)
                runtime_status["tun_online"] = bool(tun_state["online"])
                write_runtime_status(runtime_status)
            stopping.wait(0.05)
    finally:
        key1.close()
        key2.close()
        key3.close()
        joystick_up.close()
        joystick_down.close()
        joystick_left.close()
        joystick_right.close()
        joystick_press.close()
        display.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quota-url", default="http://127.0.0.1:8765/quota")
    parser.add_argument(
        "--displayport-url",
        default="http://127.0.0.1:8765/monitor/input/displayport",
    )
    parser.add_argument(
        "--hdmi-url",
        default="http://127.0.0.1:8765/monitor/input/hdmi",
    )
    parser.add_argument(
        "--brightness-up-url",
        default="http://127.0.0.1:8765/monitor/brightness/up",
    )
    parser.add_argument(
        "--brightness-down-url",
        default="http://127.0.0.1:8765/monitor/brightness/down",
    )
    parser.add_argument("--preview", type=Path, help="render a PNG without touching GPIO/SPI")
    args = parser.parse_args()
    if args.preview:
        try:
            data = get_json(args.quota_url)
        except Exception as exc:
            data = {"ok": False, "error": str(exc)}
        network_online = internet_reachable()
        tun_ip, tun_enabled = clash_verge_tun_display()
        tun_online = tun_enabled and tun_internet_reachable()
        render(
            data,
            network_online=network_online,
            local_ip=local_ipv4(),
            tun_ip=tun_ip,
            tun_enabled=tun_enabled,
            tun_online=tun_online,
        ).save(args.preview)
        return
    run(
        args.quota_url,
        args.displayport_url,
        args.hdmi_url,
        args.brightness_up_url,
        args.brightness_down_url,
    )


if __name__ == "__main__":
    main()
