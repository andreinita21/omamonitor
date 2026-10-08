#!/usr/bin/env python3
"""Per-app scaling for whatever is open on the desktop.

Usage: app-scale.py list
       app-scale.py set APP_ID SCALE|auto [KIND]

Hyprland scales whole monitors; it has no per-window scale. Each app is
scaled through its own toolkit instead, and the factor takes effect the next
time the app starts:

- Chromium-based browsers whose launcher reads ~/.config/<name>-flags.conf
  get --force-device-scale-factor in that file. Omarchy launches browsers by
  the first word of their Exec line, so a desktop entry cannot carry it.
- Everything else gets a desktop entry override in
  ~/.local/share/applications whose Exec lines start with
  omamonitor-scale-run, which applies the factor for the app's kind:
  qt (QT_SCALE_FACTOR), gtk3 (GDK_DPI_SCALE, text only), chromium for
  Electron apps (--force-device-scale-factor), and the foot, alacritty,
  kitty and ghostty terminals (font size times the factor).

`list` prints a JSON array: the browsers above when installed, every app
with an open window, and every app that already has a factor set.
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

# id, display name, launcher candidates, flags file, main-process paths
# (prefixes of argv[0], used to match windows and read the running factor).
FLAGS_APPS = [
    ("chromium", "Chromium", ["chromium"], "chromium-flags.conf", ["/usr/lib/chromium/chromium"]),
    ("chrome", "Google Chrome", ["google-chrome-stable", "google-chrome"], "chrome-flags.conf", ["/opt/google/chrome/chrome"]),
    ("brave", "Brave", ["brave"], "brave-flags.conf", ["/opt/brave-bin/brave", "/opt/brave.com/brave/brave"]),
    ("brave-origin", "Brave Origin", ["brave-origin"], "brave-origin-flags.conf", ["/opt/brave-origin"]),
    ("edge", "Microsoft Edge", ["microsoft-edge-stable"], "microsoft-edge-stable-flags.conf", ["/opt/microsoft/msedge/msedge"]),
]

TERMINALS = ("foot", "alacritty", "kitty", "ghostty")
KINDS = ("qt", "gtk3", "chromium") + TERMINALS
UNSUPPORTED = {
    "gtk4": "GTK 4 has no per-app scale",
    None: "toolkit not recognised",
}

FLAG = "--force-device-scale-factor"
FLAG_RE = re.compile(r"(^|\s+)%s(=\S*)?(?=\s|$)" % re.escape(FLAG))
SCALE_RE = re.compile(r"^\d+(\.\d+)?$")
RUNNER = "omamonitor-scale-run"
PREFIX_RE = re.compile(r"^%s\s+--scale=(\S+)\s+--kind=(\S+)\s+" % re.escape(RUNNER))
MARK_SCALE = "X-Omamonitor-Scale"
MARK_CREATED = "X-Omamonitor-Created"
DESKTOP_ID_RE = re.compile(r"^[^/\0]+$")
# Launchers are small wrappers; never scan a real browser binary.
LAUNCHER_MAX_BYTES = 4 * 1024 * 1024
HERE = os.path.dirname(os.path.realpath(__file__))


def home() -> str:
    return os.path.expanduser("~")


def config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(home(), ".config")


def user_apps_dir() -> str:
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(home(), ".local", "share")
    return os.path.join(data, "applications")


def apps_dirs() -> list:
    dirs = [user_apps_dir()]
    for base in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if base:
            dirs.append(os.path.join(base, "applications"))
    return dirs


def tilde(path: str) -> str:
    return path.replace(home(), "~", 1)


def normalize(scale: str) -> str:
    return ("%.2f" % float(scale)).rstrip("0").rstrip(".")


def valid_scale(value: str) -> bool:
    return bool(SCALE_RE.match(value or "")) and 0.25 <= float(value) <= 5


def read_lines(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return []


def write_atomic(path: str, text: str, default_mode: int = 0o644) -> None:
    # Config dirs may be a symlink farm (dotfiles); write to the real file so
    # the atomic replace does not swap the link for a plain file.
    real = os.path.realpath(path)
    parent = os.path.dirname(real) or "."
    os.makedirs(parent, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except FileNotFoundError:
        mode = default_mode
    fd, tmp = tempfile.mkstemp(prefix=".omamonitor.", suffix=".tmp", dir=parent)
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


# ---------------------------------------------------------------- processes

def proc_read(pid: int, name: str) -> bytes:
    try:
        with open("/proc/%d/%s" % (pid, name), "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def proc_exe(pid: int) -> str:
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except OSError:
        return ""


def proc_argv(pid: int) -> list:
    return [a for a in proc_read(pid, "cmdline").decode("utf-8", "replace").split("\0") if a]


def proc_env(pid: int, key: str):
    prefix = (key + "=").encode()
    for item in proc_read(pid, "environ").split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix):].decode("utf-8", "replace")
    return None


def flag_in(tokens) -> str:
    """The last --force-device-scale-factor value among `tokens`, or ""."""
    found = ""
    for token in tokens:
        if token.startswith(FLAG + "="):
            value = token.split("=", 1)[1].strip("'\"")
            if SCALE_RE.match(value) and float(value) > 0:
                found = normalize(value)
    return found


def detect_kind(pid: int, exe: str):
    base = os.path.basename(exe)
    if base in TERMINALS:
        return base
    exe_dir = os.path.dirname(exe)
    if any(os.path.exists(os.path.join(exe_dir, f)) for f in ("v8_context_snapshot.bin", "chrome_100_percent.pak")):
        return "chromium"
    maps = proc_read(pid, "maps")
    # Qt and Chromium apps can map GTK too (themes, file dialogs), so GTK last.
    if b"libQt6Gui" in maps or b"libQt5Gui" in maps:
        return "qt"
    if b"libgtk-4" in maps:
        return "gtk4"
    if b"libgtk-3" in maps:
        return "gtk3"
    return None


def windows() -> list:
    try:
        out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=3).stdout
        clients = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [c for c in clients if isinstance(c, dict) and int(c.get("pid") or 0) > 0]


# ----------------------------------------------------------- desktop entries

def parse_entry(path: str) -> dict:
    entry = {}
    group = ""
    for line in read_lines(path):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            group = stripped[1:-1]
            continue
        if group == "Desktop Entry" and "=" in line and not stripped.startswith("#"):
            key, value = line.split("=", 1)
            entry.setdefault(key.strip(), value.strip())
    return entry


_INDEX = None


def desktop_index() -> dict:
    """Desktop id -> path, the user's directory winning over system ones."""
    global _INDEX
    if _INDEX is None:
        _INDEX = {}
        for directory in apps_dirs():
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                if name.endswith(".desktop") and name[:-8] not in _INDEX:
                    _INDEX[name[:-8]] = os.path.join(directory, name)
    return _INDEX


