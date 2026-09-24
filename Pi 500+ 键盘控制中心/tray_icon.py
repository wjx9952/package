#!/usr/bin/env python3
"""GTK system-tray companion for the unified Pi 500+ keyboard controller."""

from __future__ import annotations

import os
import ctypes
import ctypes.util
import shutil
import signal
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402


APP_ID = "codex-rgb-keyboard"
APP_TITLE = "Pi 500+ 键盘控制中心"
ICON_SIZE = "64x64"
AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "codex-rgb-keyboard.desktop"


def set_autostart(enabled: bool) -> None:
    """Enable or disable desktop-session autostart for the unified app."""
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    app_dir = Path(__file__).resolve().parent
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Pi 500+ 键盘控制中心\n"
        f"Exec={app_dir / 'run.sh'}\n"
        f"Path={app_dir}\n"
        f"Icon={app_dir / 'codex-rgb.svg'}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    AUTOSTART_FILE.chmod(0o755)


def install_theme_icon(source: str) -> tuple[str, str]:
    """Install the tray PNG into the user's icon theme and return name/path.

    Wayland panels that implement the StatusNotifier/AppIndicator protocol are
    more reliable when they receive a theme icon name. Passing only an arbitrary
    SVG/PNG file path can produce a blank slot on Raspberry Pi OS' panel.
    """
    source_path = Path(source).expanduser().resolve()
    target_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / ICON_SIZE / "apps"
    target_path = target_dir / f"{APP_ID}.png"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if (
            not target_path.exists()
            or source_path.stat().st_mtime_ns != target_path.stat().st_mtime_ns
            or source_path.stat().st_size != target_path.stat().st_size
        ):
            shutil.copy2(source_path, target_path)
    except OSError:
        # If the home icon cache is not writable, keep using the bundled file.
        return source_path.stem, str(source_path.parent)
    return APP_ID, str(target_dir)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    parent_pid = int(sys.argv[1])
    icon_path = sys.argv[2]
    icon_name, icon_dir = install_theme_icon(icon_path)

    menu = Gtk.Menu()
    show_item = Gtk.MenuItem(label="显示窗口")
    autostart_item = Gtk.CheckMenuItem(label="开机自启动")
    autostart_item.set_active(AUTOSTART_FILE.is_file())
    quit_item = Gtk.MenuItem(label="退出")
    menu.append(show_item)
    menu.append(autostart_item)
    menu.append(Gtk.SeparatorMenuItem())
    menu.append(quit_item)
    menu.show_all()

    # Raspberry Pi OS uses Wayland and wf-panel-pi. Its native tray protocol is
    # Ayatana AppIndicator. The runtime library is part of the stock image even
    # when the optional Python GI typelib is absent, so call the small C API
    # directly. Fall back to Gtk.StatusIcon for older X11 Raspberry Pi OS.
    indicator = None
    status_icon = None
    library_path = ctypes.util.find_library("ayatana-appindicator3")
    if library_path:
        library = ctypes.CDLL(library_path)
        library.app_indicator_new_with_path.restype = ctypes.c_void_p
        library.app_indicator_new_with_path.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
        ]
        library.app_indicator_set_status.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.app_indicator_set_menu.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        library.app_indicator_set_icon_full.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        library.app_indicator_set_icon.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.app_indicator_set_icon_theme_path.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        encoded_icon_dir = icon_dir.encode()
        encoded_icon_name = icon_name.encode()
        indicator = library.app_indicator_new_with_path(
            APP_ID.encode(),
            encoded_icon_name,
            0,  # APP_INDICATOR_CATEGORY_APPLICATION_STATUS
            encoded_icon_dir,
        )
        library.app_indicator_set_icon_theme_path(indicator, encoded_icon_dir)
        library.app_indicator_set_icon(indicator, encoded_icon_name)
        library.app_indicator_set_icon_full(
            indicator,
            encoded_icon_name,
            APP_TITLE.encode(),
        )
        library.app_indicator_set_status(indicator, 1)  # APP_INDICATOR_STATUS_ACTIVE
        library.app_indicator_set_menu(indicator, ctypes.c_void_p(hash(menu)))
    else:
        status_icon = Gtk.StatusIcon.new_from_icon_name(icon_name)
        status_icon.set_from_file(str(Path(icon_path).resolve()))
        status_icon.set_tooltip_text(APP_TITLE)
        status_icon.set_visible(True)

    def send(sig: signal.Signals) -> None:
        try:
            os.kill(parent_pid, sig)
        except ProcessLookupError:
            Gtk.main_quit()

    if status_icon is not None:
        status_icon.connect("activate", lambda _icon: send(signal.SIGUSR1))
        status_icon.connect(
            "popup-menu",
            lambda _icon, button, timestamp: menu.popup(
                None,
                None,
                Gtk.StatusIcon.position_menu,
                status_icon,
                button,
                timestamp,
            ),
        )
    show_item.connect("activate", lambda _item: send(signal.SIGUSR1))
    quit_item.connect("activate", lambda _item: send(signal.SIGUSR2))

    changing_autostart = [False]

    def toggle_autostart(item: Gtk.CheckMenuItem) -> None:
        if changing_autostart[0]:
            return
        desired = item.get_active()
        try:
            set_autostart(desired)
        except OSError as error:
            print(f"无法修改开机自启动：{error}", file=sys.stderr)
            changing_autostart[0] = True
            item.set_active(not desired)
            changing_autostart[0] = False

    autostart_item.connect("toggled", toggle_autostart)

    def parent_is_alive() -> bool:
        try:
            os.kill(parent_pid, 0)
            return True
        except ProcessLookupError:
            Gtk.main_quit()
            return False

    GLib.timeout_add_seconds(2, parent_is_alive)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
