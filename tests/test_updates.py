"""Check for Updates: version parsing and the GitHub lookups (network faked)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill import updates  # noqa: E402


def fake_get(responses):
    """get(url) -> (status, body) from {url: (status, body)}; records the URLs asked for."""
    asked = []

    def get(url):
        asked.append(url)
        status, body = responses[url]
        if isinstance(status, Exception):
            raise status
        return status, body
    get.asked = asked
    return get


def release(tag, body="", url="https://github.com/iconnorthomas/GenFill/releases/tag/x"):
    return 200, json.dumps({"tag_name": tag, "html_url": url, "body": body}).encode()


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updates.parse_version("v0.8.1"), (0, 8, 1))
        self.assertEqual(updates.parse_version("GenFill 0.10.0-beta"), (0, 10))
        self.assertEqual(updates.parse_version("0.8"), updates.parse_version("0.8.0"))
        self.assertIsNone(updates.parse_version("latest"))

    def test_is_newer(self):
        self.assertTrue(updates.is_newer("0.8.1", "0.8.0"))
        self.assertTrue(updates.is_newer("v0.10.0", "0.9.9"))     # numeric, not text order
        self.assertFalse(updates.is_newer("0.8.0", "0.8"))
        self.assertFalse(updates.is_newer("0.7.1", "0.8.0"))
        self.assertFalse(updates.is_newer("nonsense", "0.8.0"))


class LookupTests(unittest.TestCase):
    def test_uses_the_latest_release_when_there_is_one(self):
        get = fake_get({updates.RELEASES_API: release("v0.9.0", "Faster prompts.")})
        result = updates.check(get, current="0.8.0")
        self.assertTrue(result["newer"])
        self.assertEqual(result["latest"], "0.9.0")
        self.assertEqual(result["source"], "release")
        self.assertIn("releases/tag", result["url"])
        self.assertEqual(get.asked, [updates.RELEASES_API])
        text = updates.message(result)
        self.assertIn("GenFill 0.9.0 is available. You have 0.8.0.", text)
        self.assertIn("Faster prompts.", text)

    def test_falls_back_to_the_version_on_main_without_releases(self):
        get = fake_get({updates.RELEASES_API: (404, b""),
                        updates.VERSION_FILE: (200, b'import os\n__version__ = "0.8.0"\n')})
        result = updates.check(get, current="0.8.0")
        self.assertFalse(result["newer"])
        self.assertEqual(result["source"], "main")
        self.assertEqual(updates.message(result), "You have the latest GenFill (0.8.0).")

    def test_newer_build_than_published_is_up_to_date(self):
        get = fake_get({updates.RELEASES_API: release("v0.8.0")})
        self.assertFalse(updates.check(get, current="0.9.0")["newer"])

    def test_errors_are_reported_not_raised_raw(self):
        offline = fake_get({updates.RELEASES_API: (updates.UpdateError("no network"), None)})
        with self.assertRaises(updates.UpdateError):
            updates.check(offline)
        garbage = fake_get({updates.RELEASES_API: (200, b"<html>")})
        with self.assertRaises(updates.UpdateError):
            updates.check(garbage)
        no_version = fake_get({updates.RELEASES_API: (404, b""),
                               updates.VERSION_FILE: (200, b"nothing here")})
        with self.assertRaises(updates.UpdateError):
            updates.check(no_version)

    def test_long_release_notes_are_shortened(self):
        get = fake_get({updates.RELEASES_API: release("v1.0", "x" * 2000)})
        self.assertLess(len(updates.message(updates.check(get, current="0.8.0"))), 900)


class MenuTests(unittest.TestCase):
    def test_menu_command_reports_in_a_dialog(self):
        sys.path.insert(0, os.path.dirname(__file__))
        import fake_nuke
        nuke = fake_nuke.install()
        shown = []
        from unittest import mock
        with mock.patch.object(updates, "check", return_value={
                "current": "0.8.0", "latest": "0.8.0", "newer": False, "url": "", "source": "main",
                "notes": ""}), \
                mock.patch.object(nuke, "message", side_effect=shown.append):
            updates.check_for_updates()
            for t in __import__("threading").enumerate():
                if t.name == "GenFill-update-check":
                    t.join(10)
        self.assertEqual(shown, ["You have the latest GenFill (0.8.0)."])

        shown[:] = []
        with mock.patch.object(updates, "check", side_effect=updates.UpdateError("offline")), \
                mock.patch.object(nuke, "message", side_effect=shown.append):
            updates.check_for_updates()
            for t in __import__("threading").enumerate():
                if t.name == "GenFill-update-check":
                    t.join(10)
        self.assertEqual(len(shown), 1)
        self.assertIn("couldn't check for updates", shown[0])
        self.assertIn("offline", shown[0])


if __name__ == "__main__":
    unittest.main()