def exec_program(exec_value: str) -> str:
    value = PREFIX_RE.sub("", exec_value or "")
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    while tokens and (tokens[0] == "env" or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])):
        tokens.pop(0)
    return tokens[0] if tokens else ""


def find_entry(window_class: str, exe: str):
    index = desktop_index()
    entries = {}

    def get(desktop_id):
        if desktop_id not in entries:
            entries[desktop_id] = parse_entry(index[desktop_id])
        return entries[desktop_id]

    lowered = {k.lower(): k for k in index}
    cls = (window_class or "").lower()
    exe_base = os.path.basename(exe).lower()
    exe_real = os.path.realpath(exe) if exe else ""

    if cls in lowered:
        return lowered[cls]
    for desktop_id in index:
        if cls and get(desktop_id).get("StartupWMClass", "").lower() == cls:
            return desktop_id
    if exe_base in lowered:
        return lowered[exe_base]
    for desktop_id in index:
        program = exec_program(get(desktop_id).get("Exec", ""))
        if not program:
            continue
        if os.path.basename(program).lower() == exe_base:
            return desktop_id
        resolved = shutil.which(program)
        if resolved and exe_real and os.path.realpath(resolved) == exe_real:
            return desktop_id
    return None


def override_state(desktop_id: str):
    """(scale, kind) from the user's override for `desktop_id`, or ("", None)."""
    path = os.path.join(user_apps_dir(), desktop_id + ".desktop")
    match = PREFIX_RE.match(parse_entry(path).get("Exec", "")) if os.path.exists(path) else None
    if not match:
        return "", None
    return normalize(match.group(1)) if valid_scale(match.group(1)) else "", match.group(2)


