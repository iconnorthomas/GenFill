"""GenFill local AI helper process.

Started (hidden, no window) by Nuke with the private Python environment that
the in-Nuke installer creates. It never imports ``nuke`` or ``genfill``: it is
a standalone script so its heavy dependencies (PyTorch, diffusers) stay out of
Nuke's process. If it crashes or runs out of GPU memory, Nuke keeps running.

Protocol: one JSON object per line.
  stdin  (from Nuke):  {"id": "...", "cmd": "ping" | "load" | "generate" | "selftest"
                        | "cancel" | "shutdown", ...}
  stdout (to Nuke):    {"id": "...", "event": "progress" | "result" | "done" | "error" | "pong"
                        | "bye", ...}
  "result" ({"index": i, "file": path}) is sent as each variation is saved, before "done"
  lists them all; older Nuke-side code just ignores it.
Everything else the libraries print goes to stderr (Nuke points it at a log).

The process exits when Nuke closes the pipe (Nuke quit or crashed), on
"shutdown", or after ``--idle`` seconds without work (frees the GPU; 0 = never).
Commands run one at a time in arrival order; "cancel" also works on a command
still waiting its turn (e.g. a generate queued behind a warm-up "load").
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback

PROTOCOL_VERSION = 1
_send_lock = threading.Lock()
_proto_out = None


def send(obj):
    line = json.dumps(obj) + "\n"
    with _send_lock:
        _proto_out.write(line)
        _proto_out.flush()


def log(msg, *args):
    sys.stderr.write(time.strftime("%H:%M:%S ") + (msg % args if args else msg) + "\n")
    sys.stderr.flush()


def memory_free():
    """(RAM free, commit free) in bytes on Windows, else None."""
    st = _memory_struct()
    return (st.ullAvailPhys, st.ullAvailPageFile) if st else None


def memory_status():
    """'RAM free x GB, commit free y GB' on Windows (commit = RAM + page file)."""
    st = _memory_struct()
    if not st:
        return ""
    return "RAM free %.1f/%.1f GB, commit free %.1f/%.1f GB" % (
        st.ullAvailPhys / 1e9, st.ullTotalPhys / 1e9, st.ullAvailPageFile / 1e9,
        st.ullTotalPageFile / 1e9)


def _memory_struct():
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return st
    except Exception:
        return None


WINDOWS_MEMORY_MSG = (
    "Windows ran out of memory (RAM + page file) while loading the AI. Close other memory-heavy "
    "apps (browsers, other DCCs), or let Windows manage the page file size (System > About > "
    "Advanced system settings > Performance > Advanced > Virtual memory), then try again.")


# ---------------------------------------------------------------- image IO
# Float in, float out: the plate arrives as a 32-bit float TIFF already encoded
# into model space by Nuke (see genfill/colorspace.py), and results go back as
# float TIFF, so nothing is rounded to 8 bits.
_A, _B, _CCT_BREAK = 10.5402377416545, 0.0729055341958355, 0.155251141552511


def load_image(path):
    """RGB float32 (H, W, 3); TIFF via tifffile (float/16/8-bit), others via PIL."""
    import numpy as np
    if path.lower().endswith((".tif", ".tiff")):
        try:
            import tifffile
        except ImportError:
            raise RuntimeError("Float image support isn't installed in the Local AI yet. Click "
                               "'Local AI...' in the node's panel and click Repair (quick).")
        arr = tifffile.imread(path)
        if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[-1] not in (3, 4):
            arr = np.transpose(arr, (1, 2, 0))          # planar -> interleaved
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        if arr.dtype == np.uint16:
            arr = arr.astype(np.float32) / 65535.0
        elif arr.dtype == np.uint8:
            arr = arr.astype(np.float32) / 255.0
        return np.ascontiguousarray(arr[..., :3].astype(np.float32))
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB")).astype(np.float32) / 255.0


def save_float_tiff(path, arr):
    import numpy as np
    import tifffile
    tifffile.imwrite(path, np.ascontiguousarray(arr.astype(np.float32)), photometric="rgb")


def preview_png(path, arr, kind):
    """8-bit thumbnail. 'log': ACEScct -> linear -> sRGB; 'display': already viewable."""
    import numpy as np
    from PIL import Image
    a = arr.astype(np.float32)
    if kind == "log":
        a = np.where(a <= _CCT_BREAK, (a - _B) / _A, np.exp2(a * 17.52 - 9.72))
        a = np.clip(a, 0.0, 1.0)
        a = np.where(a <= 0.0031308, 12.92 * a, 1.055 * np.power(a, 1 / 2.4) - 0.055)
    Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8)).save(path)


def resize_float(arr, width, height):
    import torch
    import torch.nn.functional as F
    t = torch.from_numpy(arr.transpose(2, 0, 1))[None]
    t = F.interpolate(t, size=(height, width), mode="bicubic", align_corners=False)
    return t[0].permute(1, 2, 0).numpy()


def stream_blocks(model, blocks, device):
    """Run ``model`` on ``device`` while most of its weights stay in RAM.

    Everything outside ``blocks`` (embeddings, final norm) moves to the device for
    good; each block's weights are copied over just before that block runs and
    dropped right after. So a 9.5 GB text encoder needs well under 1 GB of VRAM
    and the FLUX transformer can stay on the GPU.
    """
    in_blocks = set()
    for block in blocks:
        in_blocks.update(id(t) for t in block.parameters())
        in_blocks.update(id(t) for t in block.buffers())
    for t in list(model.parameters()) + list(model.buffers()):
        if id(t) not in in_blocks:
            t.data = t.data.to(device)

    def hooks(tensors):
        home = [t.data for t in tensors]

        def onload(_module, _args):
            for t, h in zip(tensors, home):
                t.data = h.to(device, non_blocking=True)

        def offload(_module, _args, _out):
            for t, h in zip(tensors, home):
                t.data = h
        return onload, offload

    for block in blocks:
        onload, offload = hooks(list(block.parameters()) + list(block.buffers()))
        block.register_forward_pre_hook(onload)
        block.register_forward_hook(offload)
    return model


class EmbedCache(object):
    """Prompt embeddings saved on disk, so a prompt that was read once (even before
    the helper or Nuke restarted) never needs the big text encoder again.

    One file per prompt (a few MB), named by a hash of ``model_key`` and the prompt;
    ``model_key`` changes when the model is reinstalled. The least recently used
    files beyond ``max_files`` are deleted. ``save(obj, path)`` / ``load(path)``
    default to torch.save / torch.load. Never raises: a broken cache is just a miss.
    """

    def __init__(self, folder, model_key, max_files=64, save=None, load=None):
        self.folder = folder
        self.model_key = model_key
        self.max_files = max_files
        self._save = save or _torch_save
        self._load = load or _torch_load

    def path(self, prompt):
        import hashlib
        digest = hashlib.sha256((self.model_key + "\0" + prompt).encode("utf-8")).hexdigest()
        return os.path.join(self.folder, digest[:32] + ".pt")

    def get(self, prompt):
        path = self.path(prompt)
        if not os.path.isfile(path):
            return None
        try:
            value = self._load(path)
            os.utime(path, None)           # most recently used
            return value
        except Exception as exc:
            log("embedding cache: unreadable %s (%s), ignoring", os.path.basename(path), exc)
            try:
                os.remove(path)
            except OSError:
                pass
            return None

    def put(self, prompt, value):
        path = self.path(prompt)
        try:
            os.makedirs(self.folder, exist_ok=True)
            self._save(value, path + ".part")
            os.replace(path + ".part", path)
            self._prune()
        except Exception as exc:
            log("embedding cache: couldn't save (%s)", exc)

    def _prune(self):
        files = [os.path.join(self.folder, f) for f in os.listdir(self.folder)
                 if f.endswith(".pt")]
        if len(files) <= self.max_files:
            return
        files.sort(key=os.path.getmtime)
        for f in files[:len(files) - self.max_files]:
            try:
                os.remove(f)
            except OSError:
                pass


def _torch_save(value, path):
    import torch
    torch.save(tuple(t.detach().to("cpu") for t in value), path)


def _torch_load(path):
    import torch
    return tuple(torch.load(path, map_location="cpu", weights_only=True))


class Cancelled(Exception):
    pass


class Job(object):
    def __init__(self, msg):
        self.msg = msg
        self.id = msg.get("id")
        self.cancel = threading.Event()

    def progress(self, fraction, message):
        send({"id": self.id, "event": "progress", "fraction": float(fraction), "message": message})

    def result(self, index, path):
        send({"id": self.id, "event": "result", "index": int(index), "file": path})

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()


# --------------------------------------------------------------------------
# real engine: FLUX.1 Fill (diffusers, GGUF) or FLUX.2 [klein] 4B, + LaMa
# --------------------------------------------------------------------------

FLUX1, KLEIN = "flux1-fill", "flux2-klein"
KLEIN_MAX_PIXELS = 1024 * 1024


def klein_size(width, height):
    """Largest size with the same aspect, at most 1 MP, on klein's 16 px grid."""
    scale = min(1.0, (KLEIN_MAX_PIXELS / float(width * height)) ** 0.5)
    w = max(16, int(width * scale) // 16 * 16)
    h = max(16, int(height * scale) // 16 * 16)
    return w, h


# Keep the text encoder in RAM between prompts only while RAM and commit both
# have at least this much free afterwards. Reading T5 again from a hard disk
# takes ~40-65 s; running it from RAM ~2 s. Below this, Nuke needs the room more.
KEEP_TEXT_ENCODER_FREE = 4e9
EMBED_FORMAT = 1     # bump if the cached embeddings' meaning changes


def embed_key(manifest):
    """Identifies the text encoder(s) the disk cache's embeddings came from."""
    family = manifest.get("family") or FLUX1
    parts = [str(EMBED_FORMAT), family, str(manifest.get("installed", ""))]
    if family == KLEIN:
        parts.append(str(manifest.get("model_dir", "")))
    else:
        parts += [str(manifest.get("t5_dir", "")), str(manifest.get("clip_dir", ""))]
        if manifest.get("t5_file"):       # 8-bit T5: different numbers, its own cache
            parts.append(str(manifest["t5_file"]))
    return "|".join(parts)


class Engine(object):
    def __init__(self, root):
        self.root = root
        with open(os.path.join(root, "manifest.json"), "r") as fh:
            self.manifest = json.load(fh)
        self.family = self.manifest.get("family") or FLUX1    # old installs: FLUX.1 Fill
        self.pipe = None
        self.lama = None
        self.device = None
        self.text_encoder = None
        self.embed_cache = {}
        self.disk_cache = EmbedCache(os.path.join(root, "cache", "embeds"), embed_key(self.manifest))
        self._encoder_lock = threading.Lock()     # the background read vs. a prompt
        self._gpu_lock = threading.Lock()         # a job vs. the background warm-up

    def _path(self, key):
        return os.path.join(self.root, self.manifest[key])

    def info(self):
        import torch
        out = {"torch": torch.__version__, "cuda": torch.cuda.is_available()}
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            free, total = torch.cuda.mem_get_info()
            out.update(gpu=p.name, vram_total=total, vram_free=free)
        out["model"] = self.manifest.get("unet")
        out["family"] = self.family
        out["loaded"] = self.pipe is not None
        return out

    def load(self, job=None):
        if self.pipe is not None:
            return
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError(
                "The Local AI's PyTorch can't use your NVIDIA GPU (on the CPU a fill would take "
                "hours). Click 'Local AI...' in the node's panel (or GenFill > Local AI: Install / Repair in the top menu) and click Repair. "
                "(PyTorch %s, CUDA build %s)" % (torch.__version__, torch.version.cuda))
        self.device = "cuda"
        log("libraries imported (torch %s, CUDA %s, %s)", torch.__version__, torch.version.cuda,
            torch.cuda.get_device_name(0))
        log("memory: %s", memory_status())
        if self.family == KLEIN:
            return self._load_klein(job)
        from diffusers import (AutoencoderKL, FlowMatchEulerDiscreteScheduler, FluxFillPipeline,
                               FluxTransformer2DModel, GGUFQuantizationConfig)
        from transformers import CLIPTextModel, CLIPTokenizer, T5TokenizerFast
        dtype = torch.bfloat16
        cfg = self._path("flux_config_dir")
        t0 = time.time()
        if job:
            job.progress(0.0, "Loading FLUX.1 Fill (first time takes a minute)...")
        t1 = time.time()
        transformer = FluxTransformer2DModel.from_single_file(
            self._path("unet"), quantization_config=GGUFQuantizationConfig(compute_dtype=dtype),
            config=cfg, subfolder="transformer", torch_dtype=dtype)
        log("FLUX transformer read in %.1fs", time.time() - t1)
        vae = AutoencoderKL.from_single_file(self._path("vae"), config=cfg, subfolder="vae",
                                             torch_dtype=dtype)
        scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(cfg, subfolder="scheduler")
        text_encoder = CLIPTextModel.from_pretrained(self._path("clip_dir"), torch_dtype=dtype)
        tokenizer = CLIPTokenizer.from_pretrained(self._path("clip_dir"))
        tokenizer_2 = T5TokenizerFast.from_pretrained(self._path("t5_dir"))
        self.pipe = FluxFillPipeline(scheduler=scheduler, vae=vae, text_encoder=text_encoder,
                                     tokenizer=tokenizer, text_encoder_2=None,
                                     tokenizer_2=tokenizer_2, transformer=transformer)
        self.pipe.to(self.device)
        self.pipe.vae.enable_tiling()
        self.pipe.set_progress_bar_config(disable=True)
        log("FLUX.1 Fill loaded in %.1fs", time.time() - t0)
        self.preload_text_encoder()

    def _load_klein(self, job):
        """FLUX.2 [klein] 4B (Apache 2.0). The 8 GB Qwen3 text encoder stays on disk
        until a prompt needs reading (see encode), like T5 for FLUX.1."""
        import torch
        from diffusers import Flux2KleinInpaintPipeline
        t0 = time.time()
        if job:
            job.progress(0.0, "Loading FLUX.2 [klein] (first time takes a minute)...")
        self.pipe = Flux2KleinInpaintPipeline.from_pretrained(
            self._path("model_dir"), text_encoder=None, torch_dtype=torch.bfloat16)
        self.pipe.to(self.device)
        if hasattr(self.pipe.vae, "enable_tiling"):
            self.pipe.vae.enable_tiling()
        self.pipe.set_progress_bar_config(disable=True)
        log("FLUX.2 [klein] loaded in %.1fs (distilled: %s)", time.time() - t0,
            self.pipe.config.get("is_distilled"))
        self.preload_text_encoder()

    # -- prompt encoding -------------------------------------------------------------------
    # The text encoders (T5 9.5 GB, Qwen3 8 GB) don't fit on a 12 GB GPU next to the
    # transformer. They used to swap places with it, which on a busy machine meant
    # minutes of paging. Now the encoder sits in RAM and streams through the GPU one
    # block at a time while the transformer stays put; results are cached on disk.
    def encode(self, prompt, job):
        if prompt in self.embed_cache:
            return self.embed_cache[prompt]
        result = self.disk_cache.get(prompt)
        if result is not None:
            result = tuple(t.to(self.device) for t in result)
            log("prompt embeddings from the disk cache")
        else:
            job.progress(0.02, "Reading the prompt...")
            t0 = time.time()
            result = self._encode_with_text_encoder(prompt)
            log("prompt read in %.1fs", time.time() - t0)
            self.disk_cache.put(prompt, result)
        if len(self.embed_cache) > 16:
            self.embed_cache.clear()
        self.embed_cache[prompt] = result
        return result

    def _encode_with_text_encoder(self, prompt):
        with self._encoder_lock:          # waits for a background read in progress
            return self._run_text_encoder(prompt)

    def _run_text_encoder(self, prompt):
        import torch
        encoder = self._text_encoder()
        pipe = self.pipe
        t0 = time.time()
        try:
            with torch.no_grad():
                if self.family == KLEIN:
                    pipe.text_encoder = encoder
                    embeds, _ids = pipe.encode_prompt(prompt=prompt, device=self.device,
                                                      max_sequence_length=512)
                    result = (embeds.detach(),)
                else:
                    pipe.text_encoder_2 = encoder
                    pe, ppe, _ids = pipe.encode_prompt(prompt=prompt, prompt_2=prompt,
                                                       device=self.device, max_sequence_length=512)
                    result = (pe.detach(), ppe.detach())
            log("text encoder ran in %.1fs", time.time() - t0)
            return result
        finally:
            if self.family == KLEIN:
                pipe.text_encoder = None
            else:
                pipe.text_encoder_2 = None
            del encoder
            self._maybe_free_text_encoder()

    def _text_encoder(self):
        """The text encoder in RAM, streaming through the GPU (see stream_blocks)."""
        if self.text_encoder is not None:
            return self.text_encoder
        import torch
        log("before text encoder: %s", memory_status())
        t0 = time.time()
        if self.family == KLEIN:
            from transformers import Qwen3ForCausalLM
            enc = Qwen3ForCausalLM.from_pretrained(
                os.path.join(self._path("model_dir"), "text_encoder"), torch_dtype=torch.bfloat16)
            stream_blocks(enc, enc.model.layers, self.device)
        else:
            enc = self._load_t5()
            stream_blocks(enc, enc.encoder.block, self.device)
        enc.eval()
        log("text encoder read into RAM in %.1fs: %s", time.time() - t0, memory_status())
        self.text_encoder = enc
        return enc

    def _load_t5(self):
        import torch
        from transformers import T5EncoderModel
        if not self.manifest.get("t5_file"):
            return T5EncoderModel.from_pretrained(self._path("t5_dir"), torch_dtype=torch.bfloat16)
        # 8-bit GGUF: the weights stay 8-bit in RAM. Its metadata makes transformers build
        # T5 v1.0 (ReLU) instead of v1.1 (gated-GELU), which garbles every prompt, so the
        # config comes from t5_dir.
        from transformers import T5Config
        gguf = os.path.normpath(self._path("t5_file"))
        return T5EncoderModel.from_pretrained(
            os.path.dirname(gguf), gguf_file=os.path.basename(gguf),
            config=T5Config.from_pretrained(self._path("t5_dir")), torch_dtype=torch.bfloat16)

    def _text_encoder_bytes(self):
        """RAM the text encoder takes once loaded."""
        if self.manifest.get("t5_file") and self.family != KLEIN:
            try:
                # Q8_0 is ~8.5 bits a weight on disk and is unpacked to 16-bit on load.
                return int(os.path.getsize(self._path("t5_file")) * 16 / 8.5)
            except OSError:
                return 0
        if self.family == KLEIN:
            folder = os.path.join(self._path("model_dir"), "text_encoder")
        else:
            folder = self._path("t5_dir")
        try:
            return sum(os.path.getsize(os.path.join(folder, f)) for f in os.listdir(folder)
                       if f.endswith(".safetensors"))
        except OSError:
            return 0

    def preload_text_encoder(self):
        """Read the text encoder into RAM in the background right after the model
        loads, so the first prompt doesn't wait for it (~60 s from a hard disk).
        Only when RAM has room for it and still KEEP_TEXT_ENCODER_FREE to spare."""
        free = memory_free()
        need = self._text_encoder_bytes() + KEEP_TEXT_ENCODER_FREE
        if free is None or free[0] < need:
            log("text encoder not read ahead (RAM free %s, needs %.1f GB)",
                "?" if free is None else "%.1f GB" % (free[0] / 1e9), need / 1e9)
            return None

        def read():
            with self._encoder_lock:
                try:
                    self._text_encoder()
                    # Encode the empty prompt (used for removals) if no job is running:
                    # it also warms up the GPU kernels, which saves ~3 s on the first
                    # real prompt. A job waits for this (a few seconds) to share VRAM.
                    if self._gpu_lock.acquire(False):
                        try:
                            self.disk_cache.put("", self._run_text_encoder(""))
                        finally:
                            self._gpu_lock.release()
                    self._maybe_free_text_encoder()
                except Exception:
                    self.text_encoder = None
                    log("reading the text encoder ahead failed (will retry on the first "
                        "prompt):\n%s", traceback.format_exc())

        thread = threading.Thread(target=read, name="text-encoder", daemon=True)
        thread.start()
        return thread

    def _maybe_free_text_encoder(self):
        """Keep the text encoder in RAM for the next prompt only while that leaves
        Windows plenty of room; otherwise give the memory back to Nuke."""
        free = memory_free()
        if free is not None and min(free) >= KEEP_TEXT_ENCODER_FREE:
            return
        self.text_encoder = None
        import gc
        gc.collect()
        log("text encoder freed (RAM is short): %s", memory_status())

    def run_lama(self, image, mask):
        """image: float32 (H, W, 3) in 0..1, mask: PIL 'L'. Returns float32 (H, W, 3)."""
        import numpy as np
        import torch
        import torch.nn.functional as F
        if self.lama is None:
            self.lama = torch.jit.load(self._path("lama"), map_location="cpu").eval()
        model = self.lama.to(self.device)
        try:
            im = np.clip(image, 0.0, 1.0).astype(np.float32)
            mk = (np.asarray(mask.convert("L")) > 127).astype(np.float32)
            h, w = mk.shape
            it = torch.from_numpy(im.transpose(2, 0, 1))[None].to(self.device)
            mt = torch.from_numpy(mk)[None, None].to(self.device)
            ph, pw = (8 - h % 8) % 8, (8 - w % 8) % 8
            if ph or pw:
                it = F.pad(it, (0, pw, 0, ph), mode="reflect")
                mt = F.pad(mt, (0, pw, 0, ph), mode="reflect")
            with torch.no_grad():
                out = model(it, mt)
            out = out[0, :, :h, :w].permute(1, 2, 0).float().cpu().numpy()
            return np.clip(out, 0.0, 1.0).astype(np.float32)
        finally:
            self.lama = model.to("cpu")
            if self.device == "cuda":
                torch.cuda.empty_cache()

    def _flux(self, job, embeds, image, mask, width, height, seed, steps, guidance, strength,
              done_before, total):
        import torch
        pipe = self.pipe

        def on_step(p, i, t, kwargs):
            frac = (done_before + (i + 1) / float(steps)) / total
            job.progress(0.05 + 0.95 * frac, "Generating variation %d of %d (step %d/%d)"
                         % (done_before + 1, total, i + 1, steps))
            if job.cancel.is_set():
                p._interrupt = True
            return kwargs

        t_var = time.time()
        gen = torch.Generator("cpu").manual_seed(int(seed))
        import numpy as np
        image = np.clip(image, 0.0, 1.0).astype(np.float32)   # the model's range
        if self.family == KLEIN:
            # The pipeline works at the input image's own size and shrinks anything over
            # 1 MP itself, which diffusers 0.41 gets wrong for (H, W, C) float arrays. So
            # resize here when needed, pass a (1, H, W, C) batch, and scale the result back.
            kw, kh = klein_size(int(width), int(height))
            k_image, k_mask = image, mask
            if (kw, kh) != (int(width), int(height)):
                log("klein: working at %dx%d instead of %dx%d (1 MP limit)", kw, kh, width, height)
                k_image = resize_float(image, kw, kh)
                k_mask = mask.resize((kw, kh))
            # Distilled: no classifier-free guidance, so guidance_scale is 1.
            out = pipe(prompt_embeds=embeds[0], image=k_image[None], mask_image=k_mask,
                       width=kw, height=kh, strength=float(strength), guidance_scale=1.0,
                       num_inference_steps=int(steps), generator=gen,
                       callback_on_step_end=on_step, output_type="np")
            if (kw, kh) != (int(width), int(height)):
                job.check()
                result = resize_float(np.asarray(out.images[0], dtype=np.float32),
                                      int(width), int(height))
                log("variation %d: %d steps in %.1fs", done_before + 1, steps, time.time() - t_var)
                return np.clip(result, 0.0, 1.0).astype(np.float32)
        else:
            out = pipe(prompt_embeds=embeds[0], pooled_prompt_embeds=embeds[1], image=image,
                       mask_image=mask, width=int(width), height=int(height),
                       strength=float(strength), guidance_scale=float(guidance),
                       num_inference_steps=int(steps), generator=gen, max_sequence_length=512,
                       callback_on_step_end=on_step, output_type="np")
        job.check()
        log("variation %d: %d steps in %.1fs", done_before + 1, steps, time.time() - t_var)
        return np.asarray(out.images[0], dtype=np.float32)    # float, never 8-bit

    def generate(self, job):
        with self._gpu_lock:
            return self._generate(job)

    def _generate(self, job):
        import torch
        from PIL import Image
        m = job.msg
        if self.pipe is None:
            job.progress(0.0, "Loading the AI libraries...")
        self.load(job)
        image = load_image(m["image"])
        mask = Image.open(m["mask"]).convert("L")
        width, height = int(m["width"]), int(m["height"])
        log("input %s: %dx%d %s, range %.3f..%.3f", os.path.basename(m["image"]), image.shape[1],
            image.shape[0], image.dtype, float(image.min()), float(image.max()))
        if image.shape[:2] != (height, width):
            image = resize_float(image, width, height)
        if mask.size != (width, height):
            mask = mask.resize((width, height), Image.NEAREST)
        preview_kind = m.get("preview", "display")
        count, steps = int(m.get("count", 3)), int(m.get("steps", 24))
        seed, guidance = int(m.get("seed", 1)), float(m.get("guidance", 30.0))
        prompt = (m.get("prompt") or "").strip()
        removal = not prompt
        os.makedirs(m["out_dir"], exist_ok=True)
        outputs = []
        try:
            prefill = None
            if removal:
                job.progress(0.01, "Removing with LaMa...")
                prefill = self.run_lama(image, mask)
            embeds = self.encode(prompt, job)
            job.progress(0.05, "Generating variation 1 of %d..." % count)
            for i in range(count):
                job.check()
                if removal and i == 0:
                    result = prefill
                elif removal:
                    result = self._flux(job, embeds, prefill, mask, width, height, seed + i, steps,
                                        guidance, float(m.get("refine_strength", 0.8)), i, count)
                else:
                    result = self._flux(job, embeds, image, mask, width, height, seed + i, steps,
                                        guidance, 1.0, i, count)
                path = os.path.join(m["out_dir"], "local_%02d.tif" % (i + 1))
                save_float_tiff(path, result)
                preview_png(os.path.join(m["out_dir"], "local_%02d_preview.png" % (i + 1)),
                            result, preview_kind)
                outputs.append(path)
                job.result(i, path)
            return outputs
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            raise MemoryError(
                "The GPU ran out of memory. Close other GPU-heavy apps, lower the viewer "
                "resolution, or reinstall Local AI with a smaller model size.")

    def selftest(self, job):
        import numpy as np
        from PIL import Image, ImageDraw
        tmp = os.path.join(self.root, "selftest")
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        img = np.zeros((512, 512, 3), np.float32)
        img[:] = (0.35, 0.47, 0.63)
        img[300:] = (0.27, 0.35, 0.24)
        save_float_tiff(os.path.join(tmp, "src.tif"), img)       # also proves TIFF IO works
        mask = Image.new("L", (512, 512), 0)
        ImageDraw.Draw(mask).ellipse([180, 180, 330, 330], fill=255)
        mask.save(os.path.join(tmp, "mask.png"))
        t0 = time.time()
        job.msg.update(image=os.path.join(tmp, "src.tif"), mask=os.path.join(tmp, "mask.png"),
                       width=512, height=512, count=2, steps=4, seed=1, prompt="",
                       out_dir=tmp)
        files = self.generate(job)
        info = self.info()
        info["selftest_seconds"] = round(time.time() - t0, 1)
        info["files"] = files
        return info


# --------------------------------------------------------------------------
# fake engine (tests; no PyTorch)
# --------------------------------------------------------------------------

class FakeEngine(object):
    def __init__(self, root):
        self.root = root
        self.loaded = False

    def info(self):
        return {"fake": True, "gpu": "FakeGPU", "loaded": self.loaded}

    def load(self, job=None):
        if not self.loaded:
            # Tests set this to stand in for FLUX's 30-60 s load.
            time.sleep(float(os.environ.get("GENFILL_FAKE_LOAD_SECONDS") or 0))
        self.loaded = True

    def generate(self, job):
        m = job.msg
        if m.get("prompt") == "crash":
            os._exit(3)
        if m.get("prompt") == "oom":
            raise MemoryError("The GPU ran out of memory.")
        self.load(job)
        os.makedirs(m["out_dir"], exist_ok=True)
        out = []
        steps = int(m.get("steps", 4))
        for i in range(int(m.get("count", 3))):
            for s in range(steps):
                job.check()
                job.progress((i + (s + 1) / float(steps)) / m.get("count", 3), "fake step")
                time.sleep(float(m.get("step_delay", 0.0)))
            ext = os.path.splitext(m["image"])[1] or ".png"
            path = os.path.join(m["out_dir"], "local_%02d%s" % (i + 1, ext))
            shutil.copyfile(m["image"], path)
            shutil.copyfile(m["image"], os.path.join(m["out_dir"], "local_%02d_preview.png" % (i + 1)))
            out.append(path)
            job.result(i, path)
        return out

    def selftest(self, job):
        return dict(self.info(), selftest_seconds=0.0)


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def preload():
    """Import torch / diffusers / transformers up front, timing each step."""
    send({"event": "status", "message": "Loading the AI libraries (first start can take a minute)..."})
    steps = [
        ("torch", "import torch"),
        ("torch CUDA init", "import torch; torch.cuda.is_available() and torch.cuda.init()"),
        ("transformers", "import transformers; from transformers import CLIPTextModel, T5EncoderModel"),
        ("diffusers", "from diffusers import FluxFillPipeline, FluxTransformer2DModel, AutoencoderKL"),
        ("diffusers klein", "try:\n    from diffusers import Flux2KleinInpaintPipeline\n"
                            "except ImportError:\n    pass"),
        ("PIL / numpy", "import PIL.Image, numpy"),
    ]
    total = time.time()
    for name, code in steps:
        t0 = time.time()
        try:
            exec(code, {})
        except Exception as exc:
            log("preload %s FAILED after %.1fs: %s", name, time.time() - t0, exc)
            return
        log("preload %-16s %.1fs", name, time.time() - t0)
    log("libraries ready in %.1fs", time.time() - total)
    send({"event": "status", "message": "AI libraries loaded."})


def main(argv=None):
    global _proto_out
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--idle", type=float, default=600.0)
    ap.add_argument("--fake", action="store_true")
    args = ap.parse_args(argv)

    # Keep the protocol channel clean: anything printed by libraries (even C
    # code writing to fd 1) goes to stderr instead.
    _proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    engine = FakeEngine(args.root) if args.fake else Engine(args.root)
    jobs = queue.Queue()
    current = [None]
    cancelled_ids = set()     # cancel arrived while the command was still queued
    cancel_lock = threading.Lock()

    def reader():
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                log("bad message: %r", raw[:200])
                continue
            if msg.get("cmd") == "cancel":
                with cancel_lock:
                    job = current[0]
                    if job is not None and (msg.get("target") in (None, job.id)):
                        job.cancel.set()
                    elif msg.get("target"):
                        cancelled_ids.add(msg["target"])
                continue
            jobs.put(msg)
        # Nuke closed the pipe (quit or crashed): stop any running job and exit.
        job = current[0]
        if job is not None:
            job.cancel.set()
        jobs.put(None)

    send({"event": "hello", "protocol": PROTOCOL_VERSION, "pid": os.getpid(), "fake": args.fake})
    log("GenFill helper started (pid %s, idle timeout %ss)", os.getpid(), args.idle)
    # Import the heavy libraries BEFORE a thread starts blocking on stdin. On
    # Windows a pending read on the stdin pipe can stall other work on the
    # process's standard handles; inside the helper, importing PyTorch took
    # minutes while the same import elsewhere took seconds.
    if not args.fake:
        preload()
    threading.Thread(target=reader, name="stdin", daemon=True).start()

    while True:
        try:
            msg = jobs.get(timeout=args.idle if args.idle > 0 else None)
        except queue.Empty:
            log("idle for %ss, exiting to free GPU memory", args.idle)
            send({"event": "bye", "reason": "idle"})
            return 0
        if msg is None:
            log("pipe closed, exiting")
            return 0
        cmd = msg.get("cmd")
        job = Job(msg)
        with cancel_lock:
            current[0] = job
            skip = job.id in cancelled_ids
            cancelled_ids.discard(job.id)
        if skip:
            current[0] = None
            send({"id": job.id, "event": "error", "cancelled": True, "message": "Cancelled."})
            continue
        try:
            if cmd == "shutdown":
                send({"id": job.id, "event": "bye", "reason": "shutdown"})
                return 0
            elif cmd == "ping":
                send({"id": job.id, "event": "pong", "info": engine.info() if args.fake
                      else {"loaded": engine.pipe is not None}})
            elif cmd == "load":
                engine.load(job)
                send({"id": job.id, "event": "done", "info": engine.info()})
            elif cmd == "generate":
                files = engine.generate(job)
                send({"id": job.id, "event": "done", "files": files})
            elif cmd == "selftest":
                send({"id": job.id, "event": "done", "info": engine.selftest(job)})
            else:
                send({"id": job.id, "event": "error", "message": "unknown command %r" % cmd})
        except Cancelled:
            send({"id": job.id, "event": "error", "cancelled": True, "message": "Cancelled."})
        except MemoryError as exc:
            send({"id": job.id, "event": "error", "oom": True, "message": str(exc)})
        except OSError as exc:
            log(traceback.format_exc())
            if getattr(exc, "winerror", None) == 1455 or "1455" in str(exc):
                log("memory at failure: %s", memory_status())
                send({"id": job.id, "event": "error", "oom": True, "message": WINDOWS_MEMORY_MSG})
            else:
                send({"id": job.id, "event": "error",
                      "message": "%s: %s" % (type(exc).__name__, exc)})
        except Exception as exc:
            tb = traceback.format_exc()
            log(tb)
            send({"id": job.id, "event": "error", "message": "%s: %s" % (type(exc).__name__, exc),
                  "traceback": tb})
        finally:
            current[0] = None


if __name__ == "__main__":
    sys.exit(main())
