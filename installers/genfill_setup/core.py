"""Install / uninstall logic for GenFill Setup (no Qt, so it can be unit tested).

Writes exactly what installers/install.ps1 writes: a copy of the repo's nuke/
folder in ~/.nuke/plugins/GenFill with a build_info.json stamp, plus a marked
block in ~/.nuke/init.py. The markers are shared, so the two installers can be
mixed freely.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections import namedtuple

BEGIN_MARK = ("# >>> GenFill (managed by GenFill installers\\install.ps1 - "
              "edits here are overwritten) >>>")
END_MARK = "# <<< GenFill <<<"
_BLOCK_RE = re.compile(re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK) + r"[ \t]*(\r?\n)?",
                       re.S)

NukeInstall = namedtuple("NukeInstall", "name version exe launch")


class SetupError(Exception):
    pass


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

def nuke_home():
    return os.path.join(os.path.expanduser("~"), ".nuke")


def init_py_path():
    return os.path.join(nuke_home(), "init.py")


def default_target():
    return os.path.join(nuke_home(), "plugins", "GenFill")


def config_dir():
    return os.path.join(nuke_home(), "genfill")


def bundled_source():
    """The nuke/ folder shipped inside the app (PyInstaller) or the repo's own."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "nuke")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "nuke")


def use_bundled_genfill(source=None):
    """Make the bundled genfill package importable (theme, Local AI installer)."""
    path = os.path.join(source or bundled_source(), "python")
    if path not in sys.path:
        sys.path.insert(0, path)


def read_version(source):
    try:
        with open(os.path.join(source, "python", "genfill", "__init__.py"), encoding="utf-8") as fh:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read())
        return m.group(1) if m else "?"
    except (IOError, OSError):
        return "?"


def read_build_info(plugin_dir):
    """build_info.json of an installed (or bundled) build, or None."""
    try:
        with open(os.path.join(plugin_dir, "python", "genfill", "build_info.json"),
                  encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return None


def installed_version(target=None):
    info = read_build_info(target or default_target())
    if info:
        return info.get("version") or "?"
    if os.path.isdir(os.path.join(target or default_target(), "python", "genfill")):
        return "?"
    return None


# --------------------------------------------------------------------------
# finding Nuke
# --------------------------------------------------------------------------

def _version_of(name):
    m = re.search(r"Nuke(\d+)\.(\d+)v(\d+)", name)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _search_roots(platform):
    if platform.startswith("win"):
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 r"C:\Program Files"]
    elif platform == "darwin":
        roots = ["/Applications", os.path.expanduser("~/Applications")]
    else:
        roots = ["/usr/local", "/opt", os.path.expanduser("~")]
    seen, out = set(), []
    for r in roots:
        if r and os.path.normcase(r) not in seen:
            seen.add(os.path.normcase(r))
            out.append(r)
    return out


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def find_nuke_installs(platform=None, roots=None):
    """Nuke installs found in the usual places, newest first."""
    platform = platform or sys.platform
    found = {}
    for root in roots or _search_roots(platform):
        for d in _listdir(root):
            folder = os.path.join(root, d)
            if not d.startswith("Nuke") or not os.path.isdir(folder):
                continue
            if platform.startswith("win"):
                for f in _listdir(folder):
                    if re.match(r"^Nuke\d+\.\d+\.exe$", f, re.I):
                        exe = os.path.join(folder, f)
                        found[exe] = NukeInstall(d, _version_of(d), exe, exe)
                        break
            elif platform == "darwin":
                for app in _listdir(folder):
                    if not (app.startswith("Nuke") and app.endswith(".app")):
                        continue
                    macos = os.path.join(folder, app, "Contents", "MacOS")
                    exe = next((os.path.join(macos, f) for f in _listdir(macos)
                                if re.match(r"^Nuke\d+\.\d+(v\d+)?$", f)), None)
                    if exe:
                        found[exe] = NukeInstall(d, _version_of(d), exe, os.path.join(folder, app))
                        break
            else:
                for f in _listdir(folder):
                    exe = os.path.join(folder, f)
                    if re.match(r"^Nuke\d+\.\d+$", f) and os.path.isfile(exe):
                        found[exe] = NukeInstall(d, _version_of(d), exe, exe)
                        break
    return sorted(found.values(), key=lambda n: n.version, reverse=True)


def launch_nuke(install, platform=None):
    platform = platform or sys.platform
    if platform == "darwin":
        subprocess.Popen(["open", "-a", install.launch])
    else:
        subprocess.Popen([install.launch], cwd=os.path.dirname(install.launch))


def nuke_is_running(platform=None):
    platform = platform or sys.platform
    try:
        if platform.startswith("win"):
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return bool(re.search(r'^"Nuke\d', out.stdout, re.M | re.I))
        out = subprocess.run(["pgrep", "-f", r"Nuke[0-9]+\.[0-9]+"], capture_output=True, timeout=10)
        return out.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# init.py
