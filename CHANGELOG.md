# Changelog

## Unreleased

- **Fix: local AI self-test fails on Windows with "CUDA driver error: unknown error"**
  ("Failed to create GPU mapping", `cuMemSetAccess`). The helper no longer turns on PyTorch's
  `expandable_segments` allocator on Windows: recent PyTorch builds implement it with CUDA
  virtual memory, which WDDM drivers can reject (seen with PyTorch 2.14 cu130, driver 610.88,
  RTX PRO 6000 Blackwell). Linux keeps it.

## 0.7.1 - 2026-10-13

- README rewritten for the current build, with a full colour science section (encode / generate /
  decode, the three encodings, Auto decisions, float precision, limits) and troubleshooting.

- **Roto tools now appear in the Viewer when the GenerativeFill panel is open** (with *selection
  from* = Shapes). Nuke only shows a Roto's Viewer tools while that Roto's own panel is open, so
  GenFill opens the built-in Roto's panel as a hidden floating window: you get the tools and the
  curve overlay, and the GenerativeFill panel stays the only one you see (works with a
  Properties bin limit of 1). Closing the GenerativeFill panel removes the tools.

## 0.7.0 - 2026-10-13

- **The selection is drawn on the GenerativeFill node itself.** The Roto now lives inside the
  node, so there's no separate *GenFillSelection* node: draw, edit and generate from one
  properties panel.
  - **✏ Draw Selection** (under the prompt panel) puts the Roto tools in the Viewer.
  - The Roto's **shape list** is linked onto the Generative Fill tab, so shapes can be renamed,
    toggled, animated and deleted there.
- *selection from* is now **Shapes (built-in Roto)** (default), **Mask input** or
  **Source alpha**. Connecting a matte to the Mask input switches to *Mask input* when no shapes
  are drawn; disconnecting it switches back to *Shapes*.
- Generating with no shapes says what to do instead of failing on an empty probe.
- Existing nodes are upgraded when their panel opens: the built-in Roto is added, their
  *selection from* choice is kept, and the Draw Selection button and shape list go on a new
  *Selection* tab. An external GenFillSelection Roto keeps working through the Mask input; you
  can copy its shapes into the built-in one (select in the shape list, Ctrl+C / Ctrl+V) and
  delete it.

## 0.6.0 - 2026-10-13

- **Colour pipeline rebuilt for HDR plates.** New *color pipeline* setting:
  - **Auto (match input)** (default): display-referred plates round-trip in their own Read
    colorspace (matte_paint / sRGB / LogC...), so the model sees the original file values;
    scene-linear plates (EXR, ACEScg) go through the **display transform**.
  - **Display transform**: e.g. ACES *Output - Rec.709* (auto-detected from the OCIO config or
    typed in *display colorspace*). Its tone curve rolls highlights into 0..1; the exact inverse
    brings them back above 1.
  - **Log (ACEScct)**: config-independent, keeps scene-linear up to ~222.
  - **Input colorspace** and **Custom**.
- **32-bit float transport** for the local model: float TIFF in, FLUX's float output back out,
  no 8-bit rounding anywhere (the old path was an 8-bit PNG in the 8-bit default colourspace,
  i.e. matte_paint, clamped at 1). Cloud models still get 8-bit PNG, with the same encoding.
- Each variation stores its encoding, so older variations always decode correctly; variations
  made by earlier versions decode the old way. Existing nodes are upgraded when their panel opens.
- Float results carry an 8-bit preview for the variation thumbnails.
- The log records the decision per generation (plate Read, its colorspace, chosen encoding).
- Local AI needs **Repair** once (adds the small `tifffile` package).

## 0.5.0 - 2026-10-12

- **Layer** output (replaces "Fill only"): just the generated fill in Source's format,
  premultiplied by the expanded + feathered selection, with that as alpha. "Fill only" output
  the raw patch in the crop's own format, so it came out black and oddly cropped.
- Existing GenerativeFill nodes are upgraded automatically when their panel opens.
- README: Privacy and security section (what runs locally, what touches the network, where
  data is stored).

## 0.4.7 - 2026-10-12

