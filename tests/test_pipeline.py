"""End-to-end test of node build + generate using a fake ``nuke`` module and the
offline mock backend. Verifies the Python plumbing, not Nuke's rendering."""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import fake_nuke  # noqa: E402

nuke = fake_nuke.install()

from genfill import engine, node as gfnode, pngio, state  # noqa: E402


def _join_workers():
    for t in threading.enumerate():
        if t.name.startswith("GenFill-"):
            t.join(30)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = os.path.join(self.tmp, "shot.nk")
        fake_nuke.EXECUTED[:] = []
        fake_nuke.EXPRESSION_ERRORS[:] = []
        self.plate = nuke.nodes.Read(name="Plate")
        self.g = gfnode.create()
        self.g.setInput(0, self.plate)
        fake_nuke.add_shape(self.g.node(gfnode.ROTO))
        mock_label = [l for l in gfnode.backend_labels() if "Offline" in l][0]
        self.g["gf_backend"].setValue(mock_label)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_build(self):
        g = self.g
        self.assertEqual(fake_nuke.EXPRESSION_ERRORS, [])
        for name in (gfnode.READ_RESULT, gfnode.COMP, gfnode.OUT_SWITCH, gfnode.PROBE_WRITE,
                     gfnode.WRITE_SRC, gfnode.WRITE_MASK, gfnode.WRITE_MASK_A):
            self.assertIsNotNone(g.node(name), name)
        for name in gfnode.WRITE_NODES:
            self.assertTrue(g.node(name)["disable"].value(), "%s must start disabled" % name)
        self.assertEqual(g.node(gfnode.OUT_SWITCH)["which"].expression,
                         "parent.gf_has_result ? parent.gf_output : 2")
        self.assertEqual(g.node(gfnode.COMP).input(2), g.node(gfnode.FEATHER))
        # The selection Roto lives inside the node; nothing is created outside it.
        self.assertIsNone(g.input(1))
        roto = g.node(gfnode.ROTO)
        self.assertEqual(roto.Class(), "Roto")
        self.assertTrue(roto["replace"].value())
        self.assertIs(roto.input(0), g.node(gfnode.SRC_IN))
        sw = g.node(gfnode.SEL_SWITCH)
        self.assertIs(sw.input(gfnode.SEL_SHAPES), roto)
        self.assertIs(sw.input(gfnode.SEL_MASK), g.node(gfnode.MASK_IN))
        self.assertIs(sw.input(gfnode.SEL_ALPHA), g.node(gfnode.SRC_IN))
        self.assertEqual(g["gf_selection"].getValue(), gfnode.SEL_SHAPES)
        self.assertEqual(g["gf_shapes"].link, "gf_roto.curves")
        self.assertIn("gf_draw", g.knobs())
        self.assertEqual(g["gf_variations"].value(), 3)

    def test_layer_output_has_source_format(self):
        g = self.g
        sw = g.node(gfnode.OUT_SWITCH)
        layer = g.node(gfnode.LAYER)
        self.assertIs(sw.input(1), layer)
        self.assertEqual(layer["operation"].value(), "over")
        self.assertIs(layer.input(1), g.node(gfnode.PREMULT))           # A: premultiplied fill
        blank = layer.input(0)                                           # B: Source, emptied
        self.assertEqual(blank.Class(), "Remove")
        self.assertIs(blank.input(0), g.node(gfnode.SRC_IN))
        self.assertEqual(g["gf_output"].values(), ["Comp", "Layer", "Bypass"])
        # the alpha that premultiplies the fill is the feathered, expanded selection
        self.assertIs(g.node(gfnode.COPY_ALPHA).input(1), g.node(gfnode.FEATHER))

    def test_old_nodes_are_upgraded(self):
        g = self.g
        with g:
            fake_nuke.delete(g.node(gfnode.LAYER))
            fake_nuke.delete(g.node(gfnode.BLANK))
        g.node(gfnode.OUT_SWITCH).setInput(1, g.node(gfnode.PREMULT))
        g["gf_output"].setValues(["Comp", "Fill only (premult)", "Bypass"])
        g["gf_output"].setValue(1)
        self.assertTrue(gfnode.ensure_current(g))
        self.assertIs(g.node(gfnode.OUT_SWITCH).input(1), g.node(gfnode.LAYER))
        self.assertEqual(g["gf_output"].value(), "Layer")
        self.assertFalse(gfnode.ensure_current(g))      # idempotent

    def test_failed_build_leaves_no_empty_group(self):
        # Issue #3: an error while building used to leave a blank Group behind.
        before = list(gfnode.nuke.allNodes())
        messages = []
        with mock.patch.object(gfnode, "_build_internals", side_effect=RuntimeError("boom")), \
                mock.patch.object(gfnode.nuke, "message", messages.append):
            self.assertIsNone(gfnode.create())
        self.assertEqual(gfnode.nuke.allNodes(), before)
        self.assertEqual(len(messages), 1)
        self.assertIn("RuntimeError: boom", messages[0])
        self.assertIs(gfnode.create().Class(), self.g.Class())   # still works afterwards

    def test_old_local_label_still_means_local_ai(self):
        g = self.g
        old = "Local: FLUX.1 Fill (free, runs on your GPU)"
        labels = gfnode.backend_labels()
        g["gf_backend"].setValues([old] + labels[1:])
        g["gf_backend"].setValue(old)
        self.assertEqual(gfnode.backend_for(g).id, "local")
        self.assertTrue(gfnode.ensure_current(g))
        self.assertEqual(list(g["gf_backend"].values()), labels)
        self.assertEqual(g["gf_backend"].value(), "Local AI (free, runs on your GPU)")
        self.assertFalse(gfnode.ensure_current(g))

    def test_empty_shapes_explains_what_to_do(self):
        g = self.g
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        self.assertFalse(engine.generate(g, "x", blocking=True))
        self.assertIn("Draw Selection", engine.status(g)["error"])

    def test_mask_input_switches_selection_mode(self):
        g = self.g
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        matte = nuke.nodes.Read(name="Matte")
        g.setInput(1, matte)
        n = gfnode.nuke          # the fake module node.py imported
        n.thisNode = lambda: g
        n.thisKnob = lambda: g["inputChange"]
        try:
            gfnode.knob_changed()
            self.assertEqual(g["gf_selection"].getValue(), gfnode.SEL_MASK)
            g.setInput(1, None)
            gfnode.knob_changed()
            self.assertEqual(g["gf_selection"].getValue(), gfnode.SEL_SHAPES)
            # Shapes already drawn: connecting a mask doesn't take them away.
            fake_nuke.add_shape(g.node(gfnode.ROTO))
            g.setInput(1, matte)
            gfnode.knob_changed()
            self.assertEqual(g["gf_selection"].getValue(), gfnode.SEL_SHAPES)
        finally:
            del n.thisNode, n.thisKnob

    def test_pre_07_selection_is_upgraded(self):
        g = self.g
        sw = g.node(gfnode.SEL_SWITCH)
        with g:
            fake_nuke.delete(g.node(gfnode.ROTO))
        sw.setInput(0, g.node(gfnode.MASK_IN))
        sw.setInput(1, g.node(gfnode.SRC_IN))
        sw.setInput(2, None)
        for name in ("gf_shapes", "gf_draw"):
            del g._knobs[name]
        g["gf_selection"].setValues(["Mask input", "Source alpha"])
        g["gf_selection"].setValue(1)                  # old "Source alpha"
        self.assertTrue(gfnode.ensure_current(g))
        roto = g.node(gfnode.ROTO)
        self.assertIsNotNone(roto)
        self.assertIs(sw.input(gfnode.SEL_SHAPES), roto)
        self.assertIs(sw.input(gfnode.SEL_MASK), g.node(gfnode.MASK_IN))
        self.assertIs(sw.input(gfnode.SEL_ALPHA), g.node(gfnode.SRC_IN))
        self.assertEqual(g["gf_selection"].value(), "Source alpha")
        self.assertIn("gf_shapes", g.knobs())
        self.assertFalse(gfnode.ensure_current(g))

    def test_generate_and_select(self):
        g = self.g
        self.assertTrue(engine.generate(g, "a red vintage car"))
        _join_workers()
        st = engine.get_state(g)
        self.assertEqual(len(st["generations"]), 1, engine.status(g))
        gen = st["generations"][0]
        self.assertEqual(len(gen["files"]), 3)
        self.assertEqual(gen["frame"], 1001)
        self.assertEqual(gen["prompt"], "a red vintage car")

        # Selection box in the fake probe: x 40-60%, y 30-60% from top of 1920x1080.
        x, y, w, h = gen["region"]
        self.assertLessEqual(x, 0.4 * 1920)
        self.assertGreaterEqual(x + w, 0.6 * 1920)
        self.assertLessEqual(y, (1 - 0.6) * 1080)
        self.assertGreaterEqual(y + h, (1 - 0.3) * 1080)
        mw, mh = gen["model_size"]
        self.assertAlmostEqual(float(w) / h, float(mw) / mh, delta=0.03)

        # Crops were rendered at model size and in order; writes re-disabled.
        names = [e[0] for e in fake_nuke.EXECUTED]
        self.assertEqual(names, [gfnode.PROBE_WRITE, gfnode.WRITE_SRC, gfnode.WRITE_MASK,
                                 gfnode.WRITE_MASK_A])
        src = pngio.read(fake_nuke.EXECUTED[1][1])
        self.assertEqual((src.width, src.height), (mw, mh))
        for name in gfnode.WRITE_NODES:
            self.assertTrue(g.node(name)["disable"].value())

        # Result wiring.
        read = g.node(gfnode.READ_RESULT)
        self.assertTrue(read["file"].value().endswith(gen["files"][0]))
        self.assertTrue(os.path.exists(read["file"].value()))
        self.assertEqual(g.node(gfnode.REFORMAT_RECV)["box_width"].value(), w)
        self.assertEqual(g.node(gfnode.TRANSFORM_RECV)["translate"].value(), [x, y])
        self.assertTrue(g["gf_has_result"].value())
        self.assertIn("(1/3)", g["gf_info"].value())
        self.assertTrue(read["file"].value().startswith(
            os.path.join(self.tmp, "genfill_cache").replace("\\", "/")))

        # Second generation, then navigate and delete.
        self.assertTrue(engine.generate(g, ""))
        _join_workers()
        thumbs = engine.thumbnails(g)
        self.assertEqual(len(thumbs), 6)
        self.assertEqual(thumbs[0]["prompt"], "")          # newest first, selected
        self.assertTrue(thumbs[0]["selected"])
        engine.step(g, 1)
        self.assertIn("(2/6)", g["gf_info"].value())
        engine.select(g, 0, 2)
        self.assertTrue(g.node(gfnode.READ_RESULT)["file"].value().endswith("var_03.png"))
        path = engine.thumbnails(g)[5]["path"]
        engine.delete_variation(g, 0, 2)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(engine.thumbnails(g)), 5)

    def test_copied_node_gets_its_own_uid_and_keeps_originals_files(self):
        g = self.g
        self.assertTrue(engine.generate(g, "a red vintage car", blocking=True))
        # A copy/paste duplicates every knob, uid and history included.
        copy = gfnode.create()
        copy.setName("GenerativeFill_copy")
        copy.setInput(0, self.plate)
        fake_nuke.add_shape(copy.node(gfnode.ROTO))
        copy["gf_backend"].setValue(g["gf_backend"].value())
        for k in ("gf_uid", "gf_state"):
            copy[k].setValue(g[k].value())
        shared = engine.get_state(g)["generations"][0]
        original_file = engine.resolve(g, shared, shared["files"][0])

        engine.delete_variation(copy, 0, 0)
        self.assertNotEqual(copy["gf_uid"].value(), g["gf_uid"].value())
        self.assertTrue(os.path.exists(original_file), "the original still uses this file")
        self.assertEqual(len(engine.thumbnails(copy)), 2)
        self.assertEqual(len(engine.thumbnails(g)), 3)

        self.assertTrue(engine.generate(copy, "", blocking=True))
        newest = engine.get_state(copy)["generations"][-1]
        self.assertTrue(newest["abs_dir"].startswith(engine.cache_dir(copy)))
        self.assertNotEqual(engine.cache_dir(copy), engine.cache_dir(g))
        self.assertEqual(newest["id"][:4], "g002")

    def test_failed_prepare_leaves_no_empty_job_folder(self):
        g = self.g
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        self.assertFalse(engine.generate(g, "x", blocking=True))
        cache = engine.cache_dir(g)
        self.assertEqual(os.listdir(cache) if os.path.isdir(cache) else [], [])

    def test_state_knob_has_no_tcl_characters(self):
        engine.generate(self.g, "a [bracketed] {prompt} $var", blocking=True)
        raw = self.g["gf_state"].value()
        self.assertTrue(raw.startswith("b64:"))
        for ch in "[]{}$":
            self.assertNotIn(ch, raw)
            self.assertNotIn(ch, self.g["gf_info"].value())
        self.assertEqual(engine.get_state(self.g)["generations"][0]["prompt"],
                         "a [bracketed] {prompt} $var")
        # Old plain-JSON state still loads.
        self.assertEqual(engine.decode_state('{"generations": []}')["generations"], [])

    def test_missing_key_reports_instead_of_silently_returning(self):
        from unittest import mock
        import genfill.ui
        stability_label = [l for l in gfnode.backend_labels() if "Stability" in l][0]
        self.g["gf_backend"].setValue(stability_label)
        fake_settings = mock.MagicMock()
        fake_settings.show.return_value = False
        with mock.patch.dict(sys.modules, {"genfill.ui.settings": fake_settings}), \
                mock.patch.object(genfill.ui, "settings", fake_settings, create=True), \
                mock.patch("genfill.config.get_api_key", return_value=""):
            self.assertFalse(engine.generate(self.g, "x"))
        self.assertTrue(fake_settings.show.called)
        self.assertIn("No API key", engine.status(self.g)["error"])
        self.assertFalse(engine.is_running(self.g))

    def test_worker_never_leaves_node_running(self):
        from unittest import mock
        with mock.patch.object(fake_nuke, "executeInMainThread", lambda fn, args=(): None):
            engine.generate(self.g, "x")      # result never gets dispatched back
            _join_workers()
        # dispatch "happened" (it was swallowed), so status is still running here;
        # simulate a crash inside the worker instead:
        engine._set_status(engine._uid(self.g), running=False, error="")
        with mock.patch.object(engine.backends.get("mock"), "generate",
                               side_effect=SystemError("boom")):
            engine.generate(self.g, "x", blocking=True)
        self.assertFalse(engine.is_running(self.g))
        self.assertIn("boom", engine.status(self.g)["error"])

    def test_log_file_written(self):
        from genfill import log as gflog
        engine.generate(self.g, "logged prompt", blocking=True)
        for h in gflog.get().handlers:
            h.flush()
        text = open(gflog.log_path(), encoding="utf-8").read()
        self.assertIn("Generate clicked", text)
        self.assertIn("logged prompt", text)

    def test_empty_selection_is_reported(self):
        fake_nuke.SELECTION_BOX[:] = [0, 0, 0, 0]
        try:
            self.assertFalse(engine.generate(self.g, "x"))
            self.assertIn("selection is empty", engine.status(self.g)["error"])
            self.assertFalse(self.g["gf_has_result"].value())
        finally:
            fake_nuke.SELECTION_BOX[:] = [0.4, 0.3, 0.6, 0.6]

    def test_missing_source(self):
        self.g.setInput(0, None)
        self.assertFalse(engine.generate(self.g, "x"))
        self.assertIn("Source", engine.status(self.g)["error"])


