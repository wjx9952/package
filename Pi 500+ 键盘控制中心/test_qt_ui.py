"""Run with QT_QPA_PLATFORM=offscreen python3 -m unittest test_qt_ui."""
import threading
import unittest
from PyQt5 import QtCore, QtWidgets
import qt_tk_compat as ui
from codex_rgb_keyboard import CodexRGBApp, IDLE_EFFECTS


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
            effect, _hue, _sat, _speed, label, _color = app.lighting_for("idle")
            self.assertEqual(effect, IDLE_EFFECTS[key][0])
            self.assertIn(IDLE_EFFECTS[key][2], label)
        self.assertFalse(CodexRGBApp.valid_state_color("idle", "cycle_left_right"))


if __name__ == "__main__":
    unittest.main()
