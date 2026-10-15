"""Run with QT_QPA_PLATFORM=offscreen python3 -m unittest test_qt_ui."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from PyQt5 import QtCore, QtWidgets
import qt_tk_compat as ui
import codex_rgb_keyboard as keyboard_app
from codex_rgb_keyboard import (
    COLOR_ADJUSTABLE_IDLE_EFFECTS,
    CodexRGBApp,
    IDLE_EFFECTS,
)


class QtRegressionTests(unittest.TestCase):
    def setUp(self):
        self.root = ui.Tk()

    def tearDown(self):
        ui.Misc.destroy(self.root)
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

    def test_click_does_not_replace_default_argument(self):
        seen = []
        button = ui.Button(self.root, command=lambda state="idle": seen.append(state))
        button.qwidget.click()
        button.configure(command=lambda state="completed": seen.append(state))
        button.qwidget.click()
        self.assertEqual(seen, ["idle", "completed"])

    def test_worker_callback_runs_on_gui_thread_and_can_be_cancelled(self):
        seen = []
        token = self.root.after(10, lambda: seen.append("cancelled"))
        self.root.after_cancel(token)
        worker = threading.Thread(target=lambda: self.root.after(
            0, lambda: seen.append(QtCore.QThread.currentThread() == self.root.app.thread())))
        worker.start()
        worker.join()
        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(80, loop.quit)
        loop.exec_()
        self.assertEqual(seen, [True])

    def test_hiding_rows_does_not_accumulate_spacing(self):
        panel = ui.Frame(self.root)
        panel.pack()
        row = ui.Frame(panel)
        row.pack(pady=5)
        count = panel._layout.count()
        for _ in range(10):
            row.pack_forget()
            row.pack(pady=5)
        self.assertEqual(panel._layout.count(), count)

    def test_tray_lifetime_is_explicit(self):
        self.assertFalse(self.root.app.quitOnLastWindowClosed())
        self.root.iconify()
        self.root.deiconify()

    def test_idle_effect_catalog_contains_every_non_rainbow_effect(self):
        excluded_control_or_rainbow = {0, 1, 5, *range(13, 21), 22, 23}
        expected = set(range(45)) - excluded_control_or_rainbow - {2}
        self.assertEqual({entry[0] for entry in IDLE_EFFECTS.values()}, expected)
        for key in IDLE_EFFECTS:
            self.assertTrue(CodexRGBApp.valid_state_color("idle", key))
            swatch = CodexRGBApp.color_swatch(key)
            self.assertEqual((swatch.pixmap.width(), swatch.pixmap.height()), (28, 14))
            app = CodexRGBApp.__new__(CodexRGBApp)
            app.state_colors = {"idle": key}
            app.state_effect_colors = {"idle": "#2081ee"}
            effect, _hue, _sat, _speed, label, _color = app.lighting_for("idle")
            self.assertEqual(effect, IDLE_EFFECTS[key][0])
            self.assertIn(IDLE_EFFECTS[key][2], label)
        self.assertFalse(CodexRGBApp.valid_state_color("idle", "cycle_left_right"))

    def test_keyboard_mode_accepts_every_idle_effect(self):
        app = CodexRGBApp.__new__(CodexRGBApp)
        app.state_effect_colors = {"keyboard_mode": "#34c759"}
        for key, (expected_effect, _speed, name, _description) in IDLE_EFFECTS.items():
            self.assertTrue(CodexRGBApp.valid_state_color("keyboard_mode", key))
            app.state_colors = {"keyboard_mode": key}
            effect, _hue, _sat, _speed, label, _color = app.lighting_for(
                "keyboard_mode"
            )
            self.assertEqual(effect, expected_effect)
            self.assertIn(name, label)
        self.assertFalse(
            CodexRGBApp.valid_state_color("keyboard_mode", "cycle_left_right")
        )

    def test_adjustable_effect_uses_saved_base_colour(self):
        app = CodexRGBApp.__new__(CodexRGBApp)
        app.state_colors = {
            "idle": "breathing",
            "keyboard_mode": "solid_reactive",
        }
        app.state_effect_colors = {
            "idle": "#ff0000",
            "keyboard_mode": "#0000ff",
        }
        idle = app.lighting_for("idle")
        keyboard = app.lighting_for("keyboard_mode")
        self.assertEqual(idle[1:3], (0, 255))
        self.assertEqual(keyboard[1:3], (170, 255))

    def test_self_colouring_effects_disable_base_colour(self):
        automatic = {
            "jellybean_raindrops",
            "hue_breathing",
            "hue_pendulum",
            "hue_wave",
            "typing_heatmap",
            "splash",
            "multisplash",
        }
        self.assertTrue(automatic.isdisjoint(COLOR_ADJUSTABLE_IDLE_EFFECTS))
        self.assertEqual(
            COLOR_ADJUSTABLE_IDLE_EFFECTS | automatic,
            set(IDLE_EFFECTS),
        )

    def test_existing_solid_colours_migrate_to_effect_colours(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(
                json.dumps({
                    "state_colors": {
                        "idle": "#2081ee",
                        "keyboard_mode": "#34c759",
                    }
                }),
                encoding="utf-8",
            )
            with mock.patch.object(keyboard_app, "SETTINGS_FILE", settings):
                _brightness, _colours, effect_colours = (
                    CodexRGBApp.load_settings()
                )
        self.assertEqual(effect_colours["idle"], "#2081ee")
        self.assertEqual(effect_colours["keyboard_mode"], "#34c759")

    def test_keyboard_mode_applies_same_hue_and_saturation_as_preview(self):
        app = object.__new__(CodexRGBApp)
        app.state_colors = {"keyboard_mode": "breathing"}
        app.state_effect_colors = {"keyboard_mode": "#8090a0"}
        effect, hue, saturation, speed, _label, _colour = app.lighting_for(
            "keyboard_mode"
        )
        expected_hue, expected_saturation = app.color_to_hs("#8090a0")
        self.assertEqual((effect, speed), IDLE_EFFECTS["breathing"][:2])
        self.assertEqual((hue, saturation), (expected_hue, expected_saturation))


if __name__ == "__main__":
    unittest.main()
