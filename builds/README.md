# Builds

Ready-to-run **GenFill Setup** installers, one per platform. Each one carries the GenFill
plugin inside it, so it installs exactly the build it was made from, with no git, Python or
internet needed (except for the optional Local AI download).

| Folder | File | Built on |
|---|---|---|
| `windows/` | `GenFill-Setup-<version>-windows.exe` | Windows 10/11 x64 |
| `macos/` | `GenFill-Setup-<version>-macos-<arch>.zip` (contains the `.app`) | macOS, Apple Silicon (`arm64`) or Intel (`x86_64`) |
| `linux/` | `GenFill-Setup-<version>-linux-x86_64` | Linux x86_64 (glibc of the build machine or newer) |

## What Setup does

- **Install / Update:** copies the plugin to `~/.nuke/plugins/GenFill`, stamps it with the
  version and commit it was built from, and adds the managed block to `~/.nuke/init.py`
  (backed up first). It writes the same files as `installers/install.ps1` and uses the same
  `init.py` markers, so the two can be mixed.
- **Local AI (Windows):** optionally runs GenFill's own Local AI installer in the same window,
  with the model size picked from your GPU's VRAM and the FLUX.1 [dev] license to accept.
- **Remove:** deletes the plugin and the `init.py` block, and optionally the Local AI folder and
  your settings and API keys.
- **Done:** buttons to start each Nuke version it found. The setup log is
  `GenFill-Setup.log` in the system temp folder.

## Building

Run this on each OS you want an installer for (PyInstaller can't cross-compile):

```bash
python builds/build.py
```

The first run creates `builds/.venv-<os>` with the pinned tools in
[`requirements.txt`](requirements.txt) (PySide6-Essentials, PyInstaller, Pillow). The new
installer replaces the old one in this folder. Commit it as the last step before a release, so
the binary matches the code next to it.

Check a build without clicking through it:

```bash
builds/windows/GenFill-Setup-0.8.0-windows.exe --self-test result.json
```

`result.json` reports whether the bundled plugin, HTTPS, the Local AI installer and the window
all load, and which Nuke installs it found.

Notes:

- The plugin is bundled as plain files, not frozen into the app, because the Local AI helper
  starts `genfill/localai/worker.py` from disk. `build.py` scans the plugin's imports so the
  standard-library modules it needs are bundled too.
- **Unsigned.** Windows SmartScreen asks once (*More info > Run anyway*). On macOS, right-click
  the app and choose *Open* the first time, or sign and notarize it with a Developer ID.
- Keep each file under 100 MB (GitHub's limit). Builds are about 35-40 MB.
- PySide6 is LGPL v3. The app is a PyInstaller bundle, and the About page links to the Qt for
  Python source.
