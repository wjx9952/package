#!/usr/bin/env python3
"""Small Tk-shaped facade backed entirely by PyQt5 widgets.

The control center historically mixed hardware logic with Tk widget calls.
This module lets the mature hardware/state code remain unchanged while every
visible desktop widget is rendered by Qt.  It intentionally implements only
the subset used by codex_rgb_keyboard.py.
"""

from __future__ import annotations

import re
import sys
import uuid
from typing import Any, Callable

from PyQt5 import QtCore, QtGui, QtWidgets, sip


TclError = RuntimeError
HORIZONTAL = "horizontal"


def _application() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
        app.setApplicationName("Pi 500+ 键盘控制中心")
        app.setStyle("Fusion")
        app.setQuitOnLastWindowClosed(False)
    return app


def _pair(value: Any) -> tuple[int, int]:
    if isinstance(value, (tuple, list)):
        return int(value[0]), int(value[1])
    amount = int(value or 0)
    return amount, amount


def _font(value: Any) -> QtGui.QFont:
    if not value:
        return QtGui.QFont("Noto Sans CJK SC", 10)
    family = str(value[0])
    size = int(value[1]) if len(value) > 1 else 10
    result = QtGui.QFont(family, size)
    if len(value) > 2 and "bold" in str(value[2]).lower():
        result.setBold(True)
    return result


def _alignment(anchor: str | None) -> QtCore.Qt.Alignment:
    anchor = anchor or "center"
    horizontal = QtCore.Qt.AlignLeft if "w" in anchor else QtCore.Qt.AlignRight if "e" in anchor else QtCore.Qt.AlignHCenter
    vertical = QtCore.Qt.AlignTop if "n" in anchor else QtCore.Qt.AlignBottom if "s" in anchor else QtCore.Qt.AlignVCenter
    return horizontal | vertical


class Variable:
    def __init__(self, value: Any = "") -> None:
        self._value = value
        self._traces: list[Callable[..., Any]] = []
        self._subscribers: list[Callable[[Any], None]] = []

    def get(self) -> Any:
        return self._value

    def set(self, value: Any) -> None:
        self._value = value
        for subscriber in tuple(self._subscribers):
            subscriber(value)
        for callback in tuple(self._traces):
            callback("", "", "write")

    def trace_add(self, _mode: str, callback: Callable[..., Any]) -> str:
        self._traces.append(callback)
        return str(len(self._traces))

    def subscribe(self, callback: Callable[[Any], None]) -> None:
        self._subscribers.append(callback)
        callback(self._value)


class StringVar(Variable):
    pass


class IntVar(Variable):
    def get(self) -> int:
        return int(super().get())


