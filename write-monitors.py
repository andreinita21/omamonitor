#!/usr/bin/env python3
"""Regenerate ~/.config/hypr/monitors.lua from a layout JSON document.

Usage: write-monitors.py '{"monitors": [{"name": "DP-3", "enabled": true,
        "mode": "2560x1440@165.08", "position": "1920x0", "scale": "1.25",
        "transform": 0, "description": "Dell ..."}, ...]}'
       write-monitors.py --dump      print the saved rules as JSON
       write-monitors.py --migrate   rewrite the file with identity keys

The whole file is rewritten (the Display panel owns it). The payload lists
the displays connected right now; rules saved earlier for displays that are
not connected are kept, so a monitor comes back where it was.

External displays are keyed by description (`output = "desc:Make Model
Serial"`), so a rule follows the monitor instead of the port: a different
monitor in the same port never inherits its mode or rotation. The laptop
panel stays keyed by port name because Omarchy's clamshell scripts look it
up that way. Older files keyed every display by port; those rules are
converted using the description in the comment above them.

The GDK_SCALE value already in the file is preserved; without one it is
derived from the largest enabled scale, because GTK only honours
whole-number factors.
"""

import json
import os
import re
import stat
import sys
import tempfile

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MODE_RE = re.compile(
    r"^(\d+x\d+@\d+(\.\d+)?|modeline \d+(\.\d+)?( \d+){8} [+-]hsync [+-]vsync)$"
)
POSITION_RE = re.compile(r"^-?\d+x-?\d+$")
SCALE_RE = re.compile(r"^\d+(\.\d+)?$")
GDK_RE = re.compile(r'^hl\.env\("GDK_SCALE",\s*"?(\d+)"?\)', re.M)
GDK_LOCAL_RE = re.compile(r"^local omarchy_gdk_scale\s*=\s*(\d+)", re.M)
RULE_RE = re.compile(r"^[ \t]*hl\.monitor\(\{(.*)\}\)[ \t]*$")
FIELD_RE = re.compile(r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|[^,}\s]+)')
COMMENT_RE = re.compile(r"^[ \t]*--[ \t]?(.*)$")
INTERNAL_RE = re.compile(r"^(eDP|LVDS|DSI)-")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def monitors_path() -> str:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    # ~/.config may be a symlink farm (dotfiles); write to the real file so
    # the atomic replace does not swap the link for a plain file.
    return os.path.realpath(os.path.join(config_home, "hypr", "monitors.lua"))


def read_existing(path: str) -> str:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ""
    if not stat.S_ISREG(st.st_mode):
        raise OSError("not a regular file: %s" % path)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_atomic(path: str, text: str) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".monitors.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lua_comment(text: str) -> str:
    # Keep the comment on one line and free of anything that could end it.
    cleaned = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
    return cleaned or "unnamed display"


def lua_string(text: str) -> str:
    cleaned = re.sub(r"[\r\n]+", " ", str(text))
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lua_unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def is_internal(name: str) -> bool:
    return bool(INTERNAL_RE.match(name))


def rule_key(name: str, description: str) -> str:
    desc = re.sub(r"[\r\n]+", " ", str(description or "")).strip()
    if is_internal(name) or not desc:
        return name
    return "desc:" + desc


def parse_rules(text: str) -> list:
    """Every hl.monitor rule in the file except the catch-all, in order, with
    the comment line above it. Port-keyed rules for external displays (the
    old format) get the description from that comment as their key."""
    rules = []
    comment = ""
    for raw in text.splitlines():
        line = re.sub(r"--\[\[.*?\]\]", "", raw)
        cm = COMMENT_RE.match(line)
        if cm:
            comment = cm.group(1).strip()
            continue
        m = RULE_RE.match(line)
        if not m:
            if line.strip():
                comment = ""
            continue
        fields = {k: v for k, v in FIELD_RE.findall(m.group(1))}
        output = lua_unquote(fields.get("output", '""'))
        if not output:
            comment = ""
            continue
        note = re.sub(r"\s*\(laptop panel;.*\)$", "", comment)
        key = output
        if not output.startswith("desc:") and not is_internal(output) and note and note != "unnamed display":
            key = "desc:" + note
        rule = {
            "key": key,
            "output": output,
            "comment": note,
            "disabled": fields.get("disabled") == "true",
        }
        for field in ("mode", "position"):
            if field in fields:
                rule[field] = lua_unquote(fields[field])
        for field in ("scale", "transform"):
            if field in fields:
                rule[field] = fields[field]
        rules.append(rule)
        comment = ""
    return rules


