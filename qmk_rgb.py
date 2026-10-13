#!/usr/bin/env python3
"""Control QMK keyboard RGB lighting over the VIA raw HID protocol.

Talks straight to /dev/hidraw* with the standard library only, so the Omarchy
shell plugin can shell out to it without any extra packages. Every command
prints JSON on stdout; errors exit non-zero with {"error": ...}.

A keyboard qualifies when its firmware was built with VIA (or raw HID with
the VIA command set) enabled: it exposes a raw HID interface on usage page
0xFF60, usage 0x61, and answers `id_get_protocol_version`.
"""

import argparse
import errno
import json
import os
import select
import sys
from pathlib import Path

REPORT_SIZE = 32
TIMEOUT = 0.5

RAW_USAGE_PAGE = 0xFF60
RAW_USAGE = 0x61

ID_GET_PROTOCOL_VERSION = 0x01
ID_CUSTOM_SET_VALUE = 0x07
ID_CUSTOM_GET_VALUE = 0x08
ID_CUSTOM_SAVE = 0x09
ID_UNHANDLED = 0xFF

# VIA protocol 12+ addresses lighting through per-feature channels.
CHANNELS = {
    "rgb_matrix": 3,
    "rgblight": 2,
    "led_matrix": 5,
}
PROBE_ORDER = ["rgb_matrix", "rgblight", "led_matrix"]

VALUE_BRIGHTNESS = 1
VALUE_EFFECT = 2
VALUE_SPEED = 3
VALUE_COLOR = 4

# Protocols before 12 used a single implicit lighting channel with these ids.
LEGACY_VALUES = {
    VALUE_BRIGHTNESS: 0x80,
    VALUE_EFFECT: 0x81,
    VALUE_SPEED: 0x82,
    VALUE_COLOR: 0x83,
}

SYSFS_HIDRAW = Path("/sys/class/hidraw")
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "omarchy-qmk-rgb"
EFFECTS_DIR = Path(__file__).resolve().parent / "effects"


class QmkError(Exception):
    pass


# ---------------------------------------------------------------- discovery


def has_raw_usage(descriptor):
    """True if a HID report descriptor declares the VIA raw HID collection."""
    usage_page = 0
    usage = None
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item: skip it whole
            if i + 2 >= len(descriptor):
                break
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        data = int.from_bytes(descriptor[i + 1:i + 1 + size], "little")
        tag = prefix & 0xFC
        if tag == 0x04:  # Usage Page (global)
            usage_page = data
        elif tag == 0x08:  # Usage (local)
            usage = data
        elif tag == 0xA0:  # Collection
            if usage_page == RAW_USAGE_PAGE and usage == RAW_USAGE:
                return True
            usage = None
        elif tag in (0x80, 0x90, 0xB0, 0xC0):  # Input/Output/Feature/End: locals reset
            usage = None
        i += 1 + size
    return False


def parse_uevent(text):
    info = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        info[key] = value
    return info


def discover(sysfs=SYSFS_HIDRAW, dev="/dev"):
    """List hidraw nodes that expose the VIA raw HID interface."""
    found = []
    if not sysfs.is_dir():
        return found
    nodes = [p for p in sysfs.iterdir() if p.name.startswith("hidraw") and p.name[6:].isdigit()]
    for node in sorted(nodes, key=lambda p: int(p.name[6:])):
        try:
            descriptor = (node / "device/report_descriptor").read_bytes()
            uevent = parse_uevent((node / "device/uevent").read_text())
        except OSError:
            continue
        if not has_raw_usage(descriptor):
            continue
        # HID_ID is bus:vendor:product, each zero-padded to 4 or 8 hex digits.
        parts = uevent.get("HID_ID", "0:0:0").split(":")
        vid = int(parts[1], 16) if len(parts) > 1 else 0
        pid = int(parts[2], 16) if len(parts) > 2 else 0
        found.append({
            "path": f"{dev}/{node.name}",
            "name": uevent.get("HID_NAME", node.name),
            "vid": vid,
            "pid": pid,
            "serial": uevent.get("HID_UNIQ", ""),
        })

    # Stable ids: vid:pid, suffixed when several identical boards are attached.
    counts = {}
    for d in found:
        base = f"{d['vid']:04x}:{d['pid']:04x}"
        counts[base] = counts.get(base, 0) + 1
        d["id"] = base if counts[base] == 1 else f"{base}#{counts[base]}"
    return found


