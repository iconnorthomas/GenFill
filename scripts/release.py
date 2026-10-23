"""GenFill version guard and release helper.

A version number isn't a version until it's posted as a GitHub Release, and
several branches can be in flight at once, so the number is checked at both
ends:

* ``check-pr``      (CI, every pull request) A branch that changes
  ``__version__`` must raise it above ``main`` and above every posted release,
  and must have moved its CHANGELOG notes from "Unreleased" to that version.
  Branches that don't touch the version always pass.
* ``check-release`` (release workflow, on a ``vX.Y.Z`` tag) The tag must equal
  ``__version__`` in the tagged commit, that commit must be on ``main``, the
  version must be newer than every earlier ``v*`` tag, and the CHANGELOG must
  have its section. Writes that section as the release notes.
* ``check-build``   (release workflow) A freshly built GenFill Setup's
  ``--self-test`` passed and it carries exactly this version and commit.
* ``bump X.Y.Z``    (you, on a release branch) Sets ``__version__`` and turns
  "## Unreleased" into "## X.Y.Z - <today>".

Run ``python scripts/release.py --help``. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = "nuke/python/genfill/__init__.py"
CHANGELOG = "CHANGELOG.md"
VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
STRICT_RE = re.compile(r"^\d+\.\d+\.\d+$")


class CheckError(Exception):
    pass


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def parse(version):
    """'0.8.0' / 'v0.8.0' -> (0, 8, 0). Raises CheckError for anything else."""
    v = (version or "").strip()
    v = v[1:] if v[:1] in ("v", "V") else v
    if not STRICT_RE.match(v):
        raise CheckError("%r is not a version like 1.2.3" % version)
    return tuple(int(p) for p in v.split("."))


def version_in(text):
    m = VERSION_RE.search(text or "")
    if not m:
        raise CheckError("no __version__ in %s" % VERSION_FILE)
    return m.group(1)


def changelog_sections(text):
    """[(heading title, body)] for every '## ' heading, in order."""
    parts = re.split(r"^## +(.+?)\s*$", text, flags=re.M)
    return [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def changelog_section(text, version):
    """Body of '## X.Y.Z - date' (or '## X.Y.Z'), or None."""
    for title, body in changelog_sections(text):
        if re.match(r"^%s(\s|$)" % re.escape(version), title):
            return body
    return None


def unreleased_notes(text):
    for title, body in changelog_sections(text):
        if title.lower() == "unreleased":
            return body
    return ""


def release_tags(tags):
    """Versions of the 'vX.Y.Z' tags in ``tags``, newest first (others ignored)."""
    out = []
    for t in tags:
        try:
            out.append((parse(t), t))
        except CheckError:
            pass
    return [t for _v, t in sorted(out, reverse=True)]


def check_pr(head_version, base_version, posted_tags, changelog):
    """Problems with a pull request's version (empty list = fine)."""
    if head_version == base_version:
        return []
    problems = []
    try:
        new = parse(head_version)
    except CheckError as exc:
        return [str(exc)]
    if new <= parse(base_version):
        problems.append("__version__ %s must be higher than main's %s (another branch may have "
                        "posted it first; merge main and pick the next number)"
                        % (head_version, base_version))
    tags = release_tags(posted_tags)
    if tags and new <= parse(tags[0]):
        problems.append("__version__ %s must be higher than the latest posted release %s"
                        % (head_version, tags[0]))
    if "v%s" % head_version in posted_tags:
        problems.append("v%s is already posted" % head_version)
    if changelog_section(changelog, head_version) is None:
        problems.append('CHANGELOG.md has no "## %s - <date>" section (run: python '
                        "scripts/release.py bump %s)" % (head_version, head_version))
    if unreleased_notes(changelog):
        problems.append('CHANGELOG.md still has notes under "## Unreleased"; a version bump '
                        "moves them into the new version's section")
    return problems


def check_release(tag, version, earlier_tags, changelog, on_main):
    """Problems with posting ``tag`` (empty list = fine)."""
    problems = []
    if tag != "v%s" % version:
        problems.append("tag %s doesn't match __version__ %s in that commit (expected v%s)"
                        % (tag, version, version))
    try:
        new = parse(version)
    except CheckError as exc:
        return problems + [str(exc)]
    if not on_main:
        problems.append("the tagged commit isn't on main; tag the merge commit on main")
    older = [t for t in release_tags(earlier_tags) if t != tag]
    if older and new <= parse(older[0]):
        problems.append("%s isn't newer than the latest earlier release %s" % (version, older[0]))
    notes = changelog_section(changelog, version)
    if not notes:
        problems.append('CHANGELOG.md has no notes under "## %s - <date>"' % version)
    return problems


