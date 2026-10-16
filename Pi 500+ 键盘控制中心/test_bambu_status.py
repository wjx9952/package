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
            ("IDLE", "IDLE", (47, 128, 237)),
            ("RUNNING", "PRINTING", (255, 149, 0)),
            ("PAUSE", "PAUSED", (255, 69, 58)),
            ("FAILED", "ERROR", (255, 69, 58)),
            ("FINISH", "COMPLETED", (52, 199, 89)),
        ):
            status = dashboard.bambu_status_from_payload(
                sample_payload(raw), now=1_001.0
            )
            self.assertEqual((status["status"], status["status_colour"]), (label, colour))

    def test_user_facing_printer_status_labels(self) -> None:
        self.assertEqual(dashboard._bambu_display_status("IDLE"), "Idle")
        self.assertEqual(dashboard._bambu_display_status("PRINTING"), "Printing")
        self.assertEqual(dashboard._bambu_display_status("PAUSED"), "Paused")
        self.assertEqual(dashboard._bambu_display_status("ERROR"), "Error")
        self.assertEqual(dashboard._bambu_display_status("COMPLETED"), "Completed")
        self.assertEqual(dashboard.NOZZLE_LABEL_GAP, 5)

    def test_stale_active_state_becomes_error(self) -> None:
        status = dashboard.bambu_status_from_payload(sample_payload(), now=1_100.1)
        self.assertEqual(status["status"], "ERROR")

    def test_printer_page_is_active_during_work_and_one_hour_after_finish(self) -> None:
        printing = dashboard.bambu_status_from_payload(
            sample_payload("RUNNING"), now=1_001.0
        )
        self.assertTrue(dashboard.bambu_page_active(printing, now=99_999.0))
        payload = sample_payload("FINISH")
        payload["status_changed_at"] = 1_000.0
        completed = dashboard.bambu_status_from_payload(payload, now=1_001.0)
        self.assertTrue(dashboard.bambu_page_active(completed, now=4_600.0))
        self.assertFalse(dashboard.bambu_page_active(completed, now=4_600.1))
        self.assertFalse(dashboard.bambu_page_active(None, now=4_600.1))
        self.assertEqual(dashboard.DASHBOARD_PAGE_SECONDS, 10.0)

    def test_idle_printer_does_not_participate_in_carousel(self) -> None:
        idle = dashboard.bambu_status_from_payload(
            sample_payload("IDLE"), now=1_001.0
        )
        self.assertFalse(dashboard.bambu_page_active(idle, now=1_001.0))

    def test_missing_printer_data_has_offline_page(self) -> None:
        status = dashboard.bambu_status_for_display(None)
        self.assertEqual(status["status"], "ERROR")
        self.assertEqual(status["status_colour"], (255, 69, 58))
        self.assertEqual(status["job_name"], "Printer Offline")

    def test_completed_job_becomes_idle_after_thirty_minutes(self) -> None:
        payload = sample_payload("FINISH")
        payload["status_changed_at"] = 1_000.0
        completed = dashboard.bambu_status_from_payload(payload, now=2_800.0)
        idle = dashboard.bambu_status_from_payload(payload, now=2_800.1)
        self.assertEqual(
            (completed["status"], completed["status_colour"]),
            ("COMPLETED", (52, 199, 89)),
        )
        self.assertEqual(
            (idle["status"], idle["status_colour"]),
            ("IDLE", (47, 128, 237)),
        )

    def test_bambu_page_renders_at_lcd_resolution(self) -> None:
        status = dashboard.bambu_status_from_payload(sample_payload(), now=1_001.0)
        image = dashboard.render_bambu_status(status)
        self.assertEqual(image.size, (240, 240))
        self.assertGreater(len(image.getcolors(maxcolors=100_000)), 10)

    def test_dual_nozzle_temperatures_use_telemetry_ids(self) -> None:
        payload = sample_payload()
        payload["print"]["device"] = {"extruder": {"info": [
            {"id": 1, "temp": (210 << 16) | 205},
            {"id": 0, "temp": (230 << 16) | 228},
        ]}}
        status = dashboard.bambu_status_from_payload(payload, now=1_001.0)
        self.assertEqual((status["nozzle_left"], status["nozzle_left_target"]), (205, 210))
        self.assertEqual((status["nozzle_right"], status["nozzle_right_target"]), (228, 230))

    def test_bambu_page_no_longer_displays_camera_frame(self) -> None:
        status = dashboard.bambu_status_from_payload(sample_payload(), now=1_001.0)
        with TemporaryDirectory() as directory:
            frame = Path(directory) / "camera.jpg"
            Image.new("RGB", (120, 80), (231, 17, 199)).save(frame)
            image = dashboard.render_bambu_status(status, camera_frame_path=frame)
        without_camera = dashboard.render_bambu_status(status, camera_frame_path=Path("/missing-camera.jpg"))
        self.assertEqual(image.tobytes(), without_camera.tobytes())

    def test_monitor_deep_merge_keeps_unchanged_fields(self) -> None:
        state = {"print": {"gcode_state": "RUNNING", "mc_percent": 10}}
        monitor.deep_merge(state, {"print": {"mc_percent": 11}})
        self.assertEqual(
            state,
            {"print": {"gcode_state": "RUNNING", "mc_percent": 11}},
        )


if __name__ == "__main__":
    unittest.main()
