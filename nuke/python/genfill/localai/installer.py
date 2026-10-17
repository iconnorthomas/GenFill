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
import hashlib
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
# Pinned so the hash stays valid. SHA-256 from the release's own
# uv-x86_64-pc-windows-msvc.zip.sha256 file (matches GitHub's asset digest).
UV_VERSION = "0.12.21"
UV_URL = ("https://github.com/astral-sh/uv/releases/download/%s/uv-x86_64-pc-windows-msvc.zip"
          % UV_VERSION)
UV_SHA256 = "5d223efa0bf00208c3853246af09420419dfbd352536aa6bb8163d6170e23890"
TORCH_INDEX_BASE = "https://download.pytorch.org/whl/"
# Newest first. Only the PyTorch CUDA index is used for torch: PyPI's Windows
# torch wheels are CPU-only, and uv prefers --extra-index-url over --index-url.
TORCH_CUDA_TAGS = ["cu130", "cu128", "cu126"]
RUNTIME_PREFIXES = ("msvcp140", "vcruntime140", "concrt140", "vcomp140", "vccorlib140")
PYTHON_VERSION = "3.12"
LIBRARIES = ["diffusers>=0.34", "transformers>=4.48", "accelerate>=1.2", "gguf>=0.10.0",
             "sentencepiece", "protobuf", "safetensors", "pillow", "numpy", "tifffile"]

# Model families the Local AI can install. One is installed at a time; switching
# re-runs the installer, which keeps the environment and LaMa and only downloads
# the other model.
FLUX1 = "flux1-fill"
KLEIN = "flux2-klein"
FAMILIES = {
    FLUX1: {
        "label": "FLUX.1 Fill [dev]",
        "summary": "Best fill quality. Non-commercial use only (FLUX.1 [dev] license).",
        "license": "FLUX.1 [dev] Non-Commercial License",
        "license_url": "https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev/blob/main/LICENSE.md",
        "commercial": False,
        "libraries": [],
    },
    KLEIN: {
        "label": "FLUX.2 [klein] 4B",
        "summary": "Licensed for commercial and studio work (Apache 2.0). Fast: 4 steps per image.",
        "license": "Apache License 2.0",
        "license_url": "https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/main/LICENSE.md",
        "commercial": True,
        # Flux2KleinInpaintPipeline arrived in diffusers 0.41; its Qwen3 text
        # encoder needs a recent transformers.
        "libraries": ["diffusers>=0.41", "transformers>=4.51"],
    },
}
KLEIN_REPO = "black-forest-labs/FLUX.2-klein-4B"
KLEIN_DIR = "models/flux2-klein"

QUANTS = {
    # id: (label, file size GB, minimum VRAM GB it's meant for)
    "Q4_K_S": ("Standard: fits a 12 GB GPU alongside Nuke (6.8 GB model)", 6.8, 8),
    "Q6_K": ("High: near-full quality, for 16 GB+ GPUs (9.9 GB model)", 9.9, 14),
    "Q8_0": ("Maximum: full quality, for 24 GB GPUs (12.7 GB model)", 12.7, 20),
}

# Hugging Face downloads are pinned to a commit so the SHA-256 values below
# (the LFS sha256 Hugging Face publishes on each file page) can't drift.
HF_REVISIONS = {
    "YarvixPA/FLUX.1-Fill-dev-GGUF": "78b83f1da140a4dcd6466580516544e1f6effe3e",
    "city96/t5-v1_1-xxl-encoder-bf16": "1b9c856aadb864af93c1dcdc226c2774fa67bc86",
    "openai/clip-vit-large-patch14": "32bd64288804d66eefd0ccbe215aa642df71cc41",
    "Comfy-Org/Lumina_Image_2.0_Repackaged": "5b072540ef86570fecb8249c505f23d5bdeb88cd",
    KLEIN_REPO: "e7b7dc27f91deacad38e78976d1f2b499d76a294",
}
# FLUX.2 [klein] 4B in diffusers layout: (path in the repo, approx GB, LFS SHA-256).
# The repo's single-file copy (flux-2-klein-4b.safetensors) duplicates transformer/
# and is skipped. Files without a hash are plain git files fixed by the commit pin.
KLEIN_FILES = [
    ("model_index.json", 0.0, None),
    ("scheduler/scheduler_config.json", 0.0, None),
    ("transformer/config.json", 0.0, None),
    ("transformer/diffusion_pytorch_model.safetensors", 7.75,
     "9f29f9edcfdae452a653ffb51a534ca4decd389952c225724ff3b94042612a6e"),
    ("vae/config.json", 0.0, None),
    ("vae/diffusion_pytorch_model.safetensors", 0.17,
     "ca70d2202afe6415bdbcb8793ba8cd99fd159cfe6192381504d6c4d3036e0f04"),
    ("text_encoder/config.json", 0.0, None),
    ("text_encoder/generation_config.json", 0.0, None),
    ("text_encoder/model.safetensors.index.json", 0.0, None),
    ("text_encoder/model-00001-of-00002.safetensors", 4.97,
     "8c0506e7f4936fa7e26183a4fd8da4e2bdbc5990ba64ae441f965d51228f36ea"),
    ("text_encoder/model-00002-of-00002.safetensors", 3.08,
     "82f2bd839378541b0557bfabaf37c7d3d637071fdcb73302dedd7cf61162ce07"),
    ("tokenizer/added_tokens.json", 0.0, None),
    ("tokenizer/chat_template.jinja", 0.0, None),
    ("tokenizer/merges.txt", 0.0, None),
    ("tokenizer/special_tokens_map.json", 0.0, None),
    ("tokenizer/tokenizer.json", 0.01,
     "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"),
    ("tokenizer/tokenizer_config.json", 0.0, None),
    ("tokenizer/vocab.json", 0.0, None),
]
LAMA_FILE = (
    # TODO: SHA-256 for big-lama.pt. The release publishes no digest; the
    # author's IOPaint code lists only an MD5 (e3aa4aaa15225a33ec84f9f4bc47e500).
    "models/lama/big-lama.pt",
    "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt", 0.2, None)
