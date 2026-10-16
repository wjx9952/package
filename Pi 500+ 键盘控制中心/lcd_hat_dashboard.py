#!/usr/bin/env python3
"""Codex quota dashboard for the Waveshare 1.3inch LCD HAT.

With the HAT mounted upside down, user-facing Key3 (BCM GPIO 21) toggles
Bluetooth keyboard mode, Key2 (BCM GPIO 20) toggles Clash Verge virtual NIC
(TUN) mode, and Key1 (BCM GPIO 16) confirms a pending Codex request with one
tap and rejects it
with two quick taps.
Joystick press (BCM GPIO 13) locks the Raspberry Pi and Windows; holding it
for five seconds restarts the display manager.
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import math
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
from PIL import Image, ImageDraw, ImageFont, ImageOps


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
KEYBOARD_UNLOCK_FILE = Path("/run/pi500-bt-keyboard/unlock-sequence")
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
CODEX_THREAD_SETTLE_SECONDS = 0.55
CONFIRMATION_FAST_TIMEOUT_SECONDS = 0.8
CONFIRMATION_RESULT_TIMEOUT_SECONDS = 6.0
CONFIRMATION_CHECK_SECONDS = 0.2
REFRESH_SECONDS = 60
QUOTA_STALE_SECONDS = 5 * 60
DISPLAY_RECOVERY_SECONDS = 5
PIXEL_SHIFT_SECONDS = 45
PIXEL_SHIFT_OFFSETS = ((0, 0), (1, 0), (0, 1), (-1, 0), (0, -1))
BAMBU_STATE_FILE = Path(
    os.environ.get(
        "BAMBU_STATE_FILE",
        str(Path.home() / ".local/state/pi500-keyboard-control-center/bambu-state.json"),
    )
)
BAMBU_CAMERA_FRAME_FILE = Path(
    os.environ.get(
        "BAMBU_CAMERA_FRAME_FILE",
        str(BAMBU_STATE_FILE.with_name("bambu-camera.jpg")),
    )
)
BAMBU_STATE_CHECK_SECONDS = 1.0
BAMBU_STATE_STALE_SECONDS = 90.0
BAMBU_COMPLETED_TO_IDLE_SECONDS = 30 * 60
BAMBU_COMPLETED_CAROUSEL_SECONDS = 60 * 60
DASHBOARD_PAGE_SECONDS = 10.0
NOZZLE_LABEL_GAP = 5
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
KEY3_DOUBLE_PRESS_SECONDS = 0.4
KEY_APP_HOLD_SECONDS = 2.0
PI_LOCK_START_TIMEOUT_SECONDS = 1.0
PI_LOCK_START_CHECK_SECONDS = 0.05
JOYSTICK_RESTART_HOLD_SECONDS = 5.0
JOYSTICK_STARTUP_RELEASE_SECONDS = 0.5
LCD_BUILD_ID = "2026-10-16-lock-unlock-token-baseline"
LCD_RUNTIME_STATUS_FILE = Path("/tmp/codex-lcd-hat-status.json")

CODEX_APPROVE_LABELS = {
    "allow",
    "allow once",
    "approve",
    "approve once",
    "run",
    "允许",
    "允许一次",
    "批准",
    "批准一次",
    "允許",
    "允許一次",
    "核准",
    "核准一次",
}
CODEX_REJECT_LABELS = {
    "deny",
    "deny once",
    "reject",
    "decline",
    "don't allow",
    "do not allow",
    "拒绝",
    "拒絕",
    "不允许",
    "不允許",
}

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


class Key1DecisionTracker:
    """Turn one user-facing Key1 tap into confirm and two taps into reject."""

    def __init__(self, window: float = KEY3_DOUBLE_PRESS_SECONDS) -> None:
        self.window = window
        self.pending_at: float | None = None

    def tap(self, pressed_at: float) -> str | None:
        if (
            self.pending_at is not None
            and pressed_at - self.pending_at <= self.window
        ):
            self.pending_at = None
            return "reject"
        self.pending_at = pressed_at
        return None

    def poll(self, now: float) -> str | None:
        if self.pending_at is None or now - self.pending_at < self.window:
            return None
        self.pending_at = None
        return "confirm"


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
F_BAMBU_VALUE = font(15, bold=True)


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
F_BAMBU_JOB = cjk_font(14, bold=True)


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


def ensure_keyboard_mode(enabled: bool, request=bt_keyboard_request) -> tuple[dict, bool]:
    """Set keyboard mode only when its current state differs from the target."""
    current = request("status")
    if not current.get("ok"):
        raise RuntimeError(current.get("error", "keyboard status failed"))
    if bool(current.get("active")) == enabled:
        return current, False
    result = request("start" if enabled else "stop")
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "keyboard mode failed"))
    if bool(result.get("active")) != enabled:
        raise RuntimeError("keyboard mode did not reach requested state")
    return result, True


def keyboard_activity_token() -> int:
    try:
        return int(KEYBOARD_ACTIVITY_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def keyboard_unlock_token() -> int:
    try:
        return int(KEYBOARD_UNLOCK_FILE.read_text(encoding="ascii").strip())
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
    try:
        wait_for_raspberry_pi_lock(process)
    except Exception:
        if process.poll() is None:
            process.terminate()
        raise


def wait_for_raspberry_pi_lock(
    process,
    *,
    is_locked=None,
    timeout: float = PI_LOCK_START_TIMEOUT_SECONDS,
    check_interval: float = PI_LOCK_START_CHECK_SECONDS,
) -> None:
    """Require swaylock to remain alive and become observable before success."""
    is_locked = is_locked or raspberry_pi_locked
    deadline = time.monotonic() + timeout
    while True:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"system lock screen exited before locking ({return_code})"
            )
        if is_locked():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("system lock screen did not become active")
        time.sleep(check_interval)


def ensure_raspberry_pi_locked(
    is_locked=None,
    lock=None,
) -> bool:
    """Lock the Pi only when needed; return true when a new lock was started."""
    is_locked = is_locked or raspberry_pi_locked
    lock = lock or lock_raspberry_pi
    if is_locked():
        return False
    try:
        lock()
    except Exception:
        # A lock may have appeared between the initial check and launch.
        if is_locked():
            return False
        raise
    return True


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


def request_windows_lock() -> None:
    """Send idempotent Win+L and record the requested Windows lock state."""
    result = bt_keyboard_request("lock_windows")
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown error"))
    set_windows_session_locked(True)


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


def locked_display_policy(
    lock_cycle_active: bool,
    sequence_unlock_active: bool,
    lock_wake_until: float,
    now: float,
    standby_not_before: float = 0.0,
) -> tuple[bool, str]:
    """Return LCD standby state and the reason it is currently awake."""
    if not lock_cycle_active:
        return False, "lock_cycle_inactive"
    if now < standby_not_before:
        return False, "lock_feedback"
    if sequence_unlock_active:
        return False, "unlock_sequence"
    if now < lock_wake_until:
        return False, "keyboard_activity"
    return True, "standby"


def lock_cycle_session_unlocked(
    pi_was_locked: bool,
    windows_was_locked: bool,
    pi_locked: bool,
    windows_locked: bool,
) -> bool:
    """Detect the first real unlock transition during the current cycle."""
    return (pi_was_locked and not pi_locked) or (
        windows_was_locked and not windows_locked
    )


def codex_monitor_module():
    """Load the shared monitor with support for current desktop event shapes."""
    if str(CODEX_MONITOR_DIR) not in sys.path:
        sys.path.insert(0, str(CODEX_MONITOR_DIR))
    import codex_usage_monitor as monitor


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


def codex_call_still_pending(payload: dict) -> bool:
    """Check this exact request, even if another task became newest."""
    pending_codex_confirmation()
    path = Path(str(payload.get("_codex_session_path", "")))
    state = _confirmation_sessions.get(path)
    if state is None:
        # Missing evidence must never be reported as a successful approval.
        return True
    return str(payload.get("call_id", "")) in state["calls"]


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
    monitor = codex_monitor_module()
    if monitor.browser_use_permission_kind(payload) == "upload":
        return "Codex Approval", "浏览器请求上传文件"

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
    return "Codex Approval", detail


def desktop_environment() -> dict[str, str]:
    """Build the Wayland environment used by the bundled desktop helpers."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment = os.environ.copy()
    environment["XDG_RUNTIME_DIR"] = runtime_dir
    environment.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus"
    )
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