def select_devices(devices, selector):
    if not devices:
        raise QmkError("no VIA-enabled QMK keyboard found")
    if selector in (None, ""):
        return devices[:1]
    if selector == "all":
        return devices
    if selector.isdigit() and int(selector) < len(devices):
        return [devices[int(selector)]]
    for d in devices:
        if selector in (d["id"], d["path"]):
            return [d]
    needle = selector.lower()
    matches = [d for d in devices if needle in d["name"].lower()]
    if matches:
        return matches[:1]
    raise QmkError(f"no keyboard matches '{selector}'")


# ---------------------------------------------------------------- effect names


CONTROLS = ("speed", "hue", "saturation")


def effect_list(options):
    """Turn a VIA-style option list into per-effect entries indexed by number.

    Entries are plain names, whose position is the effect number (entry 0 is
    "off"); [name, number] pairs as in VIA custom dropdowns; or objects with
    "name", an optional "value", and an optional "controls" list naming the
    settings the effect reacts to. Returns (names, controls); a controls
    entry of None means unknown, so every control should stay available.
    """
    entries = {}
    for position, option in enumerate(options):
        if isinstance(option, str):
            entries[position] = (option, None)
        elif isinstance(option, list) and len(option) == 2 and isinstance(option[0], str) and isinstance(option[1], int):
            entries[option[1]] = (option[0], None)
        elif isinstance(option, dict) and isinstance(option.get("name"), str):
            value = option.get("value", position)
            controls = option.get("controls")
            if not isinstance(value, int):
                raise QmkError(f"invalid effect value in {option!r}")
            if controls is not None and (not isinstance(controls, list) or not set(controls) <= set(CONTROLS)):
                raise QmkError(f"controls must be a subset of {list(CONTROLS)} in {option!r}")
            entries[value] = (option["name"], controls)
        else:
            raise QmkError(f"invalid effect entry {option!r}")
    size = max(entries) + 1 if entries else 0
    blank = (None, None)
    return [entries.get(i, blank)[0] for i in range(size)], [entries.get(i, blank)[1] for i in range(size)]


def load_mapping(path, effects_dir=EFFECTS_DIR):
    """Read one mapping file, following its preset if it has one."""
    data = json.loads(path.read_text())
    if "preset" in data:
        preset = json.loads((effects_dir / "presets" / f"{data['preset']}.json").read_text())
        data = {**preset, **{k: v for k, v in data.items() if k != "preset"}}
    if not isinstance(data.get("effects"), list):
        raise QmkError(f"{path.name} has no effects list")
    return data


def effect_names(device, lighting, effects_dir=EFFECTS_DIR):
    """(names, controls) for a keyboard from effects/<vid>-<pid>.json, if any."""
    path = effects_dir / f"{device['vid']:04x}-{device['pid']:04x}.json"
    try:
        data = load_mapping(path, effects_dir)
    except (OSError, ValueError, QmkError):
        return None
    if data.get("lighting") not in (None, lighting):
        return None
    try:
        return effect_list(data["effects"])
    except QmkError:
        return None


# ---------------------------------------------------------------- transport


class HidrawTransport:
    def __init__(self, path):
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EPERM):
                raise QmkError(f"permission denied opening {path}; run `qmk_rgb.py udev-rule` for a fix") from e
            raise QmkError(f"cannot open {path}: {e.strerror}") from e

    def close(self):
        os.close(self.fd)

    def drain(self):
        while select.select([self.fd], [], [], 0)[0]:
            try:
                os.read(self.fd, REPORT_SIZE)
            except BlockingIOError:
                return

    def write(self, report):
        # hidraw wants a leading report id; VIA's interface has none, so 0.
        os.write(self.fd, b"\x00" + report)

    def read(self, timeout):
        if not select.select([self.fd], [], [], timeout)[0]:
            return None
        return os.read(self.fd, REPORT_SIZE)


