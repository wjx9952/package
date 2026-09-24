#!/usr/bin/env python3
"""Expose the logged-in Codex CLI rate limits as a tiny read-only HTTP API."""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import threading
import time
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MONITOR_MFG_ID = "AOC"
MONITOR_MODEL = "Q27V6L"
PI_INPUT_SOURCE = 0x11
WINDOWS_INPUT_SOURCE = 0x0F
INPUT_SOURCE_NAMES = {
    PI_INPUT_SOURCE: "HDMI",
    WINDOWS_INPUT_SOURCE: "DisplayPort",
}
DDC_TIMEOUT_SECONDS = 15
WINDOWS_SESSION_FILE = Path(__file__).resolve().parent / "windows_session_state.json"


class WindowsSessionState:
    """Receive Windows workstation lock/unlock notifications."""

    def __init__(self) -> None:
        self.lock = threading.Lock()

    @staticmethod
    def _read() -> dict:
        try:
            data = json.loads(WINDOWS_SESSION_FILE.read_text(encoding="utf-8"))
            return {
                "ok": True,
                "locked": bool(data.get("locked", False)),
                "updated_at": int(data.get("updated_at", 0)),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": True, "locked": False, "updated_at": 0}

    def status(self) -> dict:
        with self.lock:
            return self._read()

    def set_locked(self, locked: bool) -> dict:
        payload = {"locked": bool(locked), "updated_at": int(time.time())}
        with self.lock:
            temporary = WINDOWS_SESSION_FILE.with_name(
                WINDOWS_SESSION_FILE.name + f".{os.getpid()}.tmp"
            )
            temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            os.replace(temporary, WINDOWS_SESSION_FILE)
        return {"ok": True, **payload}


def resolve_codex_command(command: str) -> str:
    """Return an executable Codex CLI path, tolerating VS Code extension updates."""
    if (os.path.isabs(command) or "/" in command) and os.path.exists(command):
        return command
    if not os.path.isabs(command) and "/" not in command:
        found = shutil.which(command)
        if found:
            return found

    candidates = []
    for pattern in (
        Path.home() / ".vscode/extensions/openai.chatgpt-*/bin/linux-*/codex",
        Path.home() / ".vscode-server/extensions/openai.chatgpt-*/bin/linux-*/codex",
    ):
        candidates.extend(glob.glob(str(pattern)))
    candidates.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    raise FileNotFoundError(
        f"Codex CLI not found; configured path was {command!r}"
    )


class CodexAppServer:
    def __init__(self, command: str = "codex") -> None:
        self.command = command
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self.next_id = 1

    def _start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        command = resolve_codex_command(self.command)
        self.process = subprocess.Popen(
            [command, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-epaper-quota",
                    "title": "Codex e-paper quota monitor",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            start=False,
        )
        self._write({"method": "initialized", "params": {}})

    def _write(self, message: dict) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _request(self, method: str, params, *, start: bool = True) -> dict:
        if start:
            self._start()
        assert self.process and self.process.stdout
        request_id = self.next_id
        self.next_id += 1
        self._write({"id": request_id, "method": method, "params": params})
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Codex app-server closed unexpectedly")
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(message["error"].get("message", str(message["error"])))
            return message["result"]

    def rate_limits(self) -> dict:
        with self.lock:
            try:
                return self._request("account/rateLimits/read", None)
            except (BrokenPipeError, RuntimeError, json.JSONDecodeError):
                if self.process:
                    self.process.kill()
                self.process = None
                return self._request("account/rateLimits/read", None)


class DdcError(RuntimeError):
    """A controlled failure while discovering or operating the AOC monitor."""


class AocMonitor:
    """Find and control only the configured AOC monitor through DDC/CI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.display_number: int | None = None
        self.bus_number: int | None = None
        self.brightness_value: int | None = None
        self.input_value: int | None = None

    @staticmethod
    def _run(arguments: list[str]) -> str:
        command = shutil.which("ddcutil")
        if not command:
            raise DdcError("ddcutil is not installed or not in PATH")
        try:
            result = subprocess.run(
                [command, *arguments],
                capture_output=True,
                text=True,
                timeout=DDC_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DdcError(
                f"ddcutil timed out after {DDC_TIMEOUT_SECONDS} seconds"
            ) from exc
        except OSError as exc:
            raise DdcError(f"could not execute ddcutil: {exc}") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise DdcError(
                f"ddcutil exited with status {result.returncode}: "
                f"{detail or 'no error details'}"
            )
        return result.stdout

    def _detect(self) -> None:
        output = self._run(["detect"])
        blocks = re.split(r"(?=^Display\s+\d+\s*$)", output, flags=re.MULTILINE)
        matches: list[tuple[int, int]] = []
        for block in blocks:
            display_match = re.search(r"^Display\s+(\d+)\s*$", block, re.MULTILINE)
            bus_match = re.search(r"I2C bus:\s*/dev/i2c-(\d+)", block)
            mfg_match = re.search(r"Mfg id:\s*([^\r\n]+)", block)
            model_match = re.search(r"Model:\s*([^\r\n]+)", block)
            if not (display_match and bus_match and mfg_match and model_match):
                continue
            mfg = mfg_match.group(1).strip().split()[0]
            model = model_match.group(1).strip()
            if mfg == MONITOR_MFG_ID and model == MONITOR_MODEL:
                matches.append((int(display_match.group(1)), int(bus_match.group(1))))

        if len(matches) != 1:
            raise DdcError(
                f"expected exactly one {MONITOR_MFG_ID} {MONITOR_MODEL}, "
                f"found {len(matches)}"
            )
        self.display_number, self.bus_number = matches[0]

    def _targeted(self, arguments: list[str]) -> str:
        last_error: DdcError | None = None
        for attempt in range(2):
            if self.bus_number is None:
                self._detect()
            assert self.bus_number is not None
            try:
                return self._run(["--bus", str(self.bus_number), *arguments])
            except DdcError as exc:
                last_error = exc
                self.display_number = None
                self.bus_number = None
                if attempt == 0:
                    continue
        assert last_error is not None
        raise last_error

    def _get_input_unlocked(self) -> dict:
        output = self._targeted(["getvcp", "60"])
        value_match = re.search(r"\bsl=0x([0-9a-fA-F]+)\b", output)
        if not value_match:
            raise DdcError(f"could not parse VCP 0x60 response: {output.strip()}")
        value = int(value_match.group(1), 16)
        self.input_value = value
        name_match = re.search(r"Input Source[^:]*\):\s*(.*?)\s*\(sl=", output)
        name = INPUT_SOURCE_NAMES.get(value) or (
            name_match.group(1).strip() if name_match else "Unknown"
        )
        return {
            "value": value,
            "hex": f"0x{value:02x}",
            "name": name,
        }

    def status(self) -> dict:
        with self.lock:
            current = self._get_input_unlocked()
            return {
                "ok": True,
                "monitor": f"{MONITOR_MFG_ID} {MONITOR_MODEL}",
                "display": self.display_number,
                "bus": f"/dev/i2c-{self.bus_number}",
                "current_value": current["value"],
                "current_hex": current["hex"],
                "current_name": current["name"],
            }

    def _get_brightness_unlocked(self) -> int:
        output = self._targeted(["getvcp", "10"])
        value_match = re.search(r"current value\s*=\s*(\d+)", output)
        if not value_match:
            raise DdcError(f"could not parse VCP 0x10 response: {output.strip()}")
        self.brightness_value = max(0, min(100, int(value_match.group(1))))
        return self.brightness_value

    def brightness(self) -> dict:
        with self.lock:
            value = self._get_brightness_unlocked()
            return {
                "ok": True,
                "monitor": f"{MONITOR_MFG_ID} {MONITOR_MODEL}",
                "brightness": value,
            }

    def cached_brightness(self) -> dict:
        """Return the latest known value without waiting for a DDC round trip."""
        value = self.brightness_value
        return {
            "ok": value is not None,
            "monitor": f"{MONITOR_MFG_ID} {MONITOR_MODEL}",
            "brightness": value,
        }

    def adjust_brightness(self, delta: int) -> dict:
        with self.lock:
            previous = (
                self.brightness_value
                if self.brightness_value is not None
                else self._get_brightness_unlocked()
            )
            target = max(0, min(100, previous + delta))
            if target != previous:
                self._targeted(["setvcp", "10", str(target), "--noverify"])
                self.brightness_value = target
            return {
                "ok": True,
                "monitor": f"{MONITOR_MFG_ID} {MONITOR_MODEL}",
                "from": previous,
                "brightness": target,
            }

    def set_input(self, target_value: int) -> dict:
        """Select a known input directly without a preliminary DDC read."""
        if target_value not in INPUT_SOURCE_NAMES:
            raise ValueError(f"unsupported input 0x{target_value:02x}")
        with self.lock:
            previous_value = self.input_value
            previous = {
                "value": previous_value,
                "hex": f"0x{previous_value:02x}" if previous_value is not None else "--",
                "name": INPUT_SOURCE_NAMES.get(previous_value, "Unknown"),
            }
            if target_value != previous_value:
                self._targeted(
                    ["setvcp", "60", f"0x{target_value:02x}", "--noverify"]
                )
                self.input_value = target_value
            current = {
                "value": target_value,
                "hex": f"0x{target_value:02x}",
                "name": INPUT_SOURCE_NAMES[target_value],
            }
            return {
                "ok": True,
                "monitor": f"{MONITOR_MFG_ID} {MONITOR_MODEL}",
                "display": self.display_number,
                "bus": f"/dev/i2c-{self.bus_number}",
                "from": previous,
                "to": current,
            }

    def toggle(self) -> dict:
        with self.lock:
            if self.input_value is None:
                previous = self._get_input_unlocked()
            else:
                previous = {
                    "value": self.input_value,
                    "hex": f"0x{self.input_value:02x}",
                    "name": INPUT_SOURCE_NAMES.get(self.input_value, "Unknown"),
                }
            if previous["value"] == PI_INPUT_SOURCE:
                target_value = WINDOWS_INPUT_SOURCE
            elif previous["value"] == WINDOWS_INPUT_SOURCE:
                target_value = PI_INPUT_SOURCE
            else:
                allowed = ", ".join(
                    f"0x{value:02x}" for value in (PI_INPUT_SOURCE, WINDOWS_INPUT_SOURCE)
                )
                raise ValueError(
                    f"current input {previous['hex']} is not one of {allowed}"
                )

            # Do not ask ddcutil to verify after changing input. Some monitors
            # briefly stop answering DDC/CI while the new source is selected,
            # even though the switch itself succeeded. That made a valid KEY3
            # press look like a failure and added several seconds of latency.
            self._targeted(
                ["setvcp", "60", f"0x{target_value:02x}", "--noverify"]
            )
            self.input_value = target_value
            current = {
                "value": target_value,
                "hex": f"0x{target_value:02x}",
                "name": INPUT_SOURCE_NAMES.get(target_value, "Unknown"),
            }
            return {
                "ok": True,
                "monitor": f"{MONITOR_MFG_ID} {MONITOR_MODEL}",
                "display": self.display_number,
                "bus": f"/dev/i2c-{self.bus_number}",
                "from": previous,
                "to": current,
            }
def normalize(raw: dict) -> dict:
    snapshot = raw.get("rateLimitsByLimitId", {}).get("codex") or raw["rateLimits"]

    def window(value: dict | None) -> dict | None:
        if not value:
            return None
        used = max(0, min(100, int(value["usedPercent"])))
        return {
            "used_percent": used,
            "remaining_percent": 100 - used,
            "resets_at": value.get("resetsAt"),
            "window_minutes": value.get("windowDurationMins"),
        }

    credits = snapshot.get("credits") or {}
    return {
        "ok": True,
        "fetched_at": int(time.time()),
        "plan": snapshot.get("planType"),
        "primary": window(snapshot.get("primary")),
        "secondary": window(snapshot.get("secondary")),
        "credits": {
            "balance": credits.get("balance"),
            "unlimited": credits.get("unlimited", False),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    codex = CodexAppServer(args.codex)
    monitor = AocMonitor()
    windows_session = WindowsSessionState()

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/session/windows":
                self.send_json(200, windows_session.status())
                return
            if path == "/monitor/status":
                try:
                    self.send_json(200, monitor.status())
                except Exception as exc:
                    self.send_json(
                        503,
                        {"ok": False, "error": str(exc)},
                    )
                return
            if path == "/monitor/brightness/cached":
                self.send_json(200, monitor.cached_brightness())
                return
            if path == "/monitor/brightness":
                try:
                    self.send_json(200, monitor.brightness())
                except Exception as exc:
                    self.send_json(503, {"ok": False, "error": str(exc)})
                return
            if path not in ("", "/quota"):
                self.send_error(404)
                return
            try:
                self.send_json(200, normalize(codex.rate_limits()))
            except Exception as exc:
                self.send_json(
                    503,
                    {"ok": False, "fetched_at": int(time.time()), "error": str(exc)},
                )

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("/session/windows/locked", "/session/windows/unlocked"):
                try:
                    self.send_json(
                        200,
                        windows_session.set_locked(path == "/session/windows/locked"),
                    )
                except Exception as exc:
                    self.send_json(503, {"ok": False, "error": str(exc)})
                return
            if path in ("/monitor/brightness/up", "/monitor/brightness/down"):
                try:
                    delta = 5 if path.endswith("/up") else -5
                    self.send_json(200, monitor.adjust_brightness(delta))
                except Exception as exc:
                    self.send_json(503, {"ok": False, "error": str(exc)})
                return
            if path in ("/monitor/input/displayport", "/monitor/input/hdmi"):
                try:
                    target = (
                        WINDOWS_INPUT_SOURCE
                        if path.endswith("/displayport")
                        else PI_INPUT_SOURCE
                    )
                    self.send_json(200, monitor.set_input(target))
                except ValueError as exc:
                    self.send_json(409, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self.send_json(503, {"ok": False, "error": str(exc)})
                return
            if path != "/monitor/toggle":
                self.send_error(404)
                return
            try:
                self.send_json(200, monitor.toggle())
            except ValueError as exc:
                self.send_json(409, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self.send_json(503, {"ok": False, "error": str(exc)})

        def log_message(self, fmt: str, *args) -> None:
            print("%s - %s" % (self.address_string(), fmt % args))

    print(f"Codex quota API: http://{args.host}:{args.port}/quota")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
