#!/usr/bin/env python3
"""Regenerate ~/.config/hypr/monitors.lua from a layout JSON document.

Usage: write-monitors.py '{"monitors": [{"name": "DP-3", "enabled": true,
        "mode": "2560x1440@165.08", "position": "1920x0", "scale": "1.25",
        "transform": 0, "description": "Dell ..."}, ...]}'

The whole file is rewritten (the Display panel owns it). The GDK_SCALE value
already in the file is preserved; without one it is derived from the largest
enabled scale, because GTK only honours whole-number factors.
"""

import json
import os
import re
import stat
import sys
import tempfile

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MODE_RE = re.compile(r"^\d+x\d+@\d+(\.\d+)?$")
POSITION_RE = re.compile(r"^-?\d+x-?\d+$")
SCALE_RE = re.compile(r"^\d+(\.\d+)?$")
GDK_RE = re.compile(r'^hl\.env\("GDK_SCALE",\s*"?(\d+)"?\)', re.M)
GDK_LOCAL_RE = re.compile(r"^local omarchy_gdk_scale\s*=\s*(\d+)", re.M)


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


def render(monitors: list, gdk_scale: int) -> str:
    lines = [
        "-- Monitor layout written by the Display panel (andrei.display shell plugin).",
        "-- Pressing Apply in the panel regenerates this whole file, so hand edits",
        "-- made here are lost on the next Apply.",
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
        lines.append("")
        lines.append("-- %s" % lua_comment(m["description"]))
        if not m["enabled"]:
            lines.append('hl.monitor({ output = "%s", disabled = true })' % m["name"])
            continue
        lines.append(
            'hl.monitor({ output = "%s", mode = "%s", position = "%s", scale = %s, transform = %d })'
            % (m["name"], m["mode"], m["position"], m["scale"], m["transform"])
        )
    return "\n".join(lines) + "\n"


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
        print("usage: write-monitors.py JSON", file=sys.stderr)
        return 2
    try:
        monitors = validate(json.loads(sys.argv[1]))
    except (ValueError, TypeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    path = monitors_path()
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

    write_atomic(path, render(monitors, gdk_scale))
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
