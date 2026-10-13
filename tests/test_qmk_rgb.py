import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("qmk_rgb", ROOT / "qmk_rgb.py")
qmk_rgb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qmk_rgb)

# The descriptor QMK emits for its raw HID interface.
RAW_DESCRIPTOR = bytes.fromhex(
    "0660ff" "0961" "a101"            # usage page 0xFF60, usage 0x61, collection
    "0962" "1500" "26ff00" "7508" "9520" "8102"  # 32-byte input report
    "0963" "1500" "26ff00" "7508" "9520" "9102"  # 32-byte output report
    "c0"
)
KEYBOARD_DESCRIPTOR = bytes.fromhex("05010906a101050719e029e715002501750195088102c0")


class FakeFirmware:
    """Answers VIA lighting commands the way QMK's via.c does."""

    def __init__(self, protocol=12, channel=3, effects=44, brightness_cap=None):
        self.protocol = protocol
        self.channel = channel
        self.effects = effects
        self.values = {1: [200], 2: [5], 3: [127], 4: [25, 241]}
        # RGB_MATRIX_MAXIMUM_BRIGHTNESS: via.c stores scale8(v, cap) and
        # reports level * 255 / cap, both rounding down.
        self.brightness_cap = brightness_cap
        if brightness_cap:
            self.values[1] = [(200 * brightness_cap) >> 8]
        self.saved = 0
        self.replies = []

    def drain(self):
        self.replies.clear()

    def write(self, report):
        data = bytearray(report.ljust(32, b"\x00"))
        cmd = data[0]
        if cmd == qmk_rgb.ID_GET_PROTOCOL_VERSION:
            data[1:3] = self.protocol.to_bytes(2, "big")
        elif self.protocol < 12 and cmd in (7, 8):
            value_id = {v: k for k, v in qmk_rgb.LEGACY_VALUES.items()}.get(data[1])
            self._value(data, cmd, value_id, 2)
        elif self.protocol < 12 and cmd == qmk_rgb.ID_CUSTOM_SAVE:
            self.saved += 1
        elif cmd in (7, 8, 9) and data[1] != self.channel:
            data[0] = qmk_rgb.ID_UNHANDLED
        elif cmd in (7, 8):
            self._value(data, cmd, data[2], 3)
        elif cmd == qmk_rgb.ID_CUSTOM_SAVE:
            self.saved += 1
        else:
            data[0] = qmk_rgb.ID_UNHANDLED
        self.replies.append(bytes(data))

    def _value(self, data, cmd, value_id, offset):
        if value_id is None:
            data[0] = qmk_rgb.ID_UNHANDLED
            return
        size = len(self.values[value_id])
        if cmd == 7:
            incoming = list(data[offset:offset + size])
            if value_id == 2 and incoming[0] != 0:
                incoming[0] = min(incoming[0], self.effects - 1)
            if value_id == 1 and self.brightness_cap:
                incoming[0] = (incoming[0] * self.brightness_cap) >> 8
            self.values[value_id] = incoming
        else:
            values = list(self.values[value_id])
            if value_id == 1 and self.brightness_cap:
                values[0] = values[0] * 255 // self.brightness_cap
            data[offset:offset + size] = bytes(values)

    def read(self, timeout):
        return self.replies.pop(0) if self.replies else None


def keyboard(**kwargs):
    kb = qmk_rgb.Keyboard(FakeFirmware(**kwargs))
    kb.handshake()
    return kb


class DescriptorTest(unittest.TestCase):
    def test_detects_raw_hid_interface(self):
        self.assertTrue(qmk_rgb.has_raw_usage(RAW_DESCRIPTOR))

    def test_ignores_plain_keyboard_interface(self):
        self.assertFalse(qmk_rgb.has_raw_usage(KEYBOARD_DESCRIPTOR))

    def test_ignores_other_vendor_pages(self):
        self.assertFalse(qmk_rgb.has_raw_usage(bytes.fromhex("0600ff0901a101c0")))


