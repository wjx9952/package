#!/usr/bin/python3
"""Pi 500+ BLE HID keyboard daemon.

Runs as root, publishes a Bluetooth LE HID-over-GATT keyboard and forwards the
built-in Pi 500+ keyboard while keyboard mode is active.
"""

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
import fcntl
import ctypes
import json
import os
import select
import signal
import socket
import struct
import threading
import time
import traceback

from gi.repository import GLib


BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROP_IFACE = "org.freedesktop.DBus.Properties"
GATT_MANAGER = "org.bluez.GattManager1"
ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHRC = "org.bluez.GattCharacteristic1"
GATT_DESC = "org.bluez.GattDescriptor1"
ADV_IFACE = "org.bluez.LEAdvertisement1"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_MANAGER = "org.bluez.AgentManager1"

APP_PATH = "/com/pi500/blekeyboard"
SOCKET_PATH = "/run/pi500-bt-keyboard/control.sock"
ACTIVITY_PATH = "/run/pi500-bt-keyboard/activity"
KEYBOARD_NAME = "Raspberry Pi Ltd Pi 500+ Keyboard (ANSI)"
PAIRING_SECONDS = 120
HID_SERVICE_UUID = "00001812-0000-1000-8000-00805f9b34fb"

# Linux Bluetooth Management protocol constants.
MGMT_OP_ADD_ADVERTISING = 0x003E
MGMT_OP_REMOVE_ADVERTISING = 0x003F
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002
HCI_CHANNEL_CONTROL = 3
HCI_DEV_NONE = 0xFFFF

# Linux input event constants.
EV_SYN = 0x00
EV_KEY = 0x01
EV_LED = 0x11
SYN_REPORT = 0x00
LED_CAPSL = 0x01
EVIOCGRAB = 0x40044590
EVIOCGLED_8 = 0x80084519
INPUT_EVENT = struct.Struct("llHHi")


def byte_array(values):
    return dbus.Array([dbus.Byte(v) for v in values], signature="y")


