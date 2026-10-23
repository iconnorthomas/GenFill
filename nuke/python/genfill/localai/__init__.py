"""GenFill Local AI: free, offline generation on the artist's own GPU.

Layout of the install folder (default ``%USERPROFILE%\\GenFill-LocalAI``,
deliberately outside OneDrive)::

    bin/uv.exe              tool that builds the private Python environment
    python/                 private Python (managed by uv)
    env/                    virtual environment with PyTorch + diffusers
    models/flux/...         FLUX.1 Fill [dev] GGUF + configs        (family "flux1-fill")
    models/t5, models/clip  its text encoders
    models/vae/ae.safetensors
    models/flux2-klein/     FLUX.2 [klein] 4B, diffusers layout     (family "flux2-klein")
    models/lama/big-lama.pt
    manifest.json           what's installed (written last, so it means "complete")
    logs/                   installer and helper logs

Nothing here imports torch: that only happens in the hidden helper process
(``worker.py``) so Nuke stays stable.
"""
from __future__ import annotations

import json
import os
import sys

from .. import config

MANIFEST = "manifest.json"
WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")


def default_root():
    return os.path.join(os.path.expanduser("~"), "GenFill-LocalAI")


def root():
    return (config.load().get("local_ai", {}) or {}).get("root") or default_root()


# How long the helper keeps the model loaded with nothing to do before it exits to
# free the GPU. 0 = until Nuke closes. Loading it again takes 30-60 s, so the
# default is generous; "Stop AI Helper" frees the GPU right away.
IDLE_MINUTES_DEFAULT = 30
IDLE_CHOICES = (10, 30, 60, 120, 0)


def idle_minutes():
    section = config.load().get("local_ai")
    value = section.get("idle_minutes", IDLE_MINUTES_DEFAULT) if isinstance(section, dict)         else IDLE_MINUTES_DEFAULT
    try:
        value = int(value)
    except (TypeError, ValueError):
        return IDLE_MINUTES_DEFAULT
    return value if value >= 0 else IDLE_MINUTES_DEFAULT


def set_idle_minutes(minutes):
    data = config.load()
    section = data.get("local_ai")
    if not isinstance(section, dict):
        section = data["local_ai"] = {}
    section["idle_minutes"] = int(minutes)
    config.save(data)


def idle_seconds():
    """The helper's --idle value; 0 means never exit while Nuke is open."""
    return idle_minutes() * 60


def env_python(base=None):
    base = base or root()
    if sys.platform.startswith("win"):
        return os.path.join(base, "env", "Scripts", "python.exe")
    return os.path.join(base, "env", "bin", "python")


def manifest(base=None):
    try:
        with open(os.path.join(base or root(), MANIFEST), "r") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (IOError, OSError, ValueError):
        return None


def family(base=None):
    """Which model the install holds; installs from before 0.8 are FLUX.1 Fill."""
    return (manifest(base) or {}).get("family") or "flux1-fill"


def is_installed(base=None):
    base = base or root()
    m = manifest(base)
    if not m:
        return False
    if not os.path.exists(env_python(base)):
        return False
    for key in ("unet", "vae", "lama"):
        if not m.get(key) or not os.path.exists(os.path.join(base, m[key])):
            return False
    return True


def log_dir(base=None):
    return os.path.join(base or root(), "logs")
