import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A bar button that opens a lighting panel for QMK keyboards. Everything that
// touches the hardware goes through qmk_rgb.py next to this file, which speaks
// the VIA raw HID protocol; this file only keeps the last state it reported
// and queues changes back to it, one process at a time.
Panel {
  id: root
  moduleName: "kcotugno.qmk-rgb"
  ipcTarget: "kcotugno.qmk-rgb"
  manageIpc: false

  readonly property string pluginDir: {
    var here = Qt.resolvedUrl(".").toString()
    return here.replace(/^file:\/\//, "").replace(/\/$/, "")
  }
  readonly property var backend: ["python3", pluginDir + "/qmk_rgb.py"]

  readonly property bool autoSave: setting("autoSave", true) !== false
  readonly property bool applyToAll: setting("applyToAll", false) === true
  readonly property int step: Math.max(1, Number(setting("step", 16)) || 16)
  readonly property var effectNames: {
    var raw = String(setting("effectNames", "") || "")
    if (raw.trim() === "") return []
    return raw.split(",").map(function(name) { return name.trim() })
  }

  property var devices: []
  property int deviceIndex: 0
  property string error: ""
  property bool loaded: false
  property var pendingChange: null
  property string pendingTarget: ""

  // Keyboard cursor: rows of the panel walked with up/down, adjusted with
  // left/right.
  property int cursorRow: 0
  property bool cursorActive: false

  readonly property var device: devices.length > 0 ? devices[Math.min(deviceIndex, devices.length - 1)] : null
  readonly property bool ready: !!(device && !device.error && device.lighting)
  readonly property bool lightingOn: ready && device.effect > 0
  readonly property bool hasColor: ready && device.hasColor === true
  // Firmware with a brightness cap never reads back 255; the backend learns
  // the real top so the slider's end means 100%.
  readonly property int brightnessMax: ready && device.brightnessMax > 0 ? device.brightnessMax : 255
  // Which of speed/hue/saturation the current effect reacts to, from the
  // keyboard's mapping file. null means unknown: offer everything.
  readonly property var effectControls: {
    if (!ready || !device.effectControls) return null
    var controls = device.effectControls[device.effect]
    return controls instanceof Array ? controls : null
  }
  readonly property bool showSpeed: ready && uses("speed")
  readonly property bool showHue: hasColor && uses("hue")
  readonly property bool showSaturation: hasColor && uses("saturation")
  readonly property var rows: {
    var list = ["brightness", "effect"]
    if (showSpeed) list.push("speed")
    if (showHue) list.push("hue")
    if (showSaturation) list.push("saturation")
    return list
  }
  readonly property string target: applyToAll ? "all" : (device ? device.id : "")
  readonly property color swatch: showHue
    ? Qt.hsva(device.hue / 255, device.saturation / 255, 1, 1)
    : (bar ? bar.foreground : Commons.Color.foreground)

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property var presets: [
    { label: "Red", hue: 0, saturation: 255 },
    { label: "Orange", hue: 21, saturation: 255 },
    { label: "Yellow", hue: 43, saturation: 255 },
    { label: "Green", hue: 85, saturation: 255 },
    { label: "Cyan", hue: 128, saturation: 255 },
    { label: "Blue", hue: 170, saturation: 255 },
    { label: "Purple", hue: 191, saturation: 255 },
    { label: "Pink", hue: 213, saturation: 255 },
    { label: "White", hue: 0, saturation: 0 }
  ]

  // The effectNames setting wins; otherwise use the names the backend found
  // in effects/<vid>-<pid>.json, indexed by effect number.
  function effectLabel(effect) {
    if (!effect) return "Off"
    var name = effectNames[effect - 1]
    if (!name && device && device.effects) name = device.effects[effect]
    return name ? name : "Effect " + effect
  }

  function uses(control) {
    return effectControls === null || effectControls.indexOf(control) >= 0
  }

  function percent(value, maximum) {
    var top = maximum || 255
    return Math.round(Math.min(Number(value) || 0, top) * 100 / top) + "%"
  }

  function limit(row) {
    return row === "brightness" ? brightnessMax : 255
  }

  function refresh() {
    if (!statusProc.running) statusProc.running = true
  }

  // Merge into whatever change is still waiting, so a burst of slider moves
  // reaches the keyboard as one write with the latest values.
  function apply(change) {
    if (!target) return
    // Reflect the change right away; the backend's reply corrects it.
    if (device && !("toggle" in change) && !("effectStep" in change)) {
      var next = Object.assign({}, device, change)
      var list = devices.slice()
      list[deviceIndex] = next
      devices = list
    }
    pendingChange = Object.assign({}, pendingChange || {}, change)
    pendingTarget = target
    flush()
  }

  function flush() {
    if (setProc.running || !pendingChange) return
    var change = pendingChange
    var args = root.backend.concat(["set", "--device", pendingTarget])
    pendingChange = null
    if (change.toggle) args.push("--toggle")
    if (change.brightness !== undefined) args.push("--brightness", String(Math.round(change.brightness)))
    if (change.effectStep !== undefined) args.push("--effect", change.effectStep > 0 ? "next" : "prev")
    else if (change.effect !== undefined) args.push("--effect", String(change.effect))
    if (change.speed !== undefined) args.push("--speed", String(Math.round(change.speed)))
    if (change.hue !== undefined) args.push("--hue", String(Math.round(change.hue)))
    if (change.saturation !== undefined) args.push("--saturation", String(Math.round(change.saturation)))
    setProc.command = args
    setProc.running = true
  }

  function toggleLighting() {
    if (ready) apply({ toggle: true })
  }

  function stepEffect(direction) {
    if (ready) apply({ effectStep: direction })
  }

  // The keyboard's hue/saturation for a color; QMK hue spans 0-255 for the
  // full circle. Grays report a hue of -1.
  function hueSaturation(color) {
    return {
      hue: color.hsvHue < 0 ? 0 : Math.round(color.hsvHue * 255) % 256,
      saturation: Math.round(color.hsvSaturation * 255)
    }
  }

  // Set the lighting to the theme's accent. An effect that ignores hue would
  // hide the change, so fall back to Solid Color (effect 1) for those.
  function matchTheme() {
    if (!hasColor) return
    var change = hueSaturation(Commons.Color.accent)
    if (!uses("hue")) change.effect = 1
    apply(change)
  }

  function save() {
    if (!target || saveProc.running) return
    saveTimer.stop()
    saveProc.command = root.backend.concat(["save", "--device", target])
    saveProc.running = true
  }

  function adjustRow(row, direction) {
    if (!ready) return
    if (row === "effect") {
      stepEffect(direction)
      return
    }
    var change = {}
    change[row] = Math.max(0, Math.min(limit(row), device[row] + direction * step))
    apply(change)
  }

  function ingest(text, replaceAll) {
    var parsed
    try {
      parsed = JSON.parse(String(text || "").trim() || "{}")
    } catch (e) {
      error = "Unexpected backend output"
      return false
    }
    if (parsed.error) {
      error = parsed.error
      return false
    }
    error = ""
    var incoming = parsed.devices || []
    if (replaceAll) {
      var selectedId = device ? device.id : ""
      devices = incoming
      var idx = -1
      for (var i = 0; i < incoming.length; i++) if (incoming[i].id === selectedId) idx = i
      deviceIndex = idx >= 0 ? idx : 0
      loaded = true
      return true
    }
    // A set reply covers only the keyboards it touched; patch those in.
    var list = devices.slice()
    for (var j = 0; j < incoming.length; j++) {
      for (var k = 0; k < list.length; k++) if (list[k].id === incoming[j].id) list[k] = incoming[j]
    }
    if (!pendingChange) devices = list
    return true
  }

  onOpenedChanged: {
    if (opened) {
      cursorActive = false
      cursorRow = 0
      refresh()
    }
  }

  // Switching effects can hide the row under the keyboard cursor.
  onRowsChanged: if (cursorRow >= rows.length) cursorRow = rows.length - 1

  Component.onCompleted: refresh()

  ShellIpc {
    target: "kcotugno.qmk-rgb"

    function open() { root.open() }
    function close() { root.close() }
    function show() { root.open() }
    function hide() { root.close() }
    function toggle() { root.toggle() }
    function refresh() { root.refresh() }
    function toggleLighting() { root.toggleLighting() }
    function nextEffect() { root.stepEffect(1) }
    function matchTheme() { root.matchTheme() }
    function previousEffect() { root.stepEffect(-1) }
  }

  Process {
    id: statusProc
    command: root.backend.concat(["status"])
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.ingest(text, true) }
  }

  Process {
    id: setProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (root.ingest(text, false) && root.autoSave) saveTimer.restart()
      }
    }
    onExited: Qt.callLater(root.flush)
  }

  Process {
    id: saveProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = {}
        try { parsed = JSON.parse(String(text || "").trim() || "{}") } catch (e) {}
        if (parsed.error) root.error = parsed.error
      }
    }
  }

  // EEPROM has a finite number of writes, so only persist once the user has
  // stopped fiddling.
  Timer {
    id: saveTimer
    interval: 1500
    repeat: false
    onTriggered: root.save()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰌌"
    dimmed: root.loaded && !root.lightingOn
    tooltipText: root.device ? root.device.name + " · " + root.effectLabel(root.device.effect) : "QMK RGB"
    onPressed: function(b) {
      if (b === Qt.RightButton) root.toggleLighting()
      else if (b === Qt.MiddleButton) root.refresh()
      else root.toggle()
    }
    onWheelMoved: function(delta) {
      if (root.ready) root.apply({ brightness: Math.max(0, Math.min(root.brightnessMax, root.device.brightness + (delta > 0 ? root.step : -root.step))) })
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.cursorRow = Math.max(0, Math.min(root.rows.length - 1, root.cursorRow + dy))
        else if (dx !== 0) root.adjustRow(root.rows[root.cursorRow], dx)
      }
      onActivateRequested: root.toggleLighting()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero: color swatch · name/status · power switch ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, power.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: "󰌌"
            color: root.lightingOn ? root.swatch : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter

            Behavior on color { ColorAnimation { duration: Style.duration(200) } }
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: power.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: root.device ? root.device.name : "QMK keyboard"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: {
                if (!root.loaded) return "LOOKING FOR KEYBOARDS"
                if (!root.device) return "NO VIA KEYBOARD FOUND"
                if (root.device.error) return "UNAVAILABLE"
                if (!root.device.lighting) return "NO LIGHTING CONTROLS"
                var kind = String(root.device.lighting).replace("_", " ")
                return (kind + " · " + root.effectLabel(root.device.effect)).toUpperCase()
              }
              color: Qt.darker(root.foreground, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }

          ToggleSwitch {
            id: power
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            visible: root.ready
            checked: root.lightingOn
            foreground: root.foreground
            onToggled: root.toggleLighting()
          }
        }

        // ---------- Keyboard picker (only with more than one) ----------
        Row {
          id: deviceRow
          visible: root.devices.length > 1
          width: parent.width
          spacing: Style.space(6)

          readonly property real cellWidth: root.devices.length > 0
            ? (width - spacing * (root.devices.length - 1)) / root.devices.length
            : 0

          Repeater {
            model: root.devices
            Button {
              required property var modelData
              required property int index
              width: deviceRow.cellWidth
              text: String(modelData.name)
              fontSize: Style.font.bodySmall
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              active: root.applyToAll || root.deviceIndex === index
              onClicked: root.deviceIndex = index
            }
          }
        }

        // ---------- Problems ----------
        Text {
          visible: text !== ""
          width: parent.width
          wrapMode: Text.Wrap
          textFormat: Text.PlainText
          color: root.bar ? root.bar.urgent : Commons.Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          text: {
            if (root.error) return root.error
            if (root.device && root.device.error) {
              if (root.device.accessible === false)
                return "No permission to open " + root.device.path + ". Install the udev rule printed by:\n" + root.pluginDir + "/qmk_rgb.py udev-rule"
              return root.device.error
            }
            if (root.loaded && !root.device)
              return "Plug in a QMK keyboard whose firmware has VIA enabled."
            return ""
          }
        }

        // ---------- Controls ----------
        Column {
          visible: root.ready
          width: parent.width
          spacing: Style.space(12)

          PanelSeparator { foreground: root.foreground }

          ControlRow {
            row: "brightness"
            title: "BRIGHTNESS"
            valueText: root.ready ? root.percent(root.device.brightness, root.brightnessMax) : ""
            PanelSlider {
              width: parent.width
              bar: root.bar
              minimum: 0
              maximum: root.brightnessMax
              step: root.step
              integer: true
              value: root.ready ? root.device.brightness : 0
              onReleased: function(v) { root.apply({ brightness: v }) }
            }
          }

          ControlRow {
            row: "effect"
            title: "EFFECT"
            valueText: root.ready ? "#" + root.device.effect : ""
            Item {
              width: parent.width
              implicitHeight: effectPrev.implicitHeight

              Button {
                id: effectPrev
                anchors.left: parent.left
                iconText: "󰅁"
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                tooltipText: "Previous effect"
                onClicked: root.stepEffect(-1)
              }

              Text {
                anchors.left: effectPrev.right
                anchors.right: effectNext.left
                anchors.verticalCenter: parent.verticalCenter
                horizontalAlignment: Text.AlignHCenter
                textFormat: Text.PlainText
                text: root.ready ? root.effectLabel(root.device.effect) : ""
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }

              Button {
                id: effectNext
                anchors.right: parent.right
                iconText: "󰅂"
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                tooltipText: "Next effect"
                onClicked: root.stepEffect(1)
              }
            }
          }

          ControlRow {
            row: "speed"
            title: "SPEED"
            visible: root.showSpeed
            valueText: root.ready ? root.percent(root.device.speed) : ""
            PanelSlider {
              width: parent.width
              bar: root.bar
              minimum: 0
              maximum: 255
              step: root.step
              integer: true
              value: root.ready ? root.device.speed : 0
              onReleased: function(v) { root.apply({ speed: v }) }
            }
          }

          PanelSeparator { visible: root.showHue || root.showSaturation; foreground: root.foreground }

          Row {
            id: presetRow
            visible: root.showHue
            width: parent.width
            spacing: Style.space(6)

            readonly property real cell: (width - spacing * (root.presets.length - 1)) / root.presets.length

            Repeater {
              model: root.presets
              Rectangle {
                required property var modelData
                readonly property bool current: root.hasColor
                  && root.device.hue === modelData.hue && root.device.saturation === modelData.saturation
                width: presetRow.cell
                height: Math.min(presetRow.cell, Style.space(22))
                radius: Style.cornerRadius > 0 ? height / 2 : 0
                color: Qt.hsva(modelData.hue / 255, modelData.saturation / 255, 1, 1)
                border.width: current || presetMouse.containsMouse ? Math.max(1, Style.space(2)) : 0
                border.color: root.foreground

                MouseArea {
                  id: presetMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.apply({ hue: modelData.hue, saturation: modelData.saturation })
                }
              }
            }
          }

          ControlRow {
            row: "hue"
            title: "HUE"
            visible: root.showHue
            valueText: root.hasColor ? Math.round(root.device.hue * 360 / 256) + "°" : ""

            Rectangle {
              width: parent.width
              height: Math.max(3, Style.space(4))
              radius: height / 2
              gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: "#ff0000" }
                GradientStop { position: 0.17; color: "#ffff00" }
                GradientStop { position: 0.33; color: "#00ff00" }
                GradientStop { position: 0.5; color: "#00ffff" }
                GradientStop { position: 0.67; color: "#0000ff" }
                GradientStop { position: 0.83; color: "#ff00ff" }
                GradientStop { position: 1.0; color: "#ff0000" }
              }
            }

            PanelSlider {
              width: parent.width
              bar: root.bar
              minimum: 0
              maximum: 255
              step: root.step
              integer: true
              fillColor: root.swatch
              knobColor: root.swatch
              value: root.hasColor ? root.device.hue : 0
              onReleased: function(v) { root.apply({ hue: v }) }
            }
          }

          ControlRow {
            row: "saturation"
            title: "SATURATION"
            visible: root.showSaturation
            valueText: root.hasColor ? root.percent(root.device.saturation) : ""
            PanelSlider {
              width: parent.width
              bar: root.bar
              minimum: 0
              maximum: 255
              step: root.step
              integer: true
              value: root.hasColor ? root.device.saturation : 0
              onReleased: function(v) { root.apply({ saturation: v }) }
            }
          }

          Button {
            id: themeButton
            readonly property var theme: root.hueSaturation(Commons.Color.accent)
            visible: root.hasColor
            width: parent.width
            text: "Match theme"
            iconText: "󰏘"
            tooltipText: "Set the keyboard color to the theme's accent"
            active: root.showHue && root.device.hue === theme.hue && root.device.saturation === theme.saturation
            foreground: root.foreground
            fontFamily: root.fontFamily
            bordered: true
            onClicked: root.matchTheme()
          }

          Button {
            visible: !root.autoSave
            width: parent.width
            text: saveProc.running ? "Saving…" : "Save to keyboard"
            iconText: "󰆓"
            foreground: root.foreground
            fontFamily: root.fontFamily
            bordered: true
            onClicked: root.save()
          }
        }
      }
    }
  }

  // A section label with its current value on the right, then whatever
  // control is nested inside. The label brightens under the keyboard cursor.
  component ControlRow: Column {
    id: controlRow
    property string row: ""
    property string title: ""
    property string valueText: ""
    default property alias content: body.data
    readonly property bool hasCursor: root.cursorActive && root.rows[root.cursorRow] === row

    width: parent.width
    spacing: Style.space(6)

    Item {
      width: parent.width
      implicitHeight: header.implicitHeight

      PanelSectionHeader {
        id: header
        text: controlRow.title
        foreground: controlRow.hasCursor ? Qt.lighter(root.foreground, 1.6) : root.foreground
        fontFamily: root.fontFamily
      }

      Text {
        anchors.right: parent.right
        anchors.verticalCenter: header.verticalCenter
        textFormat: Text.PlainText
        text: controlRow.valueText
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    Column {
      id: body
      width: parent.width
      spacing: Style.space(4)
    }
  }
}
