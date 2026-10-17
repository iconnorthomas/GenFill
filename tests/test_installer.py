"""Tests for the in-Nuke Local AI installer (no network, no GPU)."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill import config, localai  # noqa: E402
from genfill.localai import installer as inst  # noqa: E402

PAYLOAD = bytes(range(256)) * 4000   # ~1 MB


class RangeServer(BaseHTTPRequestHandler):
    drop_first = [True]
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        RangeServer.requests.append(rng)
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        body = PAYLOAD[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if RangeServer.drop_first[0]:
            RangeServer.drop_first[0] = False
            self.wfile.write(body[:300000])     # connection drops part way
            return
        self.wfile.write(body)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.events = []
        self.inst = inst.Installer(base=os.path.join(self.tmp, "GenFill-LocalAI"), quant="Q4_K_S",
                                   on_event=lambda k, **kw: self.events.append((k, kw)))
        os.makedirs(self.inst.base)
        self._sleep = mock.patch.object(inst, "_sleep", lambda s: None)   # no backoff waits
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_download_resumes_after_drop(self):
        RangeServer.drop_first[0] = True
        RangeServer.requests[:] = []
        srv = HTTPServer(("127.0.0.1", 0), RangeServer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            dest = os.path.join(self.inst.base, "models", "x.bin")
            self.inst.download("http://127.0.0.1:%d/x" % srv.server_address[1], dest, "x")
            with open(dest, "rb") as fh:
                self.assertEqual(fh.read(), PAYLOAD)
            self.assertEqual(RangeServer.requests[0], None)
            self.assertEqual(RangeServer.requests[1], "bytes=300000-")
            self.assertFalse(os.path.exists(dest + ".part"))
            # already complete -> no request at all
            n = len(RangeServer.requests)
            self.inst.download("http://127.0.0.1:%d/x" % srv.server_address[1], dest, "x")
            self.assertEqual(len(RangeServer.requests), n)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_recommended_quant(self):
        self.assertEqual(inst.recommended_quant(12.0), "Q4_K_S")    # 3080 Ti shares with Nuke
        self.assertEqual(inst.recommended_quant(16.0), "Q6_K")
        self.assertEqual(inst.recommended_quant(24.0), "Q8_0")
        self.assertEqual(inst.recommended_quant(None), "Q4_K_S")

    def test_model_files_are_public_and_complete(self):
        files = inst.model_files("Q4_K_S")
        rels = [f[0] for f in files]
        for needed in ("models/flux/flux1-fill-dev-Q4_K_S.gguf", "models/vae/ae.safetensors",
                       "models/lama/big-lama.pt", "models/t5/model.safetensors",
                       "models/t5/spiece.model", "models/clip/model.safetensors",
                       "models/clip/vocab.json"):
            self.assertIn(needed, rels)
        for _rel, url, _gb, sha256 in files:
            self.assertNotIn("black-forest-labs", url)   # gated repos need a login
            self.assertNotIn("/resolve/main/", url)      # pinned so hashes stay valid
            if sha256 is not None:
                self.assertRegex(sha256, "^[0-9a-f]{64}$")
        self.assertGreater(inst.estimate_gb("Q4_K_S"), 20)
        for quant in inst.QUANTS:
            self.assertRegex(inst.model_files(quant)[0][3], "^[0-9a-f]{64}$")
        self.assertNotIn("latest", inst.UV_URL)
        self.assertIn(inst.UV_VERSION, inst.UV_URL)
        self.assertRegex(inst.UV_SHA256, "^[0-9a-f]{64}$")

    def _serve_payload(self):
        RangeServer.drop_first[0] = False
        RangeServer.requests[:] = []
        srv = HTTPServer(("127.0.0.1", 0), RangeServer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d/x" % srv.server_address[1]

    def test_download_accepts_matching_sha256(self):
        url = self._serve_payload()
        dest = os.path.join(self.inst.base, "models", "x.bin")
        self.inst.download(url, dest, "x", sha256=hashlib.sha256(PAYLOAD).hexdigest().upper())
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), PAYLOAD)
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_download_rejects_mismatching_sha256(self):
        url = self._serve_payload()
        dest = os.path.join(self.inst.base, "models", "x.bin")
        with self.assertRaises(inst.InstallError) as ctx:
            self.inst.download(url, dest, "x", sha256=hashlib.sha256(b"something else").hexdigest())
        self.assertIn("SHA-256", str(ctx.exception))
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))
        self.assertEqual(len(RangeServer.requests), 1)   # no retry loop on a bad hash

    def test_configure_writes_configs_manifest_and_config(self):
        cfg_dir = os.path.join(self.tmp, "nukecfg")
        with mock.patch.object(config, "CONFIG_DIR", cfg_dir), \
                mock.patch.object(config, "CONFIG_PATH", os.path.join(cfg_dir, "config.json")):
            config.save({"api_keys": {"openai": "k"}, "local_ai": {"url": "http://old-comfyui"}})
            self.inst.step_configure()
            data = config.load()
        self.assertEqual(data["local_ai"], {"root": self.inst.base})
        self.assertEqual(data["api_keys"], {"openai": "k"})            # keys preserved
        m = localai.manifest(self.inst.base)
        self.assertEqual(m["unet"], "models/flux/flux1-fill-dev-Q4_K_S.gguf")
        self.assertFalse(m["verified"])
        t = json.load(open(os.path.join(self.inst.base, "models/flux/config/transformer/config.json")))
        self.assertEqual((t["in_channels"], t["out_channels"], t["num_single_layers"]), (384, 64, 38))
        v = json.load(open(os.path.join(self.inst.base, "models/flux/config/vae/config.json")))
        self.assertEqual((v["latent_channels"], v["scaling_factor"], v["shift_factor"]),
                         (16, 0.3611, 0.1159))

    def test_check_refuses_onedrive_and_cleans_comfyui(self):
        od = inst.Installer(base=os.path.join(self.tmp, "OneDrive", "GenFill-LocalAI"))
        with self.assertRaises(inst.InstallError):
            od.step_check()
        os.makedirs(os.path.join(self.inst.base, "ComfyUI_windows_portable", "x"))
        os.makedirs(os.path.join(self.inst.base, "_downloads"))
        Usage = namedtuple("Usage", "total used free")
        with mock.patch.object(inst, "detect_gpu", return_value=("RTX 3080 Ti", 12.0)), \
                mock.patch.object(inst.shutil, "disk_usage", return_value=Usage(0, 0, 100e9)):
            self.inst.quant = None
            self.inst.step_check()
        self.assertEqual(self.inst.quant, "Q4_K_S")
        self.assertFalse(os.path.exists(os.path.join(self.inst.base, "ComfyUI_windows_portable")))
        self.assertFalse(os.path.exists(os.path.join(self.inst.base, "_downloads")))

    def test_check_disk_space(self):
        Usage = namedtuple("Usage", "total used free")
        with mock.patch.object(inst, "detect_gpu", return_value=(None, None)), \
                mock.patch.object(inst.shutil, "disk_usage", return_value=Usage(0, 0, 5e9)):
            with self.assertRaises(inst.InstallError) as ctx:
                self.inst.step_check()
        self.assertIn("disk space", str(ctx.exception))

    def test_run_cmd_streams_and_fails(self):
        self.inst.run_cmd([sys.executable, "-c", "print('hello from step')"], "Test")
        self.assertTrue(any(k == "log" and "hello from step" in kw["line"] for k, kw in self.events))
        with self.assertRaises(inst.InstallError):
            self.inst.run_cmd([sys.executable, "-c", "import sys; sys.exit(3)"], "Failing")

    def test_run_events_success_and_failure(self):
        steps = [k for k, _ in inst.Installer.STEPS]
        with mock.patch.multiple(inst.Installer, **dict(("step_" + k, lambda self: None)
                                                        for k in steps)):
            self.assertTrue(self.inst.run())
        states = [kw["state"] for k, kw in self.events if k == "step"]
        self.assertEqual(states, ["running", "done"] * len(steps))
        self.assertTrue(self.events[-1][1]["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.inst.base, "logs", "install.log")))

        self.events[:] = []

        def boom(self):
            raise inst.InstallError("disk on fire")
        patches = dict(("step_" + k, lambda self: None) for k in steps)
        patches["step_torch"] = boom
        with mock.patch.multiple(inst.Installer, **patches):
            self.assertFalse(self.inst.run())
        fin = self.events[-1][1]
        self.assertFalse(fin["ok"])
        self.assertIn("disk on fire", fin["message"])
        failed = [kw for k, kw in self.events if k == "step" and kw["state"] == "failed"]
        self.assertEqual(failed[0]["index"], steps.index("torch"))

    def _runtime_layout(self):
        base = self.inst.base
        home = os.path.join(base, "python", "cpython-3.12")
        torch_lib = os.path.join(base, "env", "Lib", "site-packages", "torch", "lib")
        for d in (home, torch_lib, os.path.join(base, "env", "Scripts")):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(base, "env", "pyvenv.cfg"), "w") as fh:
            fh.write("home = %s\nversion_info = 3.12.15\n" % home)
        for n in ("msvcp140.dll", "vcruntime140_1.dll"):      # from msvc-runtime
            open(os.path.join(base, "env", n), "wb").write(b"pkg")
        self.inst.platform = "win32"
        self.inst.uv = "uv.exe"
        return home, torch_lib

    def _versions(self, system, private, existing=None):
        existing = existing or {}

        def fv(path):
            if not path or not os.path.exists(path) and "System32" not in path:
                return None
            if "System32" in path:
                return system
            if os.sep + "env" + os.sep + os.path.basename(path) in path and \
                    os.path.dirname(path).endswith("env"):
                return private
            return existing.get(os.path.basename(path), private)
        return fv

    def test_private_runtime_always_placed(self):
        """The real 3080 Ti machine: Windows had 14.50, PyTorch only worked with 14.44."""
        home, torch_lib = self._runtime_layout()
        removed_by_043 = os.path.join(torch_lib, "msvcp140.dll")
        self.assertFalse(os.path.exists(removed_by_043))
        with mock.patch.object(inst, "file_version", self._versions("14.50.35719.0", "14.44.35211.0")):
            self.inst.fix_runtime()
        for target in (home, torch_lib):
            for n in ("msvcp140.dll", "vcruntime140_1.dll"):
                self.assertTrue(os.path.exists(os.path.join(target, n)), (target, n))

    def test_private_runtime_when_windows_is_older_or_missing(self):
        home, torch_lib = self._runtime_layout()
        with mock.patch.object(inst, "file_version", lambda p: None):
            self.inst.fix_runtime()
        self.assertTrue(os.path.exists(os.path.join(torch_lib, "msvcp140.dll")))

    def test_cpu_torch_is_replaced_from_cuda_index_only(self):
        self.inst.platform = "win32"
        self.inst.uv = "uv.exe"
        self.inst.gpu = ("NVIDIA GeForce RTX 3080 Ti", 12.0)
        statuses = iter([
            {"version": "2.14.1+cpu", "cuda": None, "available": False},   # what was installed
            {"version": "2.14.1+cu130", "cuda": "13.0", "available": True},
        ])
        cmds = []

        def fake_run(self_, cmd, label, extra_env=None):
            cmds.append(cmd)
            if cmd[-1].endswith("cu130") and len(cmds) == 1:
                raise inst.InstallError("network hiccup")              # first index fails
        with mock.patch.object(inst.Installer, "run_cmd", fake_run), \
                mock.patch.object(inst.Installer, "torch_status", lambda self_: next(statuses)):
            self.inst.step_torch()
        self.assertEqual(len(cmds), 2)
        for cmd in cmds:
            self.assertIn("--reinstall-package", cmd)
            self.assertNotIn("--extra-index-url", cmd)
            self.assertNotIn("https://pypi.org/simple", cmd)
        self.assertTrue(cmds[0][-1].endswith("/cu130"))
        self.assertTrue(cmds[1][-1].endswith("/cu128"))

    def test_gpu_torch_already_ok_is_left_alone(self):
        self.inst.uv = "uv.exe"
        with mock.patch.object(inst.Installer, "torch_status",
                               lambda self_: {"version": "2.14.1+cu130", "cuda": "13.0",
                                              "available": True}), \
                mock.patch.object(inst.Installer, "run_cmd",
                                  lambda *a, **k: self.fail("should not reinstall")):
            self.inst.step_torch()

    def test_runtime_step_refuses_cpu_torch(self):
        self._runtime_layout()
        with mock.patch.object(inst.Installer, "run_cmd", lambda *a, **k: None), \
                mock.patch.object(inst.Installer, "fix_runtime", lambda self_: None), \
                mock.patch.object(inst.Installer, "torch_status",
                                  lambda self_: {"version": "2.14.1+cpu", "cuda": None,
                                                 "available": False}):
            with self.assertRaises(inst.InstallError) as ctx:
                self.inst.step_runtime()
        self.assertIn("GPU", str(ctx.exception))

    def test_runtime_step_explains_import_failure(self):
        self._runtime_layout()
        with mock.patch.object(inst.Installer, "run_cmd", lambda *a, **k: None), \
                mock.patch.object(inst.Installer, "fix_runtime", lambda self_: None), \
                mock.patch.object(inst.Installer, "torch_status", lambda self_: None):
            with self.assertRaises(inst.InstallError) as ctx:
                self.inst.step_runtime()
        self.assertIn("vc_redist.x64.exe", str(ctx.exception))

    def _write_safetensors(self, path, tensors):
        import struct
        header, blob, off = {"__metadata__": {"format": "pt"}}, b"", 0
        for name, data in tensors:
            header[name] = {"dtype": "BF16", "shape": [len(data) // 2], "data_offsets": [off, off + len(data)]}
            blob += data
            off += len(data)
        hb = json.dumps(header).encode()
        with open(path, "wb") as fh:
            fh.write(struct.pack("<Q", len(hb)) + hb + blob)

    def _read_safetensors(self, path):
        import struct
        with open(path, "rb") as fh:
            (n,) = struct.unpack("<Q", fh.read(8))
            raw = fh.read(n)
            header = json.loads(raw)
            body = fh.read()
        return n, header, body

    def test_reshard_safetensors_streams_exact_bytes(self):
        folder = os.path.join(self.tmp, "t5")
        os.makedirs(folder)
        src = os.path.join(folder, "model.safetensors")
        tensors = [("encoder.block.%d.w" % i, bytes([i]) * (1000 + 10 * i)) for i in range(7)]
        tensors.append(("shared.weight", b"\x07" * 2500))
        self._write_safetensors(src, tensors)
        paths = inst.reshard_safetensors(src, folder, shard_bytes=3000)
        self.assertGreater(len(paths), 2)
        index = json.load(open(os.path.join(folder, "model.safetensors.index.json")))
        self.assertEqual(set(index["weight_map"]), set(n for n, _ in tensors))
        self.assertEqual(index["metadata"]["total_size"], sum(len(d) for _, d in tensors))
        seen = {}
        for p in paths:
            n, header, body = self._read_safetensors(p)
            self.assertEqual(n % 8, 0)                       # header aligned
            self.assertEqual(header["__metadata__"], {"format": "pt"})
            for name, info in header.items():
                if name == "__metadata__":
                    continue
                a, b = info["data_offsets"]
                seen[name] = body[a:b]
                self.assertEqual(index["weight_map"][name], os.path.basename(p))
        self.assertEqual(seen, dict(tensors))

    def test_configure_splits_t5_and_models_step_skips_it(self):
        folder = os.path.join(self.inst.base, "models", "t5")
        os.makedirs(folder)
        src = os.path.join(folder, "model.safetensors")
        self._write_safetensors(src, [("a", b"x" * 4000), ("b", b"y" * 4000)])
        with mock.patch.object(inst, "SHARD_BYTES", 3000):
            self.inst.split_big_weights()
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(os.path.join(folder, "model.safetensors.index.json")))
        downloads = []
        with mock.patch.object(inst.Installer, "download",
                               lambda self_, url, dest, *a, **k: downloads.append(dest)):
            self.inst.step_models()
        self.assertFalse(any(d.endswith(os.path.join("t5", "model.safetensors")) for d in downloads))

    def test_cancel(self):
        steps = [k for k, _ in inst.Installer.STEPS]
        patches = dict(("step_" + k, lambda self: None) for k in steps)

        def cancel_here(self):
            self.cancel()
            self._check_cancel()
        patches["step_libs"] = cancel_here
        with mock.patch.multiple(inst.Installer, **patches):
            self.assertFalse(self.inst.run())
        self.assertIn("cancelled", self.events[-1][1]["message"].lower())


if __name__ == "__main__":
    unittest.main()
