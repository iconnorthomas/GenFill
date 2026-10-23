"""Stability AI Stable Image "Edit" API (inpaint + erase).

* With a prompt  -> ``/v2beta/stable-image/edit/inpaint``
* Empty prompt   -> ``/v2beta/stable-image/edit/erase``

The empty-prompt behaviour mirrors Photoshop, where Generative Fill with no
prompt removes the selected content and fills from the surroundings.
One image comes back per request, so N variations = N requests with
consecutive seeds.
"""
from __future__ import annotations

import random

from .base import Backend, BackendError, Cancelled, encode_multipart, http_post
from .. import region

API_ROOT = "https://api.stability.ai/v2beta/stable-image/edit"
MAX_SEED = 4294967294


class StabilityBackend(Backend):
    id = "stability"
    label = "Stability AI (Inpaint / Erase)"
    env_var = "STABILITY_API_KEY"
    description = "True masked inpainting; empty prompt = object removal."
    grow_mask = 5

    def model_size(self, width, height, model=""):
        # ~1 MP, sides on a 64 px grid.
        return region.scale_to_pixel_budget(width, height, 1024 * 1024, 64,
                                            min_side=256, max_aspect=2.5)

    def generate(self, request, api_key, progress=None, cancelled=None, on_image=None):
        if not api_key:
            raise BackendError("No Stability API key set (STABILITY_API_KEY or API Keys...).")
        image = request.read("image_path")
        mask = request.read("mask_path")
        erase = not request.prompt.strip()
        url = "%s/%s" % (API_ROOT, "erase" if erase else "inpaint")
        base_seed = request.seed or random.randint(1, MAX_SEED - request.count)
        results = []
        for i in range(request.count):
            if cancelled and cancelled():
                raise Cancelled("Cancelled")
            if progress:
                progress(float(i) / request.count,
                         "Generating variation %d of %d..." % (i + 1, request.count))
            fields = {
                "seed": (base_seed + i) % MAX_SEED,
                "output_format": "png",
                "grow_mask": self.grow_mask,
            }
            if not erase:
                fields["prompt"] = request.prompt
                if request.negative_prompt:
                    fields["negative_prompt"] = request.negative_prompt
            body, ctype = encode_multipart(fields, {
                "image": ("image.png", image, "image/png"),
                "mask": ("mask.png", mask, "image/png"),
            })
            status, headers, data = http_post(url, body, {
                "Authorization": "Bearer %s" % api_key,
                "Accept": "image/*",
                "Content-Type": ctype,
                "stability-client-id": "GenFill-Nuke",
            })
            if not data.startswith(b"\x89PNG"):
                raise BackendError("Stability returned an unexpected response (%s)."
                                   % headers.get("Content-Type", "unknown type"))
            finish = headers.get("finish-reason") or headers.get("Finish-Reason")
            if finish == "CONTENT_FILTERED":
                raise BackendError("Stability's content filter blocked this result. "
                                   "Try a different prompt.")
            results.append(data)
            if on_image:
                on_image(i, data)
        return results