class _KeyFilter(QtCore.QObject):
    def __init__(self, bindings: dict[str, Callable[..., Any]]) -> None:
        super().__init__()
        self.bindings = bindings

    def eventFilter(self, _obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.KeyPress:
            key_event = event
            key = key_event.key()
            modifiers = key_event.modifiers()
            name = None
            if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                name = "<Control-Return>" if modifiers & QtCore.Qt.ControlModifier else "<Return>"
            elif key == QtCore.Qt.Key_Escape:
                name = "<Escape>"
            callback = self.bindings.get(name or "")
            if callback:
                callback(event)
                return True
        if event.type() == QtCore.QEvent.FocusOut:
            callback = self.bindings.get("<FocusOut>")
            if callback:
                callback(event)
        return False


class _Window(QtWidgets.QWidget):
    closed = QtCore.pyqtSignal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.close_callback: Callable[[], Any] | None = None

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.close_callback:
            event.ignore()
            self.close_callback()
        else:
            event.accept()
        self.closed.emit()


class _Scheduler(QtCore.QObject):
    requested = QtCore.pyqtSignal(str, int, object)

    def __init__(self, parent):
        super().__init__(parent)
        self.cancelled = set()
        self.requested.connect(self.schedule, QtCore.Qt.QueuedConnection)

    @QtCore.pyqtSlot(str, int, object)
    def schedule(self, token, delay, callback):
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        def fire():
            try:
                if token not in self.cancelled:
                    callback()
            finally:
                self.cancelled.discard(token)
                timer.deleteLater()
        timer.timeout.connect(fire)
        timer.start(max(0, delay))


class Misc:
    def __init__(self, parent: "Misc | None", qwidget: QtWidgets.QWidget, **kwargs: Any) -> None:
        _application()
        self.parent = parent
        self.qwidget = qwidget
        self._manager = ""
        self._pack_host = None
        self._children = []
        if parent is not None:
            parent._children.append(self)
        self._layout: QtWidgets.QLayout | None = None
        self._layout_kind = ""
        self._padding = (_pair(kwargs.get("padx", 0)), _pair(kwargs.get("pady", 0)))
        self._bindings: dict[str, Callable[..., Any]] = {}
        self._filter: _KeyFilter | None = None
        self._bg = kwargs.get("bg", kwargs.get("background", "transparent"))
        self._fg = kwargs.get("fg", kwargs.get("foreground", "#1d1d1f"))
        self._border = kwargs.get("highlightbackground")
        self._border_width = int(kwargs.get("highlightthickness", kwargs.get("bd", 0)) or 0)
        self._radius = 12 if self._border_width else 9
        # Tk widgets do not become visible until pack/grid manages them. Qt
        # normally reveals every child when its parent is shown, so mirror the
        # Tk behaviour explicitly.
        self.qwidget.hide()
        if kwargs.get("width"):
            self._set_width(kwargs["width"])
        if kwargs.get("height"):
            self.qwidget.setMinimumHeight(int(kwargs["height"]))
        self._apply_style()

    def _set_width(self, value: Any) -> None:
        self.qwidget.setMinimumWidth(int(value))

    def _apply_style(self) -> None:
        name = "qt_" + uuid.uuid4().hex
        self.qwidget.setObjectName(name)
        border = f"{self._border_width}px solid {self._border}" if self._border_width and self._border else "none"
        self.qwidget.setStyleSheet(
            f"#{name} {{ background-color: {self._bg}; color: {self._fg}; "
            f"border: {border}; border-radius: {self._radius}px; }}"
        )

    def _ensure_pack_layout(self, side: str | None = None) -> QtWidgets.QBoxLayout:
        wanted = "h" if side in ("left", "right") else "v"
        if self._layout is None:
            self._layout_kind = wanted
            self._layout = QtWidgets.QHBoxLayout() if wanted == "h" else QtWidgets.QVBoxLayout()
            (px, py) = self._padding
            self._layout.setContentsMargins(px[0], py[0], px[1], py[1])
            self._layout.setSpacing(0)
            if wanted == "v":
                self._layout.setAlignment(QtCore.Qt.AlignTop)
            self.qwidget.setLayout(self._layout)
        if not isinstance(self._layout, QtWidgets.QBoxLayout):
            raise TclError("cannot mix pack and grid in one parent")
        return self._layout

    def _ensure_grid_layout(self) -> QtWidgets.QGridLayout:
        if self._layout is None:
            self._layout_kind = "grid"
            self._layout = QtWidgets.QGridLayout()
            (px, py) = self._padding
            self._layout.setContentsMargins(px[0], py[0], px[1], py[1])
            self._layout.setSpacing(0)
            self.qwidget.setLayout(self._layout)
        if not isinstance(self._layout, QtWidgets.QGridLayout):
            raise TclError("cannot mix pack and grid in one parent")
        return self._layout

    def pack(self, side: str | None = None, fill: str | None = None,
             expand: bool = False, padx: Any = 0, pady: Any = 0,
             anchor: str | None = None, **_kwargs: Any) -> None:
        if self.parent is None:
            self.qwidget.show()
            return
        layout = self.parent._ensure_pack_layout(side)
        if self._pack_host is not None:
            layout.removeWidget(self._pack_host)
            self._pack_host.hide()
        xpad, ypad = _pair(padx), _pair(pady)
        host = self._pack_host
        if host is None:
            host = QtWidgets.QWidget(self.parent.qwidget)
            host.setStyleSheet("background:transparent;")
            host_layout = QtWidgets.QHBoxLayout(host)
            host_layout.setSpacing(0)
            self._pack_host = host
        host_layout = host.layout()
        host_layout.setContentsMargins(xpad[0], ypad[0], xpad[1], ypad[1])
        alignment = QtCore.Qt.Alignment()
        if fill not in ("x", "both") and side not in ("left", "right"):
            alignment = QtCore.Qt.AlignHCenter
        if anchor and fill not in ("x", "both"):
            alignment = _alignment(anchor)
        stretch = 1 if expand else 0
        host_layout.addWidget(self.qwidget, 1, alignment)
        policy = QtWidgets.QSizePolicy
        self.qwidget.setSizePolicy(policy.Expanding if fill in ("x", "both") else policy.Preferred,
                                   policy.Expanding if fill in ("y", "both") else policy.Preferred)
        host.setSizePolicy(policy.Expanding if fill in ("x", "both") or expand else policy.Preferred,
                           policy.Expanding if fill in ("y", "both") else policy.Fixed)
        layout.addWidget(host, stretch)
        if isinstance(self.qwidget, QtWidgets.QLabel) and side in ("left", "right"):
            self.qwidget.setAlignment(_alignment("w" if side == "left" else "e"))
        host.show()
        self.qwidget.show()
        self._manager = "pack"

    def pack_forget(self) -> None:
        if self._pack_host is not None:
            self.parent._layout.removeWidget(self._pack_host)
            self._pack_host.hide()
        self.qwidget.hide()
        self._manager = ""

    def grid(self, row: int = 0, column: int = 0, rowspan: int = 1,
             columnspan: int = 1, sticky: str = "", padx: Any = 0,
             pady: Any = 0, **_kwargs: Any) -> None:
        if self.parent is None:
            return
        layout = self.parent._ensure_grid_layout()
        alignment = QtCore.Qt.Alignment()
        if sticky and not ("e" in sticky and "w" in sticky) and not ("n" in sticky and "s" in sticky):
            alignment = _alignment(sticky)
        left, right = _pair(padx)
        top, bottom = _pair(pady)
        # Grid padding belongs outside the styled control, not inside its
        # border. A transparent cell keeps neighbouring rounded buttons apart.
        host = getattr(self, "_grid_host", None)
        if host is None:
            host = QtWidgets.QWidget(self.parent.qwidget)
            host.setStyleSheet("background:transparent;")
            cell = QtWidgets.QHBoxLayout(host)
            cell.setSpacing(0)
            self._grid_host = host
        cell = host.layout()
        cell.setContentsMargins(left, top, right, bottom)
        cell.addWidget(self.qwidget, 1, alignment)
        layout.addWidget(host, row, column, rowspan, columnspan)
        host.show()
        self.qwidget.show()
        self._manager = "grid"

    def grid_columnconfigure(self, column: int, weight: int = 0, **_kwargs: Any) -> None:
        layout = self._ensure_grid_layout()
        layout.setColumnStretch(column, weight)

    def grid_propagate(self, _enabled: bool) -> None:
        return

    def winfo_manager(self) -> str:
        return self._manager

    def configure(self, **kwargs: Any) -> None:
        if "bg" in kwargs:
            self._bg = kwargs["bg"]
        if "fg" in kwargs:
            self._fg = kwargs["fg"]
        if "state" in kwargs:
            self.qwidget.setEnabled(kwargs["state"] != "disabled")
        if "font" in kwargs:
            self.qwidget.setFont(_font(kwargs["font"]))
        self._apply_style()

    config = configure

    def bind(self, sequence: str, callback: Callable[..., Any]) -> None:
        self._bindings[sequence] = callback
        if self._filter is None:
            self._filter = _KeyFilter(self._bindings)
            self.qwidget.installEventFilter(self._filter)

    def after(self, milliseconds: int, callback: Callable[..., Any], *args: Any) -> QtCore.QTimer:
        root = self
        while root.parent is not None:
            root = root.parent
        token = uuid.uuid4().hex
        root._scheduler.requested.emit(token, milliseconds, lambda: callback(*args))
        return token

    def after_cancel(self, timer: QtCore.QTimer) -> None:
        root = self
        while root.parent is not None:
            root = root.parent
        root._scheduler.cancelled.add(timer)

    def destroy(self) -> None:
        if isinstance(self.qwidget, _Window):
            self.qwidget.close_callback = None
            self.qwidget.hide()
            self.qwidget.closed.emit()
        else:
            self.qwidget.close()
        self.qwidget.deleteLater()

    def winfo_width(self) -> int:
        self.qwidget.adjustSize()
        return self.qwidget.width()

    def winfo_height(self) -> int:
        self.qwidget.adjustSize()
        return self.qwidget.height()

    def winfo_rootx(self) -> int:
        return self.qwidget.mapToGlobal(QtCore.QPoint(0, 0)).x()

    def winfo_rooty(self) -> int:
        return self.qwidget.mapToGlobal(QtCore.QPoint(0, 0)).y()

    def winfo_exists(self) -> bool:
        return self.qwidget is not None and not sip.isdeleted(self.qwidget)

    def update_idletasks(self) -> None:
        _application().processEvents()
        self.qwidget.adjustSize()

    def focus_set(self) -> None:
        self.qwidget.setFocus()


class Tk(Misc):
    def __init__(self) -> None:
        self.app = _application()
        super().__init__(None, _Window())
        self._scheduler = _Scheduler(self.qwidget)
        self._close_callback: Callable[[], Any] | None = None

    def title(self, value: str) -> None:
        self.qwidget.setWindowTitle(value)

    def resizable(self, width: bool, height: bool) -> None:
        if not width and not height:
            self.qwidget.layoutChanged = False

    def protocol(self, name: str, callback: Callable[[], Any]) -> None:
        if name == "WM_DELETE_WINDOW":
            self.qwidget.close_callback = callback

    def configure(self, **kwargs: Any) -> None:
        super().configure(**kwargs)

    def mainloop(self) -> int:
        self.qwidget.adjustSize()
        self.qwidget.show()
        return self.app.exec_()

    def withdraw(self) -> None:
        self.qwidget.hide()

    def iconify(self) -> None:
        self.qwidget.showMinimized()

    def destroy(self) -> None:
        super().destroy()
        if self.parent is None:
            self.app.quit()

    def deiconify(self) -> None:
        self.qwidget.showNormal()

    def lift(self) -> None:
        self.qwidget.raise_()

    def focus_force(self) -> None:
        self.qwidget.activateWindow()
        self.qwidget.setFocus()

    def clipboard_clear(self) -> None:
        self.app.clipboard().clear()

    def clipboard_append(self, value: str) -> None:
        self.app.clipboard().setText(value)

    def register(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        return callback


class Toplevel(Tk):
    def __init__(self, parent: Misc) -> None:
        self.app = _application()
        Misc.__init__(self, parent, _Window(parent.qwidget))
        self._loop: QtCore.QEventLoop | None = None
        # Tk Toplevel windows are mapped automatically; several existing
        # non-blocking dialogs rely on that behaviour.
        self.qwidget.show()

    def transient(self, parent: Misc) -> None:
        was_visible = self.qwidget.isVisible()
        self.qwidget.setParent(parent.qwidget, QtCore.Qt.Dialog)
        if was_visible:
            self.qwidget.show()

    def grab_set(self) -> None:
        self.qwidget.setWindowModality(QtCore.Qt.ApplicationModal)

    def grab_release(self) -> None:
        self.qwidget.setWindowModality(QtCore.Qt.NonModal)

    def wait_window(self) -> None:
        self.qwidget.show()
        self._loop = QtCore.QEventLoop()
        self.qwidget.closed.connect(self._loop.quit)
        self.qwidget.destroyed.connect(self._loop.quit)
        visibility_check = QtCore.QTimer()
        visibility_check.setInterval(20)
        def quit_when_hidden() -> None:
            try:
                visible = self.qwidget.isVisible()
            except RuntimeError:
                visible = False
            if not visible:
                self._loop.quit()
        visibility_check.timeout.connect(quit_when_hidden)
        visibility_check.start()
        self._loop.exec_()
        visibility_check.stop()

    def geometry(self, value: str) -> None:
        match = re.match(r"(?:\d+x\d+)?\+(\d+)\+(\d+)", value)
        if match:
            self.qwidget.move(int(match.group(1)), int(match.group(2)))


class Frame(Misc):
    def __init__(self, parent: Misc, **kwargs: Any) -> None:
        super().__init__(parent, QtWidgets.QFrame(parent.qwidget), **kwargs)


class LabelFrame(Misc):
    def __init__(self, parent: Misc, text: str = "", **kwargs: Any) -> None:
        group = QtWidgets.QGroupBox(text, parent.qwidget)
        super().__init__(parent, group, **kwargs)
        px, py = self._padding
        self._padding = (px, (py[0] + 22, py[1]))
        group.setFont(_font(kwargs.get("font", ("Noto Sans CJK SC", 9))))


class Label(Misc):
    def __init__(self, parent: Misc, text: str = "", textvariable: Variable | None = None,
                 image: "PhotoImage | None" = None, compound: str | None = None,
                 anchor: str | None = None, justify: str | None = None,
                 wraplength: int | None = None, font: Any = None, width: int | None = None,
                 **kwargs: Any) -> None:
        label = QtWidgets.QLabel(parent.qwidget)
        super().__init__(parent, label, font=font, width=0, **kwargs)
        label.setFont(_font(font))
        label.setAlignment(_alignment(anchor))
        if wraplength:
            label.setWordWrap(True)
            label.setMaximumWidth(int(wraplength))
        if width:
            label.setMinimumWidth(int(width) * 8)
        self._image = image
        if image:
            label.setPixmap(image.pixmap)
        if textvariable:
            textvariable.subscribe(lambda value: label.setText(str(value)))
        else:
            label.setText(text)

    def configure(self, **kwargs: Any) -> None:
        if "text" in kwargs:
            self.qwidget.setText(str(kwargs["text"]))
        super().configure(**kwargs)


class Button(Misc):
    def __init__(self, parent: Misc, text: str = "", command: Callable[[], Any] | None = None,
                 image: "PhotoImage | None" = None, compound: str | None = None,
                 font: Any = None, width: int | None = None, anchor: str | None = None,
                 **kwargs: Any) -> None:
        button = QtWidgets.QPushButton(text, parent.qwidget)
        super().__init__(parent, button, font=font, width=0, **kwargs)
        self._command = command
        self._active_bg = kwargs.get("activebackground", kwargs.get("bg", "#e8e8ed"))
        self._image = image
        if image:
            button.setIcon(QtGui.QIcon(image.pixmap))
            button.setIconSize(image.pixmap.size())
        if font:
            button.setFont(_font(font))
        if width:
            button.setMinimumWidth(int(width) if image else int(width) * 8)
        if command:
            button.clicked.connect(lambda _checked=False: self._command())
        self._apply_button_style(kwargs)

    def _apply_button_style(self, kwargs: dict[str, Any] | None = None) -> None:
        kwargs = kwargs or {}
        bg = kwargs.get("bg", self._bg)
        fg = kwargs.get("fg", self._fg)
        active = kwargs.get("activebackground", self._active_bg)
        self.qwidget.setStyleSheet(
            "QPushButton {"
            f"background:{bg}; color:{fg}; border:1px solid {kwargs.get('highlightbackground', '#d2d2d7')};"
            "border-radius:8px; padding:7px 10px; text-align:center;}"
            f"QPushButton:hover {{background:{active};}}"
            "QPushButton:disabled {color:#a1a1a6; background:#f2f2f4;}"
        )

    def configure(self, **kwargs: Any) -> None:
        if "text" in kwargs:
            self.qwidget.setText(str(kwargs["text"]))
        if "command" in kwargs:
            try:
                self.qwidget.clicked.disconnect()
            except TypeError:
                pass
            self._command = kwargs["command"]
            self.qwidget.clicked.connect(lambda _checked=False: self._command())
        if "image" in kwargs:
            image = kwargs["image"]
            self._image = image
            self.qwidget.setIcon(QtGui.QIcon(image.pixmap) if image and hasattr(image, "pixmap") else QtGui.QIcon())
            if image and hasattr(image, "pixmap"):
                self.qwidget.setIconSize(image.pixmap.size())
        if "state" in kwargs:
            self.qwidget.setEnabled(kwargs["state"] != "disabled")
        if "font" in kwargs:
            self.qwidget.setFont(_font(kwargs["font"]))
        self._bg = kwargs.get("bg", self._bg)
        self._fg = kwargs.get("fg", self._fg)
        self._active_bg = kwargs.get("activebackground", self._active_bg)
        self._apply_button_style(kwargs)


class Radiobutton(Button):
    def __init__(self, parent: Misc, variable: Variable, value: Any,
                 command: Callable[[], Any] | None = None, **kwargs: Any) -> None:
        text = kwargs.pop("text", "")
        image = kwargs.pop("image", None)
        super().__init__(parent, text=text, image=image, command=None, **kwargs)
        self.variable = variable
        self.value = value
        self._command = command
        self.qwidget.setCheckable(True)
        self.qwidget.setChecked(variable.get() == value)
        self.qwidget.clicked.connect(self._selected)
        variable.subscribe(lambda current: self.qwidget.setChecked(current == value))

    def _selected(self) -> None:
        self.variable.set(self.value)
        if self._command:
            self._command()


class Entry(Misc):
    def __init__(self, parent: Misc, textvariable: Variable | None = None,
                 state: str = "normal", font: Any = None, width: int | None = None,
                 justify: str | None = None, **kwargs: Any) -> None:
        entry = QtWidgets.QLineEdit(parent.qwidget)
        super().__init__(parent, entry, font=font, width=0, **kwargs)
        self.variable = textvariable
        entry.setFont(_font(font))
        entry.setReadOnly(state == "readonly")
        if width:
            entry.setMinimumWidth(int(width) * 9)
        if justify == "right":
            entry.setAlignment(QtCore.Qt.AlignRight)
        if textvariable:
            textvariable.subscribe(lambda value: self._set_text(str(value)))
            entry.textEdited.connect(textvariable.set)
        entry.setStyleSheet(
            f"QLineEdit {{background:{kwargs.get('readonlybackground', kwargs.get('bg', '#ffffff'))};"
            f"color:{kwargs.get('fg', '#6e6e73')}; border:1px solid {kwargs.get('highlightbackground', '#d2d2d7')};"
            "border-radius:6px; padding:5px 7px;}"
        )

    def _set_text(self, value: str) -> None:
        if self.qwidget.text() != value:
            self.qwidget.setText(value)

    def get(self) -> str:
        return self.qwidget.text()

    def delete(self, _first: Any, _last: Any = None) -> None:
        self.qwidget.clear()

    def insert(self, _index: Any, value: str) -> None:
        self.qwidget.setText(value)


class Spinbox(Entry):
    def __init__(self, parent: Misc, from_: int = 0, to: int = 100,
                 textvariable: Variable | None = None, font: Any = None,
                 width: int | None = None, **kwargs: Any) -> None:
        spin = QtWidgets.QSpinBox(parent.qwidget)
        Misc.__init__(self, parent, spin, font=font, width=0, **kwargs)
        self.variable = textvariable
        spin.setRange(int(from_), int(to))
        spin.setFont(_font(font))
        if width:
            spin.setMinimumWidth(int(width) * 10)
        if textvariable:
            try:
                spin.setValue(int(textvariable.get()))
            except (TypeError, ValueError):
                pass
            spin.valueChanged.connect(lambda value: textvariable.set(str(value)))
            textvariable.subscribe(lambda value: spin.setValue(int(value or 0)))


class Scale(Misc):
    def __init__(self, parent: Misc, from_: int = 0, to: int = 100,
                 orient: str = "horizontal", variable: Variable | None = None,
                 command: Callable[[str], Any] | None = None, length: int | None = None,
                 **kwargs: Any) -> None:
        orientation = QtCore.Qt.Horizontal if orient == "horizontal" else QtCore.Qt.Vertical
        container = QtWidgets.QWidget(parent.qwidget)
        slider = QtWidgets.QSlider(orientation, container)
        super().__init__(parent, container, width=0, **kwargs)
        self.slider = slider
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 2, 0, 3)
        layout.setSpacing(1)
        number = QtWidgets.QLabel(container)
        number.setAlignment(QtCore.Qt.AlignRight)
        number.setFont(_font(kwargs.get("font")))
        layout.addWidget(number)
        layout.addWidget(slider)
        number.setVisible(bool(kwargs.get("showvalue", True)))
        slider.valueChanged.connect(lambda value: number.setText(str(value)))
        slider.setRange(int(from_), int(to))
        if length:
            slider.setMinimumWidth(int(length))
        if variable:
            slider.setValue(int(variable.get()))
            variable.subscribe(lambda value: slider.setValue(int(value)))
        def changed(value: int) -> None:
            if variable:
                variable.set(value)
            if command:
                command(str(value))
        slider.valueChanged.connect(changed)
        number.setText(str(slider.value()))
        slider.setStyleSheet(
            "QSlider::groove:horizontal {height:6px; background:#dedee3; border-radius:3px;}"
            "QSlider::sub-page:horizontal {background:#0071e3; border-radius:3px;}"
            "QSlider::handle:horizontal {width:16px; margin:-5px 0; background:white;"
            "border:1px solid #b8b8bd; border-radius:8px;}"
        )


class PhotoImage:
    def __init__(self, width: int, height: int) -> None:
        self.pixmap = QtGui.QPixmap(int(width), int(height))
        self.pixmap.fill(QtCore.Qt.transparent)

    def put(self, colour: str, to: tuple[int, int, int, int]) -> None:
        painter = QtGui.QPainter(self.pixmap)
        painter.fillRect(QtCore.QRect(to[0], to[1], to[2] - to[0], to[3] - to[1]), QtGui.QColor(colour))
        painter.end()


class _PaintCanvas(QtWidgets.QWidget):
    def __init__(self, width: int, height: int, background: str) -> None:
        super().__init__()
        self.setFixedSize(int(width), int(height))
        self.background = background
        self.items: dict[int, dict[str, Any]] = {}
        self.next_id = 1

    def add(self, kind: str, coords: tuple[int, ...], **options: Any) -> int:
        item = self.next_id
        self.next_id += 1
        self.items[item] = {"kind": kind, "coords": coords, **options}
        self.update()
        return item

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor(self.background))
        for item in self.items.values():
            if item.get("state") == "hidden":
                continue
            x1, y1, x2, y2 = item["coords"][:4]
            fill = QtGui.QColor(item.get("fill") or "transparent")
            outline = item.get("outline")
            painter.setBrush(fill)
            painter.setPen(QtGui.QPen(QtGui.QColor(outline)) if outline else QtCore.Qt.NoPen)
            rect = QtCore.QRectF(x1, y1, x2 - x1, y2 - y1)
            if item["kind"] == "rectangle":
                painter.drawRect(rect)
            elif item["kind"] == "oval":
                if item.get("rainbow"):
                    gradient = QtGui.QLinearGradient(rect.topLeft(), rect.topRight())
                    for index, colour in enumerate(("#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#00bcd4", "#007aff", "#af52de")):
                        gradient.setColorAt(index / 6, QtGui.QColor(colour))
                    painter.setBrush(QtGui.QBrush(gradient))
                painter.drawEllipse(rect)
            elif item["kind"] == "arc":
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.setPen(QtGui.QPen(fill, int(item.get("width", 1))))
                painter.drawArc(rect, int(item.get("start", 0) * 16), int(item.get("extent", 0) * 16))
        painter.end()


