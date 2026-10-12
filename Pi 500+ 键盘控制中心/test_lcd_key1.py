#!/usr/bin/env python3
"""Unit tests for user-facing Key1 single/double-tap decision timing."""

import unittest

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


if __name__ == "__main__":
    unittest.main()