class Keyboard:
    def __init__(self, transport):
        self.transport = transport
        self.protocol = None
        self.lighting = None

    def command(self, *payload):
        report = bytes(payload).ljust(REPORT_SIZE, b"\x00")
        self.transport.drain()
        self.transport.write(report)
        # The firmware echoes the command id back in byte 0, or replaces it
        # with id_unhandled. Ignore any unrelated reports that slip in.
        for _ in range(8):
            reply = self.transport.read(TIMEOUT)
            if reply is None:
                break
            if reply[0] in (payload[0], ID_UNHANDLED):
                return reply
        raise QmkError("keyboard did not respond")

    def handshake(self):
        reply = self.command(ID_GET_PROTOCOL_VERSION)
        if reply[0] == ID_UNHANDLED:
            raise QmkError("keyboard does not speak the VIA protocol")
        self.protocol = (reply[1] << 8) | reply[2]
        self.lighting = self.detect_lighting()
        return self.protocol

    def detect_lighting(self):
        if self.protocol < 12:
            reply = self.command(ID_CUSTOM_GET_VALUE, LEGACY_VALUES[VALUE_BRIGHTNESS])
            return "legacy" if reply[0] != ID_UNHANDLED else None
        for name in PROBE_ORDER:
            reply = self.command(ID_CUSTOM_GET_VALUE, CHANNELS[name], VALUE_BRIGHTNESS)
            if reply[0] != ID_UNHANDLED:
                return name
        return None

    @property
    def has_color(self):
        return self.lighting in ("rgb_matrix", "rgblight", "legacy")

    def _require_lighting(self):
        if not self.lighting:
            raise QmkError("keyboard firmware exposes no VIA lighting controls")

    def get(self, value_id):
        self._require_lighting()
        if self.lighting == "legacy":
            reply = self.command(ID_CUSTOM_GET_VALUE, LEGACY_VALUES[value_id])
            data = reply[2:4]
        else:
            reply = self.command(ID_CUSTOM_GET_VALUE, CHANNELS[self.lighting], value_id)
            data = reply[3:5]
        if reply[0] == ID_UNHANDLED:
            raise QmkError(f"keyboard rejected read of value {value_id}")
        return data

    def set(self, value_id, *data):
        self._require_lighting()
        data = [max(0, min(255, int(v))) for v in data]
        if self.lighting == "legacy":
            reply = self.command(ID_CUSTOM_SET_VALUE, LEGACY_VALUES[value_id], *data)
        else:
            reply = self.command(ID_CUSTOM_SET_VALUE, CHANNELS[self.lighting], value_id, *data)
        if reply[0] == ID_UNHANDLED:
            raise QmkError(f"keyboard rejected write of value {value_id}")

    def save(self):
        self._require_lighting()
        if self.lighting == "legacy":
            self.command(ID_CUSTOM_SAVE)
        else:
            self.command(ID_CUSTOM_SAVE, CHANNELS[self.lighting])

    def state(self):
        out = {
            "brightness": self.get(VALUE_BRIGHTNESS)[0],
            "effect": self.get(VALUE_EFFECT)[0],
            "speed": self.get(VALUE_SPEED)[0],
        }
        if self.has_color:
            hue, sat = self.get(VALUE_COLOR)
            out["hue"] = hue
            out["saturation"] = sat
        return out

    def set_brightness(self, target):
        """Write a brightness and return (level read back, value written).

        Firmware with a brightness cap (RGB_MATRIX_MAXIMUM_BRIGHTNESS) scales
        VIA's 0-255 down to fewer levels and back up on read, rounding down
        both ways, so writing a value can read back lower. Writing back what
        was read would then dim the keyboard a step at a time. Nudge the
        written value up until the level read back reaches the target.
        """
        write = target
        while True:
            self.set(VALUE_BRIGHTNESS, write)
            level = self.get(VALUE_BRIGHTNESS)[0]
            if level >= target or write >= 255:
                return level, write
            write += 1

    def step_effect(self, direction):
        """Move to the next/previous effect, wrapping around.

        The firmware never reports how many effects it was built with, but it
        clamps out-of-range modes to the last one, which lets us find the end.
        Effect 0 means "off" and is skipped.
        """
        current = self.get(VALUE_EFFECT)[0]
        if direction > 0:
            target = current + 1
            self.set(VALUE_EFFECT, target)
            if self.get(VALUE_EFFECT)[0] != target:
                self.set(VALUE_EFFECT, 1)
        else:
            if current <= 1:
                self.set(VALUE_EFFECT, 255)  # clamps to the last effect
            else:
                self.set(VALUE_EFFECT, current - 1)
        return self.get(VALUE_EFFECT)[0]


