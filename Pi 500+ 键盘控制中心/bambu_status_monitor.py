#!/usr/bin/env python3
"""Keep a private Bambu LAN MQTT snapshot for the LCD dashboard."""

from __future__ import annotations

import json
import os
import ssl
import time
from pathlib import Path

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


def deep_merge(old: dict, new: dict) -> dict:
    for key, value in new.items():
        if isinstance(old.get(key), dict) and isinstance(value, dict):
            deep_merge(old[key], value)
        else:
            old[key] = value
    return old


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
        self.raw_state: dict = {}
        self.connected = False
        self.status_changed_at = time.time()
        self.last_gcode_state = ""

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
            "connected": self.connected,
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
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_forever()


def main() -> None:
    config = load_config()
    BambuMonitor(config).run()


if __name__ == "__main__":
    main()
