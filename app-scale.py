#!/usr/bin/env python3
"""Per-app scaling for Chromium-based browsers and Electron apps.

Usage: app-scale.py list
       app-scale.py set APP_ID SCALE|auto

Hyprland scales whole monitors; it has no per-window scale. Chromium and
Electron apps can render at their own factor through
--force-device-scale-factor, and their Arch launchers read extra flags from
~/.config/<name>-flags.conf. This script reads and rewrites that one flag in
those files and leaves every other line alone.

`list` prints a JSON array of the apps found on this system. An app is
listed when its launcher is on PATH and either reads the flags file (the
file name appears in the launcher) or the flags file already exists.
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile

# id, display name, launcher candidates, flags file, main-process paths
# (prefixes of argv[0] used to tell whether the app is running, and with
# which factor).
APPS = [
    ("chromium", "Chromium", ["chromium"], "chromium-flags.conf", ["/usr/lib/chromium/chromium"]),
    ("chrome", "Google Chrome", ["google-chrome-stable", "google-chrome"], "chrome-flags.conf", ["/opt/google/chrome/chrome"]),
    ("brave", "Brave", ["brave"], "brave-flags.conf", ["/opt/brave-bin/brave", "/opt/brave.com/brave/brave"]),
    ("brave-origin", "Brave Origin", ["brave-origin"], "brave-origin-flags.conf", []),
    ("edge", "Microsoft Edge", ["microsoft-edge-stable"], "microsoft-edge-stable-flags.conf", ["/opt/microsoft/msedge/msedge"]),
    ("code", "Visual Studio Code", ["code"], "code-flags.conf", []),
    ("electron", "Electron apps", ["electron"], "electron-flags.conf", []),
]

FLAG = "--force-device-scale-factor"
FLAG_RE = re.compile(r"(^|\s+)%s(=\S*)?(?=\s|$)" % re.escape(FLAG))
SCALE_RE = re.compile(r"^\d+(\.\d+)?$")
# Launchers are small wrappers; never scan a real browser binary.
LAUNCHER_MAX_BYTES = 4 * 1024 * 1024


def config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def flags_path(name: str) -> str:
    return os.path.join(config_home(), name)


def normalize(scale: str) -> str:
    return ("%.2f" % float(scale)).rstrip("0").rstrip(".")


def launcher_reads(launcher: str, flags_name: str) -> bool:
    try:
        if os.path.getsize(launcher) > LAUNCHER_MAX_BYTES:
            return False
        with open(launcher, "rb") as handle:
            return flags_name.encode() in handle.read()
    except OSError:
        return False


def read_lines(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return []


def active_lines(lines: list) -> list:
    return [line for line in lines if not line.lstrip().startswith("#")]


def scale_in(tokens) -> str:
    """The last --force-device-scale-factor value among `tokens`, or ""."""
    found = ""
    for token in tokens:
        if token.startswith(FLAG + "="):
            value = token.split("=", 1)[1].strip("'\"")
            if SCALE_RE.match(value) and float(value) > 0:
                found = normalize(value)
    return found


def running_scales() -> dict:
    """argv[0] -> scale ("" when the flag is absent) for every main browser
    process. Chromium's helper processes carry --type=..., so skip those."""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as handle:
                argv = handle.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if not argv or not argv[0] or any(a.startswith("--type=") for a in argv):
            continue
        out[argv[0]] = scale_in(argv[1:])
    return out


def list_apps() -> list:
    procs = running_scales()
    apps = []
    for app_id, name, launchers, flags_name, main_paths in APPS:
        launcher = next((p for p in (shutil.which(c) for c in launchers) if p), None)
        path = flags_path(flags_name)
        exists = os.path.exists(path)
        if not launcher or not (exists or launcher_reads(os.path.realpath(launcher), flags_name)):
            continue
        lines = active_lines(read_lines(path))
        tokens = " ".join(lines).split()
        running = None
        for argv0, scale in procs.items():
            if any(argv0.startswith(p) for p in main_paths):
                running = scale
                break
        apps.append({
            "id": app_id,
            "name": name,
            "file": path.replace(os.path.expanduser("~"), "~", 1),
            "scale": scale_in(tokens),
            "xwayland": "--ozone-platform=x11" in tokens,
            # Scale the running instance was started with; null when it is
            # not running or cannot be told apart from other processes.
            "running": running,
        })
    return apps


def write_atomic(path: str, text: str) -> None:
    # ~/.config may be a symlink farm (dotfiles); write to the real file so
    # the atomic replace does not swap the link for a plain file.
    real = os.path.realpath(path)
    parent = os.path.dirname(real) or "."
    os.makedirs(parent, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=".flags.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_scale(app_id: str, scale: str) -> None:
    entry = next((a for a in APPS if a[0] == app_id), None)
    if not entry:
        raise ValueError("unknown app: %r" % app_id)
    if scale != "auto":
        if not SCALE_RE.match(scale) or not 0.25 <= float(scale) <= 5:
            raise ValueError("bad scale: %r" % scale)
        scale = normalize(scale)

    path = flags_path(entry[3])
    lines = read_lines(path)
    if not lines and scale == "auto":
        return

    kept = []
    for line in lines:
        if line.lstrip().startswith("#") or not FLAG_RE.search(line):
            kept.append(line)
            continue
        stripped = FLAG_RE.sub("", line).strip()
        if stripped:
            kept.append(stripped)
    if scale != "auto":
        kept.append("%s=%s" % (FLAG, scale))
    write_atomic(path, "\n".join(kept) + "\n")


def main() -> int:
    args = sys.argv[1:]
    try:
        if args == ["list"]:
            print(json.dumps(list_apps()))
            return 0
        if len(args) == 3 and args[0] == "set":
            set_scale(args[1], args[2])
            print("ok")
            return 0
    except (ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("usage: app-scale.py list | set APP_ID SCALE|auto", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
