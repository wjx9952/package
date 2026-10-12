#!/usr/bin/env python3
"""Unit tests for Bambu MQTT state parsing and the LCD page."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

import bambu_status_monitor as monitor
import lcd_hat_dashboard as dashboard


def sample_payload(state: str = "RUNNING") -> dict:
    return {
        "connected": True,
        "printer_ip": "192.168.1.88",
        "updated_at": 1_000.0,
        "status_changed_at": 995.0,
        "print": {
            "gcode_state": state,
            "subtask_name": "H2D Monitor Case",
            "mc_percent": 63,
            "layer_num": 128,
            "total_layer_num": 204,
            "nozzle_temper": 220,
            "nozzle_target_temper": 220,
            "bed_temper": 55,
            "bed_target_temper": 55,
            "mc_remaining_time": 102,
            "ams": {
                "tray_now": "2",
                "ams": [{
                    "tray": [{
                        "id": "2",
                        "tray_type": "PLA",
                        "tray_color": "5F6367FF",
                    }]
                }],
            },
        },
    }


class BambuStatusTests(unittest.TestCase):
    def test_running_job_and_active_filament_are_parsed(self) -> None:
        status = dashboard.bambu_status_from_payload(sample_payload(), now=1_001.0)
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "PRINTING")
        self.assertEqual(status["status_colour"], (255, 149, 0))
        self.assertEqual(status["printer_ip"], "192.168.1.88")
        self.assertEqual(status["job_name"], "H2D Monitor Case")
        self.assertEqual(status["percent"], 63)
        self.assertEqual((status["layer"], status["total_layers"]), (128, 204))
        self.assertEqual(status["material"], "PLA")
        self.assertEqual(status["colour_name"], "Gray")
        self.assertEqual(status["filament_colour"], (95, 99, 103))

    def test_job_name_preserves_real_filename_and_cjk(self) -> None:
        payload = sample_payload()
        payload["print"]["subtask_name"] = "理线神器 数据线收纳.3mf"
        status = dashboard.bambu_status_from_payload(payload, now=1_001.0)
        self.assertEqual(status["job_name"], "理线神器 数据线收纳.3mf")

    def test_remaining_time_uses_compact_english_label(self) -> None:
        self.assertEqual(dashboard._bambu_remaining_text(102), "1h 42m left")
        self.assertEqual(dashboard._bambu_remaining_text(8), "8m left")

    def test_state_colours_match_the_confirmed_design(self) -> None:
        for raw, label, colour in (
            ("RUNNING", "PRINTING", (255, 149, 0)),
            ("PAUSE", "PAUSED", (255, 69, 58)),
            ("FAILED", "ERROR", (255, 69, 58)),
            ("FINISH", "COMPLETED", (52, 199, 89)),
        ):
            status = dashboard.bambu_status_from_payload(
                sample_payload(raw), now=1_001.0
            )
            self.assertEqual((status["status"], status["status_colour"]), (label, colour))

    def test_stale_active_state_becomes_error(self) -> None:
        status = dashboard.bambu_status_from_payload(sample_payload(), now=1_100.1)
        self.assertEqual(status["status"], "ERROR")

    def test_completed_page_expires_after_ten_minutes(self) -> None:
        status = dashboard.bambu_status_from_payload(
            sample_payload("FINISH"), now=1_001.0
        )
        self.assertTrue(dashboard.bambu_page_active(status, now=1_594.0))
        self.assertFalse(dashboard.bambu_page_active(status, now=1_596.0))

    def test_bambu_page_renders_at_lcd_resolution(self) -> None:
        status = dashboard.bambu_status_from_payload(sample_payload(), now=1_001.0)
        image = dashboard.render_bambu_status(status)
        self.assertEqual(image.size, (240, 240))
        self.assertGreater(len(image.getcolors(maxcolors=100_000)), 10)

    def test_bambu_page_uses_live_camera_frame_when_available(self) -> None:
        status = dashboard.bambu_status_from_payload(sample_payload(), now=1_001.0)
        with TemporaryDirectory() as directory:
            frame = Path(directory) / "camera.jpg"
            Image.new("RGB", (120, 80), (231, 17, 199)).save(frame)
            image = dashboard.render_bambu_status(status, camera_frame_path=frame)
        colours = image.getcolors(maxcolors=100_000)
        self.assertIsNotNone(colours)
        self.assertTrue(any(red > 180 and blue > 150 for _, (red, _green, blue) in colours))

    def test_monitor_deep_merge_keeps_unchanged_fields(self) -> None:
        state = {"print": {"gcode_state": "RUNNING", "mc_percent": 10}}
        monitor.deep_merge(state, {"print": {"mc_percent": 11}})
        self.assertEqual(
            state,
            {"print": {"gcode_state": "RUNNING", "mc_percent": 11}},
        )


if __name__ == "__main__":
    unittest.main()