def check_build(result, version, commit=None):
    """Problems with a GenFill Setup ``--self-test`` result (empty list = fine)."""
    problems = []
    if not result.get("ok"):
        problems.append("the Setup self-test failed: %s"
                        % (result.get("error") or result.get("checks")))
    info = (result.get("checks") or {}).get("build_info") or {}
    if info.get("version") != version:
        problems.append("the Setup carries GenFill %r, not %s" % (info.get("version"), version))
    if info.get("dirty"):
        problems.append("the Setup was built from uncommitted changes")
    if commit and info.get("commit") and not commit.startswith(info["commit"]):
        problems.append("the Setup was built from commit %s, not %s" % (info["commit"], commit))
    return problems


def bump(init_text, changelog, version, today=None):
    """(new __init__ text, new CHANGELOG text) for releasing ``version``."""
    parse(version)
    if changelog_section(changelog, version) is not None:
        raise CheckError("CHANGELOG.md already has a %s section" % version)
    if not unreleased_notes(changelog):
        raise CheckError('there are no notes under "## Unreleased" to release')
    current = version_in(init_text)
    if parse(version) <= parse(current):
        raise CheckError("%s isn't higher than the current %s" % (version, current))
    today = today or datetime.date.today().isoformat()
    init_text = VERSION_RE.sub('__version__ = "%s"' % version, init_text, count=1)
    changelog = re.sub(r"^## +Unreleased[ \t]*$", "## Unreleased\n\n## %s - %s" % (version, today),
                       changelog, count=1, flags=re.M | re.I)
    return init_text, changelog


# --------------------------------------------------------------------------
# git / files
# --------------------------------------------------------------------------

def git(*args):
    return subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    with open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _report(problems, ok_message):
    if problems:
        for p in problems:
            print("ERROR: %s" % p)
        return 1
    print(ok_message)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check-pr", help="CI: check a pull request's version against main")
    p.add_argument("--base", default="origin/main", help="git ref of the base branch")
    p = sub.add_parser("check-release", help="release workflow: check a vX.Y.Z tag")
    p.add_argument("--tag", required=True)
    p.add_argument("--main", default="origin/main", help="git ref of main")
    p.add_argument("--notes", help="write the release notes (CHANGELOG section) here")
    p = sub.add_parser("check-build", help="release workflow: check a Setup --self-test result")
    p.add_argument("result", help="the result.json written by GenFill-Setup --self-test")
    p.add_argument("--version", required=True)
    p.add_argument("--commit", help="the commit being released (full or short SHA)")
    sub.add_parser("version", help="print __version__")
    p = sub.add_parser("bump", help="set __version__ and date the Unreleased CHANGELOG notes")
    p.add_argument("version")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "check-pr":
            head = version_in(read(VERSION_FILE))
            base = version_in(git("show", "%s:%s" % (args.base, VERSION_FILE)))
            tags = git("tag", "--list", "v*").split()
            problems = check_pr(head, base, tags, read(CHANGELOG))
            msg = ("version unchanged (%s)" % head if head == base
                   else "version %s -> %s is ready to post" % (base, head))
            return _report(problems, msg)
        if args.cmd == "check-release":
            version = version_in(read(VERSION_FILE))
            tags = git("tag", "--list", "v*").split()
            on_main = subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", args.main],
                                     cwd=ROOT).returncode == 0
            changelog = read(CHANGELOG)
            problems = check_release(args.tag, version, tags, changelog, on_main)
            if not problems and args.notes:
                with open(args.notes, "w", encoding="utf-8") as fh:
                    fh.write(changelog_section(changelog, version) + "\n")
            return _report(problems, "%s is ready to post" % args.tag)
        if args.cmd == "version":
            print(version_in(read(VERSION_FILE)))
            return 0
        if args.cmd == "check-build":
            import json
            try:
                with open(args.result, encoding="utf-8") as fh:
                    result = json.load(fh)
            except (OSError, ValueError) as exc:
                raise CheckError("couldn't read the self-test result %s: %s" % (args.result, exc))
            problems = check_build(result, args.version.lstrip("vV"), args.commit)
            return _report(problems, "GenFill Setup %s passed its self-test" % args.version)
        if args.cmd == "bump":
            init_text, changelog = bump(read(VERSION_FILE), read(CHANGELOG), args.version)
            write(VERSION_FILE, init_text)
            write(CHANGELOG, changelog)
            print("__version__ = %s; CHANGELOG notes moved under '## %s'. Commit this on a "
                  "release branch, merge the PR, then tag the merge commit v%s and push the tag."
                  % (args.version, args.version, args.version))
            return 0
    except (CheckError, subprocess.CalledProcessError) as exc:
        print("ERROR: %s" % (getattr(exc, "stderr", "") or exc))
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
