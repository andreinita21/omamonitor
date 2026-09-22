// Pure helpers for the Display panel: brightness/scale maths inherited from
// the built-in omarchy.monitor plugin, plus the monitor-layout model used by
// the drag-and-drop arrangement editor (parsing hyprctl output, snapping,
// overlap resolution, and building the hyprctl/Lua output).

function clampBrightness(value) {
  var n = Number(value)
  if (!isFinite(n)) return 1
  return Math.max(1, Math.min(100, Math.round(n)))
}

function normalizeScale(scale) {
  var n = parseFloat(String(scale || ""))
  if (!isFinite(n)) return ""
  return String(Math.round(n * 100) / 100)
}

function gcd(a, b) {
  while (b) {
    var remainder = a % b
    a = b
    b = remainder
  }
  return a
}

// Hyprland only accepts scales where the mode divides into whole logical
// pixels (in 1/120 steps), so clean scales are divisors of gcd(w*120, h*120).
function cleanScale(scale, width, height) {
  var requested = Number(scale)
  var modeWidth = Number(width)
  var modeHeight = Number(height)
  if (!isFinite(requested) || !isFinite(modeWidth) || !isFinite(modeHeight)
      || requested <= 0 || modeWidth <= 0 || modeHeight <= 0) return ""

  var divisor = gcd(Math.round(modeWidth * 120), Math.round(modeHeight * 120))
  var scaleUnits = Math.round(requested * 120)
  if (scaleUnits > divisor) scaleUnits = divisor
  while (divisor % scaleUnits !== 0) scaleUnits++
  return normalizeScale(scaleUnits / 120)
}

function matchingScaleIndex(scales, currentScale, width, height) {
  var current = Number(currentScale)
  if (!Array.isArray(scales) || !isFinite(current)) return -1

  var bestIndex = -1
  var bestDistance = Infinity
  var normalizedCurrent = normalizeScale(current)
  for (var i = 0; i < scales.length; i++) {
    if (cleanScale(scales[i], width, height) !== normalizedCurrent) continue

    var distance = Math.abs(Number(scales[i]) - current)
    if (distance < bestDistance) {
      bestIndex = i
      bestDistance = distance
    }
  }
  return bestIndex
}

function availableScales(scales, width, height) {
  if (!Array.isArray(scales) || Number(width) <= 0 || Number(height) <= 0) return scales || []

  var byEffectiveScale = {}
  for (var i = 0; i < scales.length; i++) {
    var requested = Number(scales[i])
    var effective = Number(cleanScale(requested, width, height))

    if (!isFinite(requested) || !isFinite(effective)) continue

    var key = normalizeScale(effective)
    var existing = byEffectiveScale[key]
    if (!existing || Math.abs(requested - effective) < existing.distance) {
      byEffectiveScale[key] = {
        value: String(scales[i]),
        index: i,
        distance: Math.abs(requested - effective)
      }
    }
  }

  return Object.keys(byEffectiveScale)
    .map(function(key) { return byEffectiveScale[key] })
    .sort(function(a, b) { return a.index - b.index })
    .map(function(candidate) { return candidate.value })
}

function brightnessName(percent) {
  var p = Math.round(percent)
  if (p >= 95) return "Sun blast"
  if (p >= 80) return "Solar flare"
  if (p >= 65) return "Golden hour"
  if (p >= 45) return "Even day"
  if (p >= 30) return "Soft glow"
  if (p >= 20) return "Lamp light"
  if (p >= 10) return "Candlelit"
  return "Night owl"
}

// ---------------------------------------------------------------- monitors

function roundRefresh(rate) {
  var n = Number(rate)
  if (!isFinite(n) || n <= 0) return 0
  return Math.round(n * 100) / 100
}

function refreshLabel(rate) {
  return roundRefresh(rate).toFixed(2)
}

// hyprctl lists modes as "2560x1440@165.08Hz". Duplicates (same size and
// rate, different timings) collapse to one entry.
function parseModes(list) {
  var modes = []
  var seen = {}
  if (!Array.isArray(list)) return modes
  for (var i = 0; i < list.length; i++) {
    var match = /^(\d+)x(\d+)@([\d.]+)/.exec(String(list[i] || ""))
    if (!match) continue
    var mode = {
      width: parseInt(match[1], 10),
      height: parseInt(match[2], 10),
      refresh: roundRefresh(match[3])
    }
    var key = mode.width + "x" + mode.height + "@" + refreshLabel(mode.refresh)
    if (seen[key]) continue
    seen[key] = true
    modes.push(mode)
  }
  return modes
}