- Fix: self-test failed with "The paging file is too small" (os error 1455) while loading T5.
  safetensors maps a whole file copy-on-write and Windows reserves commit for all of it
  (9.5 GB, on top of FLUX parked in RAM and Nuke). Repair now splits the T5 (and any >2 GB
  text-encoder) weights into 1 GB shards with a streaming, pure-Python splitter, so the peak
  is ~1 GB. Already-split files aren't downloaded again.
- helper.log records free RAM and free commit at load time, before T5 and at any failure.
- Running out of Windows memory gives a plain explanation instead of a raw OSError.
- Confirmed by the 0.4.6 log: libraries now load in ~7 s (was minutes), FLUX loads in ~21 s.

## 0.4.6 - 2026-10-12

- Fix: the AI helper could hang (or take minutes) while importing PyTorch: in the helper the
  import took 3.5 min and the self-test stalled, while the same import in the installer took
  ~15 s. The helper now imports its libraries before any thread blocks reading the stdin pipe
  (on Windows a pending pipe read can stall work on the process's standard handles).
- Startup status ("Loading the AI libraries...") is shown in the panel / installer while the
  helper loads.
- helper.log now times each phase: each library import, CUDA init, reading FLUX, loading T5,
  and every variation.

## 0.4.5 - 2026-10-12

- Fix: 0.4.2-0.4.4 removed GenFill's private C++ runtime when Windows' was newer, which brought
  back `WinError 1114 ... c10.dll`. The test machine's log proved it: PyTorch imported with the
  private 14.44 runtime and failed right after it was replaced by Windows' 14.50. The private
  runtime is now always placed next to the interpreter and PyTorch (only GenFill's private
  Python uses it).

## 0.4.4 - 2026-10-12

- Easier to find: a **Local AI...** button next to the Model menu in the node's panel (shown when
  the Local model is selected), and a **GenFill** menu in Nuke's top menu bar with Install /
  Repair, Stop AI Helper, API Keys and Developer tools. Error messages point there.

## 0.4.3 - 2026-10-12

- Leftover AI helpers are cleaned up: a pid file records the helper, and the next start,
  Stop AI Helper, Reload GenFill and Repair all stop any orphan (e.g. a CPU job left running
  by an earlier version, which made the next start take minutes).
- Stopping the helper kills the whole process tree (the venv python.exe is a launcher with the
  real interpreter as a child).
- Helper errors copy the last lines of the helper's own log into genfill.log, and the helper
  logs when its libraries finished importing, so slow starts can be diagnosed.

## 0.4.2 - 2026-10-12

- Fix: Local AI ran on the CPU (looked frozen at "Starting the local AI..."). The installer had
  pulled the CPU-only PyTorch from PyPI because uv prefers `--extra-index-url` over
  `--index-url`. PyTorch now comes only from the official CUDA index (cu130, then cu128, cu126);
  an existing CPU build is detected and replaced on Repair, and the step only passes when
  PyTorch can actually use the GPU.