# ------------------------------------------------------------------ listing

def launcher_reads(launcher: str, flags_name: str) -> bool:
    try:
        if os.path.getsize(launcher) > LAUNCHER_MAX_BYTES:
            return False
        with open(launcher, "rb") as handle:
            return flags_name.encode() in handle.read()
    except OSError:
        return False


def app_record(app_id, name, method, kind, file, scale, xwayland, reason="", is_open=False) -> dict:
    return {
        "id": app_id,
        "name": name,
        "method": method,
        "kind": kind,
        "file": file,
        "scale": scale,
        "xwayland": xwayland,
        "supported": not reason,
        "reason": reason,
        "open": is_open,
        # True when an open window runs at another factor than the saved one.
        "stale": False,
    }


def flags_app(entry):
    app_id, name, launchers, flags_name, _ = entry
    launcher = next((p for p in (shutil.which(c) for c in launchers) if p), None)
    path = os.path.join(config_home(), flags_name)
    if not launcher:
        return None
    if not os.path.exists(path) and not launcher_reads(os.path.realpath(launcher), flags_name):
        return None
    tokens = " ".join(l for l in read_lines(path) if not l.lstrip().startswith("#")).split()
    return app_record(app_id, name, "flags", "chromium", tilde(path), flag_in(tokens),
                      "--ozone-platform=x11" in tokens)


def list_apps() -> list:
    apps = {}
    for entry in FLAGS_APPS:
        app = flags_app(entry)
        if app:
            apps[app["id"]] = app

    seen_pids = set()
    for client in windows():
        pid = int(client["pid"])
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        exe = proc_exe(pid)
        window_class = client.get("class") or ""
        xwayland = bool(client.get("xwayland"))

        flags_entry = next((e for e in FLAGS_APPS if any(exe.startswith(p) for p in e[4])), None)
        if flags_entry and flags_entry[0] in apps:
            app = apps[flags_entry[0]]
            app["open"] = True
            if flag_in(proc_argv(pid)[1:]) != app["scale"]:
                app["stale"] = True
            continue

        kind = detect_kind(pid, exe)
        desktop_id = find_entry(window_class, exe)
        if not desktop_id:
            key = "window:" + (window_class or os.path.basename(exe) or str(pid))
            label = window_class or os.path.basename(exe) or "Unknown app"
            apps.setdefault(key, app_record(key, label, "none", kind, "", "", xwayland,
                                            "no launcher entry to scale", True))
            continue

        app = apps.get(desktop_id)
        if not app:
            scale, set_kind = override_state(desktop_id)
            kind = set_kind or kind
            info = parse_entry(desktop_index()[desktop_id])
            reason = "" if kind in KINDS else UNSUPPORTED.get(kind, UNSUPPORTED[None])
            app = apps[desktop_id] = app_record(
                desktop_id, info.get("Name") or desktop_id, "desktop", kind,
                tilde(os.path.join(user_apps_dir(), desktop_id + ".desktop")),
                scale, xwayland, reason, True)
        app["open"] = True
        running = proc_env(pid, "OMAMONITOR_SCALE") or ""
        if running and valid_scale(running):
            running = normalize(running)
        if app["supported"] and running != app["scale"]:
            app["stale"] = True

    # Apps with a factor set that have no window open right now.
    try:
        names = os.listdir(user_apps_dir())
    except OSError:
        names = []
    for name in names:
        desktop_id = name[:-8]
        if not name.endswith(".desktop") or desktop_id in apps:
            continue
        scale, kind = override_state(desktop_id)
        if not scale or kind not in KINDS:
            continue
        path = os.path.join(user_apps_dir(), name)
        apps[desktop_id] = app_record(desktop_id, parse_entry(path).get("Name") or desktop_id,
                                      "desktop", kind, tilde(path), scale, False)

    return sorted(apps.values(), key=lambda a: (not a["supported"], a["name"].lower()))