// Turns `hyprctl monitors all -j` into the panel's monitor records. Disabled
// outputs report a 0x0 mode, so their preferred mode is borrowed from the
// first listed available mode. Enabled monitors sort first, left to right.
function parseMonitors(raw) {
  var list = []
  try {
    list = raw ? JSON.parse(String(raw)) : []
  } catch (e) {
    list = []
  }
  if (!Array.isArray(list)) list = []

  var monitors = []
  for (var i = 0; i < list.length; i++) {
    var m = list[i]
    if (!m || !m.name) continue
    var modes = parseModes(m.availableModes)
    var width = Number(m.width) || 0
    var height = Number(m.height) || 0
    var refresh = roundRefresh(m.refreshRate)
    if ((width <= 0 || height <= 0) && modes.length > 0) {
      width = modes[0].width
      height = modes[0].height
      refresh = modes[0].refresh
    }
    monitors.push({
      name: String(m.name),
      description: String(m.description || ""),
      enabled: m.disabled !== true,
      focused: m.focused === true,
      x: Math.round(Number(m.x) || 0),
      y: Math.round(Number(m.y) || 0),
      width: width,
      height: height,
      refresh: refresh,
      scale: Number(m.scale) > 0 ? Number(m.scale) : 1,
      transform: Math.max(0, Math.min(7, parseInt(m.transform, 10) || 0)),
      modes: modes
    })
  }
  monitors.sort(function(a, b) {
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
    if (a.x !== b.x) return a.x - b.x
    return a.y - b.y
  })
  return monitors
}

// Editable copy of the layout-relevant fields.
function draftFrom(monitors) {
  var draft = []
  for (var i = 0; i < monitors.length; i++) {
    var m = monitors[i]
    draft.push({
      name: m.name,
      description: m.description,
      focused: m.focused,
      enabled: m.enabled,
      x: m.x,
      y: m.y,
      width: m.width,
      height: m.height,
      refresh: m.refresh,
      scale: m.scale,
      transform: m.transform,
      modes: m.modes
    })
  }
  return draft
}

function cloneEntries(entries) {
  var out = []
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i]
    var copy = {}
    for (var key in e) copy[key] = e[key]
    out.push(copy)
  }
  return out
}

function findIndex(entries, name) {
  for (var i = 0; i < entries.length; i++) {
    if (entries[i].name === name) return i
  }
  return -1
}

// Fields that the panel can change; used for dirty tracking and counting.
var LAYOUT_FIELDS = ["enabled", "x", "y", "width", "height", "refresh", "scale", "transform"]

function entryDiffers(a, b) {
  if (!a || !b) return true
  if (!a.enabled && !b.enabled) return false
  for (var i = 0; i < LAYOUT_FIELDS.length; i++) {
    var field = LAYOUT_FIELDS[i]
    if (field === "scale") {
      if (normalizeScale(a.scale) !== normalizeScale(b.scale)) return true
    } else if (field === "refresh") {
      if (refreshLabel(a.refresh) !== refreshLabel(b.refresh)) return true
    } else if (a[field] !== b[field]) {
      return true
    }
  }
  return false
}

function changedCount(draft, live) {
  var count = 0
  for (var i = 0; i < draft.length; i++) {
    var idx = findIndex(live, draft[i].name)
    if (idx < 0 || entryDiffers(draft[i], live[idx])) count++
  }
  return count
}

function enabledCount(entries) {
  var count = 0
  for (var i = 0; i < entries.length; i++) if (entries[i].enabled) count++
  return count
}

// Size in Hyprland's logical (post-scale, post-rotation) coordinates. Odd
// transforms are the 90/270 rotations, which swap the axes.
function logicalSize(entry) {
  var scale = Number(entry.scale) > 0 ? Number(entry.scale) : 1
  var w = Math.max(1, Math.round(entry.width / scale))
  var h = Math.max(1, Math.round(entry.height / scale))
  if (entry.transform % 2 === 1) return { w: h, h: w }
  return { w: w, h: h }
}

function rectOf(entry) {
  var size = logicalSize(entry)
  return { x: entry.x, y: entry.y, w: size.w, h: size.h }
}

// Bounding box of the enabled monitors.
function bounds(entries) {
  var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (var i = 0; i < entries.length; i++) {
    if (!entries[i].enabled) continue
    var r = rectOf(entries[i])
    if (r.x < minX) minX = r.x
    if (r.y < minY) minY = r.y
    if (r.x + r.w > maxX) maxX = r.x + r.w
    if (r.y + r.h > maxY) maxY = r.y + r.h
  }
  if (!isFinite(minX)) return { x: 0, y: 0, w: 1, h: 1 }
  return { x: minX, y: minY, w: Math.max(1, maxX - minX), h: Math.max(1, maxY - minY) }
}

