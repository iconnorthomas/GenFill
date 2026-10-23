"""OpenAI Images API (``POST /v1/images/edits``) with a mask.

GPT image models treat the mask as guidance and may repaint the whole crop;
that's fine here because GenFill only composites the result back inside the
(feathered) selection, exactly like Photoshop's generative layer mask.
"""
from __future__ import annotations

import base64
import json

from .base import Backend, BackendError, Cancelled, encode_multipart, http_post
from .. import region

URL = "https://api.openai.com/v1/images/edits"

# Models limited to three fixed output sizes.
FIXED_SIZE_MODELS = ("gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5", "chatgpt-image-latest")
FIXED_SIZES = [(1024, 1024), (1536, 1024), (1024, 1536)]
FIDELITY_MODELS = ("gpt-image-1", "gpt-image-1.5")

EMPTY_PROMPT = ("Remove whatever is inside the masked area and fill it so it seamlessly "
                "matches the surrounding image, as a clean plate. Keep lighting, grain, "
                "perspective and focus consistent. Change nothing outside the mask.")
PROMPT_SUFFIX = (" Only change the masked area; match the lighting, grain, perspective and "
                 "focus of the surrounding photo.")


class OpenAIBackend(Backend):
    id = "openai"
    label = "OpenAI GPT Image"
    env_var = "OPENAI_API_KEY"
    default_model = "gpt-image-2"
    models = ("gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini")
    description = "Strong prompt following; re-renders the crop, comped through the mask."

    def _model(self, model):
        return (model or self.default_model).strip()

    def model_size(self, width, height, model=""):
        m = self._model(model)
        if m in FIXED_SIZE_MODELS:
            return region.closest_size(width, height, FIXED_SIZES)
        # gpt-image-2+: any size, sides divisible by 16, aspect 1:3..3:1, <= 3840x2160.
        return region.scale_to_pixel_budget(width, height, 1536 * 1024, 16, min_side=256,
                                            max_side=2560, max_aspect=3.0)

    def generate(self, request, api_key, progress=None, cancelled=None, on_image=None):
        # One request returns every variation at once, so there's nothing to stream.
        if not api_key:
            raise BackendError("No OpenAI API key set (OPENAI_API_KEY or API Keys...).")
        if cancelled and cancelled():
            raise Cancelled("Cancelled")
        model = self._model(request.model)
        prompt = request.prompt.strip()
        prompt = EMPTY_PROMPT if not prompt else prompt + PROMPT_SUFFIX
        fields = {
            "model": model,
            "prompt": prompt,
            "n": request.count,
            "size": "%dx%d" % (request.width, request.height),
            "output_format": "png",
        }
        if request.quality and request.quality != "auto":
            fields["quality"] = request.quality
        if model in FIDELITY_MODELS:
            fields["input_fidelity"] = "high"
        body, ctype = encode_multipart(fields, {
            "image": ("image.png", request.read("image_path"), "image/png"),
            "mask": ("mask.png", request.read("mask_alpha_path"), "image/png"),
        })
        if progress:
            progress(0.1, "Generating %d variation%s with %s..."
                     % (request.count, "s" if request.count != 1 else "", model))
        status, headers, data = http_post(URL, body, {
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": ctype,
        }, timeout=600)
        try:
            payload = json.loads(data.decode("utf-8"))
            items = payload["data"]
        except (ValueError, KeyError, TypeError):
            raise BackendError("OpenAI returned an unexpected response.")
        results = []
        for item in items:
            b64 = item.get("b64_json")
            if not b64:
                raise BackendError("OpenAI response had no image data.")
            results.append(base64.b64decode(b64))
        if not results:
            raise BackendError("OpenAI returned no images (it may have refused the prompt).")
        return results