def render_rule(key: str, rule: dict) -> str:
    """A saved rule re-rendered under `key`, or "" when a field looks off."""
    if rule.get("disabled"):
        return "hl.monitor({ output = %s, disabled = true })" % lua_string(key)
    mode = rule.get("mode", "preferred")
    position = rule.get("position", "auto")
    scale = str(rule.get("scale", "1"))
    transform = str(rule.get("transform", "0"))
    if not (MODE_RE.match(mode) or mode in ("preferred", "highres", "highrr")):
        return ""
    if not (POSITION_RE.match(position) or position.startswith("auto")):
        return ""
    if not (SCALE_RE.match(scale) or IDENT_RE.match(scale) or scale == '"auto"'):
        return ""
    if not re.match(r"^[0-7]$", transform):
        return ""
    return 'hl.monitor({ output = %s, mode = "%s", position = "%s", scale = %s, transform = %s })' % (
        lua_string(key), mode, position, scale, transform)


def existing_rule(text: str, name: str) -> str:
    """The hl.monitor line the file already has for `name`, unless it pins the
    output off. Used for outputs the clamshell toggle is holding: the panel
    cannot see their geometry while they are off, so the last rule stays."""
    pattern = re.compile(
        r'^[ \t]*hl\.monitor\(\{[^\n]*output[ \t]*=[ \t]*"%s"[^\n]*\}\)[ \t]*$' % re.escape(name),
        re.M,
    )
    for match in pattern.finditer(text):
        line = match.group(0).strip()
        if re.search(r"disabled[ \t]*=[ \t]*true", line):
            continue
        return line
    return ""


def render(monitors: list, kept: list, gdk_scale: int, kept_heading: bool = True) -> str:
    lines = [
        "-- Monitor layout written by the Omamonitor panel (andreinita21.omamonitor).",
        "-- Every change in the panel regenerates this whole file, so hand edits",
        "-- made here are lost on the next change. External displays are matched",
        "-- by description (desc:), so each rule follows its monitor, not the port.",
        "-- List current monitors and supported resolutions with: hyprctl monitors all",
        "",
        "local omarchy_gdk_scale = %d" % gdk_scale,
        "",
        'hl.env("GDK_SCALE", tostring(omarchy_gdk_scale))',
        "",
        "-- Any monitor not listed below (one plugged in later) gets sane defaults.",
        'hl.monitor({ output = "", mode = "preferred", position = "auto", scale = "auto" })',
    ]
    for m in monitors:
        if m.get("held"):
            if not m.get("kept_rule"):
                # Nothing known about it yet: the catch-all above handles it
                # once the lid opens.
                continue
            lines.append("")
            lines.append("-- %s (laptop panel; managed by the lid, rule kept as is)" % lua_comment(m["description"]))
            lines.append(m["kept_rule"])
            continue
        key = lua_string(rule_key(m["name"], m["description"]))
        lines.append("")
        lines.append("-- %s" % lua_comment(m["description"]))
        if not m["enabled"]:
            lines.append("hl.monitor({ output = %s, disabled = true })" % key)
            continue
        lines.append(
            'hl.monitor({ output = %s, mode = "%s", position = "%s", scale = %s, transform = %d })'
            % (key, m["mode"], m["position"], m["scale"], m["transform"])
        )
    if kept and kept_heading:
        lines.append("")
        lines.append("-- Displays not connected when this was written; kept for when they return.")
    for comment, line in kept:
        lines.append("")
        lines.append("-- %s" % lua_comment(comment))
        lines.append(line)
    return "\n".join(lines) + "\n"


