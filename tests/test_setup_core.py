"""Tests for GenFill Setup's install logic (installers/genfill_setup/core.py)."""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "installers"))

from genfill_setup import core  # noqa: E402

REPO_NUKE = os.path.join(os.path.dirname(__file__), "..", "nuke")


class SetupCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self._env = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, *parts, executable=False):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        if executable:
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    # -- finding Nuke -----------------------------------------------------
    def test_find_nuke_windows(self):
        self._touch("pf", "Nuke15.1v3", "Nuke15.1.exe")
        self._touch("pf", "Nuke17.0v1", "Nuke17.0.exe")
        self._touch("pf", "Nuke16.0v2", "readme.txt")          # no exe: ignored
        self._touch("pf", "NotNuke", "Nuke1.0.exe")
        found = core.find_nuke_installs("win32", [os.path.join(self.tmp, "pf")])
        self.assertEqual([n.name for n in found], ["Nuke17.0v1", "Nuke15.1v3"])
        self.assertEqual(found[0].version, (17, 0, 1))
        self.assertTrue(found[0].exe.endswith("Nuke17.0.exe"))

    def test_find_nuke_macos(self):
        self._touch("apps", "Nuke15.1v3", "Nuke15.1v3.app", "Contents", "MacOS", "Nuke15.1")
        self._touch("apps", "Nuke15.1v3", "NukeX15.1v3.app", "Contents", "MacOS", "Nuke15.1")
        found = core.find_nuke_installs("darwin", [os.path.join(self.tmp, "apps")])
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].launch.endswith("Nuke15.1v3.app"))

    def test_find_nuke_linux(self):
        self._touch("usr", "Nuke14.0v5", "Nuke14.0", executable=True)
        self._touch("usr", "Nuke14.0v5", "libfoo.so")
        found = core.find_nuke_installs("linux", [os.path.join(self.tmp, "usr")])
        self.assertEqual([n.name for n in found], ["Nuke14.0v5"])

    # -- init.py ------------------------------------------------------------
    def test_init_block_roundtrip_keeps_user_lines(self):
        init = core.init_py_path()
        os.makedirs(os.path.dirname(init))
        with open(init, "w") as fh:
            fh.write("import nuke\nnuke.pluginAddPath('./mytools')\n"
                     "nuke.pluginAddPath('C:/old/GenFill/nuke')\n")
        res = core.set_init_block(os.path.join(self.tmp, "plugins", "GenFill") + os.sep)
        text = open(init).read()
        self.assertEqual(res["disabled"], ["nuke.pluginAddPath('C:/old/GenFill/nuke')"])
        self.assertIn("# (disabled by GenFill installer) nuke.pluginAddPath('C:/old", text)
        self.assertIn("nuke.pluginAddPath('./mytools')", text)
        self.assertEqual(text.count(core.BEGIN_MARK), 1)
        self.assertIn('pluginAddPath(r"%s")' % os.path.join(self.tmp, "plugins", "GenFill"), text)
        self.assertTrue(os.path.exists(init + ".genfill-backup"))
        core.set_init_block(os.path.join(self.tmp, "plugins", "GenFill"))   # idempotent
        self.assertEqual(open(init).read().count(core.BEGIN_MARK), 1)
        self.assertIsNotNone(core.clear_init_block())
        text = open(init).read()
        self.assertNotIn(core.BEGIN_MARK, text)
        self.assertIn("nuke.pluginAddPath('./mytools')", text)
        self.assertIsNone(core.clear_init_block())                          # nothing left

    def test_reads_block_written_by_install_ps1(self):
        ps1_block = "\r\n".join([core.BEGIN_MARK, "import nuke",
                                 'nuke.pluginAddPath(r"C:\\Users\\x\\.nuke\\plugins\\GenFill")',
                                 core.END_MARK]) + "\r\n"
        self.assertEqual(core.remove_init_block("a = 1\r\n\r\n" + ps1_block), "a = 1\r\n\r\n")

    def test_creates_init_py(self):
        res = core.set_init_block(os.path.join(self.tmp, "GenFill"))
        self.assertIsNone(res["backup"])
        self.assertTrue(open(core.init_py_path()).read().startswith(core.BEGIN_MARK))

    # -- install / uninstall ------------------------------------------------
    def test_install_copies_and_stamps(self):
        target = core.default_target()
        info = core.install_plugin(target, REPO_NUKE)
        self.assertEqual(info["version"], core.read_version(REPO_NUKE))
        self.assertTrue(os.path.exists(os.path.join(target, "init.py")))
        self.assertTrue(os.path.exists(os.path.join(target, "menu.py")))
        self.assertEqual(core.read_build_info(target)["source"], "GenFill Setup")
        self.assertEqual(core.installed_version(target), info["version"])
        for _dirpath, dirs, _files in os.walk(target):
            self.assertNotIn("__pycache__", dirs)
        stale = os.path.join(target, "stale.txt")
        open(stale, "w").close()
        core.install_plugin(target, REPO_NUKE)                              # update replaces
        self.assertFalse(os.path.exists(stale))

    def test_install_uses_bundled_build_info(self):
        src = os.path.join(self.tmp, "bundle", "nuke")
        shutil.copytree(REPO_NUKE, src, ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(src, "python", "genfill", "build_info.json"), "w") as fh:
            json.dump({"version": "9.9.9", "commit": "abc1234", "built": "2026-10-17 12:00"}, fh)
        info = core.install_plugin(core.default_target(), src)
        self.assertEqual((info["version"], info["commit"]), ("9.9.9", "abc1234"))

    def test_unsafe_targets_are_refused(self):
        for bad in (self.home, os.path.join(self.tmp, "plugins"), REPO_NUKE):
            with self.assertRaises(core.SetupError):
                core.install_plugin(bad, REPO_NUKE)
        with self.assertRaises(core.SetupError):
            core.assert_safe_target(os.path.join(self.tmp, "GenFill"), os.path.join(self.tmp, "GenFill"))

    def test_uninstall(self):
        target = core.default_target()
        core.install_plugin(target, REPO_NUKE)
        core.set_init_block(target)
        ai = os.path.join(self.tmp, "GenFill-LocalAI")
        os.makedirs(ai)
        os.makedirs(core.config_dir())
        core.uninstall(target, remove_ai_root=ai, remove_config=False)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(ai))
        self.assertTrue(os.path.isdir(core.config_dir()))                   # kept unless asked
        self.assertNotIn(core.BEGIN_MARK, open(core.init_py_path()).read())
        self.assertIsNone(core.installed_version(target))
        core.uninstall(target, remove_config=True)
        self.assertFalse(os.path.exists(core.config_dir()))


if __name__ == "__main__":
    unittest.main()