class IndieTests(unittest.TestCase):
    """Nuke Indie / Non-commercial: Python may retrieve only 10 nodes (issues #3, #5)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = os.path.join(self.tmp, "shot.nk")
        fake_nuke.env["indie"] = True
        # An existing comp: far more than 10 nodes.
        self.comp = [nuke.nodes.Blur(name="IndieBlur%d" % i) for i in range(25)]
        self.plate = nuke.nodes.Read(name="IndiePlate")
        fake_nuke.SELECTED[:] = [self.plate]
        fake_nuke.RETRIEVED[0] = 0

    def tearDown(self):
        fake_nuke.env["indie"] = False
        fake_nuke.SELECTED[:] = []
        for n in self.comp + [self.plate]:
            fake_nuke.delete(n)
        shutil.rmtree(self.tmp)

    def _create(self):
        g = gfnode.create()
        fake_nuke.add_shape(g.node(gfnode.ROTO))
        g["gf_backend"].setValue([l for l in gfnode.backend_labels() if "Offline" in l][0])
        return g

    def test_create_in_existing_script_stays_under_the_limit(self):
        g = self._create()
        self.assertIsNotNone(g)
        self.assertIs(g.input(0), self.plate)          # still connects to the selected node
        self.assertEqual(fake_nuke.SELECTED, [])        # and deselects the rest
        self.assertLessEqual(fake_nuke.RETRIEVED[0], 1)

    def test_generate_and_delete_retrieve_no_nodes(self):
        g = self._create()
        fake_nuke.RETRIEVED[0] = 0
        self.assertTrue(engine.generate(g, "a car", blocking=True), engine.status(g))
        engine.delete_variation(g, 0, 0)
        self.assertEqual(fake_nuke.RETRIEVED[0], 0)

    def test_copied_node_still_gets_its_own_uid(self):
        g = self._create()
        copy = self._create()
        copy.setName("IndieCopy")
        copy["gf_uid"].setValue(g["gf_uid"].value())
        self.assertTrue(engine.ensure_unique_uid(copy))
        self.assertNotEqual(copy["gf_uid"].value(), g["gf_uid"].value())
        fake_nuke.delete(copy)

    def test_copy_inside_another_group_gets_its_own_uid(self):
        g = self._create()
        comp = nuke.nodes.Group(name="IndieComp")
        with comp:
            inner = gfnode.create()
        try:
            self.assertEqual(inner.fullName(), "IndieComp." + inner.name())
            inner["gf_uid"].setValue(g["gf_uid"].value())
            fake_nuke.RETRIEVED[0] = 0
            self.assertTrue(engine.ensure_unique_uid(inner))
            self.assertNotEqual(inner["gf_uid"].value(), g["gf_uid"].value())
            self.assertFalse(engine.ensure_unique_uid(inner))   # unique now: no change
            self.assertFalse(engine.ensure_unique_uid(g))       # the original keeps its uid
            self.assertEqual(fake_nuke.RETRIEVED[0], 0)
        finally:
            fake_nuke.delete(comp)
            fake_nuke.delete(g)

    def test_env_var_forces_limited_mode(self):
        fake_nuke.env["indie"] = False
        self.assertFalse(gfnode.limited_python())
        with mock.patch.dict(os.environ, {"GENFILL_LIMITED_PYTHON": "1"}):
            self.assertTrue(gfnode.limited_python())


class DevtoolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = ""

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_smoke_test_runs_and_reports(self):
        import json
        from genfill import devtools
        out = os.path.join(self.tmp, "out")
        passed = devtools.run_smoke_test(out, show_result=False)
        with open(os.path.join(out, "report.json")) as fh:
            report = json.load(fh)
        failed = [s for s in report["steps"] if not s["ok"]]
        self.assertTrue(passed, failed)
        for name in ("comp.png", "fill.png", "source.png", "mask.png", "var_01.png"):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        self.assertIn("knob_inventory", report)
        self.assertTrue(report["values"]["gf_has_result"])
        # Running twice replaces the container instead of piling up.
        devtools.run_smoke_test(out, show_result=False)
        names = [n.name() for n in nuke.allNodes()]
        self.assertEqual(names.count(devtools.CONTAINER), 1)

    def test_smoke_test_on_indie_stays_under_the_limit(self):
        import json
        from genfill import devtools
        comp = [nuke.nodes.Blur(name="SmokeIndieBlur%d" % i) for i in range(25)]
        fake_nuke.SELECTED[:] = comp[:15]
        fake_nuke.env["indie"] = True
        fake_nuke.RETRIEVED[0] = 0
        try:
            out = os.path.join(self.tmp, "out")
            passed = devtools.run_smoke_test(out, show_result=False)
            retrieved = fake_nuke.RETRIEVED[0]
        finally:
            fake_nuke.env["indie"] = False
            fake_nuke.SELECTED[:] = []
            for n in comp:
                fake_nuke.delete(n)
        with open(os.path.join(out, "report.json")) as fh:
            report = json.load(fh)
        self.assertTrue(passed, [s for s in report["steps"] if not s["ok"]])
        self.assertTrue(report["limited_python"])
        self.assertLessEqual(retrieved, 1)     # at most the old smoke-test container

    def test_build_info_dev_and_installed(self):
        import genfill
        self.assertIn("live from repo", genfill.build_label())
        path = genfill.BUILD_INFO_PATH
        try:
            with open(path, "w") as fh:
                fh.write('{"commit": "abc1234", "dirty": true, "built": "2026-10-12 20:41",'
                         ' "mode": "copy", "source": "%s"}' % self.tmp.replace("\\", "/"))
            self.assertEqual(genfill.build_label(), "GenFill %s (abc1234*, 2026-10-12 20:41)"
                             % genfill.__version__)
            self.assertEqual(genfill.repo_root(), self.tmp.replace("\\", "/"))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
