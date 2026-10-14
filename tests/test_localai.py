"""Tests the hidden AI helper protocol for real: worker.py runs as a separate
process (in --fake mode, so no PyTorch needed) and the Nuke-side client talks
to it over pipes exactly as it does inside Nuke."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill import localai, pngio  # noqa: E402
from genfill.backends import base, local  # noqa: E402
from genfill.localai import client as lclient  # noqa: E402


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "src.png")
        pngio.write(self.img, pngio.Image(32, 32, 3))
        self.mask = os.path.join(self.tmp, "mask.png")
        pngio.write(self.mask, pngio.Image(32, 32, 3))
        self.client = lclient.WorkerClient(python=sys.executable, base=self.tmp, fake=True, idle=30)

    def tearDown(self):
        self.client.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def payload(self, **kw):
        p = {"image": self.img, "mask": self.mask, "prompt": "a cat", "width": 32, "height": 32,
             "count": 3, "seed": 5, "steps": 4, "out_dir": os.path.join(self.tmp, "out")}
        p.update(kw)
        return p

    def test_generate_with_progress(self):
        seen = []
        done = self.client.request("generate", self.payload(),
                                   progress=lambda f, m: seen.append(f))
        self.assertEqual(len(done["files"]), 3)
        self.assertTrue(all(os.path.exists(f) for f in done["files"]))
        self.assertEqual(len(seen), 12)
        self.assertAlmostEqual(seen[-1], 1.0)
        # the helper stays up between requests (model stays warm)
        pid = self.client.proc.pid
        self.client.request("generate", self.payload(count=1))
        self.assertEqual(self.client.proc.pid, pid)

    def test_cancel(self):
        flag = {"go": False}

        def progress(f, m):
            flag["go"] = True
        with self.assertRaises(lclient.WorkerError) as ctx:
            self.client.request("generate", self.payload(step_delay=0.05, steps=50),
                                progress=progress, cancelled=lambda: flag["go"])
        self.assertTrue(ctx.exception.cancelled)
        self.assertTrue(self.client.is_running())   # cancelling doesn't kill the helper

    def test_crash_is_contained_and_recovers(self):
        with self.assertRaises(lclient.WorkerError) as ctx:
            self.client.request("generate", self.payload(prompt="crash"))
        self.assertTrue(ctx.exception.crashed)
        self.assertIn("helper.log", str(ctx.exception))
        done = self.client.request("generate", self.payload(count=1))   # restarts by itself
        self.assertEqual(len(done["files"]), 1)

    def test_out_of_memory_flag(self):
        with self.assertRaises(lclient.WorkerError) as ctx:
            self.client.request("generate", self.payload(prompt="oom"))
        self.assertTrue(ctx.exception.oom)

    def test_idle_exit_and_restart(self):
        c = lclient.WorkerClient(python=sys.executable, base=self.tmp, fake=True, idle=0.5)
        try:
            c.request("ping")
            time.sleep(1.5)
            self.assertFalse(c.is_running())
            self.assertEqual(c.request("ping")["event"], "pong")
        finally:
            c.stop()

    def test_exits_when_nuke_goes_away(self):
        self.client.start()
        proc = self.client.proc
        proc.stdin.close()            # what happens when Nuke quits or crashes
        proc.wait(10)
        self.assertIsNotNone(proc.poll())

    def test_stale_helper_is_killed_on_next_start(self):
        self.client.start()
        old = self.client.proc
        # Simulate GenFill being reloaded: a new client knows nothing about the old process.
        fresh = lclient.WorkerClient(python=sys.executable, base=self.tmp, fake=True, idle=30)
        try:
            fresh.start()
            old.wait(10)
            self.assertIsNotNone(old.poll())          # orphan was stopped
            self.assertTrue(fresh.is_running())
        finally:
            fresh.stop()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "logs", "helper.pid")))

    def test_helper_log_tail(self):
        self.client.request("ping")
        self.assertIn("GenFill helper started", lclient.helper_log_tail(self.tmp))

    def test_clean_env_strips_nuke_python(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "C:/Nuke/lib", "PYTHONHOME": "C:/Nuke"}):
            env = lclient.clean_env()
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        nuke_path = r"C:\Program Files\Nuke17.0v1;C:\Windows\System32"
        with mock.patch.dict(os.environ, {"PATH": nuke_path, "SystemRoot": r"C:\Windows",
                                          "NUKE_PATH": "x", "KMP_DUPLICATE_LIB_OK": "TRUE",
                                          "CUDA_PATH": r"C:\CUDA"}):
            env = lclient.clean_env(platform="win32")
        self.assertNotIn("Nuke17", env["PATH"])
        self.assertIn("System32", env["PATH"])
        for k in ("NUKE_PATH", "KMP_DUPLICATE_LIB_OK", "CUDA_PATH"):
            self.assertNotIn(k, env)

    def test_clean_env_allocator(self):
        with mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            os.environ.pop("PYTORCH_CUDA_ALLOC_CONF", None)
            self.assertNotIn("PYTORCH_CUDA_ALLOC_CONF", lclient.clean_env(platform="win32"))
            self.assertEqual(lclient.clean_env(platform="linux")["PYTORCH_CUDA_ALLOC_CONF"],
                             "expandable_segments:True")

    def test_local_backend_via_helper(self):
        req = base.GenerationRequest(self.img, self.mask, self.mask, "", 32, 32, seed=1, count=2)
        with mock.patch.object(localai, "is_installed", return_value=True), \
                mock.patch.object(lclient, "get_client", return_value=self.client):
            out = local.LocalBackend().generate(req, "")
        self.assertEqual(len(out), 2)
        data, preview = out[0]                 # (result file, 8-bit preview for the panel)
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertTrue(preview.startswith(b"\x89PNG"))

    def test_local_backend_not_installed(self):
        req = base.GenerationRequest(self.img, self.mask, self.mask, "x", 32, 32)
        with mock.patch.object(localai, "is_installed", return_value=False):
            with self.assertRaises(local.NotInstalled):
                local.LocalBackend().generate(req, "")

    def test_is_installed_requires_manifest_and_files(self):
        self.assertFalse(localai.is_installed(self.tmp))
        os.makedirs(os.path.dirname(localai.env_python(self.tmp)))
        open(localai.env_python(self.tmp), "w").close()
        for rel in ("models/flux/x.gguf", "models/vae/ae.safetensors", "models/lama/big-lama.pt"):
            os.makedirs(os.path.join(self.tmp, os.path.dirname(rel)), exist_ok=True)
            open(os.path.join(self.tmp, rel), "w").close()
        import json
        with open(os.path.join(self.tmp, "manifest.json"), "w") as fh:
            json.dump({"unet": "models/flux/x.gguf", "vae": "models/vae/ae.safetensors",
                       "lama": "models/lama/big-lama.pt"}, fh)
        self.assertTrue(localai.is_installed(self.tmp))
        os.remove(os.path.join(self.tmp, "models/lama/big-lama.pt"))
        self.assertFalse(localai.is_installed(self.tmp))


if __name__ == "__main__":
    unittest.main()