def kept_rules(existing: str, monitors: list) -> list:
    """Saved rules for displays that are not in the payload, re-keyed."""
    present_keys = {rule_key(m["name"], m["description"]) for m in monitors}
    present_names = {m["name"] for m in monitors}
    kept, seen = [], set()
    for rule in parse_rules(existing):
        key = rule["key"]
        if key in present_keys or key in seen:
            continue
        # A port-keyed rule left over for a connected port belongs to
        # whatever is plugged in there now, which the payload describes.
        if not key.startswith("desc:") and key in present_names:
            continue
        line = render_rule(key, rule)
        if not line:
            continue
        seen.add(key)
        kept.append((rule["comment"] or key, line))
    return kept


def validate(raw: dict) -> list:
    monitors = raw.get("monitors") if isinstance(raw, dict) else None
    if not isinstance(monitors, list) or not monitors:
        raise ValueError("payload needs a non-empty 'monitors' list")
    out = []
    for m in monitors:
        if not isinstance(m, dict):
            raise ValueError("monitor entries must be objects")
        name = str(m.get("name", ""))
        if not NAME_RE.match(name):
            raise ValueError("unsafe monitor name: %r" % name)
        enabled = bool(m.get("enabled"))
        entry = {
            "name": name,
            "enabled": enabled,
            "held": bool(m.get("held")) and not enabled,
            "description": m.get("description", ""),
        }
        if enabled:
            mode = str(m.get("mode", ""))
            position = str(m.get("position", ""))
            scale = str(m.get("scale", ""))
            transform = int(m.get("transform", 0))
            if not MODE_RE.match(mode):
                raise ValueError("bad mode for %s: %r" % (name, mode))
            if not POSITION_RE.match(position):
                raise ValueError("bad position for %s: %r" % (name, position))
            if not SCALE_RE.match(scale) or float(scale) <= 0:
                raise ValueError("bad scale for %s: %r" % (name, scale))
            if transform < 0 or transform > 7:
                raise ValueError("bad transform for %s: %r" % (name, transform))
            entry.update(mode=mode, position=position, scale=scale, transform=transform)
        out.append(entry)
    if not any(e["enabled"] or e["held"] for e in out):
        raise ValueError("refusing to write a layout with every monitor disabled")
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: write-monitors.py JSON | --dump | --migrate", file=sys.stderr)
        return 2

    path = monitors_path()
    if sys.argv[1] == "--dump":
        try:
            print(json.dumps(parse_rules(read_existing(path)), separators=(",", ":")))
        except OSError:
            print("[]")
        return 0
    if sys.argv[1] == "--migrate":
        existing = read_existing(path)
        if not existing:
            return 0
        rules = parse_rules(existing)
        if all(r["output"] == r["key"] for r in rules):
            print("ok")
            return 0
        match = GDK_RE.search(existing) or GDK_LOCAL_RE.search(existing)
        gdk_scale = int(match.group(1)) if match else 1
        write_atomic(path, render([], kept_rules(existing, []), gdk_scale, kept_heading=False))
        print("ok")
        return 0

    try:
        monitors = validate(json.loads(sys.argv[1]))
    except (ValueError, TypeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    existing = read_existing(path)
    for m in monitors:
        if m["held"]:
            m["kept_rule"] = existing_rule(existing, m["name"])
    match = GDK_RE.search(existing) or GDK_LOCAL_RE.search(existing)
    if match:
        gdk_scale = int(match.group(1))
    else:
        largest = max(float(m["scale"]) for m in monitors if m["enabled"])
        gdk_scale = max(1, int(largest + 0.5))

    write_atomic(path, render(monitors, kept_rules(existing, monitors), gdk_scale))
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