function otherRects(entries, skipName) {
  var rects = []
  for (var i = 0; i < entries.length; i++) {
    if (!entries[i].enabled || entries[i].name === skipName) continue
    rects.push(rectOf(entries[i]))
  }
  return rects
}

// Snap each axis independently to the nearest butting or aligned edge of any
// other monitor when within `threshold` logical pixels.
function snapPosition(rect, others, threshold) {
  var bestX = rect.x, bestXDist = threshold
  var bestY = rect.y, bestYDist = threshold
  for (var i = 0; i < others.length; i++) {
    var o = others[i]
    var xs = [o.x + o.w, o.x - rect.w, o.x, o.x + o.w - rect.w]
    var ys = [o.y + o.h, o.y - rect.h, o.y, o.y + o.h - rect.h]
    for (var j = 0; j < xs.length; j++) {
      var dx = Math.abs(xs[j] - rect.x)
      if (dx < bestXDist) { bestXDist = dx; bestX = xs[j] }
      var dy = Math.abs(ys[j] - rect.y)
      if (dy < bestYDist) { bestYDist = dy; bestY = ys[j] }
    }
  }
  return { x: Math.round(bestX), y: Math.round(bestY) }
}

function overlaps(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y
}

// Push `rect` out of any monitor it overlaps along the axis of least
// penetration. A handful of passes settles even chained overlaps.
function resolveOverlap(rect, others) {
  for (var pass = 0; pass < 8; pass++) {
    var moved = false
    for (var i = 0; i < others.length; i++) {
      var o = others[i]
      if (!overlaps(rect, o)) continue
      var pushLeft = rect.x + rect.w - o.x
      var pushRight = o.x + o.w - rect.x
      var pushUp = rect.y + rect.h - o.y
      var pushDown = o.y + o.h - rect.y
      var min = Math.min(pushLeft, pushRight, pushUp, pushDown)
      if (min === pushLeft) rect.x -= pushLeft
      else if (min === pushRight) rect.x += pushRight
      else if (min === pushUp) rect.y -= pushUp
      else rect.y += pushDown
      moved = true
    }
    if (!moved) break
  }
  return rect
}

// Hyprland positions are absolute; keep the layout anchored at 0,0.
function normalize(entries) {
  var box = bounds(entries)
  for (var i = 0; i < entries.length; i++) {
    if (!entries[i].enabled) continue
    entries[i].x -= box.x
    entries[i].y -= box.y
  }
  return entries
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v))
}

function overlapArea(a, b) {
  var w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)
  var h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y)
  return (w > 0 && h > 0) ? w * h : 0
}

// Put `rect` flush against one side of `o`. Along the shared edge it keeps
// its dropped offset, clamped so at least a quarter of the shorter side
// overlaps (the pointer needs a shared edge to cross), and snaps to the
// aligned-edge positions when close.
function placeBeside(rect, o, horizontal, before, threshold) {
  if (horizontal) {
    rect.x = before ? o.x - rect.w : o.x + o.w
    var share = Math.round(Math.min(rect.h, o.h) * 0.25)
    rect.y = clamp(rect.y, o.y - rect.h + share, o.y + o.h - share)
    if (Math.abs(rect.y - o.y) <= threshold) rect.y = o.y
    else if (Math.abs((rect.y + rect.h) - (o.y + o.h)) <= threshold) rect.y = o.y + o.h - rect.h
  } else {
    rect.y = before ? o.y - rect.h : o.y + o.h
    var shareX = Math.round(Math.min(rect.w, o.w) * 0.25)
    rect.x = clamp(rect.x, o.x - rect.w + shareX, o.x + o.w - shareX)
    if (Math.abs(rect.x - o.x) <= threshold) rect.x = o.x
    else if (Math.abs((rect.x + rect.w) - (o.x + o.w)) <= threshold) rect.x = o.x + o.w - rect.w
  }
}

// True when the two share an edge segment (not just a corner).
function touches(rect, o) {
  var vOverlap = Math.min(rect.y + rect.h, o.y + o.h) - Math.max(rect.y, o.y) > 0
  var hOverlap = Math.min(rect.x + rect.w, o.x + o.w) - Math.max(rect.x, o.x) > 0
  if (vOverlap && (rect.x + rect.w === o.x || o.x + o.w === rect.x)) return true
  if (hOverlap && (rect.y + rect.h === o.y || o.y + o.h === rect.y)) return true
  return false
}

