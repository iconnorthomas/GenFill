"""Installs GenFill Local AI from inside Nuke (no command windows).

Runs on a background thread and reports progress through ``on_event``:
    on_event("step", index=i, state="running"|"done"|"failed"|"skipped", label=...)
    on_event("progress", fraction=0..1 or None, text="...")
    on_event("log", line="...")
    on_event("finished", ok=bool, message="...")

Steps (each is safe to re-run; finished work is skipped, downloads resume):
    1. Check GPU and disk, remove leftovers of the old ComfyUI setup
    2. Get uv (a small tool that builds the private Python)
    3. Create the private Python environment
    4. Install PyTorch (CUDA)
    5. Install diffusers / transformers / GGUF support
    6. Download the models (all public, no account needed)
    7. Write model configs and the manifest
    8. Self-test: start the helper, run a tiny removal + fill, report speed
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from . import MANIFEST, default_root, env_python, log_dir
from .client import WorkerClient, WorkerError, clean_env, hidden_popen_kwargs
from .. import config

try:
    from urllib import request as _urlrequest
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover
    raise

HF = "https://huggingface.co"
UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
TORCH_INDEX_BASE = "https://download.pytorch.org/whl/"
# Newest first. Only the PyTorch CUDA index is used for torch: PyPI's Windows
# torch wheels are CPU-only, and uv prefers --extra-index-url over --index-url.
TORCH_CUDA_TAGS = ["cu130", "cu128", "cu126"]
RUNTIME_PREFIXES = ("msvcp140", "vcruntime140", "concrt140", "vcomp140", "vccorlib140")
PYTHON_VERSION = "3.12"
LIBRARIES = ["diffusers>=0.34", "transformers>=4.48", "accelerate>=1.2", "gguf>=0.10.0",
             "sentencepiece", "protobuf", "safetensors", "pillow", "numpy", "tifffile"]

QUANTS = {
    # id: (label, file size GB, minimum VRAM GB it's meant for)
    "Q4_K_S": ("Standard: fits a 12 GB GPU alongside Nuke (6.8 GB model)", 6.8, 8),
    "Q6_K": ("High: near-full quality, for 16 GB+ GPUs (9.9 GB model)", 9.9, 14),
    "Q8_0": ("Maximum: full quality, for 24 GB GPUs (12.7 GB model)", 12.7, 20),
}


def recommended_quant(vram_gb):
    if not vram_gb or vram_gb < 14:
        return "Q4_K_S"
    if vram_gb < 20:
        return "Q6_K"
    return "Q8_0"


def model_files(quant):
    """(relative path, url, approx GB) for every model file."""
    t5 = HF + "/city96/t5-v1_1-xxl-encoder-bf16/resolve/main/"
    clip = HF + "/openai/clip-vit-large-patch14/resolve/main/"
    files = [
        ("models/flux/flux1-fill-dev-%s.gguf" % quant,
         HF + "/YarvixPA/FLUX.1-Fill-dev-gguf/resolve/main/flux1-fill-dev-%s.gguf" % quant,
         QUANTS[quant][1]),
        ("models/t5/model.safetensors", t5 + "model.safetensors", 9.5),
        ("models/clip/model.safetensors", clip + "model.safetensors", 1.7),
        ("models/vae/ae.safetensors",
         HF + "/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors", 0.33),
        ("models/lama/big-lama.pt",
         "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt", 0.2),
    ]
    for name in ("config.json", "special_tokens_map.json", "spiece.model", "tokenizer_config.json"):
        files.append(("models/t5/" + name, t5 + name, 0.0))
    for name in ("config.json", "merges.txt", "vocab.json", "tokenizer.json", "tokenizer_config.json",
                 "special_tokens_map.json"):
        files.append(("models/clip/" + name, clip + name, 0.0))
    return files


# Diffusers configs for FLUX.1 Fill [dev]. Shipped here because the official
# repositories require a Hugging Face login; values match the public configs.
FLUX_CONFIGS = {
    "transformer/config.json": {
        "_class_name": "FluxTransformer2DModel", "attention_head_dim": 128,
        "axes_dims_rope": [16, 56, 56], "guidance_embeds": True, "in_channels": 384,
        "joint_attention_dim": 4096, "num_attention_heads": 24, "num_layers": 19,
        "num_single_layers": 38, "out_channels": 64, "patch_size": 1,
        "pooled_projection_dim": 768,
    },
    "vae/config.json": {
        "_class_name": "AutoencoderKL", "act_fn": "silu",
        "block_out_channels": [128, 256, 512, 512],
        "down_block_types": ["DownEncoderBlock2D"] * 4, "force_upcast": True, "in_channels": 3,
        "latent_channels": 16, "latents_mean": None, "latents_std": None, "layers_per_block": 2,
        "mid_block_add_attention": True, "norm_num_groups": 32, "out_channels": 3,
        "sample_size": 1024, "scaling_factor": 0.3611, "shift_factor": 0.1159,
        "up_block_types": ["UpDecoderBlock2D"] * 4, "use_post_quant_conv": False,
        "use_quant_conv": False,
    },
    "scheduler/scheduler_config.json": {
        "_class_name": "FlowMatchEulerDiscreteScheduler", "base_image_seq_len": 256,
        "base_shift": 0.5, "max_image_seq_len": 4096, "max_shift": 1.15,
        "num_train_timesteps": 1000, "shift": 3.0, "use_dynamic_shifting": True,
    },
}


def _sleep(seconds):
    """Backoff between download retries (patched out in tests)."""
    time.sleep(seconds)


class InstallCancelled(Exception):
    pass


class InstallError(Exception):
    pass


def detect_gpu():
    """(name, vram_gb) from nvidia-smi, or (None, None)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=15, **hidden_popen_kwargs())
        line = out.stdout.strip().splitlines()[0]
        name, mem = [p.strip() for p in line.split(",")[:2]]
        return name, round(int(mem) / 1024.0, 1)
    except Exception:
        return None, None


