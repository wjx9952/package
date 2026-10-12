#!/usr/bin/env python3
"""Tests for Browser use permission detection shared by LCD and keyboard."""

import json
import tempfile
import unittest
from pathlib import Path

import codex_usage_monitor as monitor
import lcd_hat_dashboard as dashboard


def browser_call(code: str) -> dict:
    return {
        "type": "function_call",
        "name": "js",
        "namespace": "mcp__cua_repl",
        "arguments": json.dumps({"code": code, "title": "Browser operation"}),
        "call_id": "call-browser-test",
    }


class BrowserUseConfirmationTests(unittest.TestCase):
    def test_file_upload_requires_confirmation(self) -> None:
        payload = browser_call(
            "let chooser = await chooserP; "
            "await chooser.setFiles(['/tmp/example.txt']);"
        )
        self.assertEqual(monitor.browser_use_permission_kind(payload), "upload")
        self.assertTrue(monitor.call_needs_confirmation(payload))
        self.assertEqual(
            dashboard.confirmation_text(payload),
            ("Codex 需要确认", "浏览器请求上传文件"),
        )

    def test_read_only_browser_call_is_not_confirmation(self) -> None:
        payload = browser_call("nodeRepl.write(await page.playwright.domSnapshot());")
        self.assertEqual(monitor.browser_use_permission_kind(payload), "")
        self.assertFalse(monitor.call_needs_confirmation(payload))

    def test_non_browser_set_files_is_not_confirmation(self) -> None:
        payload = browser_call("await chooser.setFiles(['/tmp/example.txt']);")
        payload["namespace"] = "other_tool"
        self.assertEqual(monitor.browser_use_permission_kind(payload), "")
        self.assertFalse(monitor.call_needs_confirmation(payload))

    def test_upload_output_clears_waiting_state(self) -> None:
        payload = browser_call("await chooser.setFiles(['/tmp/example.txt']);")
        started = {
            "timestamp": "2099-10-12T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        }
        call = {
            "timestamp": "2099-10-12T00:00:01Z",
            "type": "response_item",
            "payload": payload,
        }
        output = {
            "timestamp": "2099-10-12T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": payload["call_id"],
                "output": "allowed",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "rollout-test.jsonl"
            session.write_text(
                "".join(json.dumps(event) + "\n" for event in (started, call)),
                encoding="utf-8",
            )
            self.assertEqual(monitor.read_codex_status(session).code, "waiting_confirmation")
            with session.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(output) + "\n")
            self.assertEqual(monitor.read_codex_status(session).code, "processing")


if __name__ == "__main__":
    unittest.main()
