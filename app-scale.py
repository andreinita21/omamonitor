#!/usr/bin/env python3
"""Per-app scaling for whatever is open on the desktop.

Usage: app-scale.py list
       app-scale.py set APP_ID SCALE|auto [KIND]
       app-scale.py relaunch APP_ID

Hyprland scales whole monitors; it has no per-window scale. Each app is
scaled through its own toolkit instead. Most toolkits only read the factor
at startup, so `relaunch` closes the app and starts it again; open foot
windows are resized on the spot instead (see apply_live_foot):

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
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

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
# Terminals are never relaunched: that would end whatever runs inside them.
NO_RELAUNCH = ("alacritty", "kitty", "ghostty")


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
        "relaunchable": not reason and method != "none" and kind not in TERMINALS,
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


def scan():
    """(apps by id, {app id: [(window address, pid), ...]})."""
    apps = {}
    owned = {}
    for entry in FLAGS_APPS:
        app = flags_app(entry)
        if app:
            apps[app["id"]] = app

    live = live_scales()
    seen_pids = {}
    for client in windows():
        pid = int(client["pid"])
        if pid in seen_pids:
            if seen_pids[pid]:
                owned.setdefault(seen_pids[pid], []).append((client.get("address"), pid))
            continue
        seen_pids[pid] = None
        exe = proc_exe(pid)
        window_class = client.get("class") or ""
        xwayland = bool(client.get("xwayland"))

        flags_entry = next((e for e in FLAGS_APPS if any(exe.startswith(p) for p in e[4])), None)
        if flags_entry and flags_entry[0] in apps:
            app = apps[flags_entry[0]]
            seen_pids[pid] = app["id"]
            owned.setdefault(app["id"], []).append((client.get("address"), pid))
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
        seen_pids[pid] = app["id"]
        owned.setdefault(app["id"], []).append((client.get("address"), pid))
        running = live.get(str(pid), proc_env(pid, "OMAMONITOR_SCALE") or "")
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

    return apps, owned


def list_apps() -> list:
    apps = scan()[0]
    return sorted(apps.values(), key=lambda a: (not a["supported"], a["name"].lower()))


# ------------------------------------------------------------------- live

def live_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(runtime, "omamonitor-live.json")


def live_scales() -> dict:
    """pid -> factor for windows resized live, dropping exited processes."""
    try:
        with open(live_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {pid: v for pid, v in data.items() if os.path.exists("/proc/" + pid)}


def save_live(data: dict) -> None:
    write_atomic(live_path(), json.dumps(data), 0o600)


def hypr(lua: str) -> str:
    out = subprocess.run(["hyprctl", "dispatch", lua], capture_output=True, text=True, timeout=3)
    return (out.stdout + out.stderr).strip()


def press(address: str, mods: str, key: str) -> None:
    # Down and up as two events: a single send_shortcut can leave the key stuck.
    for state in ("down", "up"):
        hypr('hl.dsp.send_key_state({ mods = "%s", key = "%s", state = "%s", window = "address:%s" })'
             % (mods, key, state, address))
        time.sleep(0.02)


def foot_font_px() -> tuple:
    """(base font size in px, zoom step) from foot.ini. The step is ("px", n)
    or ("pct", n); foot's default is 0.5 pt, and foot converts points to
    pixels at 96 dpi before the monitor scale is applied."""
    base, step = None, ("px", 0.5 * 96 / 72)
    section = "main"
    for line in read_lines(os.path.join(config_home(), "foot", "foot.ini")):
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.strip("[]")
            continue
        if section != "main" or "=" not in stripped or stripped.startswith("#"):
            continue
        key, value = [x.strip() for x in stripped.split("=", 1)]
        if key == "font" and base is None:
            match = re.search(r"pixelsize=([\d.]+)", value)
            if match:
                base = float(match.group(1))
            else:
                match = re.search(r"(?<!pixel)size=([\d.]+)", value)
                base = float(match.group(1)) * 96 / 72 if match else None
        elif key == "font-size-adjustment":
            match = re.match(r"^([\d.]+)\s*(px|%)?$", value)
            if match:
                n = float(match.group(1))
                unit = match.group(2)
                step = ("pct", n) if unit == "%" else ("px", n if unit == "px" else n * 96 / 72)
    return (base or 8 * 96 / 72), step


def apply_live_foot(app_id: str, scale: str) -> None:
    """Resize open foot windows with foot's own zoom keys: reset to the size
    the window started with, then zoom in or out to the target. Zoom moves in
    fixed steps, so the result is the closest step to the factor."""
    target = 1.0 if scale == "auto" else float(scale)
    base, (unit, step) = foot_font_px()
    live = live_scales()
    for address, pid in scan()[1].get(app_id, []):
        if not address:
            continue
        started = proc_env(pid, "OMAMONITOR_SCALE")
        start = float(started) if started and valid_scale(started) else 1.0
        if unit == "pct":
            steps = round(math.log(target / start) / math.log(1 + step / 100))
        else:
            steps = round(base * (target - start) / step)
        press(address, "CTRL", "0")
        for _ in range(abs(steps)):
            press(address, "CTRL", "equal" if steps > 0 else "minus")
        live[str(pid)] = "" if scale == "auto" else scale
    save_live(live)


def wait_gone(pids, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not any(os.path.exists("/proc/%d" % p) for p in pids):
            return True
        time.sleep(0.2)
    return not any(os.path.exists("/proc/%d" % p) for p in pids)


def launch(argv: list) -> None:
    subprocess.Popen(["uwsm-app", "--"] + argv, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def relaunch(app_id: str) -> None:
    apps, owned = scan()
    app = apps.get(app_id)
    if not app:
        raise ValueError("%s is not open" % app_id)
    if not app["relaunchable"]:
        raise ValueError("%s cannot be relaunched from here" % app["name"])
    windows_ = owned.get(app_id, [])
    pids = sorted({pid for _, pid in windows_})
    if not pids:
        raise ValueError("%s has no open window" % app["name"])

    if app["method"] == "flags":
        # A browser asked to quit saves its session; closing windows one by
        # one would leave only the last window to restore.
        for pid in pids:
            os.kill(pid, 15)
        if not wait_gone(pids, 15):
            raise ValueError("%s did not quit" % app["name"])
        entry = next(e for e in FLAGS_APPS if e[0] == app_id)
        launcher = next((c for c in entry[2] if shutil.which(c)), entry[2][0])
        launch([launcher, "--restore-last-session"])
        return

    # Close the windows like the user would, one at a time, so the app can
    # save its state. A close sent while another of the app's windows is up
    # can be ignored, so each window is asked twice before giving up.
    asked = {}
    deadline = time.time() + 8
    while time.time() < deadline:
        if wait_gone(pids, 0):
            break
        left = [c for c in windows() if int(c["pid"]) in pids]
        if not left:
            # No window but still running: it lives on in the tray, and
            # terminating it is what quitting from the tray would do.
            for pid in pids:
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass
            if not wait_gone(pids, 5):
                raise ValueError("%s did not quit; close it and open it again" % app["name"])
            break
        fresh = [c for c in left if asked.get(c.get("address"), 0) < 2]
        if fresh:
            target = sorted(fresh, key=lambda c: (asked.get(c.get("address"), 0), not c.get("title")))[0]
            asked[target.get("address")] = asked.get(target.get("address"), 0) + 1
            hypr('hl.dsp.window.close({ window = "address:%s" })' % target.get("address"))
        time.sleep(0.8)
    else:
        # A window that stays up after being closed is asking something
        # (unsaved work): never kill it.
        raise ValueError("%s is still open, maybe asking to save; answer it, then relaunch again" % app["name"])
    launch(["gtk-launch", app_id + ".desktop"])


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
    kind = kind or override_state(app_id)[1] or ""
    set_desktop(app_id, scale, kind)
    # foot has zoom keys, so open windows follow at once; other apps relaunch.
    if kind == "foot":
        apply_live_foot(app_id, scale)


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
        if len(args) == 2 and args[0] == "relaunch":
            relaunch(args[1])
            print("ok")
            return 0
    except (ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("usage: app-scale.py list | set APP_ID SCALE|auto [KIND] | relaunch APP_ID", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