# --------------------------------------------------------------------------

def _newline():
    return "\r\n" if sys.platform.startswith("win") else "\n"


def _read(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return fh.read()
    except (IOError, OSError):
        return ""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def remove_init_block(text):
    return _BLOCK_RE.sub("", text)


def backup_init_py(path=None):
    path = path or init_py_path()
    if not os.path.exists(path):
        return None
    backup = path + ".genfill-backup"
    shutil.copy2(path, backup)
    return backup


def set_init_block(plugin_path, path=None):
    """Point Nuke at ``plugin_path``. Returns {init_py, backup, disabled}."""
    path = path or init_py_path()
    nl = _newline()
    backup = backup_init_py(path)
    lines = remove_init_block(_read(path)).splitlines()
    disabled = []
    # Comment out hand-written GenFill pluginAddPath lines so it doesn't load twice.
    for i, line in enumerate(lines):
        if "pluginAddPath" in line and "GenFill" in line and not line.lstrip().startswith("#"):
            disabled.append(line.strip())
            lines[i] = "# (disabled by GenFill installer) " + line
    body = nl.join(lines).rstrip()
    plugin_path = plugin_path.rstrip("\\/")
    arg = 'r"%s"' % plugin_path if '"' not in plugin_path else repr(plugin_path)
    block = nl.join([BEGIN_MARK, "import nuke", "nuke.pluginAddPath(%s)" % arg, END_MARK])
    _write(path, (body + nl + nl if body else "") + block + nl)
    return {"init_py": path, "backup": backup, "disabled": disabled}


def clear_init_block(path=None):
    """Remove GenFill's block. Returns the backup path, or None if there was no block."""
    path = path or init_py_path()
    text = _read(path)
    if BEGIN_MARK not in text:
        return None
    backup = backup_init_py(path)
    _write(path, remove_init_block(text).rstrip() + _newline())
    return backup


# --------------------------------------------------------------------------
# install / uninstall
# --------------------------------------------------------------------------

def _clear_readonly(func, path, _exc_info):
    """rmtree error handler: Windows won't delete read-only files or folders."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    func(path)


def rmtree(path):
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly)
    else:
        shutil.rmtree(path, onerror=_clear_readonly)


def assert_safe_target(target, source=None):
    """The install folder is deleted and replaced on every install, so be strict."""
    target = os.path.abspath(target)
    if "genfill" not in os.path.basename(target).lower():
        raise SetupError("The install folder's name must contain 'GenFill' (%s). It is deleted "
                         "and replaced on every install." % target)
    if source and os.path.normcase(target) == os.path.normcase(os.path.abspath(source)):
        raise SetupError("The install folder can't be the folder GenFill is installed from.")
    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    if os.path.normcase(target) == home or len(target) < 8:
        raise SetupError("Refusing to use %s as the install folder." % target)


def install_plugin(target, source=None, log=None):
    """Copy the plugin to ``target`` and stamp it. Returns the build info written."""
    log = log or (lambda line: None)
    source = source or bundled_source()
    assert_safe_target(target, source)
    if not os.path.isdir(os.path.join(source, "python", "genfill")):
        raise SetupError("The GenFill files are missing from this installer (%s)." % source)
    if os.path.exists(target):
        try:
            rmtree(target)
        except OSError as exc:
            raise SetupError("Couldn't replace the previous GenFill in %s (%s). Close Nuke and "
                             "try again." % (target, exc))
        log("removed the previous build in %s" % target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bundled = read_build_info(source) or {}
    info = {
        "version": bundled.get("version") or read_version(source),
        "commit": bundled.get("commit", ""),
        "dirty": bool(bundled.get("dirty", False)),
        "built": bundled.get("built") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "mode": "copy",
        "source": "GenFill Setup",
    }
    _write(os.path.join(target, "python", "genfill", "build_info.json"), json.dumps(info, indent=4))
    log("copied GenFill %s to %s" % (info["version"], target))
    return info


def uninstall(target=None, remove_ai_root=None, remove_config=False, log=None):
    log = log or (lambda line: None)
    target = target or default_target()
    backup = clear_init_block()
    log("removed the GenFill block from %s" % init_py_path() if backup else "no GenFill block in init.py")
    assert_safe_target(target)
    if os.path.exists(target):
        try:
            rmtree(target)
        except OSError as exc:
            raise SetupError("Couldn't delete %s (%s). Close Nuke and try again." % (target, exc))
        log("deleted %s" % target)
    if remove_ai_root:
        assert_safe_target(remove_ai_root)
        if os.path.exists(remove_ai_root):
            rmtree(remove_ai_root)
            log("deleted the local AI in %s" % remove_ai_root)
    if remove_config and os.path.isdir(config_dir()):
        rmtree(config_dir())
        log("deleted settings and API keys in %s" % config_dir())
