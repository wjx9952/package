#!/usr/bin/env python3
"""Unit tests for user-facing Key1 single/double-tap decision timing."""

import unittest
from unittest import mock
import fcntl
import os
import signal
import tempfile
from pathlib import Path

import lcd_hat_dashboard as dashboard


class Key1DecisionTrackerTests(unittest.TestCase):
    def test_single_tap_confirms_after_window(self) -> None:
        tracker = dashboard.Key1DecisionTracker(window=0.4)
        self.assertIsNone(tracker.tap(10.0))
        self.assertIsNone(tracker.poll(10.399))
        self.assertEqual(tracker.poll(10.4), "confirm")
        self.assertIsNone(tracker.poll(11.0))

    def test_two_quick_taps_reject(self) -> None:
        tracker = dashboard.Key1DecisionTracker(window=0.4)
        self.assertIsNone(tracker.tap(10.0))
        self.assertEqual(tracker.tap(10.25), "reject")
        self.assertIsNone(tracker.poll(11.0))

    def test_late_second_tap_starts_new_gesture(self) -> None:
        tracker = dashboard.Key1DecisionTracker(window=0.4)
        self.assertIsNone(tracker.tap(10.0))
        self.assertEqual(tracker.poll(10.4), "confirm")
        self.assertIsNone(tracker.tap(10.5))
        self.assertEqual(tracker.poll(10.95), "confirm")

    def test_second_tap_outside_window_is_not_rejection(self) -> None:
        tracker = dashboard.Key1DecisionTracker(window=0.4)
        self.assertIsNone(tracker.tap(10.0))
        self.assertIsNone(tracker.tap(10.5))
        self.assertEqual(tracker.poll(10.95), "confirm")


class DesktopLaunchTests(unittest.TestCase):
    @mock.patch.object(dashboard, "signal_single_instance", return_value=True)
    @mock.patch.object(dashboard, "run_desktop_command")
    def test_hidden_single_instance_reports_focused(
        self,
        run_desktop_command,
        _signal_single_instance,
    ) -> None:
        run_desktop_command.return_value.returncode = 1
        helper = mock.Mock()
        helper.is_file.return_value = True
        helper.__str__ = mock.Mock(return_value="/tmp/wlrctl")
        with mock.patch.object(dashboard, "WLRCTL_PATH", helper):
            result = dashboard.focus_or_launch_window(
                "title:Pi 500+ 键盘控制中心",
                ["/tmp/run.sh"],
                "codex-rgb-keyboard.lock",
            )
        self.assertEqual(result, "Focused")

    def test_signal_single_instance_reveals_hidden_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "app.lock"
            with lock_path.open("w+") as owner:
                owner.write(str(os.getpid()))
                owner.flush()
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(dashboard.os, "kill") as kill:
                    self.assertTrue(dashboard.signal_single_instance(lock_path))
                kill.assert_called_once_with(os.getpid(), signal.SIGUSR1)

    def test_signal_single_instance_ignores_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "app.lock"
            lock_path.write_text("1234", encoding="utf-8")
            with mock.patch.object(dashboard.os, "kill") as kill:
                self.assertFalse(dashboard.signal_single_instance(lock_path))
            kill.assert_not_called()

    def test_desktop_environment_adds_session_bus_address(self) -> None:
        with mock.patch.dict(
            dashboard.os.environ,
            {
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "WAYLAND_DISPLAY": "wayland-1",
            },
            clear=True,
        ):
            environment = dashboard.desktop_environment()
        self.assertEqual(
            environment["DBUS_SESSION_BUS_ADDRESS"],
            "unix:path=/run/user/1000/bus",
        )

    @mock.patch.object(dashboard.time, "sleep")
    @mock.patch.object(dashboard.subprocess, "Popen")
    @mock.patch.object(dashboard.subprocess, "run")
    @mock.patch.object(dashboard, "desktop_environment")
    def test_direct_launch_fallback_when_systemd_run_fails(
        self,
        environment,
        run,
        popen,
        _sleep,
    ) -> None:
        environment.return_value = {"WAYLAND_DISPLAY": "wayland-1"}
        run.return_value.returncode = 1
        popen.return_value.poll.return_value = None
        dashboard.launch_desktop_app(["/tmp/app"])
        popen.assert_called_once_with(
            ["/tmp/app"],
            env={"WAYLAND_DISPLAY": "wayland-1"},
            start_new_session=True,
            stdout=dashboard.subprocess.DEVNULL,
            stderr=dashboard.subprocess.DEVNULL,
        )


class LockScreenTests(unittest.TestCase):
    @mock.patch.object(dashboard, "set_windows_session_locked")
    @mock.patch.object(dashboard, "bt_keyboard_request")
    def test_windows_lock_is_always_sent_and_recorded(
        self,
        bt_keyboard_request,
        set_windows_session_locked,
    ) -> None:
        bt_keyboard_request.return_value = {"ok": True}
        dashboard.request_windows_lock()
        bt_keyboard_request.assert_called_once_with("lock_windows")
        set_windows_session_locked.assert_called_once_with(True)

    @mock.patch.object(dashboard, "set_windows_session_locked")
    @mock.patch.object(dashboard, "bt_keyboard_request")
    def test_failed_windows_lock_is_not_recorded(
        self,
        bt_keyboard_request,
        set_windows_session_locked,
    ) -> None:
        bt_keyboard_request.return_value = {"ok": False, "error": "offline"}
        with self.assertRaisesRegex(RuntimeError, "offline"):
            dashboard.request_windows_lock()
        set_windows_session_locked.assert_not_called()

    def test_clean_early_swaylock_exit_is_failure(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 0
        with self.assertRaisesRegex(RuntimeError, r"exited before locking \(0\)"):
            dashboard.wait_for_raspberry_pi_lock(
                process,
                is_locked=lambda: False,
            )

    def test_running_swaylock_must_be_observable(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        locked = mock.Mock(side_effect=[False, True])
        with mock.patch.object(dashboard.time, "sleep"):
            dashboard.wait_for_raspberry_pi_lock(process, is_locked=locked)
        self.assertEqual(locked.call_count, 2)

    def test_display_standby_only_runs_inside_lock_cycle(self) -> None:
        self.assertEqual(
            dashboard.locked_display_policy(False, False, 0.0, 10.0),
            (False, "lock_cycle_inactive"),
        )
        self.assertEqual(
            dashboard.locked_display_policy(True, False, 0.0, 10.0),
            (True, "standby"),
        )

    def test_keyboard_activity_temporarily_keeps_display_awake(self) -> None:
        self.assertEqual(
            dashboard.locked_display_policy(True, False, 20.0, 10.0),
            (False, "keyboard_activity"),
        )

    def test_locked_feedback_is_shown_before_standby(self) -> None:
        self.assertEqual(
            dashboard.locked_display_policy(
                True, False, 0.0, 10.0, standby_not_before=12.5
            ),
            (False, "lock_feedback"),
        )
        self.assertEqual(
            dashboard.locked_display_policy(
                True, False, 0.0, 12.5, standby_not_before=12.5
            ),
            (True, "standby"),
        )

    def test_either_real_unlock_ends_current_cycle(self) -> None:
        self.assertTrue(
            dashboard.lock_cycle_session_unlocked(True, True, False, True)
        )
        self.assertTrue(
            dashboard.lock_cycle_session_unlocked(True, True, True, False)
        )
        self.assertFalse(
            dashboard.lock_cycle_session_unlocked(True, True, True, True)
        )
        self.assertFalse(
            dashboard.lock_cycle_session_unlocked(False, True, False, True)
        )

if __name__ == "__main__":
    unittest.main()
