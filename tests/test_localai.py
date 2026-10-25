"""Tests the hidden AI helper protocol for real: worker.py runs as a separate
process (in --fake mode, so no PyTorch needed) and the Nuke-side client talks
to it over pipes exactly as it does inside Nuke."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill import config, localai, pngio  # noqa: E402
from genfill.backends import base, local  # noqa: E402
from genfill.localai import client as lclient, installer, warmup  # noqa: E402


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

    def test_other_programs_leave_our_helper_alone(self):
        # GenFill Setup closing (or a second Nuke starting a helper) used to kill the
        # helper named in helper.pid, whoever owned it.
        self.client.start()
        pkg = os.path.join(os.path.dirname(__file__), "..", "nuke", "python")
        code = ("import sys; sys.path.insert(0, %r); from genfill.localai import client; "
                "print(client.kill_stale_helpers(%r))" % (pkg, self.tmp))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)
        time.sleep(0.5)
        self.assertTrue(self.client.is_running())                 # survived the other exit
        forced = code.replace("(%r)" % self.tmp, "(%r, any_owner=True)" % self.tmp)
        out = subprocess.run([sys.executable, "-c", forced], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)  # installer may still stop it
        self.client.proc.wait(10)
        self.assertFalse(self.client.is_running())

    def test_stale_pid_of_unrelated_process_is_not_killed(self):
        # The pid file outlived the helper and the pid now belongs to something else.
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            os.makedirs(os.path.join(self.tmp, "logs"), exist_ok=True)
            with open(os.path.join(self.tmp, "logs", "helper.pid"), "w") as fh:
                fh.write(str(other.pid))
            self.assertFalse(lclient.kill_stale_helpers(self.tmp))
            self.assertIsNone(other.poll())
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "logs", "helper.pid")))
        finally:
            other.kill()
            other.wait(10)

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

    def test_local_backend_streams_each_variation(self):
        req = base.GenerationRequest(self.img, self.mask, self.mask, "", 32, 32, seed=1, count=3)
        events = []

        def progress(frac, msg):
            events.append("progress")

        def on_image(i, item):
            events.append(("image", i))
            self.assertTrue(item[0].startswith(b"\x89PNG"))

        with mock.patch.object(localai, "is_installed", return_value=True), \
                mock.patch.object(lclient, "get_client", return_value=self.client):
            out = local.LocalBackend().generate(req, "", progress=progress, on_image=on_image)
        images = [e for e in events if e != "progress"]
        self.assertEqual(images, [("image", 0), ("image", 1), ("image", 2)])
        self.assertEqual(len(out), 3)
        # Variation 1 arrives before variation 2 has started its progress steps.
        first = events.index(("image", 0))
        self.assertIn("progress", events[first + 1:events.index(("image", 1))])

    def test_local_backend_not_installed(self):
        req = base.GenerationRequest(self.img, self.mask, self.mask, "x", 32, 32)
        with mock.patch.object(localai, "is_installed", return_value=False):
            with self.assertRaises(local.NotInstalled):
                local.LocalBackend().generate(req, "")

    def test_local_backend_follows_installed_model(self):
        b = local.LocalBackend()
        with mock.patch.object(localai, "family", return_value="flux1-fill"):
            self.assertIn("non-commercial", b.description)
            self.assertEqual(b._steps("auto"), 24)
        with mock.patch.object(localai, "family", return_value="flux2-klein"):
            self.assertIn("commercial", b.description)
            self.assertNotIn("non-commercial", b.description)
            self.assertEqual(b._steps("auto"), 4)
            self.assertEqual(b._steps("high"), 8)
        self.assertEqual(localai.family(self.tmp), "flux1-fill")     # no manifest: old default

    def test_klein_stays_within_one_megapixel(self):
        # 864x1216 (just over 1 MP) made diffusers' klein pipeline resize the float array
        # itself, which failed: "expected input[1216, 1, 1024, 864] to have 3 channels".
        b = local.LocalBackend()
        sizes = [(543, 697), (697, 543), (1920, 1080), (300, 900), (1000, 1000), (4000, 1000)]
        with mock.patch.object(localai, "family", return_value="flux2-klein"):
            for w, h in sizes:
                mw, mh = b.model_size(w, h)
                self.assertLessEqual(mw * mh, local.KLEIN_MAX_PIXELS, (w, h, mw, mh))
                self.assertEqual((mw % 64, mh % 64), (0, 0))
                fw, fh = local.region.scale_to_pixel_budget(w, h, 1024 * 1024, 64, min_side=512,
                                                            max_side=1536, max_aspect=3.0)
                self.assertGreater(mw * mh, 0.8 * min(fw * fh, local.KLEIN_MAX_PIXELS))
        with mock.patch.object(localai, "family", return_value="flux1-fill"):
            self.assertEqual(b.model_size(864, 1216), local.region.scale_to_pixel_budget(
                864, 1216, 1024 * 1024, 64, min_side=512, max_side=1536, max_aspect=3.0))
        from genfill.localai import worker
        for w, h in ((864, 1216), (1536, 512), (1024, 1024), (512, 512)):
            kw, kh = worker.klein_size(w, h)
            self.assertLessEqual(kw * kh, worker.KLEIN_MAX_PIXELS)
            self.assertEqual((kw % 16, kh % 16), (0, 0))
        self.assertEqual(worker.klein_size(512, 512), (512, 512))

    def test_is_installed_needs_the_8bit_t5_when_chosen(self):
        import json
        base = os.path.join(self.tmp, "inst8")
        for rel in ("env/Scripts/python.exe", "env/bin/python", "u.gguf", "v.st", "l.pt"):
            os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
            open(os.path.join(base, rel), "w").close()
        with open(os.path.join(base, "manifest.json"), "w") as fh:
            json.dump({"unet": "u.gguf", "vae": "v.st", "lama": "l.pt",
                       "t5_file": "models/t5-gguf/t5.gguf"}, fh)
        self.assertFalse(localai.is_installed(base))
        os.makedirs(os.path.join(base, "models", "t5-gguf"))
        open(os.path.join(base, "models", "t5-gguf", "t5.gguf"), "w").close()
        self.assertTrue(localai.is_installed(base))

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


class WarmupTests(unittest.TestCase):
    """Loading the model in the background before the first Generate (localai/warmup.py)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "src.png")
        pngio.write(self.img, pngio.Image(32, 32, 3))
        self.client = lclient.WorkerClient(python=sys.executable, base=self.tmp, fake=True, idle=30)
        patches = [mock.patch.object(localai, "is_installed", return_value=True),
                   mock.patch.object(lclient, "get_client", return_value=self.client),
                   mock.patch.dict(os.environ, {"GENFILL_FAKE_LOAD_SECONDS": "1.5"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._join()
        self.client.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _join(self):
        t = warmup._thread[0]
        if t is not None:
            t.join(30)

    def test_loads_once_in_the_background(self):
        t0 = time.time()
        self.assertTrue(warmup.start("test"))
        self.assertLess(time.time() - t0, 1.0)          # never waits for the helper
        self.assertTrue(warmup.is_warming())
        self.assertFalse(warmup.start("again"))         # already warming
        self._join()
        self.assertFalse(warmup.is_warming())
        self.assertTrue(self.client.is_loaded())
        self.assertFalse(warmup.start("loaded"))        # nothing left to do
        self.client.stop()
        self.assertFalse(self.client.is_loaded())       # a new helper has to load again
        self.assertTrue(warmup.start("after stop"))

    def test_not_installed_or_installing(self):
        with mock.patch.object(localai, "is_installed", return_value=False):
            self.assertFalse(warmup.start("test"))
        with mock.patch.object(installer, "is_running", return_value=True):
            self.assertFalse(warmup.start("test"))
        self.assertFalse(warmup.is_warming())
        self.assertIsNone(self.client.proc)             # the helper wasn't even started

    def test_skips_while_helper_is_busy(self):
        with mock.patch.object(self.client, "is_busy", return_value=True):
            self.assertFalse(warmup.start("test"))

    def test_failure_is_logged_not_raised(self):
        broken = lclient.WorkerClient(python=os.path.join(self.tmp, "missing.exe"), base=self.tmp)
        with mock.patch.object(lclient, "get_client", return_value=broken):
            self.assertTrue(warmup.start("test"))
            self._join()
        self.assertFalse(broken.is_loaded())

    def test_generate_during_warm_up_uses_the_same_helper(self):
        self.assertTrue(warmup.start("test"))
        while self.client.proc is None:
            time.sleep(0.02)
        pid = self.client.proc.pid
        messages = []
        req = base.GenerationRequest(self.img, self.img, self.img, "a cat", 32, 32, seed=1,
                                     count=2)
        out = local.LocalBackend().generate(req, "", progress=lambda f, m: messages.append(m))
        self.assertEqual(len(out), 2)
        self.assertEqual(messages[0], "Waiting for the local AI to finish loading...")
        self.assertEqual(self.client.proc.pid, pid)
        self._join()
        self.assertTrue(self.client.is_loaded())

    def test_cancel_reaches_a_generate_queued_behind_the_load(self):
        self.assertTrue(warmup.start("test"))
        t0 = time.time()
        while not self.client.is_busy():               # the load is on its way to the helper
            time.sleep(0.02)
        time.sleep(0.2)
        payload = {"image": self.img, "mask": self.img, "prompt": "x", "width": 32, "height": 32,
                   "count": 1, "steps": 2, "out_dir": os.path.join(self.tmp, "out")}
        with self.assertRaises(lclient.WorkerError) as ctx:
            self.client.request("generate", payload, cancelled=lambda: True)
        self.assertTrue(ctx.exception.cancelled)
        self.assertFalse(os.path.isdir(payload["out_dir"]))   # it never ran
        self._join()
        self.assertTrue(self.client.is_loaded())                # the load itself carried on
        self.assertGreater(time.time() - t0, 1.0)


class IdleSettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = os.path.join(self.tmp, "cfg")
        for p in (mock.patch.object(config, "CONFIG_DIR", cfg),
                  mock.patch.object(config, "CONFIG_PATH", os.path.join(cfg, "config.json"))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_and_saved(self):
        self.assertEqual(localai.idle_minutes(), 30)
        self.assertEqual(localai.idle_seconds(), 1800)
        localai.set_idle_minutes(120)
        self.assertEqual(localai.idle_minutes(), 120)
        self.assertEqual(config.load()["local_ai"], {"idle_minutes": 120})
        localai.set_idle_minutes(0)
        self.assertEqual(localai.idle_seconds(), 0)
        for bad in ("soon", -5, None, [1]):
            config.save({"local_ai": {"idle_minutes": bad}})
            self.assertEqual(localai.idle_minutes(), 30, bad)
        config.save({"local_ai": "broken"})
        self.assertEqual(localai.idle_minutes(), 30)

    def test_setting_keeps_other_local_ai_settings(self):
        config.save({"local_ai": {"root": "D:/AI"}, "api_keys": {"x": "y"}})
        localai.set_idle_minutes(60)
        data = config.load()
        self.assertEqual(data["local_ai"], {"root": "D:/AI", "idle_minutes": 60})
        self.assertEqual(data["api_keys"], {"x": "y"})

    def test_setting_is_passed_to_the_helper(self):
        localai.set_idle_minutes(45)
        c = lclient.WorkerClient(python=sys.executable, base=self.tmp, fake=True)
        try:
            c.request("ping")
            self.assertEqual(c.proc.args[c.proc.args.index("--idle") + 1], "2700")
            self.assertIn("idle timeout 2700.0s", lclient.helper_log_tail(self.tmp))
        finally:
            c.stop()

    def test_until_nuke_closes(self):
        localai.set_idle_minutes(0)
        c = lclient.WorkerClient(python=sys.executable, base=self.tmp, fake=True)
        try:
            c.request("ping")
            time.sleep(0.5)
            self.assertTrue(c.is_running())            # 0 isn't "exit at once"
            self.assertEqual(c.request("ping")["event"], "pong")
        finally:
            c.stop()


def _pickle_save(value, path):
    import pickle
    with open(path, "wb") as fh:
        pickle.dump(value, fh)


def _pickle_load(path):
    import pickle
    with open(path, "rb") as fh:
        return pickle.load(fh)


class _FakeTensor(object):
    """Stands in for a torch tensor: the engine only moves cached embeddings."""

    def __init__(self, value):
        self.value = value

    def to(self, device):
        return self

    def __eq__(self, other):
        return isinstance(other, _FakeTensor) and other.value == self.value


class EmbedCacheTests(unittest.TestCase):
    """The on-disk prompt embedding cache (no PyTorch: values are pickled)."""

    def setUp(self):
        from genfill.localai import worker
        self.worker = worker
        self.tmp = tempfile.mkdtemp()
        self.folder = os.path.join(self.tmp, "embeds")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cache(self, key="model-a", **kw):
        return self.worker.EmbedCache(self.folder, key, save=_pickle_save, load=_pickle_load, **kw)

    def test_round_trip_and_keys(self):
        c = self.cache()
        self.assertIsNone(c.get("a cat"))
        c.put("a cat", ("pe", "ppe"))
        self.assertEqual(self.cache().get("a cat"), ("pe", "ppe"))   # survives a new instance
        self.assertIsNone(c.get("a dog"))
        self.assertIsNone(c.get("a cat "))                  # exact prompt only
        self.assertIsNone(self.cache("model-b").get("a cat"))
        self.assertEqual(os.listdir(self.folder), [os.path.basename(c.path("a cat"))])

    def test_least_recently_used_files_are_pruned(self):
        c = self.cache(max_files=2)
        c.put("one", (1,))
        c.put("two", (2,))
        old = time.time() - 100
        os.utime(c.path("one"), (old, old))
        os.utime(c.path("two"), (old - 50, old - 50))
        self.assertEqual(c.get("two"), (2,))                # reading it makes it recent
        c.put("three", (3,))
        self.assertIsNone(c.get("one"))
        self.assertEqual(c.get("two"), (2,))
        self.assertEqual(c.get("three"), (3,))

    def test_broken_files_and_folders_are_just_misses(self):
        c = self.cache()
        os.makedirs(self.folder)
        with open(c.path("a cat"), "wb") as fh:
            fh.write(b"not a pickle")
        self.assertIsNone(c.get("a cat"))
        self.assertFalse(os.path.exists(c.path("a cat")))   # removed so it's rewritten
        not_a_folder = os.path.join(self.tmp, "a_file")
        open(not_a_folder, "w").close()
        blocked = self.worker.EmbedCache(os.path.join(not_a_folder, "sub"), "k",
                                         save=_pickle_save, load=_pickle_load)
        blocked.put("a cat", (1,))                         # can't create the folder: no error
        self.assertIsNone(blocked.get("a cat"))

    def test_key_follows_model_and_install(self):
        key = self.worker.embed_key
        flux = {"family": "flux1-fill", "installed": "2026-10-19T00:12:46",
                "t5_dir": "models/t5", "clip_dir": "models/clip"}
        self.assertEqual(key(flux), key(dict(flux)))
        self.assertNotEqual(key(flux), key(dict(flux, installed="2026-10-23T09:00:00")))
        self.assertNotEqual(key(flux), key(dict(flux, family="flux2-klein")))
        self.assertEqual(key(dict(flux, family=None)), key(flux))   # old installs are FLUX.1
        # The 8-bit T5 gives slightly different numbers: never mix its cache with 16-bit.
        self.assertNotEqual(key(flux), key(dict(flux, t5_file="models/t5-gguf/t5.gguf")))

    def test_8bit_t5_loads_with_the_v11_config(self):
        import json
        import types
        with open(os.path.join(self.tmp, "manifest.json"), "w") as fh:
            json.dump({"family": "flux1-fill", "installed": "x", "t5_dir": "models/t5",
                       "t5_file": "models/t5-gguf/t5-q8.gguf"}, fh)
        os.makedirs(os.path.join(self.tmp, "models", "t5-gguf"))
        with open(os.path.join(self.tmp, "models", "t5-gguf", "t5-q8.gguf"), "wb") as fh:
            fh.write(b"g" * 1234)
        calls = {}

        class FakeT5Config(object):
            @staticmethod
            def from_pretrained(path):
                calls["config_from"] = path
                return "v1.1-config"

        class FakeT5(object):
            @staticmethod
            def from_pretrained(path, **kw):
                calls["model_from"] = (path, kw)
                return "model"

        fake_tf = types.SimpleNamespace(T5Config=FakeT5Config, T5EncoderModel=FakeT5)
        fake_torch = types.SimpleNamespace(bfloat16="bf16")
        eng = self.worker.Engine(self.tmp)
        with mock.patch.dict(sys.modules, {"transformers": fake_tf, "torch": fake_torch}):
            self.assertEqual(eng._load_t5(), "model")
        path, kw = calls["model_from"]
        self.assertEqual(path, os.path.join(self.tmp, "models", "t5-gguf"))
        self.assertEqual(kw["gguf_file"], "t5-q8.gguf")
        self.assertEqual(kw["config"], "v1.1-config")
        self.assertEqual(calls["config_from"], os.path.join(self.tmp, "models/t5"))
        # It's unpacked to 16-bit in RAM: the RAM check must use that, not the file size.
        self.assertEqual(eng._text_encoder_bytes(), int(1234 * 16 / 8.5))

    def test_engine_reads_each_prompt_once(self):
        import json
        with open(os.path.join(self.tmp, "manifest.json"), "w") as fh:
            json.dump({"family": "flux1-fill", "installed": "x"}, fh)
        calls = []

        def make_engine():
            eng = self.worker.Engine(self.tmp)
            eng.device = "cuda"
            eng.disk_cache._save, eng.disk_cache._load = _pickle_save, _pickle_load
            eng._encode_with_text_encoder = lambda p: calls.append(p) or (_FakeTensor(p),)
            return eng

        class FakeJob(object):
            def progress(self, fraction, message):
                pass

        eng = make_engine()
        self.assertEqual(eng.encode("a cat", FakeJob()), (_FakeTensor("a cat"),))
        eng.encode("a cat", FakeJob())                      # memory cache
        self.assertEqual(calls, ["a cat"])
        restarted = make_engine()                          # new helper process
        self.assertEqual(restarted.encode("a cat", FakeJob()), (_FakeTensor("a cat"),))
        self.assertEqual(calls, ["a cat"])
        restarted.encode("a dog", FakeJob())
        self.assertEqual(calls, ["a cat", "a dog"])
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "cache", "embeds")))

    def test_text_encoder_is_read_ahead_only_with_room(self):
        import json
        os.makedirs(os.path.join(self.tmp, "models", "t5"))
        with open(os.path.join(self.tmp, "models", "t5", "model.safetensors"), "wb") as fh:
            fh.write(b"x" * 1000)
        with open(os.path.join(self.tmp, "manifest.json"), "w") as fh:
            json.dump({"family": "flux1-fill", "t5_dir": "models/t5"}, fh)
        eng = self.worker.Engine(self.tmp)
        reads = []
        eng._text_encoder = lambda: reads.append(1) or setattr(eng, "text_encoder", "T5")
        warmups = []
        eng._run_text_encoder = lambda p: warmups.append(p) or (_FakeTensor(p),)
        eng.disk_cache._save, eng.disk_cache._load = _pickle_save, _pickle_load
        keep = self.worker.KEEP_TEXT_ENCODER_FREE
        with mock.patch.object(self.worker, "memory_free", return_value=(keep + 999, keep)):
            self.assertIsNone(eng.preload_text_encoder())    # 1 byte short of size + headroom
        with mock.patch.object(self.worker, "memory_free", return_value=None):
            self.assertIsNone(eng.preload_text_encoder())    # can't tell: don't
        self.assertEqual(reads, [])
        with mock.patch.object(self.worker, "memory_free", return_value=(keep + 1000, keep)):
            eng.preload_text_encoder().join(10)
        self.assertEqual((reads, eng.text_encoder), ([1], "T5"))
        self.assertEqual(warmups, [""])                    # the removal prompt, read ahead
        self.assertEqual(eng.disk_cache.get(""), (_FakeTensor(""),))
        with eng._gpu_lock:                                # a job is running: no warm-up
            with mock.patch.object(self.worker, "memory_free", return_value=(keep + 1000, keep)):
                eng.preload_text_encoder().join(10)
        self.assertEqual(warmups, [""])
        with mock.patch.object(self.worker, "memory_free", return_value=(keep + 1000, keep - 1)):
            eng.preload_text_encoder().join(10)
        self.assertIsNone(eng.text_encoder)               # read, but then short: freed


if __name__ == "__main__":
    unittest.main()
