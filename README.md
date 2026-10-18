# QMK RGB for Omarchy

An [Omarchy](https://omarchy.org) shell plugin that controls the RGB lighting
of QMK keyboards from the bar. Click the keyboard icon for a panel with
brightness, effect, speed, and color. **Match theme** sets the keyboard to
your Omarchy theme's color, and `followTheme` does it automatically whenever
you switch themes. Use the scroll wheel on the icon to
change brightness, and right-click it to turn the lighting on or off.

It talks to the keyboard over the [VIA](https://www.caniusevia.com/) raw HID
protocol, the same channel the VIA and Vial configurators use. You don't need
to flash any firmware or install any packages; the backend is a single Python
script that uses only the standard library.

## Requirements

- Omarchy with the Quickshell-based `omarchy-shell`.
- A QMK keyboard built with VIA enabled (`VIA_ENABLE = yes`). Most keyboards
  sold as "VIA compatible" ship this way.
- The firmware must include one of RGB Matrix, RGB Light, or LED Matrix.
  LED Matrix is single-color, so the panel hides its color controls.
- `python3` (included with Omarchy).

Protocol 12 and newer (QMK 0.19+) is fully supported. Older firmware falls
back to the legacy lighting commands.

## Install

```bash
omarchy plugin add https://github.com/kcotugno/omarchy-qmk-rgb.git
omarchy plugin enable kcotugno.qmk-rgb
```

The widget goes into the right side of the bar. You can move it with
`omarchy bar move kcotugno.qmk-rgb --section <left|center|right>`.

### Keyboard permissions

Your user needs read/write access to the keyboard's `/dev/hidraw*` node. If
VIA already works in your browser, you probably have access. Otherwise, the
panel shows a permission error. To fix it, generate a udev rule for your
connected keyboards and install it:

```bash
~/.config/omarchy/plugins/kcotugno.qmk-rgb/qmk_rgb.py udev-rule \
  | sudo tee /etc/udev/rules.d/60-qmk-rgb.rules
sudo udevadm control --reload && sudo udevadm trigger
```

The rule grants access only to the keyboards' vendor/product ids, using
`TAG+="uaccess"`, so only the user logged in at the seat gets access.

## Settings

Edit the widget's entry in `~/.config/omarchy/shell.json`:

```json
{ "id": "kcotugno.qmk-rgb", "autoSave": true, "applyToAll": false, "followTheme": false, "step": 16, "effectNames": "" }
```

| Key           | Default | Meaning |
|---------------|---------|---------|
| `autoSave`    | `true`  | Writes changes to the keyboard's EEPROM 1.5s after you stop adjusting. When it's off, the panel shows a **Save to keyboard** button, and unsaved changes are lost when the keyboard is unplugged. |
| `applyToAll`  | `false` | Sends every change to all connected VIA keyboards instead of only the selected one. |
| `followTheme` | `false` | Sets the keyboard color to the new theme's color whenever you switch themes. See [Match theme](#match-theme). |
| `step`        | `16`    | How much one scroll notch or arrow key changes a value, on the 0-255 scale. |
| `effectNames` | `""`    | Comma-separated effect names, starting with effect 1. Overrides the keyboard's [mapping file](#effect-names). Leave blank to use the mapping file. |

## Effect names

The keyboard reports effects only by number. Names come from mapping files in
[`effects/`](effects/), one per keyboard, named `<vid>-<pid>.json` after the
USB vendor and product ids (lowercase hex). `omarchy plugin update` pulls in
new mappings. When a keyboard has no mapping file, the panel shows effect
numbers.

```json
{
  "name": "Keebio Iris Rev. 8",
  "vendorId": "0xCB10",
  "productId": "0x8256",
  "lighting": "rgb_matrix",
  "source": "https://github.com/the-via/keyboards/blob/master/v3/keebio/iris/iris-rev8.json",
  "preset": "via-qmk-rgb-matrix"
}
```

| Key         | Meaning |
|-------------|---------|
| `name`      | Keyboard name, for humans. |
| `vendorId`, `productId` | USB ids, as written in the keyboard's VIA definition. They must match the file name. |
| `lighting`  | `rgb_matrix`, `rgblight`, `led_matrix`, or `legacy`. The names apply only when the keyboard reports this lighting system. |
| `source`    | Where the names came from. |
| `controlsSource` | Where the per-effect `controls` came from (optional). |
| `notes`     | Caveats, such as build options the mapping assumes (optional). |
| `preset`    | Use a shared list from `effects/presets/<preset>.json`. |
| `effects`   | The effects, in place of (or overriding) a preset. |

`effects` uses the same format as VIA's effect dropdown. Each entry's position
is its effect number, and entry 0 is "off". Entries can also be
`["Name", number]` pairs, as in VIA's custom menus, to skip or reorder
numbers.

Entries can also be objects that say which settings the effect uses:

```json
{ "name": "Digital Rain", "controls": [] },
{ "name": "Solid Color", "controls": ["hue", "saturation"] },
{ "name": "Cycle All", "controls": ["speed", "saturation"] }
```

`controls` lists which of `speed`, `hue`, and `saturation` change the effect.
The panel hides the others while that effect is active. Brightness and the
effect picker are always shown. Objects may also set `"value"` to give an
explicit effect number. If an entry has no `controls`, the panel shows every
control.

The `via-qmk-rgb-matrix` preset's controls come from reading each effect in
QMK's
[`quantum/rgb_matrix/animations`](https://github.com/qmk/qmk_firmware/tree/master/quantum/rgb_matrix/animations).
For example, Cycle effects calculate their own hue from a timer, and Digital
Rain is hard-coded green. They assume the firmware leaves
`RGB_MATRIX_SOLID_REACTIVE_GRADIENT_MODE` off. With that option on, the Solid
Reactive effects ignore the hue setting.

### Adding a keyboard

1. Find the keyboard's VIA definition in
   [the-via/keyboards](https://github.com/the-via/keyboards/tree/master/v3)
   (or its vendor's VIA site) and note `vendorId` and `productId`.
   `qmk_rgb.py list` also shows them as `vid:pid`.
2. Look at the definition's `menus`:
   - `"qmk_rgb_matrix"` means VIA's built-in list. Use
     `"preset": "via-qmk-rgb-matrix"`.
   - A custom lighting menu has its own effect dropdown. Copy its `options`
     into `effects`. If the menu hides its color or speed control for some
     effects (`showIf`), add matching `controls`.
3. Save the file as `effects/<vid>-<pid>.json` and run the tests.
   `python3 -m unittest discover -s tests` checks every mapping file.

VIA's built-in list assumes the firmware enables every RGB Matrix effect, as
VIA builds of QMK usually do. If the names look shifted on your keyboard,
compare the number of effects with the list. `qmk_rgb.py set -e prev` from
effect 1 jumps to the last effect.

## Match theme

**Match theme** converts the theme's keyboard color to the keyboard's hue and
saturation. That's the theme's `keyboard.rgb`, which Omarchy generates from the
accent color unless the theme ships its own. If the current effect ignores hue (Digital Rain, the Cycle effects,
or lighting that's off), it also switches to effect 1 (Solid Color), so the
change shows up. The button is highlighted while the keyboard matches the
theme color. The keyboard's brightness stays as it is.

LEDs don't reproduce colors exactly, so expect a close match rather than an
identical one. Very desaturated colors come out close to white.

### Following the theme

With `followTheme` on, the panel watches for theme switches and applies the
new theme's color to the selected keyboard, or to every keyboard with
`applyToAll`. Unlike the button, it only changes the color: it keeps the
current effect, and it doesn't turn the lighting on. If the effect ignores hue,
the new color shows up once you switch to one that uses it. With `autoSave` on,
the color is saved to the keyboard, so it's still there after you unplug it.
Theme switches are rare, so saving on each one doesn't wear out the keyboard's
memory.

The shell has to be running when you switch themes. If you set up an Omarchy
`theme-set` hook to color the keyboard, remove it when you turn this on, so the
two don't write to the keyboard at the same time.

## Keyboard navigation

When the panel is open, the arrow keys move between rows: ↑/↓ chooses a row
and ←/→ adjusts its value. Enter toggles the lighting, and Esc closes the
panel.

## Command line and keybindings

`qmk_rgb.py` works on its own, so you can bind it to Hyprland keys:

```bash
qmk_rgb.py list                          # VIA keyboards, without opening them
qmk_rgb.py status                        # current lighting as JSON
qmk_rgb.py set --brightness +10%         # values: 0-255, +N/-N, or N%
qmk_rgb.py set --effect next             # next/prev wrap around
qmk_rgb.py set --color '#ff8800' --save  # --save persists to EEPROM
qmk_rgb.py set --toggle -d all           # every connected keyboard
qmk_rgb.py save -d "iris"                # --device: index, vid:pid, /dev/hidrawN, or name
```

The running panel also exposes IPC methods:

```bash
omarchy-shell kcotugno.qmk-rgb toggleLighting
omarchy-shell kcotugno.qmk-rgb matchTheme      # color = theme keyboard color
omarchy-shell kcotugno.qmk-rgb nextEffect      # also: previousEffect, refresh, toggle
```

Example bindings for `~/.config/hypr/bindings.lua`:

```lua
local qmk = "~/.config/omarchy/plugins/kcotugno.qmk-rgb/qmk_rgb.py"
o.bind("SUPER + ALT + F10", "Keyboard lights", "omarchy-shell kcotugno.qmk-rgb toggleLighting")
o.bind("SUPER + ALT + F11", "Keyboard dimmer", qmk .. " set -b -10% --save")
o.bind("SUPER + ALT + F12", "Keyboard brighter", qmk .. " set -b +10% --save")
```

## How it works

- **Discovery:** The backend reads each `/sys/class/hidraw/*/device/report_descriptor`
  and keeps the interfaces that declare usage page `0xFF60`, usage `0x61` (QMK's
  raw HID). It doesn't need to open a device to list it.
- **Protocol:** The backend sends 32-byte VIA reports. It asks for the protocol
  version, then probes the RGB Matrix (3), RGB Light (2), and LED Matrix (5)
  channels with `id_custom_get_value` to find the one the firmware supports.
- **Effect names:** These come from the [`effects/`](effects/) mapping files,
  not from the keyboard.
- **Effect count:** The firmware doesn't report how many effects it has. However, it
  clamps out-of-range modes to the last effect, so next/prev can find the end
  of the list and wrap.
- **Brightness:** Firmware with a brightness cap (QMK's
  `RGB_MATRIX_MAXIMUM_BRIGHTNESS`, e.g. 120 on the Iris Rev. 8) maps VIA's
  0-255 onto fewer levels and rounds down. The top reads back as 252, not 255,
  and writing a value can read back lower. The backend nudges each write up
  until the readback reaches the requested value, so brightness doesn't drift.
  The first time you set full brightness, it remembers that keyboard's real
  maximum and treats it as 100%.
- **On/off:** Effect 0 turns the lighting off. Before turning the lighting
  off, the backend saves the last effect under
  `~/.local/state/omarchy-qmk-rgb/` so that "on" can restore it.

## Development

```bash
python3 -m unittest discover -s tests    # protocol tests against a fake firmware
omarchy plugin validate .                # manifest checks
```

To work on a local checkout, symlink it into the plugins directory and enable
it:

```bash
ln -s "$PWD" ~/.config/omarchy/plugins/kcotugno.qmk-rgb
omarchy-shell shell rescanPlugins
omarchy plugin enable kcotugno.qmk-rgb
```

The plugin folder itself may be a symlink, but it may not contain symlinks.
The shell doesn't follow the link when watching for changes, and
`rescanPlugins` keeps the version of `Panel.qml` it already loaded, so after
editing run `omarchy restart shell` to reload.

## License

[MIT](LICENSE)
