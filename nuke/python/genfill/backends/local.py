"""Local generation inside the GenFill plugin (free, offline, no tokens).

Uses the hidden AI helper (``genfill.localai``) with whichever model the Local
AI install holds: FLUX.1 Fill [dev] (FLUX.1 [dev] Non-Commercial License) or
FLUX.2 [klein] 4B (Apache 2.0, commercial use allowed). LaMa (+ a refinement
pass) handles an empty prompt. Installed from Nuke with GenFill > Local AI >
Install, or with GenFill Setup.
"""
from __future__ import annotations

import os
import random

from .base import Backend, BackendError, Cancelled
from .. import colorspace, region

QUALITY_STEPS = {"low": 16, "medium": 24, "auto": 24, "high": 32}
# klein is distilled to 4 steps; a few more only help slightly.
KLEIN_STEPS = {"low": 4, "medium": 4, "auto": 4, "high": 8}
DESCRIPTIONS = {
    "flux1-fill": ("Free and offline on this PC's GPU. FLUX.1 Fill for prompts, LaMa for removal. "
                   "FLUX.1 [dev] license: non-commercial use only."),
    "flux2-klein": ("Free and offline on this PC's GPU. FLUX.2 [klein] 4B for prompts, LaMa for "
                    "removal. Apache 2.0: licensed for commercial and studio work."),
}
MAX_SEED = 2 ** 32 - 1
# Flux2KleinInpaintPipeline shrinks anything over 1024 x 1024 pixels itself (and
# diffusers 0.41 mangles float arrays when it does), so klein stays at or under it.
KLEIN_MAX_PIXELS = 1024 * 1024


class NotInstalled(BackendError):
    pass


def _read_result(path):
    """(float TIFF bytes, 8-bit preview PNG bytes or None) for one helper output."""
    with open(path, "rb") as fh:
        data = fh.read()
    preview = None
    ppath = os.path.splitext(path)[0] + "_preview.png"
    if os.path.exists(ppath):
        with open(ppath, "rb") as fh:
            preview = fh.read()
    return data, preview


class LocalBackend(Backend):
    id = "local"
    label = "Local AI (free, runs on your GPU)"
    # Menu labels saved in older scripts that still mean this backend.
    aliases = ("Local: FLUX.1 Fill (free, runs on your GPU)",)
    needs_key = False
    guidance = 30.0
    float_io = True         # 32-bit float TIFF in and out (no 8-bit rounding)
    result_ext = ".tif"
    refine_strength = 0.8

    def is_installed(self):
        from .. import localai
        return localai.is_installed()

    def family(self):
        from .. import localai
        return localai.family()

    @property
    def description(self):
        return DESCRIPTIONS.get(self.family(), DESCRIPTIONS["flux1-fill"])

    def model_size(self, width, height, model=""):
        # FLUX works best around 1 MP; keep sides on a 64 px grid.
        budget = 1024 * 1024
        while True:
            w, h = region.scale_to_pixel_budget(width, height, budget, 64, min_side=512,
                                                max_side=1536, max_aspect=3.0)
            # Rounding to the grid can land just over 1 MP (e.g. 864 x 1216).
            if self.family() != "flux2-klein" or w * h <= KLEIN_MAX_PIXELS or budget < 256 * 256:
                return w, h
            budget *= 0.96

    def _steps(self, quality):
        table = KLEIN_STEPS if self.family() == "flux2-klein" else QUALITY_STEPS
        return table.get(quality, table["auto"])

    def generate(self, request, api_key, progress=None, cancelled=None, on_image=None):
        from .. import localai
        from ..localai.client import WorkerError, get_client
        if not localai.is_installed():
            raise NotInstalled("Local AI isn't installed yet. Click 'Local AI...' in the node's panel to install it.")
        out_dir = os.path.join(os.path.dirname(request.image_path), "local_out")
        payload = {
            "image": request.image_path,
            "mask": request.mask_path,
            "prompt": request.prompt,
            "width": request.width,
            "height": request.height,
            "count": request.count,
            "seed": request.seed or random.randint(1, MAX_SEED - request.count),
            "steps": self._steps(request.quality),
            "guidance": self.guidance,
            "refine_strength": self.refine_strength,
            "out_dir": out_dir,
            "preview": colorspace.preview_kind(getattr(request, "transfer", None)),
        }
        client = get_client()
        if progress and not client.is_running():
            progress(0.0, "Starting the local AI...")

        def on_result(ev):
            if on_image:
                on_image(int(ev["index"]), _read_result(ev["file"]))

        try:
            done = client.request("generate", payload, progress=progress, cancelled=cancelled,
                                  on_result=on_result)
        except WorkerError as exc:
            if exc.cancelled:
                raise Cancelled("Cancelled")
            try:
                from .. import log as gflog
                from ..localai.client import helper_log_tail
                gflog.error("AI helper error: %s\n--- helper.log (last lines) ---\n%s", exc,
                            helper_log_tail())
            except Exception:
                pass
            raise BackendError(str(exc))
        results = [_read_result(path) for path in done.get("files", [])]
        if not results:
            raise BackendError("The local AI returned no images.")
        return results