FLUX_SHA256 = {
    "Q4_K_S": "3782c22eda0622d50eb8683d2aa664e8534a8c1e29ce9e91df491cc400944dc8",
    "Q6_K": "d538635d81ed5eca59fa2f5a7b68709f0b5584d936570a8695e4d1ae4d518a72",
    "Q8_0": "345ffd7acf39148e391133ae2a2fa7cc3f85e8dd0dbdb4636e760c3e7fe7952d",
}


def hf_url(repo, path):
    return "%s/%s/resolve/%s/%s" % (HF, repo, HF_REVISIONS[repo], path)


def recommended_quant(vram_gb):
    if not vram_gb or vram_gb < 14:
        return "Q4_K_S"
    if vram_gb < 20:
        return "Q6_K"
    return "Q8_0"


def model_files(quant, family=FLUX1):
    """(relative path, url, approx GB, SHA-256 or None) for every model file."""
    if family == KLEIN:
        return [("%s/%s" % (KLEIN_DIR, path), hf_url(KLEIN_REPO, path), gb, sha)
                for path, gb, sha in KLEIN_FILES] + [LAMA_FILE]
    t5, clip = "city96/t5-v1_1-xxl-encoder-bf16", "openai/clip-vit-large-patch14"
    files = [
        ("models/flux/flux1-fill-dev-%s.gguf" % quant,
         hf_url("YarvixPA/FLUX.1-Fill-dev-GGUF", "flux1-fill-dev-%s.gguf" % quant),
         QUANTS[quant][1], FLUX_SHA256[quant]),
        ("models/t5/model.safetensors", hf_url(t5, "model.safetensors"), 9.5,
         "565cb2487351282e8e4dbeb88e63f4ad28217ce0439f5a8e6525a924807d2d9b"),
        ("models/t5/spiece.model", hf_url(t5, "spiece.model"), 0.0,
         "d60acb128cf7b7f2536e8f38a5b18a05535c9e14c7a355904270e15b0945ea86"),
        ("models/clip/model.safetensors", hf_url(clip, "model.safetensors"), 1.7,
         "a2bf730a0c7debf160f7a6b50b3aaf3703e7e88ac73de7a314903141db026dcb"),
        ("models/vae/ae.safetensors",
         hf_url("Comfy-Org/Lumina_Image_2.0_Repackaged", "split_files/vae/ae.safetensors"), 0.33,
         "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38"),
        LAMA_FILE,
    ]
    # Small tokenizer/config files are plain git files on Hugging Face, which
    # publishes no SHA-256 for them; the pinned commit in the URL fixes their content.
    for name in ("config.json", "special_tokens_map.json", "tokenizer_config.json"):
        files.append(("models/t5/" + name, hf_url(t5, name), 0.0, None))
    for name in ("config.json", "merges.txt", "vocab.json", "tokenizer.json", "tokenizer_config.json",
                 "special_tokens_map.json"):
        files.append(("models/clip/" + name, hf_url(clip, name), 0.0, None))
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


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def version_tuple(text):
    if not text:
        return None
    try:
        return tuple(int(p) for p in text.split("."))
    except ValueError:
        return None


