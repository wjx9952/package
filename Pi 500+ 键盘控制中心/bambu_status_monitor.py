#!/usr/bin/env python3
"""Keep a private Bambu LAN MQTT snapshot for the LCD dashboard."""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

import paho.mqtt.client as mqtt


CONFIG_FILE = Path(
    os.environ.get(
        "BAMBU_CONFIG_FILE",
        str(Path.home() / ".config/pi500-keyboard-control-center/bambu.json"),
    )
)
STATE_FILE = Path(
    os.environ.get(
        "BAMBU_STATE_FILE",
        str(Path.home() / ".local/state/pi500-keyboard-control-center/bambu-state.json"),
    )
)
CAMERA_FRAME_FILE = Path(
    os.environ.get(
        "BAMBU_CAMERA_FRAME_FILE",
        str(STATE_FILE.with_name("bambu-camera.jpg")),
    )
)
CAMERA_REFRESH_SECONDS = 15.0
CAMERA_RETRY_SECONDS = 5.0
CAMERA_ACTIVE_STATES = {"RUNNING", "PRINTING", "PREPARE", "PREPARING", "PAUSE", "PAUSED"}
STATUS_TRACKING_VERSION = 2


def deep_merge(old: dict, new: dict) -> dict:
    for key, value in new.items():
        if isinstance(old.get(key), dict) and isinstance(value, dict):
            deep_merge(old[key], value)
        else:
            old[key] = value
    return old


def load_previous_state(path: Path, *, now: float | None = None) -> tuple[dict, str, float]:
    """Restore the last printer state so completion time survives restarts."""
    epoch_now = time.time() if now is None else now
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}, "", epoch_now
    print_data = payload.get("print") if isinstance(payload, dict) else None
    if not isinstance(print_data, dict):
        return {}, "", epoch_now
    gcode_state = str(print_data.get("gcode_state", "")).strip()
    try:
        tracking_version = int(payload.get("status_tracking_version", 0))
    except (TypeError, ValueError):
        tracking_version = 0
    try:
        changed_at = float(payload.get("status_changed_at", 0))
    except (TypeError, ValueError):
        changed_at = 0
    # Older snapshots reset this timestamp whenever the monitor restarted.
    # Treat them as unknown instead of presenting a fabricated completion time.
    if tracking_version < STATUS_TRACKING_VERSION or changed_at <= 0:
        changed_at = 0
    return {"print": print_data}, gcode_state, changed_at


def load_config(path: Path = CONFIG_FILE) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Bambu config unavailable: {path}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Bambu config is not valid JSON: {path}") from exc
    required = ("host", "serial", "access_code")
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise RuntimeError("Bambu config is missing: " + ", ".join(missing))
    return config


class BambuMonitor:
    def __init__(self, config: dict, state_file: Path = STATE_FILE) -> None:
        self.host = str(config["host"]).strip()
        self.serial = str(config["serial"]).strip()
        self.access_code = str(config["access_code"]).strip()
        self.port = int(config.get("port", 8883))
        self.report_topic = f"device/{self.serial}/report"
        self.request_topic = f"device/{self.serial}/request"
        self.state_file = state_file
        previous_state, previous_gcode_state, previous_changed_at = load_previous_state(
            state_file
        )
        self.raw_state: dict = previous_state
        self.connected = False
        self.status_changed_at = previous_changed_at
        self.last_gcode_state = previous_gcode_state
        self.camera_frame_file = CAMERA_FRAME_FILE

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"pi500-bambu-status-{int(time.time())}",
        )
        self.client.username_pw_set(username="bblp", password=self.access_code)
        self.client.tls_set(
            cert_reqs=ssl.CERT_NONE,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        self.client.tls_insecure_set(True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

    def write_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status_tracking_version": STATUS_TRACKING_VERSION,
            "connected": self.connected,
            "printer_ip": self.host,
            "updated_at": time.time(),
            "status_changed_at": self.status_changed_at,
            "print": self.raw_state.get("print", {}),
        }
        temporary = self.state_file.with_name(
            self.state_file.name + f".{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_file)

    def request_full_status(self) -> None:
        self.client.publish(
            self.request_topic,
            json.dumps({
                "pushing": {
                    "sequence_id": str(int(time.time() * 1000)),
                    "command": "pushall",
                }
            }),
            qos=0,
        )

    def capture_camera_frame(self, rtsp_url: str) -> bool:
        """Atomically refresh the small live model preview from the LAN camera."""
        if not rtsp_url.startswith("rtsps://") or "@" in rtsp_url:
            return False
        authenticated = rtsp_url.replace(
            "rtsps://",
            f"rtsps://bblp:{quote(self.access_code, safe='')}@",
            1,
        )
        self.camera_frame_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.camera_frame_file.with_name(
            self.camera_frame_file.name + f".{os.getpid()}.tmp.jpg"
        )
        try:
            result = subprocess.run(
                [
                    "/usr/bin/ffmpeg",
                    "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp",
                    "-i", authenticated,
                    "-frames:v", "1",
                    "-vf", "scale=320:-2",
                    "-q:v", "4",
                    "-y", str(temporary),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=12,
                check=False,
            )
            if result.returncode != 0 or not temporary.is_file():
                return False
            if temporary.stat().st_size < 512:
                return False
            os.replace(temporary, self.camera_frame_file)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def camera_loop(self) -> None:
        while True:
            print_data = self.raw_state.get("print")
            if not isinstance(print_data, dict):
                print_data = {}
            state = str(
                print_data.get("gcode_state") or print_data.get("state") or ""
            ).strip().upper()
            ipcam = print_data.get("ipcam")
            rtsp_url = (
                str(ipcam.get("rtsp_url") or "")
                if isinstance(ipcam, dict) else ""
            )
            if self.connected and state in CAMERA_ACTIVE_STATES and rtsp_url:
                self.capture_camera_frame(rtsp_url)
                time.sleep(CAMERA_REFRESH_SECONDS)
            else:
                time.sleep(CAMERA_RETRY_SECONDS)

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        del userdata, flags, properties
        if reason_code.is_failure:
            self.connected = False
            self.write_state()
            return
        self.connected = True
        client.subscribe(self.report_topic, qos=0)
        self.request_full_status()
        self.write_state()

    def on_disconnect(
        self,
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties,
    ) -> None:
        del client, userdata, disconnect_flags, reason_code, properties
        self.connected = False
        self.write_state()

    def on_message(self, client, userdata, message) -> None:
        del client, userdata
        try:
            data = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        deep_merge(self.raw_state, data)
        print_data = self.raw_state.get("print")
        gcode_state = (
            str(print_data.get("gcode_state", ""))
            if isinstance(print_data, dict) else ""
        )
        if gcode_state and gcode_state != self.last_gcode_state:
            self.last_gcode_state = gcode_state
            self.status_changed_at = time.time()
        self.connected = True
        self.write_state()

    def run(self) -> None:
        threading.Thread(target=self.camera_loop, daemon=True).start()
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_forever()


def main() -> None:
    config = load_config()
    BambuMonitor(config).run()


if __name__ == "__main__":
    main()