# ---------------------------------------------------------------- helpers


def resolve(value, current, low=0, high=255):
    """Apply an absolute ("120") or relative ("+16", "-16") value."""
    text = str(value).strip()
    if text.endswith("%"):
        number = round(float(text[:-1].lstrip("+-") or 0) * high / 100)
        sign = text[0] if text[0] in "+-" else ""
        text = f"{sign}{number}"
    if text[:1] in ("+", "-"):
        result = current + int(text)
    else:
        result = int(text)
    return max(low, min(high, result))


def state_file(device):
    return STATE_DIR / (device["id"].replace(":", "-").replace("#", "-") + ".json")


def load_state(device):
    try:
        data = json.loads(state_file(device).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_state(device, **values):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_file(device).write_text(json.dumps({**load_state(device), **values}))
    except OSError:
        pass


def remember_effect(device, effect):
    update_state(device, lastEffect=effect)


def recall_effect(device):
    try:
        return int(load_state(device).get("lastEffect", 1)) or 1
    except (TypeError, ValueError):
        return 1


def brightness_max(device):
    """Highest brightness the keyboard reports, learned from writing 255."""
    try:
        return max(1, min(255, int(load_state(device).get("brightnessMax", 255))))
    except (TypeError, ValueError):
        return 255


def open_keyboard(device):
    kb = Keyboard(HidrawTransport(device["path"]))
    try:
        kb.handshake()
    except Exception:
        kb.transport.close()
        raise
    return kb


def describe(device, kb=None, error=None):
    out = {
        "id": device["id"],
        "name": device["name"],
        "path": device["path"],
        "accessible": error is None or "permission denied" not in error,
    }
    if kb:
        out["protocol"] = kb.protocol
        out["lighting"] = kb.lighting
        out["hasColor"] = kb.has_color
        out["brightnessMax"] = brightness_max(device)
        mapping = effect_names(device, kb.lighting) if kb.lighting else None
        if mapping:
            out["effects"], out["effectControls"] = mapping
    if error:
        out["error"] = error
    return out


# ---------------------------------------------------------------- commands


def cmd_list(args):
    return {"devices": discover()}


def cmd_status(args):
    devices = discover()
    if args.device:
        devices = select_devices(devices, args.device)
    out = []
    for device in devices:
        try:
            kb = open_keyboard(device)
        except QmkError as e:
            out.append(describe(device, error=str(e)))
            continue
        try:
            entry = describe(device, kb)
            if kb.lighting:
                entry.update(kb.state())
            out.append(entry)
        except QmkError as e:
            out.append(describe(device, kb, error=str(e)))
        finally:
            kb.transport.close()
    return {"devices": out}


def cmd_set(args):
    results = []
    for device in select_devices(discover(), args.device):
        kb = open_keyboard(device)
        try:
            current = kb.state()
            if args.toggle:
                if current["effect"]:
                    remember_effect(device, current["effect"])
                    kb.set(VALUE_EFFECT, 0)
                else:
                    kb.set(VALUE_EFFECT, recall_effect(device))
            if args.off:
                if current["effect"]:
                    remember_effect(device, current["effect"])
                kb.set(VALUE_EFFECT, 0)
            if args.on and not current["effect"]:
                kb.set(VALUE_EFFECT, recall_effect(device))
            if args.brightness is not None:
                top = brightness_max(device)
                target = resolve(args.brightness, current["brightness"], high=top)
                # At the top, write 255 so the firmware lands on its real
                # maximum, and remember what that reads back as.
                level, written = kb.set_brightness(255 if target >= top else target)
                if written >= 255 and level != top:
                    update_state(device, brightnessMax=level)
            if args.effect is not None:
                if args.effect in ("next", "prev"):
                    effect = kb.step_effect(1 if args.effect == "next" else -1)
                else:
                    effect = resolve(args.effect, current["effect"])
                    kb.set(VALUE_EFFECT, effect)
                if effect:
                    remember_effect(device, effect)
            if args.speed is not None:
                kb.set(VALUE_SPEED, resolve(args.speed, current["speed"]))
            if args.hue is not None or args.saturation is not None or args.color is not None:
                if not kb.has_color:
                    raise QmkError(f"{device['name']} has single-color lighting")
                hue, sat = current["hue"], current["saturation"]
                if args.color is not None:
                    hue, sat = hex_to_hs(args.color)
                if args.hue is not None:
                    hue = resolve(args.hue, hue)
                if args.saturation is not None:
                    sat = resolve(args.saturation, sat)
                kb.set(VALUE_COLOR, hue, sat)
            if args.save:
                kb.save()
            entry = describe(device, kb)
            entry.update(kb.state())
            results.append(entry)
        finally:
            kb.transport.close()
    return {"devices": results}


def cmd_save(args):
    saved = []
    for device in select_devices(discover(), args.device):
        kb = open_keyboard(device)
        try:
            kb.save()
            saved.append(device["id"])
        finally:
            kb.transport.close()
    return {"saved": saved}


def cmd_udev_rule(args):
    devices = discover()
    if args.device:
        devices = select_devices(devices, args.device)
    seen = set()
    lines = ["# QMK keyboards controlled by omarchy-qmk-rgb (VIA raw HID)"]
    for d in devices:
        key = (d["vid"], d["pid"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"# {d['name']}")
        lines.append(
            'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", '
            f'ATTRS{{idVendor}}=="{d["vid"]:04x}", ATTRS{{idProduct}}=="{d["pid"]:04x}", '
            'MODE="0660", TAG+="uaccess"'
        )
    if len(lines) == 1:
        raise QmkError("no VIA-enabled QMK keyboard found")
    return "\n".join(lines) + "\n"


def hex_to_hs(color):
    text = color.lstrip("#")
    if len(text) != 6:
        raise QmkError(f"invalid color '{color}', expected #rrggbb")
    r, g, b = (int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))
    high, low = max(r, g, b), min(r, g, b)
    delta = high - low
    if delta == 0:
        hue = 0.0
    elif high == r:
        hue = ((g - b) / delta) % 6
    elif high == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    sat = 0 if high == 0 else delta / high
    return round(hue / 6 * 255) % 256, round(sat * 255)


def build_parser():
    parser = argparse.ArgumentParser(prog="qmk_rgb.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def device_arg(p):
        p.add_argument("-d", "--device", help="index, vid:pid id, /dev/hidrawN, name substring, or 'all'")

    p = sub.add_parser("list", help="list VIA-capable keyboards without opening them")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="read lighting state (all keyboards unless --device)")
    device_arg(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("set", help="change lighting; values accept 0-255, +N/-N, or N%%")
    device_arg(p)
    p.add_argument("-b", "--brightness")
    p.add_argument("-e", "--effect", help="effect number, 'next', or 'prev' (0 turns lighting off)")
    p.add_argument("-s", "--speed")
    p.add_argument("--hue")
    p.add_argument("--saturation", "--sat")
    p.add_argument("-c", "--color", help="#rrggbb, converted to hue/saturation")
    toggles = p.add_mutually_exclusive_group()
    toggles.add_argument("--on", action="store_true", help="restore the last effect if lighting is off")
    toggles.add_argument("--off", action="store_true", help="turn lighting off")
    toggles.add_argument("--toggle", action="store_true", help="flip lighting on/off")
    p.add_argument("--save", action="store_true", help="persist to the keyboard's EEPROM")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("save", help="persist the current lighting to EEPROM")
    device_arg(p)
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("udev-rule", help="print a udev rule granting your session access")
    device_arg(p)
    p.set_defaults(func=cmd_udev_rule)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except QmkError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    except OSError as e:
        print(json.dumps({"error": f"device I/O failed: {e.strerror or e}"}))
        return 1
    if isinstance(result, str):
        sys.stdout.write(result)
    else:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
