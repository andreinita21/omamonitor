import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Hyprland
import qs.Ui
import qs.Commons
import "Model.js" as Model

// Display panel: brightness and text size (inherited from omarchy.monitor)
// plus a monitor arrangement editor. Monitors are dragged on a scaled-down
// canvas; resolution, refresh rate, scale, rotation and on/off live in the
// detail rows below it.
//
// Every change applies on its own: a short debounce, then `hyprctl eval`
// (hl.monitor calls) pushes the layout live and write-monitors.py regenerates
// ~/.config/hypr/monitors.lua so it survives a reboot.
//
// Connected displays are switched on automatically. The exceptions are the
// laptop panel while Omarchy's clamshell handling holds it off (lid closed)
// and any output switched off in this panel on purpose, which monitors.lua
// records as `disabled = true`.
Panel {
  id: root
  moduleName: "omarchy.monitor"
  ipcTarget: "omarchy.monitor"
  manageIpc: false

  readonly property string pluginDir: {
    var u = String(Qt.resolvedUrl("."))
    if (u.indexOf("file://") === 0) u = u.slice(7)
    if (u.length > 1 && u.charAt(u.length - 1) === "/") u = u.slice(0, u.length - 1)
    return u
  }

  // manageIpc: false so this panel can own the single IpcHandler the target
  // permits — needed for the brightness + state methods below.
  property int brightnessPercent: 0
  property int pendingBrightnessPercent: 0
  property bool brightnessSetQueued: false
  property bool brightnessAvailable: false
  property string focusedMonitor: ""

  // Carry sub-notch touchpad deltas between wheel events.
  property real wheelAccumulator: 0

  // ---- Monitor layout state ----
  // `monitors` mirrors hyprctl; `draft` is the layout being edited. Between a
  // change and the refresh that follows its apply, the two differ.
  property var monitors: []
  property var draft: []
  property string selectedMonitor: ""
  property bool applying: false
  property bool applyQueued: false
  property bool adoptLive: false
  property bool dragging: false
  property string applyError: ""

  // From display-state.sh: lid + clamshell state, and outputs monitors.lua
  // pins off. monitors.lua is the source of truth for "switched off on
  // purpose"; switches made in the panel live in `sessionOff`/`sessionOn`
  // only until the apply that writes them out has finished.
  property bool lidClosed: false
  property bool clamshellHold: false
  property var userDisabled: ({})
  property var sessionOff: ({})
  property var sessionOn: ({})
  // Outputs auto-enable already tried once; cleared when they come on or go.
  property var autoEnableTried: ({})

  readonly property var liveDraft: Model.draftFrom(monitors)
  readonly property int changeCount: Model.changedCount(draft, liveDraft)
  readonly property bool dirty: changeCount > 0
  readonly property int enabledCount: Model.enabledCount(draft)
  readonly property int selectedIdx: Model.findIndex(draft, selectedMonitor)
  readonly property var selectedEntry: selectedIdx >= 0 ? draft[selectedIdx] : null

  readonly property var scalePresets: ["1", "1.25", "1.5", "1.6", "2", "3"]
  readonly property var scaleValues: selectedEntry
    ? Model.availableScales(scalePresets, selectedEntry.width, selectedEntry.height)
    : scalePresets
  readonly property var rotationValues: [0, 1, 2, 3]
  readonly property var resolutionOptions: selectedEntry ? Model.resolutionOptions(selectedEntry) : []
  readonly property var refreshOptions: selectedEntry ? Model.refreshOptions(selectedEntry) : []

  // Cursor model shared by keyboard and mouse. Sections, top to bottom:
  //   "brightness" - slider row, selectedIndex = -1 sentinel (if a backlight exists)
  //   "textsize"   - slider row, sentinel -1
  //   "monitors"   - one row per monitor; Enter selects it for editing
  //   "enabled"    - single row, Enter toggles the selected monitor
  //   "mode"       - single row, h/l cycles resolutions, Enter opens the list
  //   "refresh"    - single row, h/l cycles refresh rates, Enter opens the list
  //   "scale"      - horizontal pills, h/l moves, Enter applies
  //   "rotation"   - horizontal pills, same as scale
  // Mouse hover on a target updates root state via the components' hover
  // signals so keyboard cursor and pointer share one highlight.
  property string focusSection: "monitors"
  property int selectedIndex: 0
  property bool cursorActive: false

  // Text size slider — curated macOS-style notches (px). The panel snaps to
  // these stops; the CLI (omarchy-display-text-size) accepts any integer in range.
  readonly property var textSizeStops: [9, 10, 11, 12, 14, 16, 20]
  property int textSizePreviewIndex: -1

  // A text-size change reflows the whole panel, which slides rows under a
  // stationary pointer and fires synthetic hover. While true, hover is not
  // allowed to hijack the keyboard focus section.
  property bool reflowingText: false
  function markReflowing() {
    root.reflowingText = true
    reflowSettle.restart()
  }

  readonly property var visibleSections: {
    var list = []
    if (brightnessAvailable) list.push("brightness")
    list.push("textsize")
    if (draft.length > 0) list.push("monitors")
    if (selectedEntry) {
      list.push("enabled")
      if (selectedEntry.enabled) {
        list.push("mode")
        list.push("refresh")
        list.push("scale")
        list.push("rotation")
      }
    }
    return list
  }

  function sectionCount(section) {
    if (section === "monitors") return draft.length
    if (section === "scale") return scaleValues.length
    if (section === "rotation") return rotationValues.length
    return 0
  }

  function sectionIsSingleRow(section) {
    return section !== "monitors"
  }

  function sectionUsesSentinel(section) {
    return section === "brightness" || section === "textsize" || section === "enabled"
      || section === "mode" || section === "refresh"
  }

  function sectionFirstIndex(section) {
    return sectionUsesSentinel(section) ? -1 : 0
  }

  function moveCursor(delta) {
    var sections = visibleSections
    if (!sections || sections.length === 0) return
    var sIdx = sections.indexOf(focusSection)
    if (sIdx < 0) {
      focusSection = sections[0]
      selectedIndex = sectionFirstIndex(focusSection)
      return
    }
    var inSingleRow = sectionIsSingleRow(focusSection)
    var max = inSingleRow ? 0 : sectionCount(focusSection) - 1

    if (delta > 0) {
      if (!inSingleRow && selectedIndex < max) { selectedIndex = selectedIndex + 1; return }
      if (sIdx < sections.length - 1) {
        focusSection = sections[sIdx + 1]
        selectedIndex = sectionFirstIndex(focusSection)
      }
    } else {
      if (!inSingleRow && selectedIndex > 0) { selectedIndex = selectedIndex - 1; return }
      if (sIdx > 0) {
        var prev = sections[sIdx - 1]
        focusSection = prev
        selectedIndex = sectionIsSingleRow(prev) ? sectionFirstIndex(prev) : sectionCount(prev) - 1
      }
    }
  }

  // h/l inside horizontal sections. Sliders handle their own horizontal
  // motion in the key catcher; mode/refresh rows cycle their options.
  function moveCursorH(delta) {
    if (focusSection === "scale" || focusSection === "rotation") {
      var count = sectionCount(focusSection)
      var next = selectedIndex + delta
      if (next < 0) next = 0
      if (next > count - 1) next = count - 1
      selectedIndex = next
      return
    }
    if (focusSection === "mode") cycleOption(resolutionOptions, currentResolutionValue(), delta, setResolution)
    else if (focusSection === "refresh") cycleOption(refreshOptions, currentRefreshValue(), delta, setRefresh)
  }

  function cycleOption(options, current, delta, apply) {
    if (!options || options.length === 0) return
    var idx = -1
    for (var i = 0; i < options.length; i++) {
      if (String(options[i].value) === String(current)) { idx = i; break }
    }
    var next = idx + delta
    if (next < 0) next = 0
    if (next > options.length - 1) next = options.length - 1
    if (next === idx) return
    apply(options[next].value)
  }

  function adjustBrightness(delta) {
    if (focusSection !== "brightness") return
    if (!brightnessAvailable) return
    setBrightness(root.brightnessPercent + delta)
  }

  function activateCursor() {
    if (focusSection === "monitors" && selectedIndex >= 0 && selectedIndex < draft.length) {
      selectMonitor(draft[selectedIndex].name)
      return
    }
    if (focusSection === "enabled") { toggleSelectedEnabled(); return }
    if (focusSection === "mode") { modeDropdown.open(); return }
    if (focusSection === "refresh") { refreshDropdown.open(); return }
    if (focusSection === "scale" && selectedIndex >= 0 && selectedIndex < scaleValues.length) {
      setScale(scaleValues[selectedIndex])
      return
    }
    if (focusSection === "rotation" && selectedIndex >= 0 && selectedIndex < rotationValues.length) {
      setRotation(rotationValues[selectedIndex])
    }
  }

  function clampCursor() {
    var sections = visibleSections
    if (!sections || !sections.length) return
    if (sections.indexOf(focusSection) < 0) {
      // The section under the cursor vanished (the detail rows after a
      // monitor was switched off). Land on the monitor list if it exists.
      var fallback = sections.indexOf("monitors") >= 0 ? "monitors" : sections[0]
      focusSection = fallback
      selectedIndex = sectionFirstIndex(focusSection)
      return
    }
    var count = sectionCount(focusSection)
    if (sectionUsesSentinel(focusSection)) { selectedIndex = -1; return }
    if (count === 0) {
      var sIdx = sections.indexOf(focusSection)
      focusSection = sIdx > 0 ? sections[sIdx - 1] : sections[0]
      selectedIndex = sectionFirstIndex(focusSection)
      return
    }
    if (selectedIndex > count - 1) selectedIndex = count - 1
    if (selectedIndex < 0) selectedIndex = 0
  }

  function setCursor(section, index) {
    if (root.reflowingText) return
    root.cursorActive = true
    root.focusSection = section
    root.selectedIndex = index
  }

  // Keep the keyboard-focused row inside the viewport when the panel grows
  // taller than its allotted height.
  function ensureCursorVisible(item) {
    if (!item || !scrollArea) return
    var flick = scrollArea.contentItem
    if (!flick || flick.contentY === undefined) return
    var pt = item.mapToItem(flick.contentItem || flick, 0, 0)
    var top = pt.y
    var bottom = top + (item.height || 0)
    var viewTop = flick.contentY
    var viewBottom = viewTop + flick.height
    var margin = 6
    if (top < viewTop + margin) flick.contentY = Math.max(0, top - margin)
    else if (bottom > viewBottom - margin)
      flick.contentY = bottom + margin - flick.height
  }

  function brightnessIpc(percent) {
    var value = Number(percent)
    root.setBrightness(value)
    return "got " + root.pendingBrightnessPercent
  }

  function stateIpc() {
    return JSON.stringify({
      brightness: root.brightnessPercent,
      brightnessAvailable: root.brightnessAvailable,
      focusedMonitor: root.focusedMonitor,
      selected: root.selectedMonitor,
      applying: root.applying,
      dirty: root.dirty,
      lidClosed: root.lidClosed,
      clamshellHold: root.clamshellHold,
      userDisabled: Object.keys(root.userDisabled),
      monitors: root.monitors,
      draft: root.draft
    })
  }

  IpcHandler {
    target: "omarchy.monitor"

    function brightness(percent: string): string { return root.brightnessIpc(percent) }
    function state(): string { return root.stateIpc() }
    function refresh(): void { root.refresh() }
    function open() { root.open() }
    function close() { root.close() }
    function toggle() { root.toggle() }
    function show() { root.open() }
    function hide() { root.close() }
  }

  function refresh() {
    if (!stateProc.running) stateProc.running = true
    if (!monitorsProc.running) monitorsProc.running = true
  }

  // ---- Brightness ----
  function setBrightness(value) {
    var percent = Model.clampBrightness(value)
    root.brightnessPercent = percent
    root.pendingBrightnessPercent = percent

    if (setBrightnessProc.running) {
      root.brightnessSetQueued = true
      return
    }

    root.brightnessSetQueued = false
    setBrightnessProc.command = ["omarchy-brightness-display", "--no-osd", "--monitor", root.focusedMonitor, percent + "%"]
    setBrightnessProc.running = true
  }

  function previewBrightness(value) {
    root.brightnessPercent = Model.clampBrightness(value)
    brightnessDebounce.restart()
  }

  function showBrightnessOsd(percent) {
    if (!bar || !bar.shell) return
    bar.shell.summon("omarchy.osd", JSON.stringify({
      icon: "brightness",
      value: percent
    }))
  }

  function brightnessName(percent) {
    return Model.brightnessName(percent)
  }

  // ---- Monitor layout editing ----
  function isHeld(entry) {
    return !entry.enabled && Model.isInternal(entry.name) && (root.lidClosed || root.clamshellHold)
  }

  function updateMonitors(monitorsJson, metaJson) {
    var meta = {}
    try { meta = JSON.parse(metaJson || "{}") || {} } catch (e) { meta = {} }
    root.lidClosed = meta.lid === "closed"
    root.clamshellHold = meta.clamshell === true
    var disabledFromFile = Array.isArray(meta.userDisabled) ? meta.userDisabled : []
    var merged = {}
    for (var d = 0; d < disabledFromFile.length; d++) merged[disabledFromFile[d]] = true
    for (var offKey in root.sessionOff) merged[offKey] = true
    for (var onKey in root.sessionOn) delete merged[onKey]

    var parsed = Model.parseMonitors(monitorsJson)
    root.monitors = parsed

    // Bookkeeping for auto-enable: an output that came on, or went away, can
    // be tried again later.
    var present = {}
    var tried = {}
    for (var i = 0; i < parsed.length; i++) {
      present[parsed[i].name] = true
      if (!parsed[i].enabled && root.autoEnableTried[parsed[i].name]) tried[parsed[i].name] = true
    }
    for (var name in merged) if (!present[name]) delete merged[name]
    root.userDisabled = merged
    root.autoEnableTried = tried

    // Never yank the draft out from under a drag.
    if (root.dragging) return

    var fresh = Model.draftFrom(parsed)
    for (var f = 0; f < fresh.length; f++) fresh[f].held = isHeld(fresh[f])

    var adopt = root.adoptLive || !root.dirty || root.draft.length === 0
    root.adoptLive = false
    if (adopt) {
      // Anything connected but off, and not held or deliberately off, comes on.
      var toEnable = []
      for (var j = 0; j < fresh.length; j++) {
        var e = fresh[j]
        if (e.enabled || e.held || merged[e.name] || tried[e.name]) continue
        toEnable.push(j)
      }
      for (var t = 0; t < toEnable.length; t++) {
        Model.placeEnabled(fresh, toEnable[t])
        tried[fresh[toEnable[t]].name] = true
      }
      root.autoEnableTried = tried
      if (Model.changedCount(fresh, root.draft) > 0 || fresh.length !== root.draft.length
          || heldChanged(fresh, root.draft))
        root.draft = fresh
      if (toEnable.length > 0) scheduleApply()
    } else {
      // Keep the pending draft, but track hold state so the rows stay honest.
      var kept = Model.cloneEntries(root.draft)
      for (var k = 0; k < kept.length; k++) kept[k].held = isHeld(kept[k])
      if (heldChanged(kept, root.draft)) root.draft = kept
    }

    if (Model.findIndex(root.draft, root.selectedMonitor) < 0) {
      var pick = ""
      for (var p = 0; p < root.draft.length; p++) {
        if (root.draft[p].focused) { pick = root.draft[p].name; break }
      }
      if (!pick && root.draft.length > 0) pick = root.draft[0].name
      root.selectedMonitor = pick
    }
  }

  function heldChanged(a, b) {
    if (a.length !== b.length) return true
    for (var i = 0; i < a.length; i++) {
      var idx = Model.findIndex(b, a[i].name)
      if (idx < 0 || (a[i].held === true) !== (b[idx].held === true)) return true
    }
    return false
  }

  function selectMonitor(name) {
    root.selectedMonitor = name
  }

  // Apply a patch to the selected entry, settle its position so a size
  // change (scale/mode/rotation) can't leave it overlapping a neighbour,
  // then push the result live.
  function patchSelected(patch, snapThreshold) {
    if (root.selectedIdx < 0) return
    var entries = Model.cloneEntries(root.draft)
    var entry = entries[root.selectedIdx]
    for (var key in patch) entry[key] = patch[key]
    Model.settleEntry(entries, root.selectedIdx, snapThreshold || 0)
    root.draft = entries
    scheduleApply()
  }

  function toggleSelectedEnabled() {
    var entry = root.selectedEntry
    if (!entry || entry.held) return
    var off = {}
    for (var offKey in root.sessionOff) off[offKey] = true
    var on = {}
    for (var onKey in root.sessionOn) on[onKey] = true
    var disabled = {}
    for (var key in root.userDisabled) disabled[key] = true
    if (entry.enabled) {
      if (root.enabledCount <= 1) return
      off[entry.name] = true
      delete on[entry.name]
      disabled[entry.name] = true
      root.sessionOff = off
      root.sessionOn = on
      root.userDisabled = disabled
      var entries = Model.cloneEntries(root.draft)
      entries[root.selectedIdx].enabled = false
      root.draft = Model.normalize(entries)
      scheduleApply()
      return
    }
    on[entry.name] = true
    delete off[entry.name]
    delete disabled[entry.name]
    root.sessionOff = off
    root.sessionOn = on
    root.userDisabled = disabled
    var placed = Model.cloneEntries(root.draft)
    Model.placeEnabled(placed, root.selectedIdx)
    root.draft = placed
    scheduleApply()
  }

  function currentResolutionValue() {
    return root.selectedEntry ? root.selectedEntry.width + "x" + root.selectedEntry.height : ""
  }

  function currentRefreshValue() {
    return root.selectedEntry ? Model.refreshLabel(root.selectedEntry.refresh) : ""
  }

  function setResolution(value) {
    var match = /^(\d+)x(\d+)$/.exec(String(value))
    if (!match || !root.selectedEntry) return
    var width = parseInt(match[1], 10)
    var height = parseInt(match[2], 10)
    var refresh = Model.bestRefreshFor(root.selectedEntry, width, height)
    patchSelected({ width: width, height: height, refresh: refresh })
  }

  function setRefresh(value) {
    var n = Number(value)
    if (!isFinite(n) || n <= 0) return
    patchSelected({ refresh: n })
  }

  function setScale(scale) {
    if (!root.selectedEntry) return
    var clean = Model.cleanScale(scale, root.selectedEntry.width, root.selectedEntry.height)
    var n = Number(clean)
    if (!isFinite(n) || n <= 0) return
    patchSelected({ scale: n })
  }

  function setRotation(transform) {
    patchSelected({ transform: parseInt(transform, 10) || 0 })
  }

  function activeScaleIndex() {
    if (!root.selectedEntry) return -1
    return Model.matchingScaleIndex(scaleValues, root.selectedEntry.scale, root.selectedEntry.width, root.selectedEntry.height)
  }

  function effectiveScale(scale) {
    if (!root.selectedEntry) return Model.normalizeScale(scale)
    return Model.cleanScale(scale, root.selectedEntry.width, root.selectedEntry.height)
  }

  // Called by a canvas tile after a drag: dx/dy are in logical pixels.
  function moveMonitor(index, dx, dy, snapThreshold) {
    var entries = Model.cloneEntries(root.draft)
    var entry = entries[index]
    if (!entry || !entry.enabled) return
    entry.x = Math.round(entry.x + dx)
    entry.y = Math.round(entry.y + dy)
    Model.settleEntry(entries, index, snapThreshold)
    root.draft = entries
    scheduleApply()
  }

  // Changes coalesce for a beat (h/l cycling, quick successive drags) and
  // one apply runs at a time; a change made mid-apply queues another.
  function scheduleApply() {
    applyDebounce.restart()
  }

  function runApply() {
    if (root.enabledCount < 1 && !anyHeld()) return
    if (root.applying) { root.applyQueued = true; return }
    root.applyError = ""
    root.applying = true
    root.applyQueued = false
    applyProc.command = ["hyprctl", "eval", Model.evalScript(root.draft)]
    applyProc.running = true
  }

  function anyHeld() {
    for (var i = 0; i < root.draft.length; i++) if (root.draft[i].held) return true
    return false
  }

  function finishApply() {
    root.applying = false
    if (root.applyQueued) { runApply(); return }
    root.adoptLive = true
    settleRefresh.restart()
  }

  // ---- Text size (shell base font + GTK text-scaling, via one CLI) ----
  function nearestTextStop(px) {
    var best = 0
    var bestDist = 1e9
    for (var i = 0; i < textSizeStops.length; i++) {
      var d = Math.abs(textSizeStops[i] - px)
      if (d < bestDist) { bestDist = d; best = i }
    }
    return best
  }

  function currentTextIndex() {
    return textSizePreviewIndex >= 0 ? textSizePreviewIndex : nearestTextStop(Style.font.baseSize)
  }

  function displayedTextPx() {
    return textSizePreviewIndex >= 0 ? textSizeStops[textSizePreviewIndex] : Style.font.baseSize
  }

  function setTextSize(px) {
    textScaleProc.command = ["omarchy-display-text-size", String(px)]
    if (!textScaleProc.running) textScaleProc.running = true
  }

  function adjustTextSize(deltaSteps) {
    var idx = currentTextIndex() + deltaSteps
    if (idx < 0) idx = 0
    if (idx > textSizeStops.length - 1) idx = textSizeStops.length - 1
    markReflowing()
    textSizePreviewIndex = idx
    setTextSize(textSizeStops[idx])
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Component.onCompleted: refresh()

  onOpenedChanged: {
    if (opened) {
      refresh()
      if (brightnessAvailable) {
        focusSection = "brightness"
        selectedIndex = -1
      } else {
        focusSection = "monitors"
        selectedIndex = 0
      }
      cursorActive = false
    }
  }

  onBrightnessAvailableChanged: clampCursor()
  onDraftChanged: clampCursor()
  onScaleValuesChanged: clampCursor()
  onVisibleSectionsChanged: clampCursor()

  // Poll while open so external changes show up. Hotplug and lid events
  // arrive through Hyprland's event socket whether or not the panel is open,
  // which is what lets a newly connected display come on by itself.
  Timer {
    interval: 5000
    running: root.opened
    repeat: true
    onTriggered: root.refresh()
  }

  Connections {
    target: Hyprland
    function onRawEvent(event) {
      var name = String(event.name || "")
      if (name === "monitoradded" || name === "monitorremoved" || name === "monitoraddedv2"
          || name === "configreloaded")
        hotplugSettle.restart()
    }
  }

  // Hyprland fires monitor events before the output finishes coming up;
  // wait a beat so the state read sees the final geometry.
  Timer {
    id: hotplugSettle
    interval: 700
    repeat: false
    onTriggered: root.refresh()
  }

  Process {
    id: stateProc
    command: ["omarchy-monitor-state"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var lines = String(text || "").split("\n")
        var brightness = String(lines[0] || "").trim()
        root.brightnessAvailable = brightness !== "unavailable" && brightness !== ""
        root.brightnessPercent = root.brightnessAvailable ? Math.max(0, Math.min(100, parseInt(brightness, 10))) : 0
        root.focusedMonitor = String(lines[5] || "").trim()
      }
    }
  }

  Process {
    id: monitorsProc
    command: ["bash", root.pluginDir + "/display-state.sh"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var lines = String(text || "").split("\n")
        root.updateMonitors(String(lines[0] || "[]").trim(), String(lines[1] || "{}").trim())
      }
    }
  }

  Timer {
    id: brightnessDebounce
    interval: 180
    repeat: false
    onTriggered: root.setBrightness(root.brightnessPercent)
  }

  Process {
    id: setBrightnessProc
    stdout: StdioCollector { waitForEnd: true }
    // Do NOT call refresh() after a brightness set completes; the local value
    // is authoritative and re-reading races the driver (bounce to zero).
    onRunningChanged: {
      if (running) return
      if (root.brightnessSetQueued) {
        root.setBrightness(root.pendingBrightnessPercent)
      }
    }
  }

  Timer {
    id: applyDebounce
    interval: 200
    repeat: false
    onTriggered: root.runApply()
  }

  // Step 1 of an apply: push the layout live. Step 2 (persistProc) writes
  // monitors.lua; Hyprland's config reload then re-applies identical rules.
  Process {
    id: applyProc
    stdout: StdioCollector { id: applyOut; waitForEnd: true }
    stderr: StdioCollector { id: applyErr; waitForEnd: true }
    onRunningChanged: {
      if (running) return
      var out = String(applyOut.text || "") + String(applyErr.text || "")
      if (/error|invalid/i.test(out)) {
        root.applyError = out.trim().split("\n")[0]
        root.finishApply()
        return
      }
      persistProc.command = ["python3", root.pluginDir + "/write-monitors.py", JSON.stringify(Model.persistPayload(root.draft))]
      persistProc.running = true
    }
  }

  Process {
    id: persistProc
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: persistErr; waitForEnd: true }
    onRunningChanged: {
      if (running) return
      var err = String(persistErr.text || "").trim()
      if (err) root.applyError = "Applied live, but saving monitors.lua failed: " + err.split("\n")[0]
      else {
        // The file now records the session's on/off switches.
        root.sessionOff = {}
        root.sessionOn = {}
      }
      root.finishApply()
    }
  }

  // Give Hyprland a beat to settle new modes before re-reading.
  Timer {
    id: settleRefresh
    interval: 400
    repeat: false
    onTriggered: root.refresh()
  }

  Process {
    id: textScaleProc
    stdout: StdioCollector { waitForEnd: true }
  }

  Timer {
    id: reflowSettle
    interval: 300
    repeat: false
    onTriggered: root.reflowingText = false
  }

  Connections {
    target: Style
    function onFontBaseSizeChanged() {
      root.markReflowing()
      if (root.textSizePreviewIndex >= 0
          && root.nearestTextStop(Style.font.baseSize) === root.textSizePreviewIndex)
        root.textSizePreviewIndex = -1
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: Quickshell.screens.length > 1 ? "󰍺" : "󰍹"
    onPressed: function(b) { root.toggle() }
    onWheelMoved: function(delta) {
      if (!root.brightnessAvailable) return
      var wheel = Util.wheelSteps(root.wheelAccumulator, delta)
      root.wheelAccumulator = wheel.remainder
      if (wheel.steps === 0) return
      root.setBrightness(root.brightnessPercent + wheel.steps * 5)
      root.showBrightnessOsd(root.brightnessPercent)
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    // Wider than the stock panel so the arrangement canvas has room.
    contentWidth: panel.fittedContentWidth(Style.space(600))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(880))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Dropdown popups own j/k/Enter while open.
      blocked: modeDropdown.popupOpen || refreshDropdown.popupOpen
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx !== 0) {
          if (root.focusSection === "brightness") root.adjustBrightness(dx * 5)
          else if (root.focusSection === "textsize") root.adjustTextSize(dx)
          else root.moveCursorH(dx)
        }
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: panelColumn.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        Binding {
          target: scrollArea.contentItem
          property: "interactive"
          // Tile drags must not scroll the panel.
          value: panelColumn.implicitHeight > scrollArea.height && !root.dragging
        }

        Column {
          id: panelColumn
          width: scrollArea.availableWidth
          spacing: Style.space(11)

          // ---------- Hero: display icon · title/status ----------
          Item {
            width: parent.width
            implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

            Text {
              id: heroIcon
              textFormat: Text.PlainText
              text: root.enabledCount > 1 ? "󰍺" : "󰍹"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroLabels
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(14)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "Display"
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
                width: parent.width
              }

              Text {
                id: heroLabel
                textFormat: Text.PlainText
                text: {
                  if (root.applying) return "APPLYING…"
                  if (root.applyError !== "") return "APPLY FAILED"
                  if (root.brightnessAvailable) {
                    return root.brightnessName(brightnessSlider.dragging ? brightnessSlider.liveValue : root.brightnessPercent).toUpperCase()
                  }
                  return root.enabledCount + (root.enabledCount === 1 ? " MONITOR" : " MONITORS")
                }
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
                elide: Text.ElideRight
                width: parent.width
              }
            }
          }

          // ---------- Brightness ----------
          PanelSeparator {
            visible: root.brightnessAvailable
            foreground: root.bar.foreground
          }

          Column {
            visible: root.brightnessAvailable
            width: parent.width
            spacing: Style.space(6)

            Item {
              width: parent.width
              implicitHeight: Math.max(brightnessHeader.implicitHeight, brightnessPercent.implicitHeight)

              PanelSectionHeader {
                id: brightnessHeader
                text: "BRIGHTNESS"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: brightnessPercent
                textFormat: Text.PlainText
                text: Math.round(brightnessSlider.dragging ? brightnessSlider.liveValue : root.brightnessPercent) + "%"
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            CursorSurface {
              id: brightnessRow
              width: parent.width
              height: brightnessSlider.implicitHeight + Style.spacing.controlGap
              hasCursor: root.cursorActive && root.focusSection === "brightness" && root.selectedIndex === -1
              onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(brightnessRow)
              foreground: root.bar.foreground
              outline: true

              PanelSlider {
                id: brightnessSlider
                bar: root.bar
                anchors.fill: parent
                anchors.leftMargin: Style.space(6)
                anchors.rightMargin: Style.space(6)
                minimum: 1
                maximum: 100
                step: 1
                value: root.brightnessPercent
                integer: true
                onMoved: function(v) { root.previewBrightness(v) }
                onReleased: function(v) {
                  brightnessDebounce.stop()
                  root.setBrightness(v)
                }
              }

              HoverHandler {
                onHoveredChanged: if (hovered) root.setCursor("brightness", -1)
              }
            }
          }

          // ---------- Text size ----------
          PanelSeparator {
            foreground: root.bar.foreground
          }

          Column {
            width: parent.width
            spacing: Style.space(6)

            Item {
              width: parent.width
              implicitHeight: Math.max(textSizeHeader.implicitHeight, textSizePx.implicitHeight)

              PanelSectionHeader {
                id: textSizeHeader
                text: "TEXT SIZE"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: textSizePx
                textFormat: Text.PlainText
                text: (textSizeSlider.dragging
                       ? root.textSizeStops[Math.round(textSizeSlider.liveValue)]
                       : root.displayedTextPx()) + "px"
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            CursorSurface {
              id: textSizeRow
              width: parent.width
              height: textSizeSlider.implicitHeight + Style.spacing.controlGap
              hasCursor: root.cursorActive && root.focusSection === "textsize" && root.selectedIndex === -1
              onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(textSizeRow)
              foreground: root.bar.foreground
              outline: true

              PanelSlider {
                id: textSizeSlider
                bar: root.bar
                anchors.fill: parent
                anchors.leftMargin: Style.space(6)
                anchors.rightMargin: Style.space(6)
                minimum: 0
                maximum: root.textSizeStops.length - 1
                step: 1
                integer: true
                tickCount: root.textSizeStops.length
                value: root.currentTextIndex()
                onReleased: function(v) { root.setTextSize(root.textSizeStops[Math.round(v)]) }
              }

              HoverHandler {
                onHoveredChanged: if (hovered) root.setCursor("textsize", -1)
              }
            }
          }

          // ---------- Arrangement ----------
          PanelSeparator {
            foreground: root.bar.foreground
          }

          Column {
            width: parent.width
            spacing: Style.space(10)

            Item {
              width: parent.width
              implicitHeight: Math.max(arrangeHeader.implicitHeight, arrangeHint.implicitHeight)

              PanelSectionHeader {
                id: arrangeHeader
                text: "ARRANGEMENT"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: arrangeHint
                textFormat: Text.PlainText
                text: root.enabledCount > 1 ? "drag to rearrange · changes apply as you go" : "connect a second monitor to arrange"
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            // Scaled-down map of the enabled monitors in logical coordinates.
            BorderSurface {
              id: canvas
              width: parent.width
              height: Style.space(180)
              radius: Style.cornerRadius
              color: Style.normalFillFor(root.bar.foreground, Color.accent)
              borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

              readonly property var box: Model.bounds(root.draft)
              readonly property real pad: Style.space(14)
              // Fit the layout into roughly two thirds of the canvas so a
              // tile can be dragged clear across its neighbour without
              // leaving the canvas.
              readonly property real factor: {
                var b = box
                if (!b || b.w <= 0 || b.h <= 0) return 0.1
                return Math.min((width - pad * 2) / b.w, (height - pad * 2) / b.h) * 0.68
              }
              readonly property real offsetX: pad + ((width - pad * 2) - box.w * factor) / 2
              readonly property real offsetY: pad + ((height - pad * 2) - box.h * factor) / 2
              // Snap distance in logical pixels: a fixed on-screen distance.
              readonly property real snapThreshold: factor > 0 ? Style.space(28) / factor : 0

              function toCanvasX(lx) { return offsetX + (lx - box.x) * factor }
              function toCanvasY(ly) { return offsetY + (ly - box.y) * factor }

              // Keyed on count, not the array, so delegates survive draft
              // edits and can animate to their new spot instead of being
              // rebuilt.
              Repeater {
                model: root.draft.length

                Item {
                  id: tileSlot
                  required property int index

                  readonly property var modelData: root.draft[index] || ({ name: "", enabled: false, x: 0, y: 0, width: 1, height: 1, scale: 1, transform: 0 })
                  readonly property var size: Model.logicalSize(modelData)
                  readonly property bool isSelected: modelData.name === root.selectedMonitor
                  // Set around the draft update on drop so this tile's own
                  // Behavior stays out of the way of the settle animation.
                  property bool settling: false

                  visible: modelData.enabled
                  x: canvas.toCanvasX(modelData.x)
                  y: canvas.toCanvasY(modelData.y)
                  width: Math.max(Style.space(24), size.w * canvas.factor)
                  height: Math.max(Style.space(18), size.h * canvas.factor)
                  // Dragged tile paints above its siblings.
                  z: tileDrag.active ? 10 : (isSelected ? 2 : 1)

                  Behavior on x { enabled: !tileDrag.active && !tileSlot.settling; NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                  Behavior on y { enabled: !tileDrag.active && !tileSlot.settling; NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                  Behavior on width { enabled: !tileDrag.active; NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                  Behavior on height { enabled: !tileDrag.active; NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

                  Item {
                    id: mover
                    width: parent.width
                    height: parent.height

                    // After a drop the tile glides from where it was let go
                    // to where the layout put it.
                    ParallelAnimation {
                      id: settleAnim
                      NumberAnimation { target: mover; property: "x"; to: 0; duration: 220; easing.type: Easing.OutCubic }
                      NumberAnimation { target: mover; property: "y"; to: 0; duration: 220; easing.type: Easing.OutCubic }
                    }

                    CursorSurface {
                      id: tile
                      anchors.fill: parent
                      anchors.margins: Style.space(2)
                      radius: Math.max(2, Style.cornerRadius / 2)
                      bordered: true
                      current: tileSlot.isSelected
                      hasCursor: root.cursorActive && root.focusSection === "monitors" && root.selectedIndex === tileSlot.index
                      foreground: root.bar.foreground
                      fill: Style.hoverFillFor(root.bar.foreground, Color.accent)
                      currentFill: Style.selectedFillFor(root.bar.foreground, Color.accent)
                      clip: true

                      Column {
                        anchors.centerIn: parent
                        width: parent.width - Style.space(8)
                        spacing: Style.space(1)

                        Text {
                          textFormat: Text.PlainText
                          width: parent.width
                          horizontalAlignment: Text.AlignHCenter
                          text: tileSlot.modelData.name + (tileSlot.modelData.focused ? " ●" : "")
                          color: root.bar.foreground
                          font.family: root.bar.fontFamily
                          font.pixelSize: Style.font.bodySmall
                          font.bold: true
                          elide: Text.ElideRight
                        }

                        Text {
                          textFormat: Text.PlainText
                          width: parent.width
                          horizontalAlignment: Text.AlignHCenter
                          text: tileSlot.modelData.width + "×" + tileSlot.modelData.height
                            + (Number(tileSlot.modelData.scale) !== 1 ? " · " + Model.normalizeScale(tileSlot.modelData.scale) + "×" : "")
                            + (tileSlot.modelData.transform ? " · " + Model.transformLabel(tileSlot.modelData.transform) : "")
                          color: Qt.darker(root.bar.foreground, 1.4)
                          font.family: root.bar.fontFamily
                          font.pixelSize: Style.font.caption
                          elide: Text.ElideRight
                          visible: tileSlot.height > Style.space(36)
                        }
                      }
                    }

                    MouseArea {
                      id: tileDrag
                      anchors.fill: parent
                      hoverEnabled: true
                      cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                      drag.target: mover
                      drag.axis: Drag.XAndYAxis
                      drag.threshold: 3
                      readonly property bool active: drag.active
                      onContainsMouseChanged: if (containsMouse) root.setCursor("monitors", tileSlot.index)
                      onPressed: {
                        settleAnim.stop()
                        root.selectMonitor(tileSlot.modelData.name)
                        root.dragging = true
                      }
                      onReleased: {
                        root.dragging = false
                        var dx = mover.x
                        var dy = mover.y
                        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) { mover.x = 0; mover.y = 0; return }
                        // Where the tile visually is right now, in canvas coords.
                        var visX = tileSlot.x + dx
                        var visY = tileSlot.y + dy
                        var factor = canvas.factor
                        tileSlot.settling = true
                        root.moveMonitor(tileSlot.index, dx / factor, dy / factor, canvas.snapThreshold)
                        tileSlot.settling = false
                        // The slot jumped to its new place; keep the visual
                        // where the pointer left it and glide from there.
                        mover.x = visX - tileSlot.x
                        mover.y = visY - tileSlot.y
                        settleAnim.restart()
                      }
                      onCanceled: {
                        root.dragging = false
                        mover.x = 0
                        mover.y = 0
                      }
                    }
                  }
                }
              }
            }

            // One row per monitor; click/Enter selects it for editing.
            Repeater {
              model: root.draft.length

              MonitorRow {
                required property int index

                width: panelColumn.width
                display: root.draft[index] || null
                rowIndex: index
              }
            }
          }

          // ---------- Selected monitor ----------
          PanelSeparator {
            visible: root.selectedEntry !== null
            foreground: root.bar.foreground
          }

          Column {
            id: detailColumn
            visible: root.selectedEntry !== null
            width: parent.width
            spacing: Style.space(8)

            Item {
              width: parent.width
              implicitHeight: Math.max(detailHeader.implicitHeight, detailDesc.implicitHeight)

              PanelSectionHeader {
                id: detailHeader
                text: root.selectedEntry ? root.selectedEntry.name.toUpperCase() : ""
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: detailDesc
                textFormat: Text.PlainText
                text: root.selectedEntry ? root.selectedEntry.description : ""
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
                width: parent.width - detailHeader.implicitWidth - Style.space(16)
                horizontalAlignment: Text.AlignRight
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            // Enabled
            DetailRow {
              id: enabledRow
              label: "Enabled"
              section: "enabled"
              hint: {
                var e = root.selectedEntry
                if (!e) return ""
                if (e.held) return "laptop panel stays off while the lid is closed"
                if (e.enabled && root.enabledCount <= 1) return "the last monitor stays on"
                if (!e.enabled) return "switched off here; turn on to have it back"
                return ""
              }
              opacity: root.selectedEntry && root.selectedEntry.held ? 0.5 : 1.0
              onActivated: root.toggleSelectedEnabled()

              ToggleSwitch {
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                checked: root.selectedEntry ? root.selectedEntry.enabled : false
                interactive: false
                foreground: root.bar.foreground
              }
            }

            // Resolution
            DetailRow {
              id: modeRow
              label: "Resolution"
              section: "mode"
              visible: root.selectedEntry !== null && root.selectedEntry.enabled

              Dropdown {
                id: modeDropdown
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(170)
                showLabel: false
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                options: root.resolutionOptions
                value: root.currentResolutionValue()
                hasCursor: modeRow.hasCursor
                onChanged: function(v) { root.setResolution(v) }
                onHovered: function(h) { if (h) root.setCursor("mode", -1) }
                onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
              }
            }

            // Refresh rate
            DetailRow {
              id: refreshRow
              label: "Refresh rate"
              section: "refresh"
              visible: root.selectedEntry !== null && root.selectedEntry.enabled

              Dropdown {
                id: refreshDropdown
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(170)
                showLabel: false
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                options: root.refreshOptions
                value: root.currentRefreshValue()
                hasCursor: refreshRow.hasCursor
                onChanged: function(v) { root.setRefresh(v) }
                onHovered: function(h) { if (h) root.setCursor("refresh", -1) }
                onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
              }
            }

            // Scale
            Column {
              width: parent.width
              spacing: Style.space(6)
              visible: root.selectedEntry !== null && root.selectedEntry.enabled

              Item {
                width: parent.width
                implicitHeight: Math.max(scaleLabel.implicitHeight, scaleCurrent.implicitHeight)

                Text {
                  id: scaleLabel
                  textFormat: Text.PlainText
                  text: "Scale"
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.body
                  anchors.left: parent.left
                  anchors.leftMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                }

                Text {
                  id: scaleCurrent
                  textFormat: Text.PlainText
                  text: root.selectedEntry ? Model.normalizeScale(root.selectedEntry.scale) + "×" : ""
                  color: Qt.darker(root.bar.foreground, 1.4)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                }
              }

              Grid {
                id: scaleRow
                width: parent.width
                columns: Math.max(1, root.scaleValues.length)
                spacing: Style.spacing.xs

                readonly property real cellWidth: root.scaleValues.length > 0
                  ? (width - spacing * (columns - 1)) / columns
                  : 0

                Repeater {
                  model: root.scaleValues

                  Pill {
                    required property string modelData
                    required property int index

                    text: root.effectiveScale(modelData) + "×"
                    section: "scale"
                    pillIndex: index
                    active: root.activeScaleIndex() === index
                    width: scaleRow.cellWidth
                    onClicked: root.setScale(modelData)
                  }
                }
              }
            }

            // Rotation
            Column {
              width: parent.width
              spacing: Style.space(6)
              visible: root.selectedEntry !== null && root.selectedEntry.enabled

              Text {
                textFormat: Text.PlainText
                text: "Rotation"
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                leftPadding: Style.space(6)
              }

              Grid {
                id: rotationRow
                width: parent.width
                columns: root.rotationValues.length
                spacing: Style.spacing.xs

                readonly property real cellWidth: (width - spacing * (columns - 1)) / columns

                Repeater {
                  model: root.rotationValues

                  Pill {
                    required property int modelData
                    required property int index

                    text: Model.transformLabel(modelData)
                    section: "rotation"
                    pillIndex: index
                    active: root.selectedEntry && root.selectedEntry.transform === modelData
                    width: rotationRow.cellWidth
                    onClicked: root.setRotation(modelData)
                  }
                }
              }
            }

          }

          // ---------- Apply failure ----------
          PanelSeparator {
            visible: root.applyError !== ""
            foreground: root.bar.foreground
          }

          Text {
            visible: root.applyError !== ""
            textFormat: Text.PlainText
            text: root.applyError
            color: root.bar.urgent !== undefined ? root.bar.urgent : root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
            width: parent.width
            leftPadding: Style.space(6)
          }

          Item {
            width: parent.width
            height: Style.space(4)
          }
        }
      }
    }
  }

  // Horizontal preset button (scale, rotation) wired into the cursor model.
  component Pill: Button {
    id: pill
    required property string section
    required property int pillIndex

    fontSize: Style.font.caption
    foreground: root.bar.foreground
    fontFamily: root.bar.fontFamily
    horizontalPadding: Style.spacing.sm
    verticalPadding: Style.spacing.controlPaddingY
    bordered: true

    hasCursor: root.cursorActive && root.focusSection === pill.section && root.selectedIndex === pill.pillIndex
    onHovered: function(isHovered) { if (isHovered) root.setCursor(pill.section, pill.pillIndex) }
  }

  // Label-on-the-left row hosting one control on the right. The row is the
  // cursor target; `activated` fires on click/Enter for rows whose control
  // isn't itself clickable (the enabled switch).
  component DetailRow: CursorSurface {
    id: detailRow
    required property string label
    required property string section
    property string hint: ""
    signal activated()

    width: detailColumn.width
    implicitHeight: Math.max(Style.spacing.controlHeight, detailLabel.implicitHeight) + Style.spacing.md
    hasCursor: root.cursorActive && root.focusSection === section && root.selectedIndex === -1
    onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(detailRow)
    foreground: root.bar.foreground
    outline: true

    Column {
      id: detailLabel
      anchors.left: parent.left
      anchors.leftMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      spacing: 0

      Text {
        textFormat: Text.PlainText
        text: detailRow.label
        color: root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.body
      }

      Text {
        visible: detailRow.hint !== ""
        textFormat: Text.PlainText
        text: detailRow.hint
        color: Qt.darker(root.bar.foreground, 1.6)
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      z: -1
      onContainsMouseChanged: if (containsMouse) root.setCursor(detailRow.section, -1)
      onClicked: detailRow.activated()
    }
  }

  // One-line summary row per monitor: name on the left, mode/scale on the
  // right, tick for "on". Kept short so the whole panel fits without scrolling.
  component MonitorRow: CursorSurface {
    id: monitorRow
    required property var display
    required property int rowIndex

    readonly property bool isSelected: display && display.name === root.selectedMonitor
    readonly property string summary: {
      var d = display
      if (!d) return ""
      if (!d.enabled) return d.held ? "off · lid closed" : "off"
      var bits = [d.width + "×" + d.height + " @ " + Math.round(d.refresh) + " Hz", Model.normalizeScale(d.scale) + "×"]
      if (d.transform) bits.push(Model.transformLabel(d.transform))
      return bits.join(" · ")
    }

    hasCursor: root.cursorActive && root.focusSection === "monitors" && root.selectedIndex === rowIndex
    onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(monitorRow)
    current: isSelected
    foreground: root.bar.foreground
    fill: Style.hoverFillFor(root.bar.foreground, Color.accent)
    currentFill: Style.selectedFillFor(root.bar.foreground, Color.accent)
    implicitHeight: Style.spacing.controlHeight + Style.spacing.sm
    opacity: display && display.enabled ? 1.0 : 0.55

    Text {
      id: rowIcon
      text: display && display.transform % 2 === 1 ? "󰹑" : "󰍹"
      color: root.bar.foreground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.subtitle
      width: Style.space(22)
      horizontalAlignment: Text.AlignHCenter
      anchors.left: parent.left
      anchors.leftMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: rowName
      textFormat: Text.PlainText
      text: display ? display.name + (display.focused ? " · focused" : "") : ""
      color: root.bar.foreground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
      elide: Text.ElideRight
      anchors.left: rowIcon.right
      anchors.leftMargin: Style.space(8)
      anchors.right: rowSummary.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: rowSummary
      textFormat: Text.PlainText
      text: monitorRow.summary
      color: Qt.darker(root.bar.foreground, 1.4)
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
      anchors.right: rowTick.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: rowTick
      textFormat: Text.PlainText
      text: display && display.enabled ? "󰄬" : ""
      color: root.bar.foreground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.subtitle
      width: Style.space(16)
      horizontalAlignment: Text.AlignRight
      anchors.right: parent.right
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onContainsMouseChanged: if (containsMouse) root.setCursor("monitors", monitorRow.rowIndex)
      onClicked: if (monitorRow.display) root.selectMonitor(monitorRow.display.name)
    }
  }
}
