"""GenFill: Photoshop-style Generative Fill inside Nuke.

Modules that import ``nuke`` (node, engine, ui, devtools) are imported lazily
so the pure-Python parts (region math, PNG IO, state, backends) can be unit
tested outside Nuke.
"""
import json
import os

__version__ = "0.9.2"

_HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_INFO_PATH = os.path.join(_HERE, "build_info.json")


def build_info():
    """Describe the loaded build.

    The installer writes ``build_info.json`` next to this file when it copies
    a build into ~/.nuke. Without it we're running live from the repository.
    """
    info = {"version": __version__, "commit": "", "dirty": False, "built": "",
            "mode": "dev", "source": "", "location": _HERE}
    try:
        with open(BUILD_INFO_PATH, "r") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            info.update(data)
            info["location"] = _HERE
    except (IOError, OSError, ValueError):
        pass
    return info


def build_label():
    """Short one-line description, e.g. 'GenFill 0.2.0 (a1b2c3d*, 2026-10-12 20:41)'."""
    b = build_info()
    if b.get("mode") == "dev" and not b.get("commit"):
        return "GenFill %s (live from repo)" % b["version"]
    commit = (b.get("commit") or "no-git") + ("*" if b.get("dirty") else "")
    return "GenFill %s (%s, %s)" % (b["version"], commit, b.get("built") or "?")


def repo_root():
    """Repository folder this build came from, if known (used for test output)."""
    b = build_info()
    if b.get("source") and os.path.isdir(b["source"]):
        return b["source"]
    # Running live: <repo>/nuke/python/genfill
    candidate = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
    if os.path.isdir(os.path.join(candidate, "installers")):
        return candidate
    return None