def estimate_gb(quant, family=FLUX1):
    return sum(f[2] for f in model_files(quant, family)) + 7.0   # + PyTorch environment


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

    @classmethod
    def steps_for(cls, family=FLUX1):
        """The step list with the download step named for ``family``."""
        if family != KLEIN:
            return list(cls.STEPS)
        return [(k, "Download FLUX.2 [klein] 4B, its text encoder and LaMa" if k == "models" else l)
                for k, l in cls.STEPS]

    def __init__(self, base=None, quant=None, on_event=None, platform=None, family=FLUX1):
        if family not in FAMILIES:
            raise ValueError("unknown Local AI model %r" % family)
        self.base = base or default_root()
        self.quant = quant
        self.family = family
        self.steps = self.steps_for(family)
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

    def _verify(self, part, label, sha256):
        """Check a finished .part file against its expected SHA-256, if one is known."""
        if not sha256:
            return
        self.progress(None, "Checking %s..." % label)
        actual = sha256_file(part)
        if actual != sha256.lower():
            os.remove(part)
            self.log("SHA-256 mismatch for %s: expected %s, got %s" % (label, sha256, actual))
            raise InstallError("The download of %s is corrupt or was changed (SHA-256 mismatch). "
                               "It was deleted; run Install again." % label)
        self.log("verified SHA-256 of %s" % label)

    def download(self, url, dest, label, approx_gb=0.0, done_gb=0.0, total_gb=0.0, sha256=None):
        """Resumable download with progress. Skips files that are already complete.

        With ``sha256``, the finished file is checked before it is moved into place.
        """
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
                self._verify(part, label, sha256)
                os.replace(part, dest)
                self.log("downloaded %s (%.2f GB)" % (os.path.relpath(dest, self.base), got / 1e9))
                return
            except InstallCancelled:
                raise
            except HTTPError as exc:
                if exc.code == 416 and os.path.exists(part):   # already complete
                    self._verify(part, label, sha256)
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
        if self.family == KLEIN:
            self.log("model: %s (%s)" % (FAMILIES[KLEIN]["label"], FAMILIES[KLEIN]["license"]))
        else:
            self.log("model size: %s (%s)" % (self.quant, QUANTS[self.quant][0]))
        need = sum(f[2] for f in model_files(self.quant, self.family)
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
        self.download(UV_URL, z, "uv", sha256=UV_SHA256)
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
                      "--compile-bytecode"] + LIBRARIES + FAMILIES[self.family]["libraries"],
                     "Installing libraries", self._uv_env())

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
        files = model_files(self.quant, self.family)
        total = sum(f[2] for f in files) or 1.0
        done = 0.0
        for rel, url, gb, sha256 in files:
            dest = self.path(*rel.split("/"))
            index = os.path.join(os.path.dirname(dest), "model.safetensors.index.json")
            if rel.endswith("/model.safetensors") and os.path.exists(index):
                self.log("already have %s (split into shards)" % rel)
                done += gb
                continue
            self.download(url, dest, os.path.basename(rel), gb, done, total, sha256=sha256)
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
        common = {
            "version": 1,
            "family": self.family,
            "lama": "models/lama/big-lama.pt",
            "installed": datetime.datetime.now().isoformat(timespec="seconds"),
            "gpu": self.gpu[0],
            "vram_gb": self.gpu[1],
            "verified": False,
        }
        if self.family == KLEIN:
            manifest = dict(common, model_dir=KLEIN_DIR,
                            unet=KLEIN_DIR + "/transformer/diffusion_pytorch_model.safetensors",
                            vae=KLEIN_DIR + "/vae/diffusion_pytorch_model.safetensors")
        else:
            self.split_big_weights()
            cfg_dir = self.path("models", "flux", "config")
            for rel, data in FLUX_CONFIGS.items():
                p = os.path.join(cfg_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    json.dump(data, fh, indent=2)
            manifest = dict(common, quant=self.quant,
                            unet="models/flux/flux1-fill-dev-%s.gguf" % self.quant,
                            flux_config_dir="models/flux/config", vae="models/vae/ae.safetensors",
                            clip_dir="models/clip", t5_dir="models/t5")
        with open(self.path(MANIFEST), "w") as fh:
            json.dump(manifest, fh, indent=2)
        data = config.load()
        data["local_ai"] = {"root": self.base}   # replaces any old ComfyUI settings
        config.save(data)
        self.log("wrote %s and %s" % (self.path(MANIFEST), config.CONFIG_PATH))

    def step_selftest(self):
        client = WorkerClient(base=self.base, idle=60)
        try:
            self.progress(None, "Loading %s on your GPU (first load takes a minute)..."
                          % FAMILIES[self.family]["label"])
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
            for i, (key, label) in enumerate(self.steps):
                current = i
                self._check_cancel()
                self.on_event("step", index=i, state="running", label=label)
                self.log("--- %s" % label)
                getattr(self, "step_" + key)()
                self.on_event("step", index=i, state="done", label=label)
            ok = True
            secs = self.result.get("selftest_seconds")
            message = ("Local AI is ready on %s. Pick 'Local AI' as the model in a GenerativeFill node."
                       % (self.result.get("gpu") or "your GPU"))
            if secs:
                message += " (Self-test took %ss.)" % secs
        except InstallCancelled:
            self.on_event("step", index=current, state="failed", label=self.steps[current][1])
            message = "Installation cancelled. Run Install again to resume where it stopped."
        except Exception as exc:
            self.on_event("step", index=current, state="failed", label=self.steps[current][1])
            message = str(exc) if isinstance(exc, InstallError) else "%s: %s" % (type(exc).__name__, exc)
            import traceback
            self.log(traceback.format_exc())
        self.log("==== %s: %s" % ("OK" if ok else "FAILED", message))
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        self.on_event("finished", ok=ok, message=message)
        return ok