class DiscoveryTest(unittest.TestCase):
    def make_node(self, root, name, descriptor, hid_id, hid_name):
        device = root / name / "device"
        device.mkdir(parents=True)
        (device / "report_descriptor").write_bytes(descriptor)
        (device / "uevent").write_text(f"HID_ID={hid_id}\nHID_NAME={hid_name}\nHID_UNIQ=\n")

    def test_lists_only_via_interfaces_with_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_node(root, "hidraw0", KEYBOARD_DESCRIPTOR, "0003:0000CB10:00008256", "Iris")
            self.make_node(root, "hidraw1", RAW_DESCRIPTOR, "0003:0000CB10:00008256", "Iris")
            self.make_node(root, "hidraw10", RAW_DESCRIPTOR, "0003:0000CB10:00008256", "Iris")
            self.make_node(root, "hidraw2", RAW_DESCRIPTOR, "0003:00003434:00000361", "Keychron Q6")
            devices = qmk_rgb.discover(root)
        self.assertEqual([d["path"] for d in devices], ["/dev/hidraw1", "/dev/hidraw2", "/dev/hidraw10"])
        self.assertEqual([d["id"] for d in devices], ["cb10:8256", "3434:0361", "cb10:8256#2"])

    def test_selects_by_index_id_path_and_name(self):
        devices = [
            {"id": "cb10:8256", "path": "/dev/hidraw1", "name": "Keebio Iris"},
            {"id": "3434:0361", "path": "/dev/hidraw2", "name": "Keychron Q6"},
        ]
        self.assertEqual(qmk_rgb.select_devices(devices, None), devices[:1])
        self.assertEqual(qmk_rgb.select_devices(devices, "1"), devices[1:])
        self.assertEqual(qmk_rgb.select_devices(devices, "3434:0361"), devices[1:])
        self.assertEqual(qmk_rgb.select_devices(devices, "/dev/hidraw2"), devices[1:])
        self.assertEqual(qmk_rgb.select_devices(devices, "keychron"), devices[1:])
        self.assertEqual(qmk_rgb.select_devices(devices, "all"), devices)
        with self.assertRaises(qmk_rgb.QmkError):
            qmk_rgb.select_devices(devices, "planck")


class ProtocolTest(unittest.TestCase):
    def test_detects_rgb_matrix_channel(self):
        kb = keyboard(channel=3)
        self.assertEqual(kb.protocol, 12)
        self.assertEqual(kb.lighting, "rgb_matrix")
        self.assertEqual(kb.state(), {"brightness": 200, "effect": 5, "speed": 127, "hue": 25, "saturation": 241})

    def test_detects_rgblight_and_led_matrix(self):
        self.assertEqual(keyboard(channel=2).lighting, "rgblight")
        led = keyboard(channel=5)
        self.assertEqual(led.lighting, "led_matrix")
        self.assertFalse(led.has_color)
        self.assertNotIn("hue", led.state())

    def test_no_lighting(self):
        kb = keyboard(channel=99)
        self.assertIsNone(kb.lighting)
        with self.assertRaises(qmk_rgb.QmkError):
            kb.state()

    def test_legacy_protocol(self):
        kb = keyboard(protocol=9)
        self.assertEqual(kb.lighting, "legacy")
        kb.set(qmk_rgb.VALUE_COLOR, 170, 255)
        self.assertEqual(kb.state()["hue"], 170)
        kb.save()
        self.assertEqual(kb.transport.saved, 1)

    def test_set_and_save(self):
        kb = keyboard()
        kb.set(qmk_rgb.VALUE_BRIGHTNESS, 300)
        self.assertEqual(kb.state()["brightness"], 255)
        kb.save()
        self.assertEqual(kb.transport.saved, 1)

    def test_capped_brightness_matches_real_firmware(self):
        # Readbacks measured on a Keebio Iris Rev. 8 (max_brightness 120).
        kb = keyboard(brightness_cap=120)
        for written, read in ((255, 252), (253, 250), (250, 248), (248, 246)):
            kb.set(qmk_rgb.VALUE_BRIGHTNESS, written)
            self.assertEqual(kb.get(qmk_rgb.VALUE_BRIGHTNESS)[0], read)

    def test_set_brightness_does_not_drift(self):
        kb = keyboard(brightness_cap=120)
        level, _ = kb.set_brightness(252)
        self.assertEqual(level, 252)
        for _ in range(5):
            level, _ = kb.set_brightness(level)
        self.assertEqual(level, 252)

    def test_set_brightness_reaches_the_top(self):
        kb = keyboard(brightness_cap=120)
        self.assertEqual(kb.set_brightness(255), (252, 255))
        uncapped = keyboard()
        self.assertEqual(uncapped.set_brightness(255), (255, 255))
        self.assertEqual(uncapped.set_brightness(100), (100, 100))

    def test_effect_steps_wrap_both_ways(self):
        kb = keyboard(effects=4)
        kb.set(qmk_rgb.VALUE_EFFECT, 3)
        self.assertEqual(kb.step_effect(1), 1)
        self.assertEqual(kb.step_effect(-1), 3)
        self.assertEqual(kb.step_effect(-1), 2)