class Canvas(Misc):
    def __init__(self, parent: Misc, width: int, height: int, **kwargs: Any) -> None:
        canvas = _PaintCanvas(width, height, kwargs.get("bg", "transparent"))
        super().__init__(parent, canvas, width=0, height=0, **kwargs)

    def create_rectangle(self, *coords: int, **kwargs: Any) -> int:
        return self.qwidget.add("rectangle", tuple(coords), **kwargs)

    def create_oval(self, *coords: int, **kwargs: Any) -> int:
        return self.qwidget.add("oval", tuple(coords), **kwargs)

    def create_arc(self, *coords: int, **kwargs: Any) -> int:
        return self.qwidget.add("arc", tuple(coords), **kwargs)

    def delete(self, what: Any) -> None:
        if what == "all":
            self.qwidget.items.clear()
        else:
            self.qwidget.items.pop(int(what), None)
        self.qwidget.update()

    def itemconfigure(self, item: int, **kwargs: Any) -> None:
        if item in self.qwidget.items:
            self.qwidget.items[item].update(kwargs)
            self.qwidget.update()


class _MessageBox:
    @staticmethod
    def askyesno(title: str, text: str, **_kwargs: Any) -> bool:
        return QtWidgets.QMessageBox.question(None, title, text) == QtWidgets.QMessageBox.Yes

    @staticmethod
    def askokcancel(title: str, text: str, **_kwargs: Any) -> bool:
        return QtWidgets.QMessageBox.question(
            None, title, text, QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        ) == QtWidgets.QMessageBox.Ok

    @staticmethod
    def showinfo(title: str, text: str, **_kwargs: Any) -> None:
        QtWidgets.QMessageBox.information(None, title, text)


messagebox = _MessageBox()
