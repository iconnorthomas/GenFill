"""Build GenFill Setup for the platform this runs on.

    python builds/build.py

Creates (or reuses) builds/.venv-<os> with the pinned tools in
builds/requirements.txt, then packages installers/genfill_setup with
PyInstaller. The finished installer replaces the previous one in
builds/windows/, builds/macos/ or builds/linux/. Run it once on each OS.

The GenFill plugin (nuke/) ships inside the app as plain files, not frozen
modules: the Local AI helper starts genfill/localai/worker.py from disk, and the
installer copies the same files into ~/.nuke. Standard-library modules the
plugin needs are found by scanning its imports and bundled explicitly.
"""
from __future__ import annotations

import ast
import datetime
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORK = os.path.join(HERE, ".work")
OS_NAME = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
VENV = os.path.join(HERE, ".venv-" + OS_NAME)
VENV_PY = os.path.join(VENV, "Scripts", "python.exe") if OS_NAME == "windows" \
    else os.path.join(VENV, "bin", "python")
# Imported by the plugin but never needed by the installer itself.
NOT_BUNDLED = {"nuke", "nukescripts", "PySide2", "torch", "diffusers", "transformers", "gguf",
               "numpy", "PIL", "safetensors", "accelerate", "tifffile", "OpenImageIO"}


def run(cmd, **kw):
    print("$ " + " ".join(cmd), flush=True)
    subprocess.check_call(cmd, **kw)


def ensure_venv():
    """Re-run this script inside the build venv, creating it the first time."""
    if os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(VENV)):
        return
    if not os.path.exists(VENV_PY):
        run([sys.executable, "-m", "venv", VENV])
        run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"])
    run([VENV_PY, "-m", "pip", "install", "-r", os.path.join(HERE, "requirements.txt")])
    sys.exit(subprocess.call([VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))


def rmtree(path):
    """shutil.rmtree that also removes read-only files and folders (Windows)."""
    def clear(func, p, _exc):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(p)
    shutil.rmtree(path, onerror=clear)


def git(*args):
    try:
        return subprocess.check_output(["git", "-C", REPO] + list(args), text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def stage_plugin():
    """Copy nuke/ to .work/stage/nuke and stamp it with this build's info."""
    stage = os.path.join(WORK, "stage", "nuke")
    if os.path.exists(stage):
        rmtree(stage)
    shutil.copytree(os.path.join(REPO, "nuke"), stage,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build_info.json"))
    with open(os.path.join(stage, "python", "genfill", "__init__.py"), encoding="utf-8") as fh:
        version = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read()).group(1)
    info = {"version": version, "commit": git("rev-parse", "--short", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--", "nuke", "installers")),
            "built": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "mode": "installer"}
    with open(os.path.join(stage, "python", "genfill", "build_info.json"), "w") as fh:
        json.dump(info, fh, indent=4)
    return stage, info


def plugin_imports(stage):
    """Top-level non-GenFill modules imported anywhere in the plugin."""
    names = set()
    for dirpath, _dirs, files in os.walk(os.path.join(stage, "python")):
        for f in files:
            if not f.endswith(".py"):
                continue
            with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names.add(node.module)
                    # "from urllib import request" needs urllib.request itself.
                    names.update("%s.%s" % (node.module, a.name) for a in node.names)
    keep = set()
    for name in names:
        top = name.split(".")[0]
        if top == "genfill" or top in NOT_BUNDLED:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                keep.add(name)
        except (ImportError, ValueError, AttributeError):
            pass       # a name imported from a module, not a submodule
    return sorted(keep)


def render_icon():
    """256 px app icon from the same artwork the window uses."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, os.path.join(REPO, "installers"))
    from genfill_setup import app
    qapp = app.QtWidgets.QApplication.instance() or app.QtWidgets.QApplication([])
    path = os.path.join(WORK, "icon.png")
    pm = app.logo_pixmap(256)
    pm.setDevicePixelRatio(1)
    pm.scaled(256, 256, app.Qt.KeepAspectRatio, app.Qt.SmoothTransformation).save(path)
    del qapp
    return path


def arch():
    m = platform.machine().lower()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(m, m)


def main():
    ensure_venv()
    os.makedirs(WORK, exist_ok=True)
    stage, info = stage_plugin()
    suffix = OS_NAME if OS_NAME == "windows" else "%s-%s" % (OS_NAME, arch())
    name = "GenFill-Setup-%s-%s" % (info["version"], suffix)
    print("Building %s (commit %s%s)" % (name, info["commit"] or "?",
                                        ", uncommitted changes" if info["dirty"] else ""))
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", name, "--icon", render_icon(),
           "--distpath", os.path.join(WORK, "dist"), "--workpath", os.path.join(WORK, "build"),
           "--specpath", WORK, "--paths", os.path.join(REPO, "installers"),
           "--add-data", "%s%snuke" % (stage, os.pathsep),
           "--exclude-module", "genfill", "--exclude-module", "tkinter"]
    for mod in plugin_imports(stage) + ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"]:
        cmd += ["--hidden-import", mod]
    if OS_NAME == "macos":
        cmd += ["--osx-bundle-identifier", "com.github.iconnorthomas.genfill.setup"]
    else:
        cmd += ["--onefile"]
    cmd.append(os.path.join(REPO, "installers", "genfill_setup", "__main__.py"))
    run(cmd)

    out_dir = os.path.join(HERE, OS_NAME)
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.startswith("GenFill-Setup-"):
            os.remove(os.path.join(out_dir, old))
    dist = os.path.join(WORK, "dist")
    if OS_NAME == "windows":
        final = os.path.join(out_dir, name + ".exe")
        shutil.copy2(os.path.join(dist, name + ".exe"), final)
    elif OS_NAME == "macos":
        final = os.path.join(out_dir, name + ".zip")
        run(["ditto", "-c", "-k", "--keepParent", os.path.join(dist, name + ".app"), final])
    else:
        final = os.path.join(out_dir, name)
        shutil.copy2(os.path.join(dist, name), final)
        os.chmod(final, 0o755)
    size = os.path.getsize(final) / 1e6
    print("\nBuilt %s (%.1f MB)" % (os.path.relpath(final, REPO), size))
    if size > 95:
        print("WARNING: GitHub rejects files over 100 MB; this one can't be committed.")
    elif size > 50:
        print("Note: GitHub warns about files over 50 MB.")


if __name__ == "__main__":
    main()