// Close the gap to the nearest monitor so the layout never leaves an island.
function attachToNearest(rect, others, threshold) {
  var bestO = null, bestD = Infinity, horizontal = true
  for (var i = 0; i < others.length; i++) {
    var o = others[i]
    var gapX = rect.x >= o.x + o.w ? rect.x - (o.x + o.w)
      : (rect.x + rect.w <= o.x ? o.x - (rect.x + rect.w) : 0)
    var gapY = rect.y >= o.y + o.h ? rect.y - (o.y + o.h)
      : (rect.y + rect.h <= o.y ? o.y - (rect.y + rect.h) : 0)
    var d = gapX + gapY
    if (d < bestD) { bestD = d; bestO = o; horizontal = gapX >= gapY }
  }
  if (!bestO) return
  if (horizontal) placeBeside(rect, bestO, true, rect.x < bestO.x, threshold)
  else placeBeside(rect, bestO, false, rect.y < bestO.y, threshold)
}

// Re-place one entry after it moved or changed size. Dropped onto another
// monitor, it lands on whichever side of that monitor its centre ended up
// on (left/right wins ties, which is what people usually mean); dropped in
// open space, it snaps to nearby edges and is pulled flush against the
// nearest monitor. Threshold 0 skips edge snapping (size-only changes).
function settleEntry(entries, index, threshold) {
  var entry = entries[index]
  if (!entry || !entry.enabled) return normalize(entries)
  var others = otherRects(entries, entry.name)
  var rect = rectOf(entry)
  if (others.length > 0) {
    var target = null, best = 0
    for (var i = 0; i < others.length; i++) {
      var area = overlapArea(rect, others[i])
      if (area > best) { best = area; target = others[i] }
    }
    if (target) {
      var cx = (rect.x + rect.w / 2) - (target.x + target.w / 2)
      var cy = (rect.y + rect.h / 2) - (target.y + target.h / 2)
      var nx = cx / ((rect.w + target.w) / 2)
      var ny = cy / ((rect.h + target.h) / 2)
      if (Math.abs(nx) * 1.25 >= Math.abs(ny)) placeBeside(rect, target, true, cx < 0, threshold)
      else placeBeside(rect, target, false, cy < 0, threshold)
    } else if (threshold > 0) {
      var snapped = snapPosition(rect, others, threshold)
      rect.x = snapped.x
      rect.y = snapped.y
    }
    resolveOverlap(rect, others)
    var attached = false
    for (var j = 0; j < others.length; j++) {
      if (touches(rect, others[j])) { attached = true; break }
    }
    if (!attached) {
      attachToNearest(rect, others, threshold)
      resolveOverlap(rect, others)
    }
  }
  entry.x = Math.round(rect.x)
  entry.y = Math.round(rect.y)
  return normalize(entries)
}

// A monitor being switched on lands to the right of everything else.
function placeEnabled(entries, index) {
  var entry = entries[index]
  var box = bounds(entries)
  entry.enabled = true
  entry.x = enabledCount(entries) > 1 ? box.x + box.w : 0
  entry.y = enabledCount(entries) > 1 ? box.y : 0
  return normalize(entries)
}

// Unique resolutions for a monitor, largest first, as Dropdown options.
function resolutionOptions(entry) {
  var options = []
  var seen = {}
  var modes = (entry && entry.modes) || []
  var sorted = modes.slice().sort(function(a, b) {
    return (b.width * b.height) - (a.width * a.height) || b.width - a.width
  })
  for (var i = 0; i < sorted.length; i++) {
    var key = sorted[i].width + "x" + sorted[i].height
    if (seen[key]) continue
    seen[key] = true
    options.push({ value: key, label: sorted[i].width + " × " + sorted[i].height })
  }
  var current = entry ? entry.width + "x" + entry.height : ""
  if (current && !seen[current]) options.unshift({ value: current, label: entry.width + " × " + entry.height })
  return options
}

// Refresh rates offered for the entry's current resolution, highest first.
function refreshOptions(entry) {
  var options = []
  var seen = {}
  var modes = (entry && entry.modes) || []
  var rates = []
  for (var i = 0; i < modes.length; i++) {
    if (modes[i].width !== entry.width || modes[i].height !== entry.height) continue
    var label = refreshLabel(modes[i].refresh)
    if (seen[label]) continue
    seen[label] = true
    rates.push(modes[i].refresh)
  }
  rates.sort(function(a, b) { return b - a })
  for (var j = 0; j < rates.length; j++) {
    options.push({ value: refreshLabel(rates[j]), label: refreshLabel(rates[j]) + " Hz" })
  }
  var current = entry ? refreshLabel(entry.refresh) : ""
  if (current && !seen[current]) options.unshift({ value: current, label: current + " Hz" })
  return options
}

