"""scripts/release.py: the version guard and release checks."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import release  # noqa: E402

CHANGELOG = """# Changelog

## Unreleased

- New thing.

## 0.8.0 - 2026-10-17

- Setup.

## 0.7.1 - 2026-10-13

- Fix.
"""

RELEASED = CHANGELOG.replace("## Unreleased\n\n- New thing.\n",
                             "## Unreleased\n\n## 0.9.0 - 2026-10-23\n\n- New thing.\n")
INIT = 'import os\n\n__version__ = "0.8.0"\n'


class ParseTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(release.parse("v0.10.2"), (0, 10, 2))
        for bad in ("0.9", "0.9.0-beta", "latest", ""):
            with self.assertRaises(release.CheckError):
                release.parse(bad)

    def test_changelog(self):
        self.assertEqual(release.unreleased_notes(CHANGELOG), "- New thing.")
        self.assertEqual(release.changelog_section(CHANGELOG, "0.8.0"), "- Setup.")
        self.assertIsNone(release.changelog_section(CHANGELOG, "0.8"))   # no partial matches
        self.assertEqual(release.unreleased_notes(RELEASED), "")
        self.assertEqual(release.changelog_section(RELEASED, "0.9.0"), "- New thing.")

    def test_release_tags_sorted_numerically(self):
        self.assertEqual(release.release_tags(["v0.9.0", "v0.10.0", "junk", "v0.8.1"]),
                         ["v0.10.0", "v0.9.0", "v0.8.1"])


class PullRequestTests(unittest.TestCase):
    def test_branches_that_dont_touch_the_version_pass(self):
        self.assertEqual(release.check_pr("0.8.0", "0.8.0", [], CHANGELOG), [])

    def test_a_proper_bump_passes(self):
        self.assertEqual(release.check_pr("0.9.0", "0.8.0", ["v0.8.0"], RELEASED), [])

    def test_another_branch_got_there_first(self):
        # main is already at 0.9.0 (another session's release merged): 0.9.0 again is stale.
        problems = release.check_pr("0.9.0", "0.9.1", ["v0.9.1"], RELEASED)
        self.assertTrue(any("higher than main's 0.9.1" in p for p in problems), problems)

    def test_must_beat_posted_releases(self):
        problems = release.check_pr("0.9.0", "0.8.0", ["v0.9.0", "v0.9.5"], RELEASED)
        self.assertTrue(any("latest posted release v0.9.5" in p for p in problems), problems)
        self.assertTrue(any("already posted" in p for p in problems), problems)

    def test_bump_needs_its_changelog_section_and_no_leftovers(self):
        problems = release.check_pr("0.9.0", "0.8.0", [], CHANGELOG)
        self.assertTrue(any('no "## 0.9.0' in p for p in problems), problems)
        self.assertTrue(any("Unreleased" in p for p in problems), problems)

    def test_lowering_or_garbage_fails(self):
        self.assertTrue(release.check_pr("0.7.0", "0.8.0", [], RELEASED))
        self.assertTrue(release.check_pr("0.9", "0.8.0", [], RELEASED))


class ReleaseTests(unittest.TestCase):
    def test_good_release(self):
        self.assertEqual(release.check_release("v0.9.0", "0.9.0", ["v0.8.0", "v0.9.0"], RELEASED,
                                               on_main=True), [])

    def test_tag_must_match_the_code(self):
        problems = release.check_release("v0.9.1", "0.9.0", [], RELEASED, on_main=True)
        self.assertTrue(any("doesn't match __version__ 0.9.0" in p for p in problems), problems)

    def test_must_be_on_main_newer_and_documented(self):
        problems = release.check_release("v0.8.0", "0.8.0", ["v0.9.0", "v0.8.0"], RELEASED,
                                         on_main=False)
        self.assertTrue(any("isn't on main" in p for p in problems), problems)
        self.assertTrue(any("isn't newer than" in p for p in problems), problems)
        problems = release.check_release("v1.0.0", "1.0.0", [], RELEASED, on_main=True)
        self.assertTrue(any("no notes" in p for p in problems), problems)


class BumpTests(unittest.TestCase):
    def test_bump(self):
        init, log = release.bump(INIT, CHANGELOG, "0.9.0", today="2026-10-23")
        self.assertIn('__version__ = "0.9.0"', init)
        self.assertEqual(log, RELEASED)
        self.assertEqual(release.check_pr("0.9.0", "0.8.0", ["v0.8.0"], log), [])

    def test_bump_refuses_mistakes(self):
        for version, log in (("0.8.0", CHANGELOG),           # not higher
                             ("0.9", CHANGELOG),              # not X.Y.Z
                             ("0.9.0", RELEASED),             # already has a section
                             ("0.9.0", CHANGELOG.replace("- New thing.\n", ""))):  # nothing new
            with self.assertRaises(release.CheckError, msg=version):
                release.bump(INIT, log, version)


if __name__ == "__main__":
    unittest.main()
