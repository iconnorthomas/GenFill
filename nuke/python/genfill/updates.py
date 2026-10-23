"""GenFill > Check for Updates: is there a newer GenFill on GitHub?

Only runs when the artist asks (no background checks), only reads public data
from GitHub, and never downloads or installs anything: it says what's
available and offers to open the download page.

The newest version is the latest GitHub Release if the repository has any,
otherwise ``__version__`` in ``nuke/python/genfill/__init__.py`` on ``main``.
"""
from __future__ import annotations

import json
import re
import threading

from . import __version__

REPO = "iconnorthomas/GenFill"
BRANCH = "main"
RELEASES_API = "https://api.github.com/repos/%s/releases/latest" % REPO
VERSION_FILE = "https://raw.githubusercontent.com/%s/%s/nuke/python/genfill/__init__.py" % (
    REPO, BRANCH)
RELEASES_PAGE = "https://github.com/%s/releases" % REPO
INSTALL_PAGE = "https://github.com/%s#install" % REPO
TIMEOUT = 10


class UpdateError(Exception):
    """Couldn't find out (offline, GitHub unreachable, unexpected reply)."""


def parse_version(text):
    """'v0.8.1' / '0.8.1' / '0.9.0-beta' -> (0, 8, 1) etc.; None if there's no version."""
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:   # 0.8 == 0.8.0
        parts.pop()
    return tuple(parts)


def is_newer(remote, local):
    r, l = parse_version(remote), parse_version(local)
    return r is not None and l is not None and r > l


def _http_get(url):
    """Returns (status, body bytes); status 404 is returned, other failures raise UpdateError."""
    from urllib import request
    from urllib.error import HTTPError, URLError
    req = request.Request(url, headers={"User-Agent": "GenFill-Nuke/%s" % __version__,
                                        "Accept": "application/vnd.github+json"})
    try:
        with request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.getcode(), resp.read()
    except HTTPError as exc:
        if exc.code == 404:
            return 404, b""
        raise UpdateError("GitHub replied HTTP %s." % exc.code)
    except (URLError, OSError) as exc:
        raise UpdateError("Couldn't reach GitHub (%s). Check your internet connection."
                          % getattr(exc, "reason", exc))


def latest(get=_http_get):
    """{'version', 'url', 'source', 'notes'} for the newest published GenFill."""
    status, body = get(RELEASES_API)
    if status == 200:
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            raise UpdateError("GitHub sent an unexpected reply.")
        version = data.get("tag_name") or data.get("name") or ""
        if parse_version(version) is None:
            raise UpdateError("The latest GitHub release (%r) has no version number." % version)
        return {"version": version.lstrip("vV"), "url": data.get("html_url") or RELEASES_PAGE,
                "source": "release", "notes": (data.get("body") or "").strip()}
    # No releases yet: the version on main is what's published.
    status, body = get(VERSION_FILE)
    if status != 200:
        raise UpdateError("Couldn't find GenFill's version on GitHub.")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', body.decode("utf-8", "replace"))
    if not m:
        raise UpdateError("Couldn't read GenFill's version on GitHub.")
    return {"version": m.group(1), "url": INSTALL_PAGE, "source": "main", "notes": ""}


def check(get=_http_get, current=__version__):
    """{'current', 'latest', 'newer', 'url', 'source', 'notes'}; raises UpdateError."""
    info = latest(get)
    return dict(info, current=current, latest=info["version"],
                newer=is_newer(info["version"], current))


def message(result):
    """What to tell the artist (plain text)."""
    if result["newer"]:
        text = ("GenFill %s is available. You have %s.\n\n"
                "To update, download it from GitHub and run GenFill Setup or "
                "installers\\install.bat, then restart Nuke."
                % (result["latest"], result["current"]))
        if result.get("notes"):
            notes = result["notes"]
            text += "\n\nWhat's new:\n%s" % (notes if len(notes) <= 600 else notes[:600] + "...")
        return text
    return "You have the latest GenFill (%s)." % result["current"]


# --------------------------------------------------------------------------
# menu command (Nuke)
# --------------------------------------------------------------------------

def check_for_updates():
    """Menu entry point. Asks GitHub on a background thread; answers in a dialog."""
    import nuke
    from . import log as gflog

    def work():
        try:
            result = check()
            gflog.info("update check: installed %s, latest %s (%s)", result["current"],
                       result["latest"], result["source"])
        except UpdateError as exc:
            gflog.warning("update check failed: %s", exc)
            result = {"error": str(exc)}
        except Exception as exc:     # never let a menu click raise into Nuke
            gflog.exception("update check failed")
            result = {"error": "Unexpected error: %s" % exc}
        nuke.executeInMainThread(_show, args=(result,))

    threading.Thread(target=work, name="GenFill-update-check", daemon=True).start()


def _show(result):
    import nuke
    if "error" in result:
        nuke.message("GenFill couldn't check for updates.\n\n%s" % result["error"])
        return
    if result["newer"]:
        if nuke.ask(message(result) + "\n\nOpen the download page?"):
            import webbrowser
            webbrowser.open(result["url"])
    else:
        nuke.message(message(result))
