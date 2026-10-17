"""Unit tests for the pure-Python parts. Run: python -m unittest discover tests"""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill import pngio, region, state  # noqa: E402
from genfill import backends  # noqa: E402
from genfill.backends import base, openai_images, stability  # noqa: E402


def _square_mask(w, h, x0, y0, x1, y1, channels=3):
    img = pngio.Image(w, h, channels)
    for y in range(y0, y1):
        for x in range(x0, x1):
            img.rows[y][x * channels:(x + 1) * channels] = b"\xff" * channels
    return img


class PngTests(unittest.TestCase):
    def test_roundtrip_all_channel_counts(self):
        for ch in (1, 2, 3, 4):
            img = pngio.Image(7, 5, ch)
            for y in range(5):
                for i in range(7 * ch):
                    img.rows[y][i] = (y * 31 + i * 17) % 256
            back = pngio.read(pngio.encode(img))
            self.assertEqual((back.width, back.height, back.channels), (7, 5, ch))
            self.assertEqual(back.rows, img.rows)

    def test_filters_decode(self):
        # zlib-level: build a PNG using all five filter types by hand.
        import struct
        import zlib
        w, h = 4, 5
        rows = [bytes([10 * y + x for x in range(w * 3)]) for y in range(h)]
        raw = b"".join(bytes([0]) + r for r in rows[:1])
        # Sub filter for row 1
        r = rows[1]
        sub = bytes([r[i] - (r[i - 3] if i >= 3 else 0) & 0xFF for i in range(len(r))])
        raw += b"\x01" + sub
        # Up filter for row 2
        r, p = rows[2], rows[1]
        raw += b"\x02" + bytes([(r[i] - p[i]) & 0xFF for i in range(len(r))])
        # Average for row 3
        r, p = rows[3], rows[2]
        avg = bytes([(r[i] - (((r[i - 3] if i >= 3 else 0) + p[i]) >> 1)) & 0xFF
                     for i in range(len(r))])
        raw += b"\x03" + avg
        # Paeth for row 4
        r, p = rows[4], rows[3]
        out = bytearray()
        for i in range(len(r)):
            a = r[i - 3] if i >= 3 else 0
            c = p[i - 3] if i >= 3 else 0
            out.append((r[i] - pngio._paeth(a, p[i], c)) & 0xFF)
        raw += b"\x04" + bytes(out)

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(
                ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        img = pngio.read(png)
        self.assertEqual([bytes(r) for r in img.rows], rows)
        self.assertEqual(pngio.size_of(png), (4, 5))


class RegionTests(unittest.TestCase):
    def test_mask_bbox(self):
        img = _square_mask(20, 10, 5, 2, 9, 7)
        self.assertEqual(region.mask_bbox(img), (5, 2, 9, 7))
        self.assertIsNone(region.mask_bbox(pngio.Image(4, 4, 3)))

    def test_probe_flip_to_nuke_space(self):
        # Selection at top of a 100x50 probe -> high Y in Nuke space.
        box = region.probe_bbox_to_format((10, 0, 20, 10), 100, 50, 1000, 500)
        self.assertLessEqual(box.x, 100)
        self.assertGreaterEqual(box.r, 200)
        self.assertEqual(box.t, 500)          # touches the top
        self.assertLessEqual(box.y, 400)

    def test_expand_region_inside_format_and_aspect(self):
        box = region.Box(900, 500, 1000, 600)
        r = region.expand_region(box, 1920, 1080, context=0.35, min_size=512, aspect=1.5)
        self.assertGreaterEqual(r.x, 0)
        self.assertGreaterEqual(r.y, 0)
        self.assertLessEqual(r.r, 1920)
        self.assertLessEqual(r.t, 1080)
        self.assertLessEqual(r.x, box.x)
        self.assertGreaterEqual(r.r, box.r)
        self.assertAlmostEqual(r.aspect, 1.5, delta=0.01)
        self.assertGreaterEqual(min(r.w, r.h), 512)

    def test_expand_region_clamps_at_edges(self):
        box = region.Box(0, 0, 50, 50)
        r = region.expand_region(box, 1920, 1080, aspect=1.0)
        self.assertEqual((r.x, r.y), (0, 0))
        box = region.Box(1900, 1060, 1920, 1080)
        r = region.expand_region(box, 1920, 1080, aspect=1.0)
        self.assertEqual((r.r, r.t), (1920, 1080))

    def test_expand_region_huge_selection(self):
        box = region.Box(0, 0, 1920, 1080)
        r = region.expand_region(box, 1920, 1080, aspect=1.0)
        self.assertEqual(r, region.Region(0, 0, 1920, 1080))

    def test_pixel_budget(self):
        w, h = region.scale_to_pixel_budget(1920, 1080, 1024 * 1024, 64)
        self.assertEqual(w % 64, 0)
        self.assertEqual(h % 64, 0)
        self.assertAlmostEqual(w / float(h), 16 / 9.0, delta=0.1)
        w, h = region.scale_to_pixel_budget(5000, 100, 1024 * 1024, 16, max_aspect=3.0)
        self.assertLessEqual(w / float(h), 3.05)

    def test_closest_size(self):
        self.assertEqual(region.closest_size(1920, 1080, openai_images.FIXED_SIZES), (1536, 1024))
        self.assertEqual(region.closest_size(500, 1000, openai_images.FIXED_SIZES), (1024, 1536))


class StateTests(unittest.TestCase):
    def _gen(self, n):
        return {"id": "g%d" % n, "prompt": "p%d" % n, "files": ["g%d/v1" % n, "g%d/v2" % n,
                                                               "g%d/v3" % n],
                "region": [0, 0, 10, 10], "frame": 1}

    def test_add_select_step(self):
        st = state.empty()
        st = state.add_generation(st, self._gen(1))
        st = state.add_generation(st, self._gen(2))
        self.assertEqual(state.selected(st), (1, 0))
        order = state.variations(st)
        self.assertEqual(order[:3], [(1, 0), (1, 1), (1, 2)])   # newest first
        st = state.step(st, 1)
        self.assertEqual(state.selected(st), (1, 1))
        st = state.step(st, -2)
        self.assertEqual(state.selected(st), (0, 2))             # wraps

    def test_remove(self):
        st = state.empty()
        for i in range(2):
            st = state.add_generation(st, self._gen(i))
        st = state.select(st, 1, 2)
        st, removed = state.remove_variation(st, 1, 0)
        self.assertEqual(removed, "g1/v1")
        self.assertEqual(state.selected(st), (1, 1))
        # Remove a whole earlier generation; selection index shifts down.
        for _ in range(3):
            st, _ = state.remove_variation(st, 0, 0)
        self.assertEqual(state.selected(st), (0, 1))
        self.assertEqual(len(st["generations"]), 1)
        st, _ = state.remove_variation(st, 0, 1)   # remove the selected one
        self.assertEqual(state.selected(st), (0, 0))
        st, _ = state.remove_variation(st, 0, 0)
        self.assertIsNone(state.selected(st))

    def test_loads_tolerates_garbage(self):
        self.assertEqual(state.loads("")["generations"], [])
        self.assertEqual(state.loads("{nope")["generations"], [])
        st = state.add_generation(state.empty(), self._gen(1))
        self.assertEqual(state.loads(state.dumps(st)), st)
        bad = state.loads('{"generations": {"a": 1}, "selected": [1]}')
        self.assertEqual(bad["generations"], [])
        self.assertIsNone(state.selected(bad))
        bad = state.loads('{"generations": [3, {"files": ["a"]}], "selected": "x"}')
        self.assertEqual(bad["generations"], [{"files": ["a"]}])
        self.assertIsNone(state.selected(bad))

    def test_generation_numbers_are_not_reused(self):
        st = state.empty()
        self.assertEqual(state.next_generation_number(st), 1)
        for n in (1, 2, 3):
            st = state.add_generation(st, dict(self._gen(n), id="g%03d_20261012-2015%02d" % (n, n)))
        for _ in range(3):
            st, _ = state.remove_variation(st, 1, 0)     # drop g002
        self.assertEqual(state.next_generation_number(st), 4)


class _Req(object):
    pass


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        img = pngio.Image(64, 48, 3)
        for y in range(48):
            for x in range(64):
                img.rows[y][x * 3:x * 3 + 3] = bytes([x * 4, y * 5, 120])
        self.img = os.path.join(self.tmp, "img.png")
        pngio.write(self.img, img)
        self.mask = os.path.join(self.tmp, "mask.png")
        pngio.write(self.mask, _square_mask(64, 48, 16, 12, 48, 36))
        self.mask_a = os.path.join(self.tmp, "mask_a.png")
        pngio.write(self.mask_a, _square_mask(64, 48, 16, 12, 48, 36, channels=4))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _request(self, prompt="a red car", count=3):
        return base.GenerationRequest(self.img, self.mask, self.mask_a, prompt, 64, 48,
                                      seed=7, count=count)

    def test_registry(self):
        self.assertEqual(backends.ids(), ["local", "stability", "openai", "mock"])
        self.assertEqual(backends.get("mock").label, "Offline Preview (no AI)")

    def test_multipart(self):
        body, ctype = base.encode_multipart({"a": "1", "skip": None},
                                            {"f": ("x.png", b"\x89PNGdata", "image/png")})
        boundary = ctype.split("boundary=")[1]
        self.assertIn(b'name="a"\r\n\r\n1', body)
        self.assertNotIn(b"skip", body)
        self.assertIn(b'filename="x.png"\r\nContent-Type: image/png\r\n\r\n\x89PNGdata', body)
        self.assertTrue(body.endswith(("--%s--\r\n" % boundary).encode()))

    def test_stability_inpaint_and_erase(self):
        calls = []
        png = pngio.encode(pngio.Image(8, 8, 3))

        def fake_post(url, body, headers, timeout=180):
            calls.append((url, body, headers))
            return 200, {"Content-Type": "image/png", "finish-reason": "SUCCESS"}, png

        with mock.patch.object(stability, "http_post", fake_post):
            out = stability.StabilityBackend().generate(self._request(), "KEY")
            self.assertEqual(len(out), 3)
            self.assertTrue(all(c[0].endswith("/inpaint") for c in calls))
            self.assertEqual(calls[0][2]["Authorization"], "Bearer KEY")
            self.assertEqual(calls[0][2]["Accept"], "image/*")
            self.assertIn(b'name="seed"\r\n\r\n7', calls[0][1])
            self.assertIn(b'name="seed"\r\n\r\n9', calls[2][1])
            self.assertIn(b'name="prompt"\r\n\r\na red car', calls[0][1])
            calls[:] = []
            stability.StabilityBackend().generate(self._request(prompt="  ", count=1), "KEY")
            self.assertTrue(calls[0][0].endswith("/erase"))
            self.assertNotIn(b'name="prompt"', calls[0][1])

    def test_stability_requires_key(self):
        with self.assertRaises(base.BackendError):
            stability.StabilityBackend().generate(self._request(), "")

    def test_openai_request_and_decode(self):
        captured = {}
        png = pngio.encode(pngio.Image(8, 8, 3))
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(png).decode()}] * 3})

        def fake_post(url, body, headers, timeout=180):
            captured.update(url=url, body=body, headers=headers)
            return 200, {}, payload.encode()

        b = openai_images.OpenAIBackend()
        req = self._request()
        req.model = "gpt-image-1.5"
        req.width, req.height = b.model_size(1920, 1080, req.model)
        with mock.patch.object(openai_images, "http_post", fake_post):
            out = b.generate(req, "SK")
        self.assertEqual(out, [png] * 3)
        self.assertEqual(captured["url"], "https://api.openai.com/v1/images/edits")
        body = captured["body"]
        self.assertIn(b'name="size"\r\n\r\n1536x1024', body)
        self.assertIn(b'name="n"\r\n\r\n3', body)
        self.assertIn(b'name="input_fidelity"\r\n\r\nhigh', body)
        self.assertIn(b'name="mask"; filename="mask.png"', body)

    def test_openai_flexible_size(self):
        w, h = openai_images.OpenAIBackend().model_size(2000, 800, "gpt-image-2")
        self.assertEqual((w % 16, h % 16), (0, 0))
        self.assertLessEqual(max(w, h), 2560)

    def test_http_error_messages(self):
        self.assertIn("bad mask", base._error_message(400, b'{"error":{"message":"bad mask"}}'))
        self.assertIn("too big", base._error_message(400, b'{"name":"x","errors":["too big"]}'))
        self.assertIn("HTTP 500", base._error_message(500, b"<html>"))

    def test_mock_backend(self):
        out = backends.get("mock").generate(self._request(count=2), "")
        self.assertEqual(len(out), 2)
        a, b = pngio.read(out[0]), pngio.read(out[1])
        self.assertEqual((a.width, a.height), (64, 48))
        src = pngio.read(self.img)
        # Outside the mask is untouched; inside differs between variations.
        self.assertEqual(a.rows[0], src.rows[0])
        self.assertNotEqual(a.rows[24], b.rows[24])


if __name__ == "__main__":
    unittest.main()