def file_version(path):
    """Windows file version string (e.g. '14.44.35211.0') or None."""
    if not path or not os.path.exists(path) or not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None
        info = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        ms, ls = info[2], info[3]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return None


SHARD_BYTES = 1024 ** 3   # 1 GB pieces


def reshard_safetensors(src, out_dir, base_name="model", shard_bytes=SHARD_BYTES, log=None):
    """Split a big .safetensors file into ~1 GB shards plus an index, streaming.

    Why: safetensors maps a whole file copy-on-write, and Windows reserves
    commit (RAM + page file) for the entire mapping. Mapping the 9.5 GB T5 file
    failed with "paging file is too small" (os error 1455) on a 32 GB machine
    running Nuke. Shards are opened one at a time, so the peak is ~1 GB.
    Pure Python, constant memory. Returns the list of shard paths.
    """
    import struct
    with open(src, "rb") as fh:
        (hlen,) = struct.unpack("<Q", fh.read(8))
        header = json.loads(fh.read(hlen).decode("utf-8"))
        data_start = 8 + hlen
        meta = header.pop("__metadata__", None) or {"format": "pt"}
        tensors = sorted(header.items(), key=lambda kv: kv[1]["data_offsets"][0])
        groups, cur, cur_size = [], [], 0
        for name, info in tensors:
            size = info["data_offsets"][1] - info["data_offsets"][0]
            if cur and cur_size + size > shard_bytes:
                groups.append(cur)
                cur, cur_size = [], 0
            cur.append((name, info))
            cur_size += size
        if cur:
            groups.append(cur)
        n = len(groups)
        weight_map, paths, total = {}, [], 0
        for i, group in enumerate(groups):
            fname = "%s-%05d-of-%05d.safetensors" % (base_name, i + 1, n)
            new_header, offset = {"__metadata__": meta}, 0
            for name, info in group:
                size = info["data_offsets"][1] - info["data_offsets"][0]
                new_header[name] = {"dtype": info["dtype"], "shape": info["shape"],
                                    "data_offsets": [offset, offset + size]}
                offset += size
                weight_map[name] = fname
            hbytes = json.dumps(new_header, separators=(",", ":")).encode("utf-8")
            hbytes += b" " * ((8 - len(hbytes) % 8) % 8)
            path = os.path.join(out_dir, fname)
            with open(path + ".part", "wb") as out:
                out.write(struct.pack("<Q", len(hbytes)))
                out.write(hbytes)
                for name, info in group:
                    start, end = info["data_offsets"]
                    fh.seek(data_start + start)
                    remaining = end - start
                    while remaining:
                        chunk = fh.read(min(remaining, 8 * 1024 * 1024))
                        if not chunk:
                            raise IOError("unexpected end of %s" % src)
                        out.write(chunk)
                        remaining -= len(chunk)
            os.replace(path + ".part", path)
            paths.append(path)
            total += offset
            if log:
                log("wrote %s (%.2f GB)" % (fname, offset / 1e9))
    with open(os.path.join(out_dir, base_name + ".safetensors.index.json"), "w") as fh:
        json.dump({"metadata": {"total_size": total}, "weight_map": weight_map}, fh, indent=1)
    return paths


