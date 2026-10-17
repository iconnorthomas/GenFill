# GenFill: Generative Fill for Nuke

Photoshop's Generative Fill as a Nuke node. Draw a selection on the node, type what you want (or
leave the prompt empty to remove what's there), click **Generate** and pick from the variations.
The plate never leaves your color-managed, scene-linear comp: no round trip through Photoshop,
no 8-bit sRGB export, no clipped highlights.

![node](nuke/icons/GenFill.png)

- **One node, one panel.** The selection, prompt, variations, blend controls and output all
  live on the GenerativeFill node. The Roto tools appear in the Viewer as soon as its panel is open.
- **Free and local.** FLUX.1 Fill and LaMa run on your NVIDIA GPU inside the plugin. Nothing is
  uploaded and there are no tokens. Cloud models (Stability AI, OpenAI) are optional.
- **Linear-safe.** An invertible color encoding plus 32-bit float transport means generated
  pixels come back into your working space with highlights above 1.0 intact. See
  [Color science](#color-science-a-linear-pipeline-with-display-referred-models).
- **Farm-safe.** The node is a Group of native Nuke nodes. Once you've picked a variation, the
  script renders anywhere, even where GenFill isn't installed.

Current version: **0.7.1** (see [CHANGELOG.md](CHANGELOG.md)).

---

## Contents

- [Photoshop vs GenFill](#photoshop-vs-genfill)
- [Install](#install)
- [Quick start](#quick-start)
- [The node](#the-node)
- [Models](#models)
- [Color science: a linear pipeline with display-referred models](#color-science-a-linear-pipeline-with-display-referred-models)
- [Privacy and security](#privacy-and-security)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Development](#development)
- [Licenses](#licenses)

---

## Photoshop vs GenFill

| Photoshop | GenFill in Nuke |
|---|---|
| Lasso / marquee selection | **✏ Draw Selection**: Roto shapes drawn on the node itself, with the shape list in the same panel. Or use any matte in the **Mask** input, or the Source alpha |
| Contextual task bar: prompt + **Generate** | The same bar at the top of the node's properties. Enter generates |
| Empty prompt = remove and fill from surroundings | Same. Locally: LaMa for a clean plate, then FLUX variations with texture. Stability AI: their *Erase* model |
| 3 variations per click, browse with ‹ › | Same: thumbnail grid of every generation (newest first), ‹ 1/6 › arrows; right-click to reuse a prompt, reveal or delete |
| Generative layer with a layer mask | **output: Comp** (fill blended over the plate through the expanded, feathered selection) or **Layer** (just the fill, in the plate's format, premultiplied by that selection) |
| 8-bit sRGB document | Scene-linear float. The model sees an invertible encoding of the plate and its result is decoded back |

---

## Install

### 1. The plugin

**Easiest: GenFill Setup.** Download the installer for your system from [`builds/`](builds/)
(Windows `.exe`, macOS `.zip`, Linux binary) and run it. It installs or updates GenFill, can
set up the Local AI on Windows in the same window, and can remove GenFill again. See
[builds/README.md](builds/README.md).

**From a git checkout (Windows):** double-click **`installers\install.bat`**. It:

1. runs the unit tests with Nuke's own Python (and installs nothing if they fail),
2. copies the build to `%USERPROFILE%\.nuke\plugins\GenFill`, stamped with version and git commit,
3. adds one managed block to your `.nuke\init.py` (backed up first).

Run it again whenever you want the latest build. Restart Nuke afterwards. Details, dev mode
(live from the repo) and uninstall are in [installers/README.md](installers/README.md).

Supported: Nuke 13 to 17 (Python 3, PySide2 or PySide6). GenFill Setup runs on Windows, macOS
and Linux; the `install.bat` scripts are Windows-only.

### 2. The free local AI (optional, recommended)

In Nuke: **GenFill > Local AI > Install / Repair...** (also the **⚙ Local AI...** button on the
panel). It runs in the background with a progress dialog and needs:

- an NVIDIA GPU with 8 GB+ VRAM (12 GB recommended) and a recent driver,
- about 25 GB of disk in `%USERPROFILE%\GenFill-LocalAI` (deliberately outside OneDrive),
- internet for this one-time download only.

It builds a private Python 3.12 with CUDA PyTorch (it never touches Nuke's Python), installs
diffusers/transformers, downloads the models and runs a GPU self-test. If anything later goes
wrong (a driver update, a moved folder), run **Install / Repair** again: it only redoes what's
broken.

The model size is picked from your VRAM and can be changed in the dialog:

| Size | FLUX.1 Fill model | For |
|---|---|---|
| Standard (Q4_K_S) | 6.8 GB | 12 GB GPUs alongside Nuke (default on a 3080 Ti) |
| High (Q6_K) | 9.9 GB | 16 GB+ |
| Maximum (Q8_0) | 12.7 GB | 24 GB |

### 3. Cloud keys (optional)

**GenFill > API Keys...** or set `STABILITY_API_KEY` / `OPENAI_API_KEY`. Keys are stored in
`~\.nuke\genfill\config.json`, never in .nk scripts.

### Check the build

**GenFill > Developer > Run Smoke Test** builds a throwaway setup, generates with the offline
backend and writes a report plus images to `test_output\`. **GenFill > Developer > Build Info**
shows which build is loaded.

---

## Quick start

1. Select your plate, create **GenFill > Generative Fill** (or Tab → "Generative Fill") and view it.
2. With its panel open, the Roto tools are in the Viewer. Draw around what you want to replace.
   Edit, toggle or animate shapes in the **shapes** list right in the panel.
3. Type a prompt (*"a weathered wooden bench"*) or leave it empty to remove the object. Press
   **Generate** (or Enter).
4. Click thumbnails or use ‹ › to pick a variation. Generate again to add more.
5. Adjust **expand** / **feather** to blend, and set **output** to *Comp* or *Layer*.

Prefer your own matte? Connect it to the **Mask** input and *selection from* switches to it
automatically when no shapes are drawn.

The fill is generated on the current frame and held. The *reference frame* (Settings tab) shows
which frame a variation came from.

---

## The node

```
         Mask (optional)
            │
Source ── GenerativeFill ── output
```

### Generative Fill tab

| Control | Meaning |
|---|---|
| Prompt bar + **Generate** | What to generate. Empty = remove. Enter generates |
| Model menu / ⚙ Local AI... | Backend picker and the local AI manager |
| Variations | Thumbnails of every generation; ‹ › to browse; right-click for reuse prompt / reveal / delete |
| **selection from** | *Shapes (built-in Roto)* (default), *Mask input*, or *Source alpha* |
| **✏ Draw Selection** | Switches to Shapes and makes sure the Roto tools are in the Viewer |
| **shapes** | The built-in Roto's shape list (add, rename, hide, lock, animate, delete) |
| **expand** | Grow the selection before generating, in pixels (like Select > Modify > Expand) |
| **feather** | Soften the edge where the fill is blended back |
| **output** | *Comp*: plate with the fill blended in. *Layer*: only the fill, premultiplied by the expanded, feathered selection, in the plate's format. *Bypass*: the plate |

### Settings tab

| Knob | Meaning |
|---|---|
| model / model version | Backend, and an optional model override for cloud backends (e.g. `gpt-image-1.5`) |
| quality | Speed vs quality (local: number of steps; OpenAI: quality level) |
| variations | Variations per click (default 3, like Photoshop) |
| seed | 0 = new random seed each click |
| negative prompt | Things to avoid (Stability) |
| context | How much surrounding image the model sees, as a fraction of the selection size (default 0.35) |
| min region | Smallest crop sent, in plate pixels (default 512) |
| **color pipeline** | How the plate is encoded for the model. See [Color science](#color-science-a-linear-pipeline-with-display-referred-models) |
| display colorspace | The display transform to use (`auto` = picked from your OCIO config, e.g. `Output - Rec.709`) |
| custom colorspace | Used when *color pipeline* is *Custom* |
| cache folder | Where results are stored. Empty = `<script folder>/genfill_cache/<node id>` |
| reference frame | Frame the selected variation was generated on |

### What happens on Generate

1. The selection is probed at low resolution to find its bounds (works for any matte).
2. A **context crop** is chosen like Photoshop does: the selection plus a band of surroundings,
   at an aspect ratio and resolution the model likes, so nothing gets stretched.
3. The crop is **encoded** for the model (next section) and written as a 32-bit float TIFF
   (local) or 8-bit PNG (cloud), together with the mask.
4. The model generates N variations. Locally this happens in a hidden helper process; the
   panel shows progress and Cancel.
5. Each result is read back raw, **decoded** with the exact inverse, placed back at the crop's
   position and blended through the expanded, feathered selection.

Everything is stored per generation (region, frame, encoding, files), so selecting an older
variation restores it exactly, even if you've changed the selection or color settings since.

---

## Models

| Model | Good at | Notes |
|---|---|---|
| **Local AI: FLUX.1 Fill [dev]** | Highest-quality fills, adding objects; free, offline | Your NVIDIA GPU, a hidden helper process (no windows, no ComfyUI). Float in, float out. **Non-commercial license** |
| **Local AI: FLUX.2 [klein] 4B** | Fast fills (4 steps); free, offline | Same helper and float pipeline. **Apache 2.0: licensed for commercial and studio work.** About 16 GB download; needs ~12 GB of VRAM |
| Local removal (empty prompt) | Clean plates | Variation 1 is **LaMa** (fast, faithful); later variations run the installed fill model from the LaMa result at strength 0.8 for natural texture |
| Stability AI | Masked inpainting; *Erase* for removal | Cloud, paid. 8-bit |
| OpenAI GPT Image | Prompt following | Cloud, paid. Re-renders the crop; GenFill composites it back through the mask. 8-bit |
| Offline Preview | Testing the pipeline | No AI; smears surrounding color into the selection |

### How the local AI runs

```
Nuke (GenFill plugin)                          %USERPROFILE%\GenFill-LocalAI\
  backends/local.py                              env\  private Python 3.12 + CUDA PyTorch
  localai/client.py ── JSON over a private ──▶   localai/worker.py  (hidden helper)
                       stdin/stdout pipe           FLUX.1 Fill [dev] GGUF (diffusers)
                                                   T5-XXL + CLIP-L text encoders, FLUX VAE
                                                   LaMa (TorchScript)
```

- PyTorch and ~10 GB of weights never load into Nuke. A crash or out-of-memory error becomes an
  error in the panel; Nuke and your script stay alive, and PyTorch's CUDA libraries can't clash
  with Nuke's (CopyCat, Cattery).
- The helper starts on the first local generation and keeps the model loaded between
  generations. It exits when Nuke closes, after 10 idle minutes, or on **Local AI > Stop AI Helper**.
- To fit 12 GB, the FLUX model stays on the GPU and the 9.5 GB T5 encoder is loaded only while
  a new prompt is encoded (prompt encodings are cached). The first generation after the helper starts
  takes longer while the models load.

---

## Color science: a linear pipeline with display-referred models

### The problem

Every image generator available today (FLUX, Stable Diffusion, GPT Image, Firefly) is
**display-referred**. They were trained on hundreds of millions of ordinary photographs:
gamma-encoded, sRGB-ish images where values run from 0 (black) to 1 (display white). There is no
concept of scene-linear light, nothing above 1.0 and no color management. Inside, FLUX turns the
image into a latent with a VAE trained on those same images, and the output comes back in the
same 0..1 display range.

A Nuke comp is the opposite: **scene-linear** values (ACEScg, linear sRGB...) where 0.18 is mid
gray, a white shirt might be 0.9, a window 4.0 and a specular glint 50.0.

Feed scene-linear values straight into a model and two things go wrong:

1. **It looks wrong to the model.** Linear values viewed as display values look dark and
   contrasty: mid gray 0.18 reads as near-black. The model "fixes" what it sees, so the fill
   comes out too bright, too flat or the wrong color once you're back in linear.
2. **Highlights are destroyed.** Everything above 1.0 has to be clamped to 1.0 before the model
   sees it, and nothing the model generates can be brighter than 1.0. A sky at 3.0 comes back
   as a flat 1.0 plateau.

That's exactly what happens when you export a plate to Photoshop as an 8-bit sRGB / matte_paint
image: highlights clipped at 1, quantized to 256 levels.

### The principle: encode, generate, decode

You can't make the model work in linear, but you don't have to. Pick an **invertible transfer
function** *E* that maps the plate's whole range into 0..1 *and* makes it look like a normal
picture. Run the model in that space and map the result back with the exact inverse *E⁻¹*:

```
scene-linear plate ──E──▶ 0..1 "picture" ──▶ model ──▶ 0..1 "picture" ──E⁻¹──▶ scene-linear fill
   (0.18, 4.0, 50.0)       (what the model                (what the model         (0.18, 4.0, 50.0
                            understands)                   generates)               style values)
```

- **Unchanged pixels round-trip exactly.** If the model leaves a pixel alone, E⁻¹(E(x)) = x
  (to float precision), so the context around the fill matches the plate.
- **Generated pixels inherit the plate's range.** When the model paints a highlight at 0.95 in
  encoded space, E⁻¹ turns it back into a real scene-linear highlight, say 30.0, not a clipped
  1.0. The model never needs to know what linear light is. It paints a believable picture and
  the inverse transfer turns that picture into believable light.
- **Only the selection is replaced.** The comp merges the decoded fill over the *original*
  Source through the feathered selection. Outside the matte the plate is passed through
  untouched, so nothing outside the matte is affected by the encoding or the model.

In Nuke this is built from native nodes inside the Group, so it renders on the farm without
GenFill:

```
Source ─ Crop ─ Reformat ─ [to_model:   Expression (ACEScct) | OCIOColorSpace | none] ─ Write  (float TIFF, raw)
                                                                                         │
                                                                                       model
                                                                                         │
Read (raw) ─ [from_model: inverse Expression | inverse OCIOColorSpace | none] ─ Reformat ─ Transform ─▶ Merge over Source
```

The Write and Read are set to **raw**, so Nuke applies no extra color conversion of its own and
the encoding nodes are the only color transform in the chain.

### The encodings

Choose on the Settings tab under **color pipeline**.

#### Display transform (e.g. ACES `Output - Rec.709`): what the model sees best

The show's display transform, such as ACES RRT + Rec.709 ODT, is literally the function that
turns scene-linear light into the picture you look at. Run the plate through it and the model sees
the same image you see in the Viewer, which looks like the photographs it was trained on. The
transform's filmic tone curve rolls highlights off smoothly into the top of 0..1 instead of
clipping them, and Nuke's OCIO inverse expands them back.

This is the default for scene-linear plates. In practice it gives the most natural fills,
because the model is working on an image that looks the way it expects (this matches artists'
experience that fills made "in Output - Rec.709" integrate better than ones made in
matte_paint / sRGB).

Things to know:

- **The inverse is steep at the top.** The tone curve squeezes a huge range of highlight
  values into the last few percent of 0..1, so the inverse stretches that region back out.
  A tiny difference near display white becomes a large difference in linear. This is why GenFill
  sends **float** to the local model: with 8-bit files, each step near white becomes a big jump in
  linear, which shows up as banding and posterized highlights after the inverse.
- **There's a ceiling.** Display white (1.0) decodes to the display transform's maximum
  scene value. Anything brighter in the plate is clamped to that maximum inside the fill. Pixels
  outside the selection are untouched.
- **Gamut.** Display transforms map to the display's gamut (Rec.709). Very saturated
  scene colors outside it come back approximated. Use Log when exact saturated color matters.
- It needs an output transform in your OCIO config. GenFill finds one automatically
  (`Output - Rec.709`, Rec.1886, sRGB display...) or uses the *display colorspace* you set. If the
  config has none, it falls back to Log.

#### Log (ACEScct): maximum headroom, exact maths

ACEScct is the Academy's log encoding for grading. GenFill implements it as plain Expression
nodes, with no OCIO config needed:

```
encode:  y = (log2(x) + 9.72) / 17.52           for x > 0.0078125
         y = 10.5402377416545 · x + 0.0729055341958355   otherwise (linear toe)

decode:  x = 2^(17.52 · y − 9.72)               for y > 0.155251141552511
         x = (y − 0.0729055341958355) / 10.5402377416545   otherwise
```

| Scene-linear | ACEScct value |
|---|---|
| −0.0069 | 0.0 |
| 0 | 0.073 |
| 0.18 (mid gray) | 0.414 |
| 1.0 | 0.555 |
| 4.0 | 0.669 |
| 16.0 | 0.783 |
| 100.0 | 0.934 |
| 222.86 | 1.0 |

0..1 in ACEScct covers scene-linear −0.0069 to **222.86**, about 10 stops above mid gray, so
practically every highlight survives. Every stop takes the same share of the range (1/17.52 ≈
0.057), which is ideal for precision. The inverse is exact to float precision, and because it is pure
maths it behaves the same in every config and on the farm.

The trade-off: a log image looks flat and washed out to the model, which is unlike its
training images, so fills can come out lower in contrast or a little less convincing than with
the display transform. Use it for plates with extreme highlights (sun, practicals, speculars),
when the display transform's ceiling or gamut gets in the way, or when your config has no
output transform.

#### Input colorspace: display-referred plates stay as they are

If the plate came from a display-referred file (a PNG/JPG/TIFF in matte_paint / sRGB, a still
from Photoshop) or from a camera log encoding (LogC, S-Log3, ACEScct), the best encoding is the
one it was stored in: the model sees the **original file values**, and the round trip is the
identity on those values. GenFill reads the colorspace off the Read node feeding the plate.

#### Custom

Any OCIO colorspace you type into *custom colorspace*.

### Auto (match input): the default

**Auto** walks up the Source input to the plate's Read node and decides:

| Plate | Example | Encoding |
|---|---|---|
| Display-referred file | PNG/JPG in matte_paint, sRGB, Rec.709 | Its own colorspace (*Input*) |
| Camera / grading log | LogC, S-Log3, ACEScct | Its own colorspace (*Input*) |
| Scene-linear | EXR in ACEScg / ACES2065-1 / linear | **Display transform**, or Log if the config has none |
| No Read found | Generated / CG upstream | Display transform |

The decision is logged with each generation, e.g.

```
color: plate Read Read1 (colorspace 'ACES - ACES2065-1', .exr) -> display: Output - Rec.709 (auto: ACES - ACES2065-1 is scene-linear)
```

### Precision: why float transport matters

Encoding solves the *range* problem. Bit depth decides how *finely* that range is sampled, and
any quantization is magnified by the inverse transfer:

| Transport | ACEScct: linear change per code value | Result after decode |
|---|---|---|
| 8-bit PNG | ×1.049 (**4.9 %** per step) | Visible banding in skies and soft gradients |
| 16-bit | ×1.0002 (0.019 % per step) | Smooth |
| **32-bit float TIFF** (local) | No file quantization | Smooth; the model's own output is the only limit |

The display transform's inverse is steeper still near white, so 8-bit is worse there.

So the local path is float end to end:

1. Nuke writes the encoded crop as a **32-bit float TIFF** (uncompressed, raw).
2. The helper loads it with `tifffile` as float32. The only clamp in the whole pipeline is
   here: the model's input is limited to 0..1 *in encoded space*, which in ACEScct means
   linear −0.0069..222.86, and in display mode means the display transform's range.
3. FLUX's output is taken as float (`output_type="np"`), never rounded to 8 bits, and written
   back as a float TIFF. An 8-bit preview PNG is made separately for the thumbnails.
4. Nuke reads it raw and applies the inverse.

To be clear about the limit: FLUX computes in bfloat16 inside a VAE latent, so the
generated pixels carry the model's own precision. That precision shows up as fine,
spatially-varying noise rather than banding, and it's far below grain level. Float transport
makes sure GenFill adds no quantization of its own on top.

Cloud models only accept 8-bit PNG. They use the same encoding, just quantized, so prefer the
local model for HDR plates.

### What this does and doesn't give you

- ✅ Fills that sit in your working space with the plate's dynamic range: highlights above 1.0
  are generated as highlights, shadows keep their floor, and pixels outside the matte are
  untouched.
- ✅ Per-variation bookkeeping: each variation stores the encoding it was made with, so switching
  modes later never breaks earlier variations. Variations made before 0.6 decode the old way.
- ⚠️ Generated light is a plausible re-expansion, not measured light. A highlight the model
  paints at 0.95 in display space becomes whatever the inverse transform says that means. That's
  usually convincing, but check bright fills against the plate (an Exposure node down a few
  stops is a quick check).
- ⚠️ The model's view is clamped to the encoding's range (display transform ceiling, or ~222 in
  ACEScct), and display transforms limit gamut to the display's.
- ⚠️ Grain: generated areas are clean. Regrain the fill to match the plate (built-in grain
  matching is on the roadmap).

---

## Privacy and security

**Local models run entirely on your machine.** The crop, mask, prompt and results go from Nuke to
the hidden helper over a private pipe and are processed on your GPU. The helper makes no network
connections: it runs with Hugging Face offline mode on and telemetry off, and reads model files
only from `%USERPROFILE%\GenFill-LocalAI`. You can confirm this by blocking
`GenFill-LocalAI\python\...\python.exe` in Windows Firewall; generation keeps working.

The network is used only by the **installer**, and only to download uv (GitHub), Python packages
(PyPI and the official PyTorch index) and model files (Hugging Face, GitHub). Nothing about your
images or prompts is sent.

**Cloud models (Stability AI, OpenAI) are different:** the crop around your selection (not the
whole frame) and the prompt are uploaded to that company. Use them only when your client / TPN
rules allow it.

| What | Where |
|---|---|
| Crops, masks, variations | `genfill_cache\` next to the saved script (`%TEMP%\genfill_cache` if unsaved). If scripts live in OneDrive/Dropbox this syncs; set **cache folder** to keep it local |
| GenFill log (includes prompts) | `test_output\genfill.log` when installed from the repo, else `~\.nuke\genfill\genfill.log` |
| Helper / installer logs | `%USERPROFILE%\GenFill-LocalAI\logs\` |
| API keys (cloud only) | `~\.nuke\genfill\config.json`, never in .nk scripts |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No Roto tools in the Viewer | Open the GenerativeFill panel (it must stay open while drawing) with *selection from* = Shapes, and view the node. Click **✏ Draw Selection** if needed |
| "Nothing is selected yet" | Draw a shape, or connect a matte / switch *selection from* |
| "The selection is empty on frame N" | The shapes are hidden or keyed off on this frame, or the matte is black here |
| Stuck on "Starting the local AI..." / helper errors | **Local AI > Install / Repair...**, then check `GenFill-LocalAI\logs\helper.log` |
| `WinError 1114` (c10.dll) | Run **Install / Repair** (restores the private C++ runtime next to PyTorch) |
| `os error 1455` / paging file too small | Run **Install / Repair** (reshards T5); keep a few GB of free disk for the Windows page file |
| Out of GPU memory | Close other GPU apps (Nuke's own GPU cache counts), or choose the Standard model size |
| Fill looks washed out | You're probably on *Log (ACEScct)*; try *Display transform* |
| Banding in bright fills | Use the local model (float); cloud models are 8-bit |
| Bright highlights flatten inside the fill | Display transform ceiling; use *Log (ACEScct)* for that shot |

Send `test_output\genfill.log` and the helper log with any bug report.

---

## Roadmap

- Track-and-apply: stick a still fill to a moving plate via a tracker / CornerPin.
- Grain matching on the generated patch.
- Reference image input, like Photoshop's "Reference image".
- A commercially licensed local model (e.g. Qwen-Image-Edit, Apache 2.0).
- Adobe Firefly Fill for studios with Firefly Services access.
- Anamorphic plates (pixel aspect is currently ignored when building the crop).

---

## Development

```
nuke/                       <- add this folder to NUKE_PATH (or use installers\install_dev.bat)
  init.py, menu.py          menus: Nodes toolbar > GenFill, and the GenFill top menu
  icons/GenFill.png
  python/genfill/
    node.py                 builds the Group, knobs, built-in Roto, upgrades of older nodes
    engine.py               generate pipeline, variations, threading, state
    colorspace.py           encodings (ACEScct maths, OCIO display/input), Auto decisions
    region.py               selection bounds and context-crop maths (pure Python)
    pngio.py, state.py      dependency-free PNG IO, variation history (pure Python)
    config.py, log.py       keys / cache defaults, logging
    devtools.py             Reload GenFill, in-Nuke smoke test
    backends/               local.py, stability.py, openai_images.py, mock.py
    localai/                installer.py, client.py (Nuke side), worker.py (hidden helper)
    ui/                     Photoshop-style panel, Local AI dialog, API keys, Qt shim
installers/                 install / dev install / uninstall / smoke test (Windows)
tests/                      stdlib unittest with a fake nuke module; runs without Nuke
docs/ARCHITECTURE.md        internals: node graph, generate flow, helper, color
```

```
python -m unittest discover -s tests -v
```

`tests/fake_nuke.py` stands in for the `nuke` module, so the node build, upgrades and the full
generate → select → delete flow are tested with the offline backend. Internals are documented in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Licenses

- **FLUX.1 Fill [dev]**: [FLUX.1 [dev] Non-Commercial License](https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev/blob/main/LICENSE.md).
  The model may only be used non-commercially (personal, research, evaluation); read the license
  for the terms on outputs before using them in paid work. GenFill downloads a community GGUF
  quantization of it.
- **FLUX.2 [klein] 4B**: [Apache License 2.0](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/main/LICENSE.md),
  so it can be used in commercial work. GenFill downloads the official weights from Black Forest
  Labs, pinned to a commit and checked against their SHA-256. (The larger klein 9B is *not*
  Apache 2.0 and is not used.) Choose it in **Local AI > Install / Repair** or in GenFill Setup.
- **LaMa**: Apache 2.0.
- T5 / CLIP text encoders: Apache 2.0 / MIT.
- Cloud backends: governed by your agreement with that provider.