class EffectNamesTest(unittest.TestCase):
    def write(self, root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_plain_names_are_indexed_by_position(self):
        names, controls = qmk_rgb.effect_list(["Off", "Solid", "Breathing"])
        self.assertEqual(names, ["Off", "Solid", "Breathing"])
        self.assertEqual(controls, [None, None, None])

    def test_pairs_set_explicit_numbers(self):
        names, _ = qmk_rgb.effect_list([["Off", 0], ["Rainbow", 3]])
        self.assertEqual(names, ["Off", None, None, "Rainbow"])

    def test_objects_carry_controls(self):
        names, controls = qmk_rgb.effect_list([
            {"name": "Off", "controls": []},
            {"name": "Solid", "controls": ["hue", "saturation"]},
            {"name": "Rain", "value": 3},
        ])
        self.assertEqual(names, ["Off", "Solid", None, "Rain"])
        self.assertEqual(controls, [[], ["hue", "saturation"], None, None])

    def test_rejects_bad_entries(self):
        for bad in ([42], [{"name": "X", "controls": ["color"]}], [{"name": "X", "value": "1"}]):
            with self.assertRaises(qmk_rgb.QmkError):
                qmk_rgb.effect_list(bad)

    def test_looks_up_by_vid_pid_and_follows_presets(self):
        device = {"vid": 0xCB10, "pid": 0x8256}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "presets/base.json", {"lighting": "rgb_matrix", "effects": ["Off", "Solid"]})
            self.write(root, "cb10-8256.json", {"name": "Iris", "preset": "base"})
            self.assertEqual(qmk_rgb.effect_names(device, "rgb_matrix", root), (["Off", "Solid"], [None, None]))
            # A mapping for one lighting system doesn't apply to another.
            self.assertIsNone(qmk_rgb.effect_names(device, "rgblight", root))
            self.assertIsNone(qmk_rgb.effect_names({"vid": 1, "pid": 2}, "rgb_matrix", root))

    def test_keyboard_file_overrides_preset_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "presets/base.json", {"lighting": "rgb_matrix", "effects": ["Off", "Solid"]})
            self.write(root, "0001-0002.json", {"preset": "base", "effects": ["Off", "Custom"]})
            self.assertEqual(qmk_rgb.effect_names({"vid": 1, "pid": 2}, "rgb_matrix", root)[0], ["Off", "Custom"])

    def test_shipped_mappings_are_valid(self):
        effects_dir = ROOT / "effects"
        files = sorted(effects_dir.glob("*.json"))
        self.assertTrue(files, "no mapping files shipped")
        for path in files:
            with self.subTest(path.name):
                data = qmk_rgb.load_mapping(path, effects_dir)
                vid, pid = int(data["vendorId"], 16), int(data["productId"], 16)
                self.assertEqual(path.name, f"{vid:04x}-{pid:04x}.json", "file name must be <vid>-<pid>.json in lowercase hex")
                self.assertIn(data.get("lighting"), qmk_rgb.CHANNELS.keys() | {"legacy"})
                self.assertTrue(data.get("name"))
                names, _ = qmk_rgb.effect_list(data["effects"])
                self.assertGreater(len(names), 1)

    def test_rgb_matrix_preset_controls(self):
        data = qmk_rgb.load_mapping(ROOT / "effects/presets/via-qmk-rgb-matrix.json")
        names, controls = qmk_rgb.effect_list(data["effects"])
        by_name = dict(zip(names, controls))
        self.assertEqual(by_name["Solid Color"], ["hue", "saturation"])
        self.assertEqual(by_name["Digital Rain"], [])
        self.assertNotIn("hue", by_name["Cycle All"])
        self.assertNotIn("hue", by_name["Jellybean Raindrops"])


class HelperTest(unittest.TestCase):
    def test_resolve_absolute_relative_and_percent(self):
        self.assertEqual(qmk_rgb.resolve("120", 50), 120)
        self.assertEqual(qmk_rgb.resolve("+16", 250), 255)
        self.assertEqual(qmk_rgb.resolve("-16", 10), 0)
        self.assertEqual(qmk_rgb.resolve("50%", 0), 128)
        self.assertEqual(qmk_rgb.resolve("-10%", 200), 174)

    def test_hex_to_hue_saturation(self):
        self.assertEqual(qmk_rgb.hex_to_hs("#ff0000"), (0, 255))
        self.assertEqual(qmk_rgb.hex_to_hs("00ff00"), (85, 255))
        self.assertEqual(qmk_rgb.hex_to_hs("#0000ff"), (170, 255))
        self.assertEqual(qmk_rgb.hex_to_hs("#ffffff"), (0, 0))
        with self.assertRaises(qmk_rgb.QmkError):
            qmk_rgb.hex_to_hs("red")


if __name__ == "__main__":
    unittest.main()