// Best refresh rate when switching to a new resolution: keep the current
// rate if that mode exists, otherwise the highest available.
function bestRefreshFor(entry, width, height) {
  var modes = (entry && entry.modes) || []
  var best = 0
  var currentLabel = refreshLabel(entry.refresh)
  for (var i = 0; i < modes.length; i++) {
    if (modes[i].width !== width || modes[i].height !== height) continue
    if (refreshLabel(modes[i].refresh) === currentLabel) return modes[i].refresh
    if (modes[i].refresh > best) best = modes[i].refresh
  }
  return best > 0 ? best : entry.refresh
}

function transformLabel(transform) {
  var t = parseInt(transform, 10) || 0
  var labels = ["Normal", "90°", "180°", "270°", "Flipped", "Flipped 90°", "Flipped 180°", "Flipped 270°"]
  return labels[t] || "Normal"
}

function safeName(name) {
  return /^[A-Za-z0-9._-]+$/.test(String(name || ""))
}

// Laptop panels, the outputs Omarchy's clamshell handling manages.
function isInternal(name) {
  return /^(eDP|LVDS|DSI)-/.test(String(name || ""))
}

function modeString(entry) {
  return entry.width + "x" + entry.height + "@" + refreshLabel(entry.refresh)
}

// Lua for `hyprctl eval` that applies the whole layout at once. Hyprland's
// Lua config mode refuses `hyprctl keyword`, so live changes go through the
// same hl.monitor() calls the persisted file uses. Wrapped in a function so
// the whole thing is one expression however eval prefixes it.
function luaMonitorCall(e) {
  if (!e.enabled) return 'hl.monitor({ output = "' + e.name + '", disabled = true })'
  return 'hl.monitor({ output = "' + e.name + '", mode = "' + modeString(e) + '", position = "'
    + e.x + "x" + e.y + '", scale = ' + normalizeScale(e.scale) + ", transform = " + (parseInt(e.transform, 10) || 0) + " })"
}

// Lua for `hyprctl eval` that applies the whole layout at once. Entries the
// clamshell toggle holds off (`held`) are skipped: that toggle owns them.
function evalScript(entries) {
  var parts = []
  for (var i = 0; i < entries.length; i++) {
    if (!safeName(entries[i].name) || entries[i].held) continue
    parts.push(luaMonitorCall(entries[i]))
  }
  return "(function() " + parts.join(" ") + ' return "ok" end)()'
}

// Payload for write-monitors.py. A held entry asks the writer to keep
// whatever rule the file already has for that output (so the laptop panel
// comes back where it was once the lid opens) instead of pinning it off.
function persistPayload(entries) {
  var monitors = []
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i]
    if (!safeName(e.name)) continue
    monitors.push({
      name: e.name,
      description: e.description,
      enabled: e.enabled,
      held: e.held === true,
      mode: modeString(e),
      position: e.x + "x" + e.y,
      scale: normalizeScale(e.scale),
      transform: e.transform
    })
  }
  return { monitors: monitors }
}

if (typeof module !== "undefined") {
  module.exports = {
    clampBrightness: clampBrightness,
    normalizeScale: normalizeScale,
    cleanScale: cleanScale,
    matchingScaleIndex: matchingScaleIndex,
    availableScales: availableScales,
    brightnessName: brightnessName,
    parseModes: parseModes,
    parseMonitors: parseMonitors,
    draftFrom: draftFrom,
    cloneEntries: cloneEntries,
    findIndex: findIndex,
    entryDiffers: entryDiffers,
    changedCount: changedCount,
    enabledCount: enabledCount,
    logicalSize: logicalSize,
    bounds: bounds,
    snapPosition: snapPosition,
    resolveOverlap: resolveOverlap,
    normalize: normalize,
    settleEntry: settleEntry,
    placeEnabled: placeEnabled,
    resolutionOptions: resolutionOptions,
    refreshOptions: refreshOptions,
    bestRefreshFor: bestRefreshFor,
    transformLabel: transformLabel,
    isInternal: isInternal,
    luaMonitorCall: luaMonitorCall,
    evalScript: evalScript,
    persistPayload: persistPayload
  }
}
