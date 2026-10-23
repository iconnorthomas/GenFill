"""Offline preview backend: no network, no key, no AI.

Fills the selection by diffusing the surrounding colours inward (a cheap
"content-aware" smear) and gives each variation a slightly different tint and
texture. Its job is to let you test the whole Nuke round trip (crop, colour
transform, variations, comp) without spending credits or sending a plate
anywhere.
"""
from __future__ import annotations

import random
import time

from .base import Backend, Cancelled
from .. import pngio, region

GRID = 40


def _grid_fill(img, mask, gw, gh):
    w, h, ch = img.width, img.height, img.channels
    sums = [[0.0, 0.0, 0.0, 0] for _ in range(gw * gh)]
    for y in range(h):
        gy = min(gh - 1, y * gh // h)
        row, mrow = img.rows[y], mask.rows[y]
        for x in range(0, w, 2):
            if mrow[x * mask.channels] >= 128:
                continue
            gx = min(gw - 1, x * gw // w)
            s = sums[gy * gw + gx]
            i = x * ch
            s[0] += row[i]
            s[1] += row[i + 1]
            s[2] += row[i + 2]
            s[3] += 1
    cells = [None if s[3] == 0 else (s[0] / s[3], s[1] / s[3], s[2] / s[3]) for s in sums]
    if all(c is None for c in cells):
        return [(128.0, 128.0, 128.0)] * (gw * gh)
    known = [c is not None for c in cells]
    while not all(c is not None for c in cells):
        nxt = list(cells)
        for gy in range(gh):
            for gx in range(gw):
                k = gy * gw + gx
                if cells[k] is not None:
                    continue
                acc = [0.0, 0.0, 0.0]
                n = 0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < gw and 0 <= ny < gh and cells[ny * gw + nx] is not None:
                        c = cells[ny * gw + nx]
                        acc[0] += c[0]
                        acc[1] += c[1]
                        acc[2] += c[2]
                        n += 1
                if n:
                    nxt[k] = (acc[0] / n, acc[1] / n, acc[2] / n)
        cells = nxt
    # Relax the filled cells so the smear is smooth.
    for _ in range(12):
        nxt = list(cells)
        for gy in range(gh):
            for gx in range(gw):
                k = gy * gw + gx
                if known[k]:
                    continue
                acc = [0.0, 0.0, 0.0]
                n = 0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < gw and 0 <= ny < gh:
                        c = cells[ny * gw + nx]
                        acc[0] += c[0]
                        acc[1] += c[1]
                        acc[2] += c[2]
                        n += 1
                nxt[k] = (acc[0] / n, acc[1] / n, acc[2] / n)
        cells = nxt
    return cells


def _sample(cells, gw, gh, fx, fy):
    x0 = int(fx)
    y0 = int(fy)
    x1 = min(gw - 1, x0 + 1)
    y1 = min(gh - 1, y0 + 1)
    tx, ty = fx - x0, fy - y0
    a, b = cells[y0 * gw + x0], cells[y0 * gw + x1]
    c, d = cells[y1 * gw + x0], cells[y1 * gw + x1]
    return [
        (a[i] * (1 - tx) + b[i] * tx) * (1 - ty) + (c[i] * (1 - tx) + d[i] * tx) * ty
        for i in range(3)
    ]


def render_variation(img, mask, cells, gw, gh, seed):
    rng = random.Random(seed)
    tint = [1.0 + rng.uniform(-0.08, 0.08) for _ in range(3)]
    freq = rng.uniform(0.02, 0.06)
    amp = rng.uniform(2.0, 7.0)
    out = pngio.Image(img.width, img.height, 3)
    w, h, ch, mch = img.width, img.height, img.channels, mask.channels
    sx = (gw - 1) / float(max(1, w - 1))
    sy = (gh - 1) / float(max(1, h - 1))
    for y in range(h):
        src, mrow, dst = img.rows[y], mask.rows[y], out.rows[y]
        for x in range(w):
            i = x * ch
            o = x * 3
            m = mrow[x * mch]
            if m == 0:
                dst[o], dst[o + 1], dst[o + 2] = src[i], src[i + 1], src[i + 2]
                continue
            fill = _sample(cells, gw, gh, x * sx, y * sy)
            tex = amp * (((x + y) * freq) % 1.0 - 0.5)
            a = m / 255.0
            for c in range(3):
                v = fill[c] * tint[c] + tex
                v = src[i + c] * (1.0 - a) + v * a
                dst[o + c] = 0 if v < 0 else (255 if v > 255 else int(v))
    return pngio.encode(out)


class MockBackend(Backend):
    id = "mock"
    label = "Offline Preview (no AI)"
    needs_key = False
    description = "Tests the pipeline locally. Smears surrounding colour into the selection."

    def model_size(self, width, height, model=""):
        return region.scale_to_pixel_budget(width, height, 384 * 384, 8, min_side=64)

    def generate(self, request, api_key, progress=None, cancelled=None, on_image=None):
        img = pngio.read(request.image_path)
        mask = pngio.read(request.mask_path)
        aspect = img.width / float(img.height)
        gw = GRID if aspect >= 1 else max(4, int(GRID * aspect))
        gh = GRID if aspect < 1 else max(4, int(GRID / aspect))
        cells = _grid_fill(img, mask, gw, gh)
        seed = request.seed or random.randint(1, 1 << 30)
        out = []
        for i in range(request.count):
            if cancelled and cancelled():
                raise Cancelled("Cancelled")
            if progress:
                progress(float(i) / request.count,
                         "Preview variation %d of %d..." % (i + 1, request.count))
            out.append(render_variation(img, mask, cells, gw, gh, seed + i))
            if on_image:
                on_image(i, out[-1])
            time.sleep(0.05)
        return out