# ------------------------------------------------------------------ setting

def set_flags(entry, scale: str) -> None:
    path = os.path.join(config_home(), entry[3])
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


def install_runner() -> None:
    """Put omamonitor-scale-run on PATH so desktop entries can call it, and
    so it keeps working if the plugin is removed while overrides remain."""
    with open(os.path.join(HERE, RUNNER), "r", encoding="utf-8") as handle:
        text = handle.read()
    target = os.path.join(home(), ".local", "bin", RUNNER)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                return
    except OSError:
        pass
    write_atomic(target, text, 0o755)
    os.chmod(os.path.realpath(target), 0o755)


def set_desktop(desktop_id: str, scale: str, kind: str) -> None:
    if not DESKTOP_ID_RE.match(desktop_id) or desktop_id.startswith("."):
        raise ValueError("bad app id: %r" % desktop_id)
    target = os.path.join(user_apps_dir(), desktop_id + ".desktop")
    if os.path.exists(target):
        lines = read_lines(target)
        created = any(l.strip() == MARK_CREATED + "=true" for l in lines)
    else:
        if scale == "auto":
            return
        source = desktop_index().get(desktop_id)
        if not source:
            raise ValueError("no desktop entry for %r" % desktop_id)
        lines = read_lines(source)
        created = True

    # An override this script made only to carry the factor goes away again.
    if scale == "auto" and created:
        os.unlink(os.path.realpath(target))
        return
    if scale != "auto":
        if kind not in KINDS:
            raise ValueError("cannot scale apps of kind %r" % kind)
        install_runner()

    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in (MARK_SCALE, MARK_CREATED):
            continue
        if key == "Exec" and "=" in line:
            value = PREFIX_RE.sub("", line.split("=", 1)[1].strip())
            if scale != "auto":
                value = "%s --scale=%s --kind=%s %s" % (RUNNER, scale, kind, value)
            line = "Exec=" + value
        out.append(line)
        if line.strip() == "[Desktop Entry]" and scale != "auto":
            out.append("%s=%s" % (MARK_SCALE, scale))
            if created:
                out.append(MARK_CREATED + "=true")
    write_atomic(target, "\n".join(out) + "\n")


def set_scale(app_id: str, scale: str, kind: str) -> None:
    if scale != "auto":
        if not valid_scale(scale):
            raise ValueError("bad scale: %r" % scale)
        scale = normalize(scale)
    entry = next((e for e in FLAGS_APPS if e[0] == app_id), None)
    if entry:
        set_flags(entry, scale)
        return
    if scale != "auto" and not kind:
        kind = override_state(app_id)[1] or ""
    set_desktop(app_id, scale, kind)


def main() -> int:
    args = sys.argv[1:]
    try:
        if args == ["list"]:
            print(json.dumps(list_apps()))
            return 0
        if len(args) in (3, 4) and args[0] == "set":
            set_scale(args[1], args[2], args[3] if len(args) == 4 else "")
            print("ok")
            return 0
    except (ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("usage: app-scale.py list | set APP_ID SCALE|auto [KIND]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
