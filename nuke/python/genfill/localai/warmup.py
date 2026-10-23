"""Start the AI helper and load the model before the first Generate.

Starting the helper and loading FLUX takes 30-60 s. A GenerativeFill node set to
the Local AI is a good sign a Generate is coming, so creating one, opening its
panel or picking the Local AI starts that work on a background thread. A
Generate clicked meanwhile queues behind the load in the helper and then runs
on the warm model.

Nothing here may block Nuke's UI or raise into it.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_thread = [None]


def is_warming():
    t = _thread[0]
    return t is not None and t.is_alive()


def start(reason=""):
    """Warm up the Local AI in the background. Returns True if a warm-up started.

    Does nothing when Local AI isn't installed (or is being installed), a warm-up
    is already running, the helper is busy (loading or generating) or the model is
    already loaded."""
    try:
        from .. import localai
        from . import installer
        from .client import get_client
        if not localai.is_installed() or installer.is_running():
            return False
        with _lock:
            if is_warming():
                return False
            client = get_client()
            if client.is_busy() or client.is_loaded():
                return False
            t = threading.Thread(target=_run, args=(client, reason), name="GenFill-warmup",
                                 daemon=True)
            _thread[0] = t
            t.start()
        return True
    except Exception:
        _log("exception", "Local AI warm-up could not start")
        return False


def _run(client, reason):
    t0 = time.time()
    _log("info", "Local AI warm-up started (%s)", reason or "requested")
    try:
        client.request("load")
        _log("info", "Local AI warm-up done in %.1fs: model loaded", time.time() - t0)
    except Exception as exc:
        # A failed warm-up is harmless: the next Generate starts again and reports it.
        _log("warning", "Local AI warm-up stopped after %.1fs: %s", time.time() - t0, exc)


def _log(level, msg, *args):
    try:
        from .. import log as gflog
        getattr(gflog, level)(msg, *args)
    except Exception:
        pass