def version_tuple(text):
    if not text:
        return None
    try:
        return tuple(int(p) for p in text.split("."))
    except ValueError:
        return None


def estimate_gb(quant):
    return sum(f[2] for f in model_files(quant)) + 7.0   # + PyTorch environment


class Installer(object):
    STEPS = [
        ("check", "Check GPU and disk space"),
        ("uv", "Get the Python environment tool (uv)"),
        ("env", "Create the private Python environment"),
        ("torch", "Install PyTorch for your NVIDIA GPU (~3 GB)"),
        ("libs", "Install diffusers and model support"),
        ("runtime", "Finish the environment (C++ runtime check, precompile)"),
        ("models", "Download FLUX.1 Fill, text encoders, VAE and LaMa"),
        ("configure", "Write model configs"),
        ("selftest", "Self-test on your GPU"),
    ]

    def __init__(self, base=None, quant=None, on_event=None, platform=None):
        self.base = base or default_root()
        self.quant = quant
        self.on_event = on_event or (lambda kind, **kw: None)
        self.platform = platform or sys.platform
        self._cancel = threading.Event()
        self._proc = None
        self.gpu = (None, None)
        self.result = {}
        self.uv = None
        self._log_fh = None

    # -- plumbing -----------------------------------------------------------
    def cancel(self):
        self._cancel.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _check_cancel(self):
        if self._cancel.is_set():
            raise InstallCancelled()

    def log(self, line):
        line = line.rstrip()
        stamp = time.strftime("%H:%M:%S ")
        if self._log_fh:
            self._log_fh.write(stamp + line + "\n")
            self._log_fh.flush()
        try:
            from .. import log as gflog
            gflog.debug("[local-ai install] %s", line)
        except Exception:
            pass
        self.on_event("log", line=line)

    def progress(self, fraction, text):
        self.on_event("progress", fraction=fraction, text=text)

    def path(self, *parts):
        return os.path.join(self.base, *parts)

    def run_cmd(self, cmd, label, extra_env=None):
        self.log("$ " + " ".join('"%s"' % c if " " in c else c for c in cmd))
        env = clean_env(extra_env)
        env.pop("HF_HUB_OFFLINE", None)
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      cwd=self.base, env=env, **hidden_popen_kwargs())
        for raw in iter(self._proc.stdout.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                self.log("  " + text)
                self.progress(None, "%s: %s" % (label, text[-90:]))
        code = self._proc.wait()
        self._proc = None
        self._check_cancel()
        if code != 0:
            raise InstallError("%s failed (exit code %s). See the details log." % (label, code))

    def download(self, url, dest, label, approx_gb=0.0, done_gb=0.0, total_gb=0.0):
        """Resumable download with progress. Skips files that are already complete."""
        if os.path.exists(dest):
            self.log("already have %s" % os.path.relpath(dest, self.base))
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        for attempt in range(1, 6):
            self._check_cancel()
            have = os.path.getsize(part) if os.path.exists(part) else 0
            headers = {"User-Agent": "GenFill-LocalAI-installer"}
            if have:
                headers["Range"] = "bytes=%d-" % have
            try:
                resp = _urlrequest.urlopen(_urlrequest.Request(url, headers=headers), timeout=60)
                status = resp.getcode()
                if have and status == 200:          # server ignored the range: start over
                    have = 0
                length = resp.headers.get("Content-Length")
                total = (int(length) + have) if length else None
                mode = "ab" if have and status == 206 else "wb"
                got = have
                t0, last = time.time(), 0.0
                with open(part, mode) as fh:
                    while True:
                        self._check_cancel()
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last > 0.25:
                            last = now
                            speed = (got - have) / max(0.001, now - t0) / 1e6
                            frac = None
                            if total_gb:
                                frac = min(1.0, (done_gb + got / 1e9) / total_gb)
                            size = (" of %.1f GB" % (total / 1e9)) if total else ""
                            self.progress(frac, "Downloading %s: %.2f GB%s  (%.0f MB/s)"
                                          % (label, got / 1e9, size, speed))
                if total is not None and got < total:
                    raise IOError("connection closed early (%d of %d bytes)" % (got, total))
                os.replace(part, dest)
                self.log("downloaded %s (%.2f GB)" % (os.path.relpath(dest, self.base), got / 1e9))
                return
            except InstallCancelled:
                raise
            except HTTPError as exc:
                if exc.code == 416 and os.path.exists(part):   # already complete
                    os.replace(part, dest)
                    return
                self.log("download error (attempt %d): HTTP %s" % (attempt, exc.code))
                if exc.code in (401, 403, 404):
                    raise InstallError("Couldn't download %s (HTTP %s): %s" % (label, exc.code, url))
            except (URLError, IOError, OSError) as exc:
                self.log("download error (attempt %d): %s" % (attempt, exc))
            _sleep(min(30, 3 * attempt))
        raise InstallError("Couldn't download %s after 5 attempts. Check your connection and run "
                           "Install again; it will resume." % label)

    # -- steps -------------------------------------------------------------
    def step_check(self):
        os.makedirs(self.base, exist_ok=True)
        from .client import kill_stale_helpers
        if kill_stale_helpers(self.base, any_owner=True):     # its files may be replaced
            self.log("stopped a leftover AI helper process")
        if "onedrive" in self.base.lower():
            raise InstallError("The install folder is inside OneDrive (%s). ~25 GB would sync to "
                               "the cloud. Choose a folder outside OneDrive." % self.base)
        for old in ("ComfyUI_windows_portable", "_downloads"):
            p = self.path(old)
            if os.path.isdir(p):
                self.log("removing leftovers of the old ComfyUI setup: %s" % p)
                shutil.rmtree(p, ignore_errors=True)
        self.gpu = detect_gpu()
        name, vram = self.gpu
        if name:
            self.log("GPU: %s, %.1f GB" % (name, vram))
        else:
            self.log("WARNING: no NVIDIA GPU detected (nvidia-smi not found).")
        if not self.quant:
            self.quant = recommended_quant(vram)
        self.log("model size: %s (%s)" % (self.quant, QUANTS[self.quant][0]))
        need = sum(f[2] for f in model_files(self.quant)
                   if not os.path.exists(self.path(*f[0].split("/")))) + \
            (0 if os.path.exists(env_python(self.base)) else 7.0)
        free = shutil.disk_usage(self.base).free / 1e9
        self.log("free disk: %.1f GB, needed: about %.1f GB" % (free, need))
        if free < need + 2:
            raise InstallError("Not enough disk space: need about %.0f GB, %.0f GB free on that drive."
                               % (need + 2, free))

    def step_uv(self):
        uv = self.path("bin", "uv.exe" if self.platform.startswith("win") else "uv")
        if os.path.exists(uv):
            self.log("uv already present")
            self.uv = uv
            return
        if not self.platform.startswith("win"):
            raise InstallError("The automatic installer currently supports Windows only.")
        z = self.path("downloads", "uv.zip")
        if os.path.exists(z):
            os.remove(z)
        self.download(UV_URL, z, "uv")
        os.makedirs(os.path.dirname(uv), exist_ok=True)
        with zipfile.ZipFile(z) as zf:
            member = next(n for n in zf.namelist() if n.lower().endswith("uv.exe"))
            with zf.open(member) as src, open(uv, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.remove(z)
        self.uv = uv

    def _uv_env(self):
        return {"UV_PYTHON_INSTALL_DIR": self.path("python"), "UV_CACHE_DIR": self.path("cache"),
                "UV_NO_CONFIG": "1", "UV_PYTHON_PREFERENCE": "only-managed"}

    def step_env(self):
        if os.path.exists(env_python(self.base)):
            self.log("environment already exists")
            return
        self.run_cmd([self.uv, "venv", "--python", PYTHON_VERSION, self.path("env")],
                     "Creating Python environment", self._uv_env())

    def torch_status(self):
        """{'version', 'cuda', 'available'} for the installed PyTorch, or None if it won't import."""
        code = ("import json, torch; print('GENFILL_TORCH=' + json.dumps({'version': torch.__version__, "
                "'cuda': torch.version.cuda, 'available': torch.cuda.is_available()}))")
        try:
            out = subprocess.run([env_python(self.base), "-c", code], capture_output=True, text=True,
                                 timeout=600, cwd=self.base, env=clean_env(),
                                 **hidden_popen_kwargs())
        except Exception as exc:
            self.log("torch check could not run: %s" % exc)
            return None
        for line in out.stdout.splitlines():
            if line.startswith("GENFILL_TORCH="):
                st = json.loads(line.split("=", 1)[1])
                self.log("PyTorch %s, CUDA %s, GPU usable: %s" % (st["version"], st["cuda"],
                                                                  st["available"]))
                return st
        self.log("PyTorch doesn't import: %s" % (out.stderr.strip().splitlines() or ["?"])[-1])
        return None

    def step_torch(self):
        st = self.torch_status()
        if st and st["available"]:
            return
        if st:
            self.log("the installed PyTorch can't use the GPU (CPU-only build); replacing it")
        for tag in TORCH_CUDA_TAGS:
            self._check_cancel()
            try:
                self.run_cmd([self.uv, "pip", "install", "--python", env_python(self.base),
                              "--reinstall-package", "torch", "--compile-bytecode", "torch",
                              "--index-url", TORCH_INDEX_BASE + tag],
                             "Installing PyTorch (%s)" % tag, self._uv_env())
            except InstallError as exc:
                self.log("PyTorch %s build didn't install: %s" % (tag, exc))
                continue
            st = self.torch_status()
            if st is None:
                self.fix_runtime()
                st = self.torch_status()
            if st and st["available"]:
                return
        if not self.gpu[0]:
            raise InstallError("Local AI needs an NVIDIA GPU, and none was detected.")
        raise InstallError("Couldn't get a GPU build of PyTorch working on %s. Update the NVIDIA "
                           "driver (nvidia.com), restart, and click Repair." % self.gpu[0])

    def step_libs(self):
        self.run_cmd([self.uv, "pip", "install", "--python", env_python(self.base),
                      "--compile-bytecode"] + LIBRARIES, "Installing libraries", self._uv_env())

    # -- C++ runtime ------------------------------------------------------------
    def base_python_dir(self):
        """Folder of the real interpreter behind the venv (its 'home')."""
        cfg = self.path("env", "pyvenv.cfg")
        try:
            with open(cfg, "r") as fh:
                for line in fh:
                    key, _, value = line.partition("=")
                    if key.strip().lower() == "home":
                        return value.strip()
        except (IOError, OSError):
            pass
        return None

    def runtime_targets(self):
        """Where fresh C++ runtime DLLs must go to win the DLL search:
        next to the interpreter (application folder) and next to torch's DLLs
        (torch loads them with LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR)."""
        targets = []
        home = self.base_python_dir()
        if home and os.path.isdir(home):
            targets.append(home)
        torch_lib = self.path("env", "Lib", "site-packages", "torch", "lib")
        if os.path.isdir(torch_lib):
            targets.append(torch_lib)
        return targets

    def runtime_dlls(self):
        """C++ runtime DLLs installed by the msvc-runtime package."""
        found = {}
        for folder in (self.path("env"), self.path("env", "Scripts")):
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                low = name.lower()
                if low.endswith(".dll") and low.startswith(RUNTIME_PREFIXES):
                    found.setdefault(low, os.path.join(folder, name))
        return found

    def fix_runtime(self):
        """Put the msvc-runtime C++ DLLs next to the interpreter and PyTorch.

        Always, whatever Windows has: on the RTX 3080 Ti test machine PyTorch's
        c10.dll failed with WinError 1114 against Windows' newer 14.50 runtime and
        worked with the 14.44 runtime PyTorch is built against. Copies placed here
        only affect GenFill's private Python, never Windows or other programs.
        """
        if not self.platform.startswith("win"):
            return
        dlls = self.runtime_dlls()
        if not dlls:
            self.run_cmd([self.uv, "pip", "install", "--python", env_python(self.base), "--upgrade",
                          "msvc-runtime"], "Getting the C++ runtime", self._uv_env())
            dlls = self.runtime_dlls()
        if not dlls:
            raise InstallError("Couldn't get the Microsoft C++ runtime files (msvc-runtime).")
        sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        self.log("system msvcp140.dll %s, GenFill's private copy %s"
                 % (file_version(os.path.join(sys32, "msvcp140.dll")) or "missing",
                    file_version(dlls.get("msvcp140.dll", "")) or "?"))
        for target in self.runtime_targets():
            placed = 0
            for low, src in sorted(dlls.items()):
                dst = os.path.join(target, os.path.basename(src))
                try:
                    if not os.path.exists(dst) or file_version(dst) != file_version(src) \
                            or os.path.getsize(dst) != os.path.getsize(src):
                        shutil.copy2(src, dst)
                    placed += 1
                except (IOError, OSError) as exc:
                    self.log("could not place %s in %s: %s" % (low, target, exc))
            self.log("C++ runtime (%d DLLs) in place in %s" % (placed, target))

    def step_runtime(self):
        self.fix_runtime()
        site = self.path("env", "Lib", "site-packages")
        if os.path.isdir(site):
            self.progress(None, "Precompiling the AI libraries so the first start is fast...")
            try:
                self.run_cmd([env_python(self.base), "-m", "compileall", "-q", "-j", "0", site],
                             "Precompiling")
            except InstallError as exc:
                self.log("precompile had problems (not fatal): %s" % exc)
        st = self.torch_status()
        if not st:
            raise InstallError(
                "PyTorch can't start. Install the latest Microsoft Visual C++ Redistributable "
                "(x64) from https://aka.ms/vs/17/release/vc_redist.x64.exe, then click Repair. "
                "Details are in the log.")
        if not st["available"]:
            raise InstallError("PyTorch is installed but can't use your GPU. Update the NVIDIA "
                               "driver, restart, and click Repair.")
        self.run_cmd([env_python(self.base), "-c",
                      "import diffusers, transformers, gguf; print('diffusers', "
                      "diffusers.__version__, 'transformers', transformers.__version__)"],
                     "Checking libraries")

    def step_models(self):
        files = model_files(self.quant)
        total = sum(f[2] for f in files) or 1.0
        done = 0.0
        for rel, url, gb in files:
            dest = self.path(*rel.split("/"))
            index = os.path.join(os.path.dirname(dest), "model.safetensors.index.json")
            if rel.endswith("/model.safetensors") and os.path.exists(index):
                self.log("already have %s (split into shards)" % rel)
                done += gb
                continue
            self.download(url, dest, os.path.basename(rel), gb, done, total)
            done += gb

    def split_big_weights(self):
        """Reshard large single-file text encoders (see reshard_safetensors)."""
        for folder in ("t5", "clip"):
            src = self.path("models", folder, "model.safetensors")
            if not os.path.exists(src) or os.path.getsize(src) <= 2 * SHARD_BYTES:
                continue
            self.progress(None, "Splitting the %s weights into 1 GB pieces (saves memory)..." % folder)
            self.log("splitting %s into 1 GB shards" % src)
            reshard_safetensors(src, os.path.dirname(src), log=self.log)
            os.remove(src)   # transformers would prefer the single file if both exist

    def step_configure(self):
        self.split_big_weights()
        cfg_dir = self.path("models", "flux", "config")
        for rel, data in FLUX_CONFIGS.items():
            p = os.path.join(cfg_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                json.dump(data, fh, indent=2)
        manifest = {
            "version": 1,
            "quant": self.quant,
            "unet": "models/flux/flux1-fill-dev-%s.gguf" % self.quant,
            "flux_config_dir": "models/flux/config",
            "vae": "models/vae/ae.safetensors",
            "clip_dir": "models/clip",
            "t5_dir": "models/t5",
            "lama": "models/lama/big-lama.pt",
            "installed": datetime.datetime.now().isoformat(timespec="seconds"),
            "gpu": self.gpu[0],
            "vram_gb": self.gpu[1],
            "verified": False,
        }
        with open(self.path(MANIFEST), "w") as fh:
            json.dump(manifest, fh, indent=2)
        data = config.load()
        data["local_ai"] = {"root": self.base}   # replaces any old ComfyUI settings
        config.save(data)
        self.log("wrote %s and %s" % (self.path(MANIFEST), config.CONFIG_PATH))

    def step_selftest(self):
        client = WorkerClient(base=self.base, idle=60)
        try:
            self.progress(None, "Loading FLUX.1 Fill on your GPU (first load takes a minute)...")
            done = client.request("selftest", {},
                                  progress=lambda f, m: self.progress(None, "Self-test: " + m),
                                  cancelled=self._cancel.is_set, timeout=1800)
        except WorkerError as exc:
            raise InstallError("Self-test failed: %s" % exc)
        finally:
            client.stop()
        info = done.get("info", {})
        self.result = info
        self.log("self-test OK: %s" % json.dumps(info))
        if not info.get("cuda"):
            raise InstallError("PyTorch can't see your GPU (CUDA unavailable). Update the NVIDIA "
                               "driver and run Install again.")
        with open(self.path(MANIFEST)) as fh:
            m = json.load(fh)
        m["verified"] = True
        m["selftest"] = info
        with open(self.path(MANIFEST), "w") as fh:
            json.dump(m, fh, indent=2)
        cache = self.path("cache")
        if os.path.isdir(cache):
            self.log("removing download cache to save space")
            shutil.rmtree(cache, ignore_errors=True)

    # -- run ---------------------------------------------------------------
    def run(self):
        os.makedirs(log_dir(self.base), exist_ok=True)
        self._log_fh = open(os.path.join(log_dir(self.base), "install.log"), "a", encoding="utf-8")
        self.log("==== GenFill Local AI install %s -> %s" % (time.strftime("%Y-%m-%d %H:%M"), self.base))
        ok, message = False, ""
        current = 0
        try:
            for i, (key, label) in enumerate(self.STEPS):
                current = i
                self._check_cancel()
                self.on_event("step", index=i, state="running", label=label)
                self.log("--- %s" % label)
                getattr(self, "step_" + key)()
                self.on_event("step", index=i, state="done", label=label)
            ok = True
            secs = self.result.get("selftest_seconds")
            message = ("Local AI is ready on %s. Pick 'Local: FLUX.1 Fill' in a GenerativeFill node."
                       % (self.result.get("gpu") or "your GPU"))
            if secs:
                message += " (Self-test took %ss.)" % secs
        except InstallCancelled:
            self.on_event("step", index=current, state="failed", label=self.STEPS[current][1])
            message = "Installation cancelled. Run Install again to resume where it stopped."
        except Exception as exc:
            self.on_event("step", index=current, state="failed", label=self.STEPS[current][1])
            message = str(exc) if isinstance(exc, InstallError) else "%s: %s" % (type(exc).__name__, exc)
            import traceback
            self.log(traceback.format_exc())
        self.log("==== %s: %s" % ("OK" if ok else "FAILED", message))
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        self.on_event("finished", ok=ok, message=message)
        return ok