def launch_desktop_app(command: list[str]) -> None:
    """Launch a GUI app in its own user unit, outside the LCD cgroup."""
    environment = desktop_environment()
    launcher = [
        "/usr/bin/systemd-run",
        "--user",
        "--collect",
        "--no-block",
        "--quiet",
    ]
    for name in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        value = environment.get(name)
        if value:
            launcher.append(f"--setenv={name}={value}")
    launcher.extend(["--", *command])
    result = subprocess.run(
        launcher,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=3,
        check=False,
    )
    if result.returncode == 0:
        return

    # Some user-service environments can reach Wayland but do not allow a
    # nested `systemd-run --user` call. Launch independently as a fallback so
    # a physical side-key hold still opens the requested application.
    try:
        process = subprocess.Popen(
            command,
            env=environment,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise RuntimeError("desktop application launch failed") from exc
    time.sleep(0.15)
    if process.poll() not in (None, 0):
        raise RuntimeError("desktop application launch failed")


def signal_single_instance(lock_path: Path) -> bool:
    """Ask a live single-instance application to show its hidden window."""
    try:
        lock = lock_path.open("r+")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            try:
                running_pid = int(lock.read().strip())
                os.kill(running_pid, signal.SIGUSR1)
            except (ValueError, ProcessLookupError, PermissionError):
                return False
            return True
        else:
            # No process owns the advisory lock. Leave the stale file for the
            # normal launcher, which safely replaces its stored PID.
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    finally:
        lock.close()


def focus_or_launch_window(
    match: str,
    command: list[str],
    activation_lock: str | None = None,
) -> str:
    """Focus, explicitly reveal, or launch a desktop application."""
    if not WLRCTL_PATH.is_file():
        raise RuntimeError("Wayland window helper is not installed")
    focused = run_desktop_command(
        [str(WLRCTL_PATH), "toplevel", "focus", match]
    )
    if focused.returncode == 0:
        return "Focused"
    if activation_lock:
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        if signal_single_instance(runtime_dir / activation_lock):
            return "Focused"
    launch_desktop_app(command)
    return "Opened"


def normalized_accessible_label(value: str) -> str:
    """Normalize a desktop accessibility label for exact matching."""
    return " ".join(value.casefold().split())


def click_codex_decision_button(labels: set[str]) -> bool:
    """Click a visible Codex decision button through AT-SPI.

    This function is run in a child process because a broken accessibility
    bus can abort a GI process instead of raising a normal Python exception.
    """
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    wanted = {normalized_accessible_label(label) for label in labels}
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        desktop = Atspi.get_desktop(0)
        pending = []
        if desktop is not None:
            for index in range(desktop.get_child_count()):
                application = desktop.get_child_at_index(index)
                if application is None:
                    continue
                application_name = normalized_accessible_label(
                    application.get_name() or ""
                )
                if "chatgpt" in application_name or "codex" in application_name:
                    pending.append(application)
        visited = 0
        while pending and visited < 6000:
            accessible = pending.pop()
            visited += 1
            try:
                name = normalized_accessible_label(accessible.get_name() or "")
                role = accessible.get_role()
                states = accessible.get_state_set()
                showing = states is None or states.contains(Atspi.StateType.SHOWING)
                sensitive = states is None or states.contains(Atspi.StateType.SENSITIVE)
                if (
                    role == Atspi.Role.PUSH_BUTTON
                    and showing
                    and sensitive
                    and name in wanted
                ):
                    action = accessible.get_action_iface()
                    if action is not None and action.get_n_actions() > 0:
                        return bool(action.do_action(0))
                for index in range(accessible.get_child_count()):
                    child = accessible.get_child_at_index(index)
                    if child is not None:
                        pending.append(child)
            except Exception:
                continue
        time.sleep(0.15)
    return False


def click_codex_approve_button() -> bool:
    return click_codex_decision_button(CODEX_APPROVE_LABELS)


def click_codex_reject_button() -> bool:
    return click_codex_decision_button(CODEX_REJECT_LABELS)


def try_accessible_codex_approve() -> bool:
    """Run the AT-SPI click helper without risking the LCD service process."""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--click-codex-approve"],
            env=desktop_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def try_accessible_codex_reject() -> bool:
    """Run the AT-SPI reject helper without risking the LCD service process."""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--click-codex-reject"],
            env=desktop_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def confirm_codex_request(payload: dict) -> str:
    """Open the exact pending task, confirm it, then restore the prior app."""
    if not WTYPE_PATH.is_file() or not WLRCTL_PATH.is_file():
        raise RuntimeError("Wayland helpers are not installed")

    was_focused = run_desktop_command(
        [str(WLRCTL_PATH), "toplevel", "find", CODEX_WINDOW_MATCH, "state:active"]
    ).returncode == 0
    method = ""
    try:
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

        # Enter is Codex's global approval shortcut and succeeds quickly in
        # the normal case. Do not make every Key1 press wait for the slower
        # accessibility scan; use that only when the first Enter was swallowed
        # by a focused sidebar button, link, menu or dialog.
        result = run_desktop_command([str(WTYPE_PATH), "-k", "Return"])
        if result.returncode != 0:
            raise RuntimeError("could not send Codex confirmation key")
        method = "keyboard"
        fast_deadline = time.monotonic() + CONFIRMATION_FAST_TIMEOUT_SECONDS
        while time.monotonic() < fast_deadline:
            if not codex_call_still_pending(payload):
                return method
            time.sleep(0.05)

        if try_accessible_codex_approve():
            method = "accessibility-fallback"
        else:
            # The accessibility bridge may be unavailable. Refocus the Codex
            # toplevel and retry its native shortcut once before giving up.
            run_desktop_command(
                [str(WLRCTL_PATH), "toplevel", "focus", CODEX_WINDOW_MATCH]
            )
            time.sleep(FOCUS_SETTLE_SECONDS)
            result = run_desktop_command([str(WTYPE_PATH), "-k", "Return"])
            if result.returncode != 0:
                raise RuntimeError("could not retry Codex confirmation key")
            method = "keyboard-retry"

        deadline = time.monotonic() + CONFIRMATION_RESULT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not codex_call_still_pending(payload):
                return method
            time.sleep(0.1)
        raise RuntimeError(
            f"Codex prompt did not accept the confirmation via {method}"
        )
    finally:
        if not was_focused:
            time.sleep(FOCUS_SETTLE_SECONDS)
            run_desktop_command(
                [str(WTYPE_PATH), "-M", "alt", "-k", "Tab", "-m", "alt"]
            )


def reject_codex_request(payload: dict) -> str:
    """Open the exact pending task, reject it, then restore the prior app."""
    if not WTYPE_PATH.is_file() or not WLRCTL_PATH.is_file():
        raise RuntimeError("Wayland helpers are not installed")

    was_focused = run_desktop_command(
        [str(WLRCTL_PATH), "toplevel", "find", CODEX_WINDOW_MATCH, "state:active"]
    ).returncode == 0
    method = ""
    try:
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

        # Escape is Codex's native refusal shortcut. If focus swallowed it,
        # click a visible deny/reject button through AT-SPI and retry Escape once.
        result = run_desktop_command([str(WTYPE_PATH), "-k", "Escape"])
        if result.returncode != 0:
            raise RuntimeError("could not send Codex rejection key")
        method = "keyboard"
        fast_deadline = time.monotonic() + CONFIRMATION_FAST_TIMEOUT_SECONDS
        while time.monotonic() < fast_deadline:
            if not codex_call_still_pending(payload):
                return method
            time.sleep(0.05)

        if try_accessible_codex_reject():
            method = "accessibility-fallback"
        else:
            run_desktop_command(
                [str(WLRCTL_PATH), "toplevel", "focus", CODEX_WINDOW_MATCH]
            )
            time.sleep(FOCUS_SETTLE_SECONDS)
            result = run_desktop_command([str(WTYPE_PATH), "-k", "Escape"])
            if result.returncode != 0:
                raise RuntimeError("could not retry Codex rejection key")
            method = "keyboard-retry"

        deadline = time.monotonic() + CONFIRMATION_RESULT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not codex_call_still_pending(payload):
                return method
            time.sleep(0.1)
        raise RuntimeError(
            f"Codex prompt did not accept the rejection via {method}"
        )
    finally:
        if not was_focused:
            time.sleep(FOCUS_SETTLE_SECONDS)
            run_desktop_command(
                [str(WTYPE_PATH), "-M", "alt", "-k", "Tab", "-m", "alt"]
            )


def website_reachable(url: str, *, use_head: bool = True) -> bool:
    """Check DNS, TLS and HTTP reachability without downloading the page."""
    try:
        request_mode = ["--head"] if use_head else ["--range", "0-0"]
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--ipv4",
                *request_mode,
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
    """Return true when at least one independent mainland site is reachable."""
    urls = (
        "https://connectivitycheck.platform.hicloud.com/generate_204",
        "https://www.qq.com/",
        "https://www.taobao.com/",
    )
    with ThreadPoolExecutor(max_workers=len(urls)) as executor:
        return any(executor.map(
            lambda url: website_reachable(url, use_head=False),
            urls,
        ))


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


def quota_reset_started(previous: dict | None, current: dict | None) -> bool:
    """Recognise a real quota-cycle reset without reacting to normal refreshes."""
    if not previous or not current:
        return False
    try:
        old_percent = int(previous.get("remaining_percent", 0))
        new_percent = int(current.get("remaining_percent", 0))
        old_reset = int(previous.get("resets_at", 0))
        new_reset = int(current.get("resets_at", 0))
    except (TypeError, ValueError):
        return False
    return old_reset > 0 and new_reset > old_reset and new_percent >= old_percent + 5


def animated_quota_data(
    data: dict,
    animations: dict[str, dict | None],
    now: float,
) -> tuple[dict, bool]:
    """Return a display-only quota snapshot with reset animations applied."""
    short, long = quota_windows(data)
    replacements: dict[int, dict] = {}
    active = False
    for name, window in (("short", short), ("long", long)):
        animation = animations.get(name)
        if not animation or not window:
            continue
        elapsed = now - float(animation["started_at"])
        if elapsed < 0.9:
            progress = max(0.0, elapsed / 0.9)
            eased = 0.5 - 0.5 * math.cos(math.pi * progress)
            displayed = round(
                int(animation["start_percent"])
                + (100 - int(animation["start_percent"])) * eased
            )
            active = True
        elif elapsed < 1.1:
            displayed = 100
            active = True
        else:
            animations[name] = None
            # Redraw once more so a rare post-reset value below 100 replaces
            # the full animation frame with the server's real percentage.
            active = True
            continue
        replacement = dict(window)
        replacement["remaining_percent"] = displayed
        replacements[id(window)] = replacement
    if not replacements:
        return data, active
    rendered = dict(data)
    for key in ("primary", "secondary"):
        window = data.get(key)
        if window is not None and id(window) in replacements:
            rendered[key] = replacements[id(window)]
    return rendered, active


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


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bounded_percent(value: object) -> int:
    return max(0, min(100, round(_number(value))))


def _bambu_job_name(value: object) -> str:
    """Return the printer's task/file name while keeping paths compact."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if "/" in text or "\\" in text:
        text = re.split(r"[/\\]", text)[-1]
    text = text.strip(" _-/\\")
    return text or "Print Job"


def _bambu_remaining_text(minutes: object) -> str:
    total = max(0, round(_number(minutes)))
    hours, minute = divmod(total, 60)
    if hours:
        return f"{hours}h {minute:02d}m left"
    return f"{minute}m left"


def _parse_filament_colour(value: object) -> tuple[int, int, int]:
    text = re.sub(r"[^0-9a-fA-F]", "", str(value or ""))
    if len(text) >= 6:
        try:
            return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))
        except ValueError:
            pass
    return (32, 129, 238)


def _colour_name(colour: tuple[int, int, int]) -> str:
    palette = {
        "Black": (15, 18, 22),
        "White": (238, 240, 242),
        "Gray": (105, 112, 120),
        "Red": (235, 68, 62),
        "Orange": (255, 149, 0),
        "Yellow": (255, 204, 0),
        "Green": (48, 209, 88),
        "Cyan": (50, 215, 225),
        "Blue": (32, 129, 238),
        "Purple": (175, 82, 222),
        "Pink": (255, 55, 95),
        "Brown": (162, 132, 94),
    }
    return min(
        palette,
        key=lambda name: sum(
            (actual - reference) ** 2
            for actual, reference in zip(colour, palette[name])
        ),
    )


def _active_filament(print_data: dict) -> tuple[str, str, tuple[int, int, int]]:
    ams = print_data.get("ams")
    if not isinstance(ams, dict):
        ams = {}
    active_id = str(ams.get("tray_now", print_data.get("tray_now", "")))
    selected: dict = {}
    units = ams.get("ams")
    if isinstance(units, list):
        for unit in units:
            if not isinstance(unit, dict):
                continue
            trays = unit.get("tray")
            if not isinstance(trays, list):
                continue
            for tray in trays:
                if isinstance(tray, dict) and str(tray.get("id", "")) == active_id:
                    selected = tray
                    break
            if selected:
                break
    if not selected and isinstance(print_data.get("vir_slot"), dict):
        selected = print_data["vir_slot"]
    material = str(
        selected.get("tray_type")
        or selected.get("tray_sub_brands")
        or "Filament"
    ).strip()
    material = re.sub(r"[^A-Za-z0-9+._-]", "", material) or "Filament"
    colour = _parse_filament_colour(
        selected.get("tray_color") or selected.get("cols") or ""
    )
    return material, _colour_name(colour), colour


def _bambu_status_label(raw_status: object, connected: bool = True) -> tuple[str, tuple[int, int, int]]:
    if not connected:
        return "ERROR", (255, 69, 58)
    status = str(raw_status or "").strip().upper()
    if status in {"RUNNING", "PRINTING", "PREPARE", "PREPARING", "SLICING"}:
        return "PRINTING", (255, 149, 0)
    if status in {"PAUSE", "PAUSED"}:
        return "PAUSED", (255, 69, 58)
    if status in {"FAILED", "FAIL", "ERROR", "FAULT", "CANCEL", "CANCELLED"}:
        return "ERROR", (255, 69, 58)
    if status in {"FINISH", "FINISHED", "COMPLETE", "COMPLETED"}:
        return "COMPLETED", (52, 199, 89)
    return "IDLE", (47, 128, 237)


def _bambu_display_status(status: object) -> str:
    """Return the user-facing English printer status label."""
    return {
        "IDLE": "Idle",
        "PRINTING": "Printing",
        "PAUSED": "Paused",
        "ERROR": "Error",
        "COMPLETED": "Completed",
    }.get(str(status or "").strip().upper(), "Idle")


def bambu_status_from_payload(
    payload: dict,
    *,
    source_mtime: float = 0.0,
    now: float | None = None,
) -> dict | None:
    """Convert either the bundled monitor state or the original raw MQTT merge."""
    print_data = payload.get("print")
    if not isinstance(print_data, dict):
        return None
    epoch_now = time.time() if now is None else now
    updated_at = _number(payload.get("updated_at"), source_mtime)
    changed_at = _number(payload.get("status_changed_at"), updated_at)
    connected = bool(payload.get("connected", True))
    raw_status = print_data.get("gcode_state") or print_data.get("state")
    status, status_colour = _bambu_status_label(raw_status, connected)
    if (
        status == "COMPLETED"
        and changed_at > 0
        and epoch_now - changed_at > BAMBU_COMPLETED_TO_IDLE_SECONDS
    ):
        status, status_colour = "IDLE", (47, 128, 237)
    if (
        status not in {"IDLE", "COMPLETED"}
        and updated_at > 0
        and epoch_now - updated_at > BAMBU_STATE_STALE_SECONDS
    ):
        status, status_colour = "ERROR", (255, 69, 58)

    material, colour_name, filament_colour = _active_filament(print_data)
    remaining_minutes = max(
        0,
        round(_number(
            print_data.get("mc_remaining_time", print_data.get("remain_time", 0))
        )),
    )
    finish_at = datetime.fromtimestamp(epoch_now + remaining_minutes * 60).astimezone()
    nozzle_temperatures = {}
    device = print_data.get("device") or {}
    extruder = device.get("extruder") or {}
    # H2D telemetry IDs: 0 right, 1 left. Low/high words are current/target.
    for item in extruder.get("info", []):
        if not isinstance(item, dict) or item.get("id") not in (0, 1):
            continue
        packed = item.get("temp")
        if not isinstance(packed, (int, float)) or packed < 0:
            continue
        side = "right" if item["id"] == 0 else "left"
        nozzle_temperatures["nozzle_" + side] = int(packed) & 0xFFFF
        nozzle_temperatures["nozzle_" + side + "_target"] = (int(packed) >> 16) & 0xFFFF
    return {
        **nozzle_temperatures,
        "status": status,
        "status_colour": status_colour,
        "raw_status": str(raw_status or ""),
        "updated_at": updated_at,
        "status_changed_at": changed_at,
        "connected": connected,
        "printer_ip": str(payload.get("printer_ip") or "--").strip() or "--",
        "job_name": _bambu_job_name(
            print_data.get("subtask_name") or print_data.get("gcode_file")
        ),
        "percent": _bounded_percent(
            print_data.get("mc_percent", print_data.get("percent", 0))
        ),
        "layer": max(0, round(_number(print_data.get("layer_num")))),
        "total_layers": max(0, round(_number(print_data.get("total_layer_num")))),
        "nozzle": round(_number(print_data.get("nozzle_temper"))),
        "nozzle_target": round(_number(print_data.get("nozzle_target_temper"))),
        "bed": round(_number(print_data.get("bed_temper"))),
        "bed_target": round(_number(print_data.get("bed_target_temper"))),
        "remaining_minutes": remaining_minutes,
        "finish_time": finish_at.strftime("%H:%M"),
        "material": material,
        "colour_name": colour_name,
        "filament_colour": filament_colour,
    }


def load_bambu_status(
    path: Path = BAMBU_STATE_FILE,
    *,
    now: float | None = None,
) -> dict | None:
    try:
        stat = path.stat()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return bambu_status_from_payload(payload, source_mtime=stat.st_mtime, now=now)


def bambu_page_active(status: dict | None, *, now: float | None = None) -> bool:
    """Show the printer page while active and for one hour after completion."""
    if not status:
        return False
    label = str(status.get("status", "")).strip().upper()
    if label in {"PRINTING", "PAUSED", "ERROR"}:
        return True
    raw_status = str(status.get("raw_status", "")).strip().upper()
    if raw_status not in {"FINISH", "FINISHED", "COMPLETE", "COMPLETED"}:
        return False
    epoch_now = time.time() if now is None else now
    changed_at = _number(status.get("status_changed_at"))
    return (
        changed_at > 0
        and epoch_now - changed_at <= BAMBU_COMPLETED_CAROUSEL_SECONDS
    )


def bambu_status_for_display(status: dict | None) -> dict:
    """Supply a stable offline page while printer telemetry is unavailable."""
    if status is not None:
        return status
    return {
        "connected": False,
        "printer_ip": "--",
        "updated_at": 0,
        "status": "ERROR",
        "status_colour": (255, 69, 58),
        "job_name": "Printer Offline",
        "material": "Filament",
        "colour_name": "--",
        "percent": 0,
        "layer": 0,
        "total_layers": 0,
        "remaining_minutes": 0,
        "finish_time": "--:--",
    }


def shift_frame(image: Image.Image, offset: tuple[int, int]) -> Image.Image:
    """Move a complete frame without wrapping pixels across opposite edges."""
    offset_x, offset_y = offset
    if offset_x == 0 and offset_y == 0:
        return image
    width, height = image.size
    source_left = max(0, -offset_x)
    source_top = max(0, -offset_y)
    source_right = min(width, width - offset_x)
    source_bottom = min(height, height - offset_y)
    shifted = Image.new(image.mode, image.size, (0, 0, 0))
    if source_right > source_left and source_bottom > source_top:
        shifted.paste(
            image.crop((source_left, source_top, source_right, source_bottom)),
            (max(0, offset_x), max(0, offset_y)),
        )
    return shifted


class SmoothDraw:
    """Supersample rounded shapes only; keep text at native LCD resolution."""

    def __init__(self, image):
        self.image = image
        self.draw = ImageDraw.Draw(image)

    def __getattr__(self, name):
        return getattr(self.draw, name)

    def rounded_rectangle(self, box, radius=0, fill=None, outline=None, width=1):
        left, top, right, bottom = map(int, box)
        w, h = right - left + 1, bottom - top + 1
        scale = 4
        layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (0, 0, w * scale - 1, h * scale - 1),
            radius=radius * scale, fill=fill, outline=outline, width=width * scale,
        )
        layer = layer.resize((w, h), Image.Resampling.LANCZOS)
        self.image.paste(layer, (left, top), layer.getchannel("A"))


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
    scale = 4
    mask = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1), radius=radius * scale, fill=255,
    )
    mask = mask.resize((width, height), Image.Resampling.LANCZOS)
    image.paste(gradient, (left, top), mask)


def draw_status_dot(
    image: Image.Image,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int],
) -> None:
    """Draw a small circular status dot with smooth sub-pixel edges."""
    left, top, right, bottom = box
    width = right - left + 1
    height = bottom - top + 1
    scale = 8
    layer = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(
        (0, 0, width * scale - 1, height * scale - 1),
        fill=(*fill, 255),
    )
    layer = layer.resize((width, height), Image.Resampling.LANCZOS)
    image.paste(layer.convert("RGB"), (left, top), layer.getchannel("A"))


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
              font=F_RESET, fill=(166, 171, 181) if stale else (246, 248, 252))


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
    draw = SmoothDraw(image)
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
    draw_status_dot(image, (10, 32, 18, 40), status_colour)
    draw.text((23, 30), "IP: " + local_ip, font=F_META, fill=meta_text_colour)
    updated = "Updated " + refreshed
    updated_box = draw.textbbox((0, 0), updated, font=F_META)
    updated_x = 231 - (updated_box[2] - updated_box[0])
    draw.text((updated_x - 10, 30), "|", font=F_META, fill=(74, 104, 139))
    draw.text((updated_x, 30), updated, font=F_META, fill=meta_text_colour)

    tun_accent = (174, 112, 255) if tun_online else (255, 69, 58)
    draw_status_dot(image, (10, 46, 18, 54), tun_accent)
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


def _fit_lcd_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    text_font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if draw.textlength(text, font=text_font) <= max_width:
        return text
    suffix = "..."
    shortened = text
    while shortened and draw.textlength(shortened + suffix, font=text_font) > max_width:
        shortened = shortened[:-1]
    return (shortened.rstrip() + suffix) if shortened else suffix


def _draw_bambu_preview(
    image: Image.Image,
    box: tuple[int, int, int, int],
    frame_path: Path,
    accent: tuple[int, int, int],
) -> None:
    left, top, right, bottom = box
    size = (right - left + 1, bottom - top + 1)
    try:
        with Image.open(frame_path) as source:
            preview = ImageOps.fit(
                source.convert("RGB"), size, method=Image.Resampling.LANCZOS
            )
    except (OSError, ValueError):
        preview = Image.new("RGB", size, (5, 10, 16))
        preview_draw = ImageDraw.Draw(preview)
        width, height = size
        preview_draw.polygon(
            [(11, 20), (width // 2, 8), (width - 10, 19), (width // 2, 31)],
            fill=accent,
        )
        darker = tuple(max(0, channel - 65) for channel in accent)
        preview_draw.polygon(
            [(11, 20), (width // 2, 31), (width // 2, height - 7), (11, height - 18)],
            fill=darker,
        )
        preview_draw.polygon(
            [(width // 2, 31), (width - 10, 19), (width - 10, height - 18), (width // 2, height - 7)],
            fill=tuple(max(0, channel - 35) for channel in accent),
        )
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=8, fill=255
    )
    image.paste(preview, (left, top), mask)
    SmoothDraw(image).rounded_rectangle(box, radius=8, outline=(29, 78, 100), width=1)


def render_bambu_status(
    status: dict,
    *,
    camera_frame_path: Path = BAMBU_CAMERA_FRAME_FILE,
) -> Image.Image:
    """Render the compact 3D printer page using the Codex Usage type hierarchy."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = SmoothDraw(image)
    title = "Bambu Monitor"
    title_width = draw.textlength(title, font=F_TITLE)
    draw.text(((WIDTH - title_width) // 2, 2), title, font=F_TITLE, fill=(250, 250, 252))

    meta_colour = (250, 250, 252)
    connection_colour = (48, 209, 88) if status.get("connected", False) else (255, 69, 58)
    draw_status_dot(image, (10, 32, 18, 40), connection_colour)
    draw.text(
        (23, 30),
        "IP: " + str(status.get("printer_ip", "--")),
        font=F_META,
        fill=meta_colour,
    )
    updated_at = _number(status.get("updated_at"))
    updated = "Updated " + (
        datetime.fromtimestamp(updated_at).astimezone().strftime("%H:%M:%S")
        if updated_at else "--:--:--"
    )
    updated_width = draw.textlength(updated, font=F_META)
    updated_x = 231 - updated_width
    draw.text((updated_x - 10, 30), "|", font=F_META, fill=(74, 104, 139))
    draw.text((updated_x, 30), updated, font=F_META, fill=meta_colour)

    status_colour = tuple(status.get("status_colour", (255, 149, 0)))
    draw_status_dot(image, (10, 46, 18, 54), status_colour)
    printer_status = "Printer Status: " + _bambu_display_status(status.get("status"))
    printer_status = _fit_lcd_text(draw, printer_status, F_META, WIDTH - 31)
    draw.text((23, 44), printer_status, font=F_META, fill=(250, 250, 252))

    # Use visible glyph bounds: font ascent must not change row spacing.
    def label(x, y, value, size, *, bold=False, muted=False, right=False, accent=False):
        face = cjk_font(size, bold=bold)
        value = _fit_lcd_text(draw, str(value), face, 206)
        bounds = draw.textbbox((0, 0), value, font=face)
        if right:
            x -= bounds[2] - bounds[0]
        colour = (25, 209, 223) if accent else (
            (188, 197, 208) if muted else (237, 241, 247)
        )
        draw.text((x - bounds[0], y - bounds[1]), value, font=face, fill=colour)

    def card(box, radius=8):
        draw.rounded_rectangle(box, radius=radius, fill=(10, 15, 21),
                               outline=(39, 49, 63), width=1)

    def temperature(key):
        current, target = status.get(key), status.get(key + "_target")
        return (
            ("--" if current is None else str(int(current))) + "°C / "
            + ("--" if target is None else str(int(target))) + "°C"
        )

    def nozzle_heading(x, y, side):
        face = cjk_font(8)
        label(x, y, side, 8, muted=True)
        side_width = draw.textlength(side, font=face)
        label(x + side_width + NOZZLE_LABEL_GAP, y, "Nozzle", 8, muted=True)

    card((7, 59, 116, 91), 12)
    card((123, 59, 233, 91), 12)
    nozzle_heading(15, 64, "Left")
    label(108, 76, temperature("nozzle_left"), 11, bold=True, right=True)
    nozzle_heading(131, 64, "Right")
    label(225, 76, temperature("nozzle_right"), 11, bold=True, right=True)
    # Five clear pixel rows between each pair of vertically stacked cards.
    card((7, 97, 233, 124), 12)
    bed_text = temperature("bed")
    bed_bounds = draw.textbbox((0, 0), bed_text, font=cjk_font(14, bold=True))
    bed_text_y = 97 + (28 - (bed_bounds[3] - bed_bounds[1])) // 2
    bed_label_bounds = draw.textbbox((0, 0), "Bed", font=cjk_font(10))
    bed_label_y = 97 + (28 - (bed_label_bounds[3] - bed_label_bounds[1])) // 2
    label(15, bed_label_y, "Bed", 10, muted=True)
    label(225, bed_text_y, bed_text, 14, bold=True, right=True)

    # Match the Usage card's lower edge; reserve padding below Finish.
    card((7, 130, 233, 236), 15)
    label(17, 139, status.get("job_name", "Print Job"), 11, bold=True)
    label(17, 155, f"{status.get('material', 'Filament')} · {status.get('colour_name', '--')}", 10)
    layer = int(status.get("layer", 0))
    total_layers = int(status.get("total_layers", 0))
    label(17, 169, f"Layer {layer} / {total_layers}" if total_layers else f"Layer {layer}",
          10, muted=True)

    percent = _bounded_percent(status.get("percent", 0))
    percent_text = f"{percent}%"
    percent_bounds = draw.textbbox((0, 0), percent_text, font=F_PERCENT)
    percent_width = percent_bounds[2] - percent_bounds[0]
    # Use exactly the Usage percentage font, right-aligned above the bar.
    draw.text((223 - percent_width - percent_bounds[0], 201 - percent_bounds[3]),
              percent_text, font=F_PERCENT, fill=(25, 209, 223))
    remaining = _bambu_remaining_text(status.get("remaining_minutes", 0))
    # Keep long durations clear of the right-aligned percentage.
    remaining_width = 223 - percent_width - 17 - 8
    remaining_size = 14
    while remaining_size > 9 and draw.textlength(remaining, font=cjk_font(remaining_size, bold=True)) > remaining_width:
        remaining_size -= 1
    label(17, 185, remaining, remaining_size, bold=True)
    bar = (17, 205, 223, 210)
    draw.rounded_rectangle(bar, radius=3, fill=(42, 47, 58))
    if percent:
        fill_right = bar[0] + max(7, round((bar[2] - bar[0]) * percent / 100))
        draw_rounded_gradient(image, (bar[0], bar[1], fill_right, bar[3]),
                              (31, 202, 255), (37, 238, 216), 3)
    label(17, 217, "Finish " + str(status.get("finish_time", "--:--")), 12, bold=True)
    return image.rotate(90)


def render_action(title: str, detail: str = "", *, failed: bool = False) -> Image.Image:
    """Render transient button feedback as a full-screen page."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = SmoothDraw(image)
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
    draw = SmoothDraw(image)
    accent = (255, 149, 0)
    title, detail = confirmation_text(payload)

    draw.rounded_rectangle(
        (8, 8, 232, 232), radius=22, fill=(14, 16, 21),
        outline=(92, 66, 30), width=2,
    )
    title_bounds = draw.textbbox((40, 16), title, font=F_CONFIRM_TITLE)
    title_centre_y = (title_bounds[1] + title_bounds[3]) // 2
    draw_status_dot(
        image,
        (20, title_centre_y - 6, 32, title_centre_y + 6),
        accent,
    )
    draw.text((40, 16), title, font=F_CONFIRM_TITLE, fill=(250, 250, 252))
    lines = wrap_by_width(draw, detail, F_CONFIRM_BODY, 194, 5)
    y = 69
    for line in lines:
        draw.text((23, y), line, font=F_CONFIRM_BODY, fill=(220, 224, 232))
        y += 23

    draw.rounded_rectangle((20, 196, 220, 220), radius=12, fill=(78, 48, 8))
    footer = "Key1 单按确认 / 双按拒绝"
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
        if standby:
            # Do not leave the last action/confirmation page electrically held
            # on the panel while its backlight is dark.  Clear GRAM first, turn
            # the panel output off, and then enter the ST7789's real sleep mode.
            self.backlight.off()
            self._write_payload(bytes(WIDTH * HEIGHT * 2))
            self._command(0x28)  # Display off
            time.sleep(0.02)
            self._command(0x10)  # Sleep in
            time.sleep(0.12)
            self.standby = True
        else:
            self.standby = False
            self._wake_controller()

    def show(self, image: Image.Image, *, recover: bool = False) -> None:
        # Keep the controller asleep while both computers are locked.  The
        # event loop marks the display dirty on wake and then sends a complete
        # fresh frame.
        if self.standby:
            return
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
        # Leave the panel electrically blank even when this service is merely
        # restarted rather than the whole HAT losing power.
        try:
            self.set_standby(True)
        except Exception:
            try:
                self.backlight.off()
            except Exception:
                pass
        for resource in (self.spi, self.dc, self.reset, self.backlight):
            try:
                resource.close()
            except Exception:
                pass


def run(
    quota_url: str,
    displayport_url: str,
    hdmi_url: str,
    brightness_up_url: str,
    brightness_down_url: str,
    bambu_state_file: Path = BAMBU_STATE_FILE,
) -> None:
    from gpiozero import Button

    display = ST7789()
    # The HAT is mounted upside down: user-facing Key3/2/1 correspond to the
    # board's KEY1/2/3 GPIO inputs. Keep the electrical object names unchanged.
    # A smaller hardware debounce
    # catches quick taps; the explicit cooldown below prevents double toggles.
    key1 = Button(KEY1_PIN, pull_up=True, bounce_time=0.05)
    key2 = Button(KEY2_PIN, pull_up=True, bounce_time=0.05)
    key3 = Button(KEY3_PIN, pull_up=True, bounce_time=0.05)
    joystick_up = Button(JOYSTICK_UP_PIN, pull_up=True, bounce_time=0.05)
    joystick_down = Button(JOYSTICK_DOWN_PIN, pull_up=True, bounce_time=0.05)
    joystick_left = Button(JOYSTICK_LEFT_PIN, pull_up=True, bounce_time=0.05)
    joystick_right = Button(JOYSTICK_RIGHT_PIN, pull_up=True, bounce_time=0.05)
    joystick_press = Button(JOYSTICK_PRESS_PIN, pull_up=True, bounce_time=0.05)
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
    next_bambu_check = 0.0
    next_dashboard_switch = 0.0
    next_display_recovery = time.monotonic() + DISPLAY_RECOVERY_SECONDS
    next_pixel_shift = time.monotonic() + PIXEL_SHIFT_SECONDS
    pixel_shift_index = 0
    network_state = {
        "online": False,
        "checking": False,
        "lan_ip": "--",
        "consecutive_failures": 0,
    }
    quota_state = {"fetching": False}
    quota_results: queue.SimpleQueue[dict] = queue.SimpleQueue()
    quota_animations: dict[str, dict | None] = {"short": None, "long": None}
    bambu_status: dict | None = None
    bambu_signature = ""
    bambu_active = False
    show_bambu_page = False
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
    lock_feedback_overlay: Image.Image | None = None
    queued_overlay: Image.Image | None = None
    queued_overlay_after: Image.Image | None = None
    queued_overlay_duration = 0.0
    dirty = True
    brightness_held = {1: False, -1: False}
    brightness_repeat_at = {1: 0.0, -1: 0.0}
    brightness_last_value: dict[int, int | None] = {1: None, -1: None}
    last_key3_action_at = -KEY3_COOLDOWN_SECONDS
    key3_decisions = Key1DecisionTracker()
    last_lock_at = -LOCK_COOLDOWN_SECONDS
    last_tun_toggle_at = -TUN_TOGGLE_COOLDOWN_SECONDS
    last_keyboard_toggle_at = -KEY3_COOLDOWN_SECONDS
    last_input_select_at = -KEY1_COOLDOWN_SECONDS
    key_hold_states = [
        {
            "button": key1,
            "physical_name": "Key1",
            "started": None,
            "fired": False,
            "short_event": keyboard_toggle_pressed,
            "title": "Pi 500+",
            "match": "title:Pi 500+ 键盘控制中心",
            "command": [str(APP_DIR / "run.sh")],
            "activation_lock": "codex-rgb-keyboard.lock",
        },
        {
            "button": key2,
            "physical_name": "Key2",
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
            "physical_name": "Key3",
            "started": None,
            "fired": False,
            "short_event": None,
            "title": "ChatGPT",
            "match": CODEX_WINDOW_MATCH,
            "command": [executable_path("chatgpt", "/usr/bin/chatgpt")],
        },
    ]
    # Capture GPIO edges in gpiozero's callback threads. Polling button levels
    # from the main rendering loop can miss a quick tap while SPI or a desktop
    # command is busy, which was especially visible on user-facing Key1.
    key_edge_events: queue.SimpleQueue[tuple[int, bool, float]] = queue.SimpleQueue()
    key3_tap_events: queue.SimpleQueue[float] = queue.SimpleQueue()
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
    lock_cycle_active = False
    lock_cycle_pi_was_locked = False
    lock_cycle_windows_was_locked = False
    last_keyboard_activity = keyboard_activity_token()
    last_keyboard_unlock = keyboard_unlock_token()
    sequence_unlock_active = False
    lock_wake_until = 0.0
    lock_standby_after = 0.0
    runtime_status = {
        "build_id": LCD_BUILD_ID,
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "confirmation_pending": False,
        "last_confirmation_error": "",
        "last_key3_event": "",
        "last_key3_result": "",
        "last_side_key_hold": "",
        "last_side_key_hold_result": "",
        "last_side_key_hold_error": "",
        "pi_locked": False,
        "windows_locked": False,
        "lock_cycle_active": False,
        "screen_standby": False,
        "lcd_wake_reason": "lock_cycle_inactive",
        "last_lock_error": "",
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
            if now >= next_bambu_check:
                next_bambu_check = now + BAMBU_STATE_CHECK_SECONDS
                latest_bambu = load_bambu_status(
                    bambu_state_file,
                    now=time.time(),
                )
                latest_active = bambu_page_active(
                    latest_bambu,
                    now=time.time(),
                )
                latest_signature = (
                    json.dumps(latest_bambu, sort_keys=True, ensure_ascii=True)
                    if latest_bambu else ""
                )
                state_changed = latest_signature != bambu_signature
                status_changed = (
                    latest_bambu is not None
                    and (
                        bambu_status is None
                        or latest_bambu.get("status") != bambu_status.get("status")
                    )
                )
                bambu_status = latest_bambu
                bambu_signature = latest_signature
                if latest_active != bambu_active:
                    bambu_active = latest_active
                    show_bambu_page = latest_active
                    next_dashboard_switch = now + DASHBOARD_PAGE_SECONDS
                    dirty = True
                elif latest_active and status_changed:
                    show_bambu_page = True
                    next_dashboard_switch = now + DASHBOARD_PAGE_SECONDS
                    dirty = True
                elif state_changed and latest_active and show_bambu_page:
                    dirty = True
            if confirmation is not None:
                next_dashboard_switch = now + DASHBOARD_PAGE_SECONDS
            if confirmation is None and bambu_active and now >= next_dashboard_switch:
                show_bambu_page = not show_bambu_page
                next_dashboard_switch = now + DASHBOARD_PAGE_SECONDS
                dirty = True
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
                            became_online = result_online and not tun_state["online"]
                            tun_state["online"] = result_online
                            if became_online:
                                # Codex quota access normally follows the TUN
                                # route. Retry immediately when it recovers.
                                next_fetch = 0.0
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
                            if data.get("ok") and quota_result.get("ok"):
                                old_short, old_long = quota_windows(data)
                                new_short, new_long = quota_windows(quota_result)
                                for name, previous, current in (
                                    ("short", old_short, new_short),
                                    ("long", old_long, new_long),
                                ):
                                    if quota_reset_started(previous, current):
                                        quota_animations[name] = {
                                            "start_percent": int(
                                                (previous or {}).get(
                                                    "remaining_percent", 0
                                                )
                                            ),
                                            "actual_percent": int(
                                                (current or {}).get(
                                                    "remaining_percent", 0
                                                )
                                            ),
                                            "started_at": now,
                                        }
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
                pi_locked = raspberry_pi_locked()
                windows_locked = windows_session_locked()
                activity = keyboard_activity_token()
                unlock = keyboard_unlock_token()
                if lock_cycle_active:
                    session_unlocked = lock_cycle_session_unlocked(
                        lock_cycle_pi_was_locked,
                        lock_cycle_windows_was_locked,
                        pi_locked,
                        windows_locked,
                    )
                    if session_unlocked:
                        lock_cycle_active = False
                        sequence_unlock_active = False
                        lock_wake_until = 0.0
                        lock_standby_after = 0.0
                    else:
                        lock_cycle_pi_was_locked = (
                            lock_cycle_pi_was_locked or pi_locked
                        )
                        lock_cycle_windows_was_locked = (
                            lock_cycle_windows_was_locked or windows_locked
                        )
                    if unlock and unlock != last_keyboard_unlock:
                        last_keyboard_unlock = unlock
                        # 0502 + Enter is the Windows unlock sequence. Treat it
                        # as the end of this lock cycle immediately instead of
                        # waiting for the Windows status callback.
                        lock_cycle_active = False
                        sequence_unlock_active = False
                        lock_wake_until = 0.0
                        lock_standby_after = 0.0
                    elif activity and activity != last_keyboard_activity:
                        last_keyboard_activity = activity
                        if lock_cycle_active:
                            lock_wake_until = now + LOCK_KEYBOARD_WAKE_SECONDS
                else:
                    last_keyboard_activity = activity
                    last_keyboard_unlock = unlock
                    sequence_unlock_active = False
                    lock_wake_until = 0.0
                should_standby, wake_reason = locked_display_policy(
                    lock_cycle_active,
                    sequence_unlock_active,
                    lock_wake_until,
                    now,
                    lock_standby_after,
                )
                if confirmation is not None:
                    should_standby, wake_reason = False, "codex_confirmation"
                runtime_status["pi_locked"] = pi_locked
                runtime_status["windows_locked"] = windows_locked
                runtime_status["lock_cycle_active"] = lock_cycle_active
                runtime_status["screen_standby"] = should_standby
                runtime_status["lcd_wake_reason"] = wake_reason
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
                        if screen_standby:
                            screen_standby = False
                            display.set_standby(False)
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
                            if key_index == 2:
                                key3_tap_events.put(edge_at)
                            else:
                                key_state["short_event"].set()
                        key_state["started"] = None
                        key_state["fired"] = False
            except queue.Empty:
                pass

            # The three side keys keep their short-press actions, but only fire
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
                    runtime_status["last_side_key_hold"] = (
                        str(key_state["physical_name"])
                    )
                    try:
                        result = focus_or_launch_window(
                            str(key_state["match"]),
                            list(key_state["command"]),
                            key_state.get("activation_lock"),
                        )
                        overlay = render_action(title, result)
                        runtime_status["last_side_key_hold_result"] = result.lower()
                        runtime_status["last_side_key_hold_error"] = ""
                    except Exception as exc:
                        overlay = render_action(
                            title, "Open Failed", failed=True
                        )
                        runtime_status["last_side_key_hold_result"] = "failed"
                        runtime_status["last_side_key_hold_error"] = repr(exc)
                    write_runtime_status(runtime_status)
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

            key3_action: str | None = None
            try:
                while True:
                    tap_at = key3_tap_events.get_nowait()
                    if tap_at - last_key3_action_at < KEY3_COOLDOWN_SECONDS:
                        continue
                    decision = key3_decisions.tap(tap_at)
                    if decision == "reject":
                        key3_action = decision
            except queue.Empty:
                pass
            if key3_action == "reject":
                # Ignore any accidental third edge from the same rapid gesture.
                key3_decisions.pending_at = None
            if key3_action is None:
                key3_action = key3_decisions.poll(now)

            if key3_action is not None:
                runtime_status["last_key3_event"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )
                if now - last_key3_action_at >= KEY3_COOLDOWN_SECONDS:
                    last_key3_action_at = now
                    try:
                        confirmation = pending_codex_confirmation()
                        if confirmation is None:
                            overlay = render_action("Codex", "No Prompt", failed=True)
                            runtime_status["last_key3_result"] = "no_prompt"
                            runtime_status["last_key3_error"] = ""
                        elif key3_action == "reject":
                            display.show(render_action("Codex", "Rejecting..."))
                            runtime_status["last_key3_result"] = "rejecting"
                            runtime_status["last_key3_error"] = ""
                            write_runtime_status(runtime_status)
                            reject_method = reject_codex_request(confirmation)
                            overlay = render_action("Codex", "Rejected")
                            runtime_status["last_key3_result"] = "rejected"
                            runtime_status["last_key3_method"] = reject_method
                            runtime_status["last_key3_error"] = ""
                            confirmation = None
                            confirmation_signature = ""
                            next_confirmation_check = time.monotonic() + 0.5
                        else:
                            # Confirmation can take several seconds when the
                            # first Enter is swallowed and the accessibility
                            # fallback/retry path is needed. Give immediate
                            # visual feedback before this synchronous work.
                            display.show(render_action("Codex", "Confirming..."))
                            runtime_status["last_key3_result"] = "confirming"
                            runtime_status["last_key3_error"] = ""
                            write_runtime_status(runtime_status)
                            confirm_method = confirm_codex_request(confirmation)
                            overlay = render_action("Codex", "Confirmed")
                            runtime_status["last_key3_result"] = "confirmed"
                            runtime_status["last_key3_method"] = confirm_method
                            runtime_status["last_key3_error"] = ""
                            confirmation = None
                            confirmation_signature = ""
                            next_confirmation_check = time.monotonic() + 0.5
                    except Exception as exc:
                        action_name = (
                            "Reject Failed"
                            if key3_action == "reject"
                            else "Confirm Failed"
                        )
                        overlay = render_action("Codex", action_name, failed=True)
                        runtime_status["last_key3_result"] = "failed"
                        runtime_status["last_key3_error"] = repr(exc)
                    overlay_until = time.monotonic() + 2.0
                    dirty = True

            if lock_pressed.is_set():
                lock_pressed.clear()
                if now - last_lock_at >= LOCK_COOLDOWN_SECONDS:
                    last_lock_at = now
                    # Snapshot input tokens before either lock request. The
                    # lock operations can take about a second; sampling after
                    # them could absorb a fast 0502 + Enter event as the new
                    # baseline and leave only the temporary 10-second wake.
                    lock_start_activity = keyboard_activity_token()
                    lock_start_unlock = keyboard_unlock_token()
                    display.show(render_action("Locking", "Pi + Windows"))
                    lock_errors: list[str] = []
                    pi_locked = raspberry_pi_locked()
                    # The Windows status file is event-driven and can retain a
                    # stale `locked: true` after an unlock callback is missed.
                    # Win+L is harmless on an already locked workstation, so
                    # send it for every physical Lock press instead of letting
                    # cached state suppress a required lock command.
                    windows_locked = False
                    try:
                        request_windows_lock()
                        windows_locked = True
                    except Exception as exc:
                        lock_errors.append(f"Windows: {exc!r}")
                    if not pi_locked:
                        try:
                            ensure_raspberry_pi_locked()
                            pi_locked = raspberry_pi_locked()
                        except Exception as exc:
                            lock_errors.append(f"Pi: {exc!r}")
                    lock_cycle_active = pi_locked or windows_locked
                    lock_cycle_pi_was_locked = pi_locked
                    lock_cycle_windows_was_locked = windows_locked
                    last_keyboard_activity = lock_start_activity
                    last_keyboard_unlock = lock_start_unlock
                    sequence_unlock_active = False
                    lock_wake_until = 0.0
                    runtime_status["last_lock_error"] = "; ".join(lock_errors)
                    runtime_status["pi_locked"] = pi_locked
                    runtime_status["windows_locked"] = windows_locked
                    runtime_status["lock_cycle_active"] = lock_cycle_active
                    if lock_cycle_active:
                        detail = (
                            "Pi + Windows" if pi_locked and windows_locked
                            else "Pi Only" if pi_locked
                            else "Windows Only"
                        )
                        overlay = render_action(
                            "Locked",
                            detail,
                            failed=not (pi_locked and windows_locked),
                        )
                        lock_feedback_overlay = overlay
                    else:
                        overlay = render_action(
                            "Lock Failed",
                            "Check Desktop",
                            failed=True,
                        )
                    overlay_until = time.monotonic() + 2.5
                    # Keep the successful Locked result visible first. Once
                    # its normal feedback duration ends, the lock-state policy
                    # writes a black frame and puts the controller to sleep.
                    if lock_cycle_active:
                        lock_standby_after = overlay_until
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
                            post_json(input_url)
                            keyboard_enabled = input_name == "DisplayPort"
                            try:
                                ensure_keyboard_mode(keyboard_enabled)
                                keyboard_overlay = render_action(
                                    "Keyboard",
                                    "ON" if keyboard_enabled else "OFF",
                                )
                            except Exception:
                                keyboard_overlay = render_action(
                                    "Keyboard", "Failed", failed=True
                                )
                            overlay = render_action("Switched", input_name)
                            queued_overlay = keyboard_overlay
                            queued_overlay_after = overlay
                            queued_overlay_duration = 1.8
                            overlay_until = time.monotonic() + 1.5
                        except Exception:
                            overlay = render_action("Failed", "Check AOC", failed=True)
                            queued_overlay = None
                            queued_overlay_after = None
                            queued_overlay_duration = 0.0
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
                expired_overlay = overlay
                if queued_overlay is not None and overlay is queued_overlay_after:
                    overlay = queued_overlay
                    overlay_until = time.monotonic() + queued_overlay_duration
                    queued_overlay = None
                    queued_overlay_after = None
                    queued_overlay_duration = 0.0
                else:
                    overlay = None
                    queued_overlay = None
                    queued_overlay_after = None
                    queued_overlay_duration = 0.0
                if expired_overlay is lock_feedback_overlay:
                    lock_feedback_overlay = None
                    should_standby, wake_reason = locked_display_policy(
                        lock_cycle_active,
                        sequence_unlock_active,
                        lock_wake_until,
                        time.monotonic(),
                        lock_standby_after,
                    )
                    if should_standby and confirmation is None:
                        # End Locked -> black/sleep atomically. Do not mark the
                        # base dashboard dirty in between, otherwise one Codex
                        # Usage frame can be transferred before the 250 ms lock
                        # poll notices that the standby deadline has elapsed.
                        screen_standby = True
                        display.set_standby(True)
                        runtime_status["screen_standby"] = True
                        runtime_status["lcd_wake_reason"] = wake_reason
                        dirty = False
                    else:
                        dirty = True
                else:
                    dirty = True
            if now >= next_pixel_shift:
                next_pixel_shift = now + PIXEL_SHIFT_SECONDS
                pixel_shift_index = (pixel_shift_index + 1) % len(PIXEL_SHIFT_OFFSETS)
                if overlay is None and confirmation is None:
                    dirty = True
            display_data, quota_animation_active = animated_quota_data(
                data, quota_animations, time.monotonic()
            )
            if quota_animation_active and overlay is None and confirmation is None:
                dirty = True
            if dirty:
                if confirmation is not None:
                    runtime_status["last_confirmation_rendered_at"] = (
                        datetime.now().astimezone().isoformat(timespec="seconds")
                    )
                base_page = (
                    render_bambu_status(bambu_status_for_display(bambu_status))
                    if bambu_active and show_bambu_page
                    else render(
                        display_data,
                        network_online=network_online,
                        local_ip=lan_ip,
                        tun_ip=str(tun_state["ip"]),
                        tun_enabled=bool(tun_state["enabled"]),
                        tun_online=bool(tun_state["online"]),
                    )
                )
                display.show(
                    render_confirmation(confirmation) if confirmation is not None
                    else overlay if overlay is not None
                    else shift_frame(
                        base_page,
                        PIXEL_SHIFT_OFFSETS[pixel_shift_index],
                    )
                )
                dirty = False
            elif now >= next_display_recovery:
                # Re-send the current page periodically. SPI writes do not
                # fail when the panel is absent, so this is also our hot-plug
                # recovery mechanism.
                base_page = (
                    render_bambu_status(bambu_status_for_display(bambu_status))
                    if bambu_active and show_bambu_page
                    else render(
                        display_data,
                        network_online=network_online,
                        local_ip=lan_ip,
                        tun_ip=str(tun_state["ip"]),
                        tun_enabled=bool(tun_state["enabled"]),
                        tun_online=bool(tun_state["online"]),
                    )
                )
                display.show(
                    render_confirmation(confirmation) if confirmation is not None
                    else overlay if overlay is not None
                    else shift_frame(
                        base_page,
                        PIXEL_SHIFT_OFFSETS[pixel_shift_index],
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
                runtime_status["bambu_status"] = (
                    str(bambu_status.get("status", "")) if bambu_status else ""
                )
                runtime_status["bambu_page_active"] = bambu_active
                runtime_status["bambu_page_visible"] = (
                    bambu_active and show_bambu_page
                )
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
    parser.add_argument(
        "--bambu-state-file",
        type=Path,
        default=BAMBU_STATE_FILE,
    )
    parser.add_argument("--preview", type=Path, help="render a PNG without touching GPIO/SPI")
    parser.add_argument(
        "--preview-bambu",
        type=Path,
        help="render the current Bambu status page without touching GPIO/SPI",
    )
    parser.add_argument("--click-codex-approve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--click-codex-reject", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.click_codex_approve:
        raise SystemExit(0 if click_codex_approve_button() else 1)
    if args.click_codex_reject:
        raise SystemExit(0 if click_codex_reject_button() else 1)
    if args.preview_bambu:
        bambu_status = load_bambu_status(args.bambu_state_file)
        if bambu_status is None:
            raise SystemExit(f"Bambu state unavailable: {args.bambu_state_file}")
        render_bambu_status(bambu_status).save(args.preview_bambu)
        return
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
        args.bambu_state_file,
    )


if __name__ == "__main__":
    main()