class InvalidArgs(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class Rejected(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class NotSupported(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"


class PropertiesObject(dbus.service.Object):
    interface = None

    @dbus.service.method(PROP_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        props = self.get_properties().get(interface)
        if props is None or prop not in props:
            raise InvalidArgs("Unknown property")
        return props[prop]

    @dbus.service.method(PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.get_properties().get(interface, {})

    @dbus.service.method(PROP_IFACE, in_signature="ssv", out_signature="")
    def Set(self, interface, prop, value):
        raise NotSupported("Properties are read-only")

    @dbus.service.signal(PROP_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


class Application(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, APP_PATH)
        self.services = []

    def add_service(self, service):
        self.services.append(service)

    def objects(self):
        result = {service.path: service for service in self.services}
        for service in self.services:
            for chrc in service.characteristics:
                result[chrc.path] = chrc
                for desc in chrc.descriptors:
                    result[desc.path] = desc
        return result

    @dbus.service.method(OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        return {dbus.ObjectPath(path): obj.get_properties()
                for path, obj in self.objects().items()}


class Service(PropertiesObject):
    interface = GATT_SERVICE

    def __init__(self, bus, index, uuid):
        self.path = f"{APP_PATH}/service{index}"
        super().__init__(bus, self.path)
        self.uuid = uuid
        self.characteristics = []

    def add_characteristic(self, chrc):
        self.characteristics.append(chrc)

    def get_properties(self):
        return {GATT_SERVICE: {
            "UUID": self.uuid,
            "Primary": dbus.Boolean(True),
            "Characteristics": dbus.Array(
                [dbus.ObjectPath(c.path) for c in self.characteristics],
                signature="o"),
        }}


class Characteristic(PropertiesObject):
    interface = GATT_CHRC

    def __init__(self, bus, service, index, uuid, flags, value=b""):
        self.path = f"{service.path}/char{index}"
        super().__init__(bus, self.path)
        self.service = service
        self.uuid = uuid
        self.flags = flags
        self.value = bytes(value)
        self.descriptors = []
        self.notifying = False

    def add_descriptor(self, desc):
        self.descriptors.append(desc)

    def get_properties(self):
        props = {
            "Service": dbus.ObjectPath(self.service.path),
            "UUID": self.uuid,
            "Flags": dbus.Array(self.flags, signature="s"),
            "Descriptors": dbus.Array(
                [dbus.ObjectPath(d.path) for d in self.descriptors],
                signature="o"),
        }
        if "notify" in self.flags:
            props["Notifying"] = dbus.Boolean(self.notifying)
        return {GATT_CHRC: props}

    @dbus.service.method(GATT_CHRC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return byte_array(self.value)

    @dbus.service.method(GATT_CHRC, in_signature="aya{sv}", out_signature="")
    def WriteValue(self, value, options):
        self.value = bytes(value)

    @dbus.service.method(GATT_CHRC, in_signature="", out_signature="")
    def StartNotify(self):
        if "notify" not in self.flags:
            raise NotSupported("Notifications not supported")
        if not self.notifying:
            self.notifying = True
            self.PropertiesChanged(GATT_CHRC,
                                   {"Notifying": dbus.Boolean(True)}, [])

    @dbus.service.method(GATT_CHRC, in_signature="", out_signature="")
    def StopNotify(self):
        if self.notifying:
            self.notifying = False
            self.PropertiesChanged(GATT_CHRC,
                                   {"Notifying": dbus.Boolean(False)}, [])

    def notify(self, value):
        self.value = bytes(value)
        if self.notifying:
            self.PropertiesChanged(
                GATT_CHRC, {"Value": byte_array(self.value)}, [])


class Descriptor(PropertiesObject):
    interface = GATT_DESC

    def __init__(self, bus, chrc, index, uuid, value):
        self.path = f"{chrc.path}/desc{index}"
        super().__init__(bus, self.path)
        self.chrc = chrc
        self.uuid = uuid
        self.value = bytes(value)

    def get_properties(self):
        return {GATT_DESC: {
            "Characteristic": dbus.ObjectPath(self.chrc.path),
            "UUID": self.uuid,
            "Flags": dbus.Array(["read", "encrypt-read"], signature="s"),
        }}

    @dbus.service.method(GATT_DESC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return byte_array(self.value)


class InputReport(Characteristic):
    def __init__(self, bus, service, index, state, uuid="2a4d"):
        super().__init__(bus, service, index, uuid,
                         ["read", "notify", "encrypt-read"], bytes(8))
        self.state = state

    @dbus.service.method(GATT_CHRC, in_signature="", out_signature="")
    def StartNotify(self):
        super().StartNotify()
        if self.state:
            self.state.refresh_connection()

    @dbus.service.method(GATT_CHRC, in_signature="", out_signature="")
    def StopNotify(self):
        super().StopNotify()
        if self.state:
            self.state.refresh_connection()


class ProtocolMode(Characteristic):
    def __init__(self, bus, service, index):
        super().__init__(bus, service, index, "2a4e",
                         ["read", "write-without-response"], b"\x01")

    def WriteValue(self, value, options):
        raw = bytes(value)
        if raw not in (b"\x00", b"\x01"):
            raise InvalidArgs("Invalid protocol mode")
        self.value = raw


class LEDOutputReport(Characteristic):
    """Receive the host's standard Num/Caps/Scroll Lock LED byte."""

    def __init__(self, bus, service, index, uuid="2a4d"):
        super().__init__(
            bus,
            service,
            index,
            uuid,
            ["read", "write", "write-without-response",
             "encrypt-read", "encrypt-write"],
            b"\x00",
        )
        self.state = None

    @dbus.service.method(GATT_CHRC, in_signature="aya{sv}", out_signature="")
    def WriteValue(self, value, options):
        raw = bytes(value)
        self.value = raw
        if self.state is not None and raw:
            self.state.update_host_leds(raw[0])


class Advertisement(PropertiesObject):
    interface = ADV_IFACE

    def __init__(self, bus):
        self.path = f"{APP_PATH}/advertisement0"
        super().__init__(bus, self.path)
        self.variant = 0

    def get_properties(self):
        # BlueZ advertising API requires UUIDs in canonical 128-bit form.
        # Some controller/firmware combinations reject particular optional AD
        # field combinations, so later variants progressively remove fields.
        props = {"Type": "peripheral"}
        if self.variant in (0, 1):
            props["ServiceUUIDs"] = dbus.Array(
                [HID_SERVICE_UUID], signature="s")
        if self.variant in (0, 2):
            props["LocalName"] = "Pi500+ Keyboard"
        return {ADV_IFACE: props}

    def next_variant(self):
        if self.variant >= 3:
            return False
        self.variant += 1
        return True

    @dbus.service.method(ADV_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass


class MgmtAdvertisement:
    """Small connectable BLE advertisement through the kernel MGMT channel.

    Raspberry Pi's BlueZ 5.82/controller combination can reject the managed
    LEAdvertisement1 path with MGMT status 0x0d even for an empty payload. A
    direct MGMT advertisement avoids that userspace translation layer.
    """

    def __init__(self, controller_index):
        self.controller_index = controller_index
        self.sock = None
        self.instance = 1

    def _command(self, opcode, payload, timeout=5):
        packet = struct.pack("<HHH", opcode, self.controller_index,
                             len(payload)) + payload
        self.sock.send(packet)
        self.sock.settimeout(timeout)
        while True:
            response = self.sock.recv(1024)
            if len(response) < 6:
                continue
            event, index, length = struct.unpack_from("<HHH", response)
            data = response[6:6 + length]
            if index not in (self.controller_index, HCI_DEV_NONE):
                continue
            if event == MGMT_EV_CMD_COMPLETE and len(data) >= 3:
                command, status = struct.unpack_from("<HB", data)
                if command != opcode:
                    continue
                return status, data[3:]
            if event == MGMT_EV_CMD_STATUS and len(data) >= 3:
                command, status = struct.unpack_from("<HB", data)
                if command != opcode:
                    continue
                return status, b""

    def start(self):
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW,
                                  socket.BTPROTO_HCI)
        # Python <= 3.13 only accepts (device_id,) for BTPROTO_HCI; support for
        # the channel member was added in 3.14. Bind sockaddr_hci directly so
        # Debian 13's Python 3.13 can select HCI_CHANNEL_CONTROL.
        sockaddr = struct.pack("=HHH", socket.AF_BLUETOOTH,
                               HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
        address = ctypes.create_string_buffer(sockaddr)
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.bind(self.sock.fileno(), ctypes.byref(address),
                           len(sockaddr))
        if result != 0:
            error = ctypes.get_errno()
            self.sock.close()
            self.sock = None
            raise OSError(error, os.strerror(error))

        # UUID 0x1812 in primary ADV, complete local name in scan response.
        adv_data = bytes([3, 0x03, 0x12, 0x18])
        name = b"Pi500+ Keyboard"
        scan_response = bytes([len(name) + 1, 0x09]) + name
        attempts = [
            # connectable + discoverable + kernel-generated Flags AD field
            (0x0000000B, adv_data, scan_response),
            # connectable + kernel-generated Flags AD field
            (0x00000009, adv_data, scan_response),
            # connectable, minimal service payload
            (0x00000001, adv_data, b""),
        ]
        errors = []
        for flags, advertising, scan in attempts:
            payload = struct.pack(
                "<BIHHBB", self.instance, flags, 0, 0,
                len(advertising), len(scan)) + advertising + scan
            status, _ = self._command(MGMT_OP_ADD_ADVERTISING, payload)
            if status == 0:
                print(f"Kernel MGMT advertisement active (flags=0x{flags:x})",
                      flush=True)
                return
            errors.append(f"flags=0x{flags:x}: status=0x{status:02x}")
        self.sock.close()
        self.sock = None
        raise RuntimeError("MGMT 广播失败：" + "; ".join(errors))

    def stop(self):
        if self.sock is None:
            return
        try:
            self._command(MGMT_OP_REMOVE_ADVERTISING,
                          bytes([self.instance]), timeout=2)
        except Exception:
            pass
        self.sock.close()
        self.sock = None


class PairingAgent(dbus.service.Object):
    def __init__(self, bus, state):
        self.path = f"{APP_PATH}/agent"
        super().__init__(bus, self.path)
        self.state = state

    def allowed(self, device):
        if self.state.pairing:
            return True
        try:
            obj = self.state.bus.get_object(BLUEZ, device)
            return bool(dbus.Interface(obj, PROP_IFACE).Get(DEVICE_IFACE,
                                                            "Paired"))
        except Exception:
            return False

    def require_allowed(self, device):
        if not self.allowed(device):
            raise Rejected("请先在 Pi500 蓝牙键盘中开启配对")

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        self.require_allowed(device)
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        self.require_allowed(device)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        self.require_allowed(device)

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        self.require_allowed(device)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        self.require_allowed(device)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        self.require_allowed(device)

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        self.require_allowed(device)
        try:
            obj = self.state.bus.get_object(BLUEZ, device)
            dbus.Interface(obj, PROP_IFACE).Set(
                DEVICE_IFACE, "Trusted", dbus.Boolean(True))
        except Exception:
            pass

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass


# Linux key code -> USB HID usage. Modifiers are handled separately.
KEY_MAP = {
    1: 41, 2: 30, 3: 31, 4: 32, 5: 33, 6: 34, 7: 35, 8: 36,
    9: 37, 10: 38, 11: 39, 12: 45, 13: 46, 14: 42, 15: 43,
    16: 20, 17: 26, 18: 8, 19: 21, 20: 23, 21: 28, 22: 24,
    23: 12, 24: 18, 25: 19, 26: 47, 27: 48, 28: 40,
    30: 4, 31: 22, 32: 7, 33: 9, 34: 10, 35: 11, 36: 13,
    37: 14, 38: 15, 39: 51, 40: 52, 41: 53, 43: 49,
    44: 29, 45: 27, 46: 6, 47: 25, 48: 5, 49: 17, 50: 16,
    51: 54, 52: 55, 53: 56, 55: 85, 57: 44, 58: 57,
    59: 58, 60: 59, 61: 60, 62: 61, 63: 62, 64: 63, 65: 64,
    66: 65, 67: 66, 68: 67, 69: 83, 70: 71,
    71: 95, 72: 96, 73: 97, 74: 86, 75: 92, 76: 93, 77: 94,
    78: 87, 79: 89, 80: 90, 81: 91, 82: 98, 83: 99,
    87: 68, 88: 69, 96: 88, 98: 84, 99: 70,
    102: 74, 103: 82, 104: 75, 105: 80, 106: 79, 107: 77,
    108: 81, 109: 78, 110: 73, 111: 76, 119: 72, 127: 101,
}
MODIFIERS = {29: 0, 42: 1, 56: 2, 125: 3,
             97: 4, 54: 5, 100: 6, 126: 7}


def keyboard_report(modifiers, keys):
    mod_byte = 0
    for code in modifiers:
        if code in MODIFIERS:
            mod_byte |= 1 << MODIFIERS[code]
    usages = sorted(KEY_MAP[k] for k in keys if k in KEY_MAP)
    if len(usages) > 6:
        usages = [1] * 6
    else:
        usages += [0] * (6 - len(usages))
    return bytes([mod_byte, 0, *usages])


def find_keyboard_event():
    override = os.environ.get("PI500_KEYBOARD_DEVICE")
    if override:
        return override
    with open("/proc/bus/input/devices", encoding="utf-8") as handle:
        blocks = handle.read().split("\n\n")
    matches = []
    for block in blocks:
        if f'N: Name="{KEYBOARD_NAME}"' not in block:
            continue
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                matches.extend(word for word in line.split() if word.startswith("event"))
    if not matches:
        raise RuntimeError(f"没有找到内置键盘：{KEYBOARD_NAME}")
    return "/dev/input/" + matches[0]


class State:
    def __init__(self, bus, adapter_path, input_report, boot_input):
        self.bus = bus
        self.adapter_path = adapter_path
        self.input_report = input_report
        self.input_characteristics = [input_report, boot_input]
        self.active = False
        self.connected = False
        self.pairing = False
        self.caps_lock = None
        self.message = "等待 Windows 连接"
        self.capture_stop = None
        self.capture_thread = None
        self.capture_fd = None
        self.local_caps_lock = False
        self.pairing_source = None
        self.activity_write_lock = threading.Lock()
        self.activity_thread = threading.Thread(
            target=self._activity_loop,
            daemon=True,
        )
        self.activity_thread.start()

    def note_keyboard_activity(self):
        """Publish a monotonically changing token readable by desktop services."""
        try:
            with self.activity_write_lock:
                os.makedirs(os.path.dirname(ACTIVITY_PATH), mode=0o755, exist_ok=True)
                with open(ACTIVITY_PATH, "w", encoding="ascii") as handle:
                    handle.write(str(time.time_ns()) + "\n")
                os.chmod(ACTIVITY_PATH, 0o644)
        except OSError:
            pass

    def _activity_loop(self):
        """Observe key activity without grabbing the keyboard when mode is off."""
        while True:
            if self.active:
                time.sleep(0.1)
                continue
            fd = None
            try:
                fd = os.open(find_keyboard_event(), os.O_RDONLY | os.O_NONBLOCK)
                buffer = b""
                while not self.active:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                    if not ready:
                        continue
                    chunk = os.read(fd, INPUT_EVENT.size * 32)
                    if not chunk:
                        break
                    buffer += chunk
                    while len(buffer) >= INPUT_EVENT.size:
                        raw, buffer = (
                            buffer[:INPUT_EVENT.size],
                            buffer[INPUT_EVENT.size:],
                        )
                        _, _, event_type, _code, value = INPUT_EVENT.unpack(raw)
                        if event_type == EV_KEY and value in (1, 2):
                            self.note_keyboard_activity()
            except (OSError, RuntimeError):
                time.sleep(0.5)
            finally:
                if fd is not None:
                    os.close(fd)

    def adapter_properties(self):
        obj = self.bus.get_object(BLUEZ, self.adapter_path)
        return dbus.Interface(obj, PROP_IFACE)

    def enable_pairing(self):
        self.pairing = True
        self.message = "可配对 120 秒：请在 Windows 中添加蓝牙设备"
        props = self.adapter_properties()
        props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
        props.Set(ADAPTER_IFACE, "PairableTimeout", dbus.UInt32(PAIRING_SECONDS))
        props.Set(ADAPTER_IFACE, "DiscoverableTimeout", dbus.UInt32(PAIRING_SECONDS))
        props.Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(True))
        props.Set(ADAPTER_IFACE, "Discoverable", dbus.Boolean(True))
        if self.pairing_source:
            GLib.source_remove(self.pairing_source)
        self.pairing_source = GLib.timeout_add_seconds(
            PAIRING_SECONDS, self.disable_pairing)

    def disable_pairing(self):
        self.pairing = False
        self.pairing_source = None
        try:
            props = self.adapter_properties()
            props.Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(False))
            props.Set(ADAPTER_IFACE, "Discoverable", dbus.Boolean(False))
        except Exception:
            pass
        if not self.active:
            self.message = "配对窗口已关闭"
        return GLib.SOURCE_REMOVE

    def paired_devices(self):
        result = []
        objects = dbus.Interface(self.bus.get_object(BLUEZ, "/"),
                                 OM_IFACE).GetManagedObjects()
        for path, interfaces in objects.items():
            dev = interfaces.get(DEVICE_IFACE)
            if dev and bool(dev.get("Paired", False)):
                if not bool(dev.get("Trusted", False)):
                    try:
                        obj = self.bus.get_object(BLUEZ, path)
                        dbus.Interface(obj, PROP_IFACE).Set(
                            DEVICE_IFACE, "Trusted", dbus.Boolean(True))
                    except Exception:
                        pass
                result.append(str(dev.get("Alias", dev.get("Name", path))))
        return result

    def refresh_connection(self):
        was_connected = self.connected
        self.connected = any(chrc.notifying
                             for chrc in self.input_characteristics)
        if was_connected and not self.connected:
            self.caps_lock = None
            self.stop_capture("Windows 已断开")

    def update_host_leds(self, value):
        """Store the Caps Lock bit from the standard HID LED output report."""
        self.caps_lock = bool(value & 0x02)
        if self.active and self.capture_fd is not None:
            self.write_caps_led(self.capture_fd, self.caps_lock)

    @staticmethod
    def read_caps_led(fd):
        """Read the local Linux Caps LED state before keyboard capture."""
        state = bytearray(8)
        try:
            fcntl.ioctl(fd, EVIOCGLED_8, state, True)
            return bool(state[LED_CAPSL // 8] & (1 << (LED_CAPSL % 8)))
        except OSError:
            return False

    @staticmethod
    def write_caps_led(fd, enabled):
        """Use the keyboard firmware's native Caps Lock highlight overlay."""
        try:
            os.write(
                fd,
                INPUT_EVENT.pack(0, 0, EV_LED, LED_CAPSL, int(bool(enabled)))
                + INPUT_EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0),
            )
            return True
        except OSError:
            return False

    def clear_devices(self):
        self.stop_capture("正在清除已配对设备")
        objects = dbus.Interface(self.bus.get_object(BLUEZ, "/"),
                                 OM_IFACE).GetManagedObjects()
        adapter = dbus.Interface(
            self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IFACE)
        removed = []
        for path, interfaces in list(objects.items()):
            dev = interfaces.get(DEVICE_IFACE)
            if not dev or not bool(dev.get("Paired", False)):
                continue
            removed.append(str(dev.get("Alias", dev.get("Name", path))))
            adapter.RemoveDevice(dbus.ObjectPath(path))
        self.connected = False
        self.caps_lock = None
        self.disable_pairing()
        self.message = ("已清除设备：" + "、".join(removed)
                        if removed else "没有已配对设备需要清除")
        result = self.status()
        result["removed_devices"] = removed
        return result

    def lock_windows(self):
        """Send the standard Win+L shortcut over the active BLE HID link."""
        if not self.connected:
            raise RuntimeError("Windows 尚未连接")
        # Linux key 125 is the left GUI/Windows modifier and key 38 is L.
        self.input_report.notify(keyboard_report({125}, {38}))
        GLib.timeout_add(100, self._release_shortcut)
        result = self.status()
        result["windows_locked"] = True
        return result

    def _release_shortcut(self):
        self.input_report.notify(bytes(8))
        return GLib.SOURCE_REMOVE

    def status(self):
        return {
            "ok": True,
            "active": self.active,
            "connected": self.connected,
            "pairing": self.pairing,
            "caps_lock": self.caps_lock,
            "message": self.message,
            "paired_devices": self.paired_devices(),
        }

    def start_capture(self):
        if self.active:
            return self.status()
        if not self.connected:
            raise RuntimeError("Windows 尚未连接，请先完成配对并等待连接")
        device = find_keyboard_event()
        stop_event = threading.Event()
        # Open and grab before reporting success, so permission/device errors are visible.
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except Exception:
            os.close(fd)
            raise
        self.capture_stop = stop_event
        self.capture_fd = fd
        self.local_caps_lock = self.read_caps_led(fd)
        self.active = True
        if self.caps_lock is not None:
            self.write_caps_led(fd, self.caps_lock)
        self.message = "键盘模式已开启；Ctrl+Alt+F12 可紧急退出"
        self.capture_thread = threading.Thread(
            target=self._capture_loop, args=(fd, stop_event), daemon=True)
        self.capture_thread.start()
        return self.status()

    def stop_capture(self, reason="键盘模式已关闭"):
        if self.capture_stop:
            self.capture_stop.set()
        self.active = False
        self.capture_stop = None
        self.message = reason
        self.input_report.notify(bytes(8))
        return self.status()

    def _capture_loop(self, fd, stop_event):
        modifiers = set()
        keys = set()
        buffer = b""
        try:
            while not stop_event.is_set():
                ready, _, _ = select.select([fd], [], [], 0.25)
                if not ready:
                    continue
                chunk = os.read(fd, INPUT_EVENT.size * 32)
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= INPUT_EVENT.size:
                    raw, buffer = buffer[:INPUT_EVENT.size], buffer[INPUT_EVENT.size:]
                    _, _, event_type, code, value = INPUT_EVENT.unpack(raw)
                    if event_type != EV_KEY or value == 2:
                        continue
                    if value:
                        self.note_keyboard_activity()
                    target = modifiers if code in MODIFIERS else keys
                    if value:
                        target.add(code)
                    else:
                        target.discard(code)
                    ctrl = 29 in modifiers or 97 in modifiers
                    alt = 56 in modifiers or 100 in modifiers
                    if value and code == 88 and ctrl and alt:
                        GLib.idle_add(self._emergency_stop)
                        stop_event.set()
                        break
                    report = keyboard_report(modifiers, keys)
                    GLib.idle_add(self.input_report.notify, report)
        except Exception as exc:
            GLib.idle_add(self._capture_failed, str(exc))
        finally:
            self.write_caps_led(fd, self.local_caps_lock)
            if self.capture_fd == fd:
                self.capture_fd = None
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            except Exception:
                pass
            os.close(fd)
            GLib.idle_add(self.input_report.notify, bytes(8))

    def _emergency_stop(self):
        self.stop_capture("已通过紧急快捷键退出")
        # GLib repeats idle callbacks while they return a truthy value. The
        # normal stop_capture result is a non-empty status dict, so returning
        # it here would create a permanent auto-stop loop after the first
        # emergency shortcut.
        return GLib.SOURCE_REMOVE

    def _capture_failed(self, message):
        self.active = False
        self.capture_stop = None
        self.message = "键盘读取失败：" + message
        return GLib.SOURCE_REMOVE


class ControlServer(threading.Thread):
    def __init__(self, state):
        super().__init__(daemon=True)
        self.state = state

    def dispatch(self, command):
        done = threading.Event()
        box = {}

        def execute():
            try:
                if command == "status":
                    box["result"] = self.state.status()
                elif command == "pair":
                    self.state.enable_pairing()
                    box["result"] = self.state.status()
                elif command == "start":
                    box["result"] = self.state.start_capture()
                elif command == "stop":
                    box["result"] = self.state.stop_capture()
                elif command == "lock_windows":
                    box["result"] = self.state.lock_windows()
                elif command == "clear":
                    box["result"] = self.state.clear_devices()
                else:
                    raise ValueError("未知命令")
            except Exception as exc:
                box["result"] = {"ok": False, "error": str(exc)}
            done.set()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(execute)
        if not done.wait(8):
            return {"ok": False, "error": "后台服务响应超时"}
        return box["result"]

    def run(self):
        os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o755, exist_ok=True)
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)
        server.listen(8)
        while True:
            conn, _ = server.accept()
            with conn:
                try:
                    raw = conn.recv(4096)
                    request = json.loads(raw.decode("utf-8"))
                    response = self.dispatch(request.get("command", ""))
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode())


def make_application(bus):
    app = Application(bus)

    # HID report descriptor: standard 8-byte boot-compatible keyboard.
    report_map = bytes([
        0x05, 0x01, 0x09, 0x06, 0xA1, 0x01,
        0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00,
        0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
        0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
        0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01,
        0x29, 0x05, 0x91, 0x02, 0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
        0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65,
        0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00, 0xC0,
    ])

    hid = Service(bus, 0, "1812")
    hid_info = Characteristic(bus, hid, 0, "2a4a", ["read"],
                              b"\x11\x01\x00\x03")
    report_map_ch = Characteristic(bus, hid, 1, "2a4b", ["read"], report_map)
    control = Characteristic(bus, hid, 2, "2a4c",
                             ["write-without-response"], b"\x00")
    protocol = ProtocolMode(bus, hid, 3)
    # Temporary placeholder; State is attached after construction.
    input_report = InputReport.__new__(InputReport)
    Characteristic.__init__(input_report, bus, hid, 4, "2a4d",
                            ["read", "notify", "encrypt-read"], bytes(8))
    input_report.state = None
    input_ref = Descriptor(bus, input_report, 0, "2908", b"\x00\x01")
    input_report.add_descriptor(input_ref)
    output_report = LEDOutputReport(bus, hid, 5)
    output_ref = Descriptor(bus, output_report, 0, "2908", b"\x00\x02")
    output_report.add_descriptor(output_ref)
    boot_input = InputReport(bus, hid, 6, None, uuid="2a22")
    boot_output = LEDOutputReport(bus, hid, 7, uuid="2a32")
    for item in (hid_info, report_map_ch, control, protocol, input_report,
                 output_report, boot_input, boot_output):
        hid.add_characteristic(item)
    app.add_service(hid)

    battery = Service(bus, 1, "180f")
    battery.add_characteristic(Characteristic(
        bus, battery, 0, "2a19", ["read", "notify"], b"\x64"))
    app.add_service(battery)

    info = Service(bus, 2, "180a")
    info.add_characteristic(Characteristic(
        bus, info, 0, "2a29", ["read"], b"Raspberry Pi"))
    # PnP ID: USB source, Raspberry Pi vendor, local product, version 1.0.
    info.add_characteristic(Characteristic(
        bus, info, 1, "2a50", ["read"],
        bytes([2, 0x8A, 0x2E, 0x01, 0x50, 0x00, 0x01])))
    app.add_service(info)
    return app, input_report, boot_input, output_report, boot_output


def find_adapter(bus):
    objects = dbus.Interface(bus.get_object(BLUEZ, "/"),
                             OM_IFACE).GetManagedObjects()
    for path, interfaces in objects.items():
        if GATT_MANAGER in interfaces and ADV_MANAGER in interfaces:
            return str(path)
    raise RuntimeError("没有找到支持 BLE 外设模式的蓝牙适配器")


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter_path = find_adapter(bus)
    app, input_report, boot_input, output_report, boot_output = make_application(bus)
    state = State(bus, adapter_path, input_report, boot_input)
    input_report.state = state
    boot_input.state = state
    output_report.state = state
    boot_output.state = state

    # Keep boot input synchronized if a host chooses boot protocol.
    original_notify = input_report.notify

    def notify_both(value):
        original_notify(value)
        boot_input.notify(value)
    input_report.notify = notify_both

    agent = PairingAgent(bus, state)
    adapter = bus.get_object(BLUEZ, adapter_path)
    props = dbus.Interface(adapter, PROP_IFACE)
    props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))

    agent_manager = dbus.Interface(bus.get_object(BLUEZ, "/org/bluez"),
                                   AGENT_MANAGER)
    agent_manager.RegisterAgent(agent.path, "NoInputNoOutput")
    agent_manager.RequestDefaultAgent(agent.path)

    # Registration must be asynchronous. During RegisterApplication BlueZ calls
    # GetManagedObjects back on this process; a blocking D-Bus call here would
    # prevent GLib from answering and both sides would time out.
    loop = GLib.MainLoop()
    control_started = [False]
    registration_errors = []
    controller_index = int(adapter_path.rsplit("hci", 1)[1])
    mgmt_advertisement = MgmtAdvertisement(controller_index)

    def registration_ok(kind):
        print(f"BlueZ {kind} registration complete", flush=True)
        if control_started[0]:
            return
        try:
            mgmt_advertisement.start()
            control_started[0] = True
            ControlServer(state).start()
            print("Pi500 Bluetooth Keyboard service ready", flush=True)
        except Exception as error:
            registration_failed("MGMT advertisement", error)

    def registration_failed(kind, error):
        registration_errors.append(f"{kind}: {error}")
        print(f"BlueZ {kind} registration failed: {error}", flush=True)
        loop.quit()

    dbus.Interface(adapter, GATT_MANAGER).RegisterApplication(
        APP_PATH, {},
        reply_handler=lambda: registration_ok("GATT"),
        error_handler=lambda error: registration_failed("GATT", error))
    signal.signal(signal.SIGTERM, lambda _signum, _frame: loop.quit())
    signal.signal(signal.SIGINT, lambda _signum, _frame: loop.quit())
    loop.run()
    mgmt_advertisement.stop()
    if registration_errors:
        raise RuntimeError("; ".join(registration_errors))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