- The helper refuses to run on the CPU and says how to fix it, instead of taking hours.
- C++ runtime: Windows' own copy is used when it's newer (it was 14.50 vs the package's 14.44);
  older private copies added by 0.4.1 are removed. The real fix for `c10.dll` was 0.4.1's
  clean PATH.
- Libraries are precompiled at install time, so the helper's first start is much faster.
- "Loading the AI libraries..." progress while the helper starts.

## 0.4.1 - 2026-10-12

- Fix: Local AI install failed with `WinError 1114 ... c10.dll` (PyTorch needs a newer Microsoft
  C++ runtime than Windows had). New installer step adds the current runtime (msvc-runtime
  14.44) inside the GenFill-LocalAI folder, next to the interpreter and PyTorch's DLLs. No admin
  rights, nothing changed in Windows. Logs the system and private runtime versions.
- The helper and installer tools now run with a clean PATH (Windows folders only) and without
  Nuke/CUDA/OpenMP variables, so Nuke's own DLLs can't leak into PyTorch.

## 0.4.0 - 2026-10-12

- **Local AI now runs entirely inside the GenFill plugin.** No ComfyUI, no command windows.
  A hidden helper process (private Python + PyTorch, kept out of Nuke's process for stability)
  starts on the first Local generation, keeps the model warm, and exits with Nuke or after
  10 idle minutes. Crashes or GPU out-of-memory show as errors; Nuke keeps running.
- FLUX.1 Fill [dev] (GGUF) via diffusers for prompts; LaMa for removal (variation 1) plus
  LaMa + FLUX refinement for the others. Prompt encodings are cached, so "Generate again"
  with the same prompt skips the text model.
- **GenFill > Local AI > Install / Repair...**: in-Nuke installer dialog with model-size choice
  (Q4 recommended for 12 GB GPUs shared with Nuke), step list, progress, details log. Runs in
  the background, resumes interrupted downloads, uses only public downloads (no account),
  self-tests on your GPU, and removes the old ComfyUI leftovers.
- **GenFill > Local AI > Stop AI Helper** frees GPU memory on demand.
- Choosing Local when it isn't installed offers to open the installer.
- Removed the ComfyUI backend and its installer scripts (`install.bat` deletes the old files).

## 0.3.0 - 2026-10-12

- New model: **Local: FLUX.1 Fill**. Free, offline, runs on your own GPU through ComfyUI.
  Prompt -> FLUX.1 Fill [dev]; empty prompt -> LaMa removal (variation 1) plus LaMa + FLUX
  refinement (other variations). GenFill starts ComfyUI automatically. Quality knob maps to
  steps (low 16 / auto 24 / high 32).
- `installers/install_local_ai.bat`: downloads ComfyUI portable (NVIDIA), ComfyUI-GGUF,
  comfyui-inpaint-nodes, FLUX.1 Fill [dev] GGUF (Q6_K for 12 GB cards), text encoders, VAE
  and LaMa; writes the config; starts ComfyUI once to verify GPU, nodes and models. Resumable.
- `installers/start_local_ai.bat` to run ComfyUI in a visible window.
- The local model is now first in the model menu.
- Tests no longer write into your real log (`GENFILL_LOG`).
- Logging around saving the variation history, to track down repeated generation ids.

## 0.2.2 - 2026-10-12

- Log file: every Generate, each pipeline step and all errors with tracebacks go to
  `test_output/genfill.log` (falls back to `~/.nuke/genfill/genfill.log`).
- Fix: variation history is stored base64-encoded. The knob is TCL-evaluated, so raw JSON
  brackets could break saving results.
- Fix: node label no longer contains `[ ]` (TCL); prompts are sanitised for the label.
- No more silent failures: missing API key, an already-running job, errors while applying a
  result and errors in panel buttons are all shown in the panel and logged.
- The worker can no longer leave a node stuck in "running".
- Generate gives immediate "Preparing selection..." feedback.

## 0.2.1 - 2026-10-12

- Fix: "Nothing is named parent.gf_frame" errors on node creation. The Group's knobs are now
  added before the internal nodes whose expressions reference them.
- Tests: the fake nuke module now reports unresolved `parent.` expressions.

## 0.2.0 - 2026-10-12

- `installers/`: one-click install of the current build (runs tests with Nuke's Python first,
  clean copy, build stamp, managed init.py block), dev mode, uninstall, terminal smoke test.
- Build info: Nuke prints which build loaded; GenFill > Developer > Build Info.
- GenFill > Developer > Reload GenFill: pick up a new build without restarting Nuke.
- GenFill > Developer > Run Smoke Test: builds a test setup, generates offline, renders and
  writes `test_output/report.json` plus images and a panel screenshot.
- `engine.generate(..., blocking=True)` for tests and terminal mode.

## 0.1.0 - 2026-10-12

First version.

- GenerativeFill node (Group of native nodes; renders without the plugin).
- Photoshop-style panel: contextual prompt bar, Generate, variation thumbnails, ‹ › navigation.
- Empty prompt removes the selected content (Stability Erase).
- Backends: Stability AI (inpaint/erase), OpenAI GPT Image, Offline Preview.
- Colour-managed 8-bit SDR round trip; only pixels inside the selection are replaced.
- Context crop matched to the model's aspect ratio.
- Variation history stored on the node; results cached next to the script.
- API keys from environment variables or ~/.nuke/genfill/config.json (never in scripts).
