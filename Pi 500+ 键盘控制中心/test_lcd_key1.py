#!/usr/bin/env python3
"""Unit tests for user-facing Key1 single/double-tap decision timing."""

import unittest
from unittest import mock

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

if __name__ == "__main__":
    unittest.main()
