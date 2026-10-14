"""Nuke's side of the hidden AI helper.

Starts ``worker.py`` with the private Python environment (no console window),
sends it JSON requests over stdin and reads events from stdout. One helper is
shared by every GenerativeFill node in the session. It is stopped when Nuke
exits (and the helper also exits by itself if Nuke disappears or after 10
idle minutes).
"""
from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time

from . import WORKER_SCRIPT, env_python, log_dir, root

IDLE_SECONDS = 600
HELLO_TIMEOUT = 90
CREATE_NO_WINDOW = 0x08000000

# Nuke's environment points Python at Nuke's own libraries; the helper's
# Python must not see these or it imports the wrong standard library.
_STRIP_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONNOUSERSITE",
              "PYTHONEXECUTABLE", "VIRTUAL_ENV", "CONDA_PREFIX")


# Variables set by Nuke (or other DCCs / CUDA toolkits) that can make the
# helper load the wrong DLLs or libraries.
_STRIP_PREFIXES = ("NUKE", "FN_", "FOUNDRY", "KMP_", "OMP_", "MKL_", "QT_", "CUDA_PATH", "CUDNN",
                   "TORCH_")


def _system_path():
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.pathsep.join([os.path.join(root, "System32"), root,
                            os.path.join(root, "System32", "Wbem")])


def clean_env(extra=None, platform=None):
    """Environment for the helper / installer tools.

    On Windows PATH is reduced to the Windows system folders: Nuke's install
    folder is on Nuke's PATH and contains its own CUDA, OpenMP and C++ runtime
    DLLs, which must not leak into the helper's PyTorch.
    """
    platform = platform or sys.platform
    env = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku in _STRIP_ENV or ku.startswith(_STRIP_PREFIXES):
            continue
        env[k] = v
    if platform.startswith("win"):
        for k in [k for k in env if k.upper() == "PATH"]:
            del env[k]
        env["PATH"] = _system_path()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["HF_HUB_OFFLINE"] = "1"          # never phone home from the helper
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["TOKENIZERS_PARALLELISM"] = "false"
    if not platform.startswith("win"):
        # Less fragmentation while T5 is loaded and unloaded. Not on Windows:
        # recent PyTorch builds implement it there with CUDA virtual memory, which
        # WDDM drivers can reject ("Failed to create GPU mapping", CUDA_ERROR_UNKNOWN
        # from cuMemSetAccess) on the first allocation; older builds ignored it.
        env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    if extra:
        env.update(extra)
    return env


def hidden_popen_kwargs():
    if sys.platform.startswith("win"):
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def _pid_file(base):
    return os.path.join(log_dir(base), "helper.pid")


def kill_tree(pid):
    """Kill a process and its children (the venv python.exe is a launcher that
    starts the real interpreter as a child)."""
    if not pid:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           timeout=15, **hidden_popen_kwargs())
        else:
            os.kill(int(pid), 9)
    except Exception:
        pass


def _is_python_process(pid):
    if not sys.platform.startswith("win"):
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15, **hidden_popen_kwargs())
        return "python" in out.stdout.lower()
    except Exception:
        return False


def kill_stale_helpers(base=None):
    """Stop a helper left behind by an earlier GenFill load (e.g. after Reload
    GenFill, or a crash of the Nuke-side code). Returns True if one was killed."""
    path = _pid_file(base or root())
    try:
        with open(path) as fh:
            pid = int(fh.read().strip() or 0)
    except (IOError, OSError, ValueError):
        return False
    killed = False
    if pid and pid != os.getpid() and _is_python_process(pid):
        kill_tree(pid)
        killed = True
    try:
        os.remove(path)
    except OSError:
        pass
    return killed


def helper_log_tail(base=None, lines=40):
    path = os.path.join(log_dir(base or root()), "helper.log")
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 16000))
            text = fh.read().decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])
    except (IOError, OSError):
        return ""


class WorkerError(RuntimeError):
    def __init__(self, message, oom=False, cancelled=False, crashed=False):
        super(WorkerError, self).__init__(message)
        self.oom = oom
        self.cancelled = cancelled
        self.crashed = crashed


