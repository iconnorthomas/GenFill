"""Backend interface plus small stdlib-only HTTP helpers.

A backend turns a :class:`GenerationRequest` (a crop of the plate, a mask and
a prompt) into a list of PNG images (bytes), one per variation. Backends know
nothing about Nuke; that keeps them testable and lets a future local backend
(ComfyUI, diffusers) drop in alongside the cloud ones.
"""
from __future__ import annotations

import json
import socket
import uuid

try:  # Python 3
    from urllib import request as _urlrequest
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover - Python 2 Nuke is not supported
    raise

from .. import __version__

USER_AGENT = "GenFill-Nuke/%s" % __version__


class BackendError(RuntimeError):
    """Raised with a message suitable for showing an artist."""


class Cancelled(BackendError):
    pass


class GenerationRequest(object):
    def __init__(self, image_path, mask_path, mask_alpha_path, prompt, width, height,
                 seed=0, count=3, negative_prompt="", model="", quality="auto"):
        self.image_path = image_path          # sRGB 8-bit RGB PNG
        self.mask_path = mask_path            # white = fill, black = keep (RGB)
        self.mask_alpha_path = mask_alpha_path  # RGBA, alpha 0 = fill (OpenAI style)
        self.prompt = prompt or ""
        self.width = int(width)
        self.height = int(height)
        self.seed = int(seed)
        self.count = int(count)
        self.negative_prompt = negative_prompt or ""
        self.model = model or ""
        self.quality = quality or "auto"

    def read(self, attr):
        with open(getattr(self, attr), "rb") as fh:
            return fh.read()


class Backend(object):
    id = "base"
    label = "Base"
    env_var = None          # environment variable holding the API key
    needs_key = True
    default_model = ""
    models = ()             # suggestions shown in the UI
    description = ""
    float_io = False        # True: takes 32-bit float TIFF in, returns float TIFF
    result_ext = ".png"

    def model_size(self, width, height, model=""):
        """Resolution to send for a crop of ``width`` x ``height``."""
        raise NotImplementedError

    def generate(self, request, api_key, progress=None, cancelled=None, on_image=None):
        """Return a list of PNG bytes, one per variation.

        Backends that make variations one at a time also call ``on_image(index, item)``
        as each is finished (``item`` as in the returned list), so it can be shown
        straight away. Others may ignore it: the returned list is always complete."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def encode_multipart(fields, files):
    """Encode form ``fields`` {name: str} and ``files`` {name: (filename, bytes, ctype)}."""
    boundary = "----GenFill%s" % uuid.uuid4().hex
    crlf = b"\r\n"
    parts = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(b"--" + boundary.encode())
        parts.append(('Content-Disposition: form-data; name="%s"' % name).encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    for name, (filename, data, ctype) in files.items():
        parts.append(b"--" + boundary.encode())
        parts.append(('Content-Disposition: form-data; name="%s"; filename="%s"'
                      % (name, filename)).encode())
        parts.append(("Content-Type: %s" % ctype).encode())
        parts.append(b"")
        parts.append(data)
    parts.append(b"--" + boundary.encode() + b"--")
    parts.append(b"")
    return crlf.join(parts), "multipart/form-data; boundary=%s" % boundary


def _error_message(status, body):
    text = body.decode("utf-8", "replace") if body else ""
    try:
        data = json.loads(text)
    except ValueError:
        return "HTTP %s: %s" % (status, text[:300] or "no details")
    # OpenAI: {"error": {"message": ...}}; Stability: {"name":..., "errors": [...]}
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return "HTTP %s: %s" % (status, err["message"])
        if data.get("errors"):
            return "HTTP %s: %s" % (status, "; ".join(str(e) for e in data["errors"]))
        if data.get("message"):
            return "HTTP %s: %s" % (status, data["message"])
    return "HTTP %s: %s" % (status, text[:300])


def http_post(url, body, headers, timeout=180):
    """POST and return ``(status, headers_dict, body_bytes)``; raises BackendError."""
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers)
    req = _urlrequest.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with _urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), dict(resp.headers), resp.read()
    except HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        if exc.code in (401, 403):
            raise BackendError("Authentication failed (%s). Check your API key. %s"
                               % (exc.code, _error_message(exc.code, body)))
        if exc.code == 429:
            raise BackendError("Rate limited or out of credits. " + _error_message(exc.code, body))
        raise BackendError(_error_message(exc.code, body))
    except (URLError, socket.timeout) as exc:
        raise BackendError("Network error talking to %s: %s" % (url.split("/")[2], exc))
