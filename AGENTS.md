# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

An Omarchy shell plugin (`kcotugno.qmk-rgb`) that controls QMK keyboard RGB
lighting over the VIA raw HID protocol. The repo root is the plugin root:
`omarchy plugin add` clones it straight into
`~/.config/omarchy/plugins/kcotugno.qmk-rgb/`.

| File                  | Role |
|-----------------------|------|
| `manifest.json`       | Plugin manifest (`bar-widget`), settings schema and defaults. |
| `Panel.qml`           | Bar button and popout panel. Holds no protocol logic; it shells out to the backend. |
| `qmk_rgb.py`          | Backend CLI. Discovery, VIA protocol, JSON output. Standard library only. |
| `effects/<vid>-<pid>.json` | Per-keyboard effect names (VIA dropdown format, index = effect number). See README "Effect names". |
| `effects/presets/*.json` | Shared effect lists that keyboard files reference with `"preset"`. |
| `tests/test_qmk_rgb.py` | Unit tests against a fake firmware that mimics QMK's `via.c`. |

## Rules

- **Git commits: do not add `Co-Authored-By:` trailers** (or any other
  AI/agent attribution trailers) to commit messages. Commits are authored
  by the human maintainer only.
- Keep `qmk_rgb.py` dependency-free (Python standard library only). Users
  install the plugin with a `git clone`; there is no package step.
- Every backend command prints exactly one JSON document on stdout, or
  `{"error": "..."}` with a non-zero exit. `Panel.qml` parses that output, so
  treat the JSON shape as an API. The one exception is `udev-rule`, which
  prints the rule text.
- Don't write to EEPROM (`save` / `--save`) on every change; the panel
  debounces saves on purpose.
- Effect names belong in mapping files under `effects/`, not in code. Every
  mapping needs a `source` URL. Take names from the keyboard's VIA definition,
  or from VIA's built-in menus in the-via/reader. Don't guess them.
  Per-effect `controls` must come from reading the effect's firmware source
  (or the definition's `showIf` rules); cite it in `controlsSource`.
- Plugin folders may not contain symlinks (`omarchy plugin validate` refuses
  them).
- QML follows Omarchy's shell conventions: import `qs.Commons as Commons`
  and use `Commons.Color`, size with `Style.space()` / `Style.font.*`, and
  build from `qs.Ui` components (`Panel`, `KeyboardPanel`, `PanelSlider`,
  `Button`, ...). Look at `$OMARCHY_PATH/shell/plugins/panels/` for patterns.

## Checks

```bash
python3 -m unittest discover -s tests
omarchy plugin validate .
```

When you change `Panel.qml`, also load it in the shell. The checkout may be
symlinked as `~/.config/omarchy/plugins/kcotugno.qmk-rgb`. File watching does
not follow that link, so run `omarchy-shell shell rescanPlugins` after
editing. Then check the shell log for QML errors
(`journalctl --user -t omarchy-shell`).

When testing against real hardware, don't use `--save` unless you mean to.
Unsaved changes revert when the keyboard is unplugged.