class WorkerClient(object):
    def __init__(self, python=None, script=WORKER_SCRIPT, base=None, idle=IDLE_SECONDS,
                 fake=False):
        self.base = base or root()
        self.python = python or env_python(self.base)
        self.script = script
        self.idle = idle
        self.fake = fake
        self.proc = None
        self._send_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._pending = {}
        self._ids = itertools.count(1)
        self._hello = threading.Event()
        self._log_fh = None

    # -- lifecycle --------------------------------------------------------------
    def log_path(self):
        return os.path.join(log_dir(self.base), "helper.log")

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self._start_lock:
            if self.is_running():
                return
            if not os.path.exists(self.python):
                raise WorkerError("Local AI is not installed (missing %s). Use GenFill > Local AI > "
                                  "Install." % self.python)
            os.makedirs(log_dir(self.base), exist_ok=True)
            kill_stale_helpers(self.base)
            path = self.log_path()
            if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                os.replace(path, path + ".old")
            self._log_fh = open(path, "ab")
            cmd = [self.python, "-u", self.script, "--root", self.base, "--idle", str(self.idle)]
            if self.fake:
                cmd.append("--fake")
            self._hello.clear()
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=self._log_fh, cwd=self.base, env=clean_env(),
                                         **hidden_popen_kwargs())
            try:
                with open(_pid_file(self.base), "w") as fh:
                    fh.write(str(self.proc.pid))
            except (IOError, OSError):
                pass
            threading.Thread(target=self._read_loop, args=(self.proc,), name="GenFill-helper-reader",
                             daemon=True).start()
            if not self._hello.wait(HELLO_TIMEOUT):
                self.stop()
                raise WorkerError("The AI helper didn't start within %ss. See %s"
                                  % (HELLO_TIMEOUT, path), crashed=True)

    def stop(self, timeout=5.0):
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                self._send({"id": "shutdown", "cmd": "shutdown"})
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.proc = None
        try:
            os.remove(_pid_file(self.base))
        except OSError:
            pass
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    # -- io -----------------------------------------------------------------
    def _send(self, msg):
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with self._send_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _read_loop(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            ev = msg.get("event")
            if ev == "hello":
                self._hello.set()
                continue
            if ev == "status":     # helper-wide status: show it on every waiting request
                for q in list(self._pending.values()):
                    q.put({"event": "progress", "fraction": 0.0, "message": msg.get("message", "")})
                continue
            q = self._pending.get(msg.get("id"))
            if q is not None:
                q.put(msg)
        # stdout closed: helper exited. Wake everyone waiting.
        for q in list(self._pending.values()):
            q.put({"event": "exited"})

    def request(self, cmd, payload=None, progress=None, cancelled=None, timeout=None):
        """Send a command and wait for its final event. Returns the 'done' message."""
        self.start()
        rid = "r%d" % next(self._ids)
        q = queue.Queue()
        self._pending[rid] = q
        msg = {"id": rid, "cmd": cmd}
        msg.update(payload or {})
        cancel_sent = False
        deadline = time.time() + timeout if timeout else None
        try:
            try:
                self._send(msg)
            except (OSError, ValueError):
                raise WorkerError("The AI helper isn't responding.", crashed=True)
            while True:
                if cancelled and not cancel_sent and cancelled():
                    try:
                        self._send({"cmd": "cancel", "target": rid})
                    except (OSError, ValueError):
                        pass
                    cancel_sent = True
                if deadline and time.time() > deadline:
                    raise WorkerError("The AI helper timed out.")
                try:
                    ev = q.get(timeout=0.25)
                except queue.Empty:
                    if not self.is_running():
                        code = self.proc.poll() if self.proc else None
                        raise WorkerError("The AI helper stopped unexpectedly (exit code %s). "
                                          "Details: %s" % (code, self.log_path()), crashed=True)
                    continue
                kind = ev.get("event")
                if kind == "progress":
                    if progress:
                        progress(ev.get("fraction", 0.0), ev.get("message", ""))
                elif kind in ("done", "pong"):
                    return ev
                elif kind == "error":
                    raise WorkerError(ev.get("message", "Unknown error"), oom=ev.get("oom", False),
                                      cancelled=ev.get("cancelled", False))
                elif kind in ("exited", "bye"):
                    time.sleep(0.2)
                    code = self.proc.poll() if self.proc else None
                    raise WorkerError("The AI helper stopped unexpectedly (exit code %s). "
                                      "Details: %s" % (code, self.log_path()), crashed=True)
        finally:
            self._pending.pop(rid, None)


_client = [None]
_client_lock = threading.Lock()


def get_client():
    with _client_lock:
        if _client[0] is None or _client[0].base != root():
            if _client[0] is not None:
                _client[0].stop()
            _client[0] = WorkerClient()
        return _client[0]


def stop_helper():
    """Stop the helper now (frees GPU memory). It restarts on the next Generate."""
    with _client_lock:
        if _client[0] is not None:
            _client[0].stop()
    kill_stale_helpers()


atexit.register(stop_helper)
