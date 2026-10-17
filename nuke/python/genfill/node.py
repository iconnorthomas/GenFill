"""Creates the GenerativeFill node (a Group built from native Nuke nodes).

Only native nodes live inside the Group, so a script containing GenFill
renders on a farm or another workstation **without the plugin installed**:
the chosen variation is a Read node with a fixed path, and the comp is plain
Reformat/Transform/Merge. The plugin is only needed to generate and to show
the Photoshop-style panel.

Internal graph::

    Source ─┬─ FrameHold ── Crop ── Reformat ── Write(src.png, sRGB 8-bit)   ┐ send
            │                                                                │ (disabled
    Mask ───┴─ Switch ── Erode(grow) ── FrameHold ── Expression(rgb=m, a=1-m)│  except while
                                │                     ├─ Reformat(probe) ── Write(probe)
                                │                     └─ Crop ── Reformat ── Write(mask, mask_a)
                                │
                                └── Blur(feather) ────────────────┐
    Read(result) ── Reformat(to crop size) ── Transform(to crop xy)┴─ Merge(copy, masked) ─┐
                                                └─ Copy(alpha) ── Premult ───────────────────┤
    Source ─────────────────────────────────────────────────────────────────── Switch ── Output
"""
from __future__ import annotations

import uuid

import nuke

from . import backends, colorspace

NODE_CLASS_LABEL = "GenerativeFill"
TILE_COLOR = 0x5B4CF5FF

# Internal node names
SRC_IN = "Source"
MASK_IN = "Mask"
HOLD_SRC = "gf_hold_src"
SEL_SWITCH = "gf_selection_switch"
GROW = "gf_grow"
HOLD_MATTE = "gf_hold_matte"
MASK_EXPR = "gf_mask_rgba"
PROBE_REFORMAT = "gf_probe_reformat"
PROBE_WRITE = "gf_probe_write"
CROP_SRC = "gf_crop_src"
REFORMAT_SRC = "gf_reformat_src"
WRITE_SRC = "gf_write_src"            # 8-bit PNG (cloud / preview backends)
WRITE_SRC_F = "gf_write_src_float"    # 32-bit float TIFF (local model)
TO_MODEL = "gf_to_model"
TO_MODEL_LOG = "gf_to_model_log"
TO_MODEL_OCIO = "gf_to_model_ocio"
FROM_MODEL = "gf_from_model"
FROM_MODEL_LOG = "gf_from_model_log"
FROM_MODEL_OCIO = "gf_from_model_ocio"
CROP_MASK = "gf_crop_mask"
REFORMAT_MASK = "gf_reformat_mask"
WRITE_MASK = "gf_write_mask"
WRITE_MASK_A = "gf_write_mask_alpha"
READ_RESULT = "gf_result"
REFORMAT_RECV = "gf_reformat_result"
TRANSFORM_RECV = "gf_place_result"
FEATHER = "gf_feather"
COMP = "gf_comp"
COPY_ALPHA = "gf_fill_alpha"
PREMULT = "gf_fill_premult"
OUT_SWITCH = "gf_output_switch"
BLANK = "gf_layer_blank"
LAYER = "gf_layer"

WRITE_NODES = (PROBE_WRITE, WRITE_SRC, WRITE_SRC_F, WRITE_MASK, WRITE_MASK_A)

ROTO = "gf_roto"
SEL_SHAPES, SEL_MASK, SEL_ALPHA = 0, 1, 2
SELECTION_MODES = ["Shapes (built-in Roto)", "Mask input", "Source alpha"]
SELECTION_TOOLTIP = ("Where the area to fill comes from:\n"
                     "Shapes: the Roto shapes drawn on this node (Draw Selection, or the shape "
                     "list below).\nMask input: the alpha of whatever is in the Mask input.\n"
                     "Source alpha: the Source's own alpha.")
OUTPUT_MODES = ["Comp", "Layer", "Bypass"]
COLOR_TOOLTIP = (
    "How the plate is encoded for the model, which only understands 0..1 values:\n"
    "Auto: use the colorspace of the Read the plate comes from (e.g. matte_paint / sRGB for a "
    "PNG, LogC for an ARRI plate). Scene-linear plates (EXR, ACEScg) use the display transform.\n"
    "Display transform: e.g. Output - Rec.709. Its tone curve rolls highlights into 0..1 so the "
    "model sees a natural image; Nuke's inverse brings them back.\n"
    "Log (ACEScct): keeps highlights up to ~222 (scene-linear); flatter look for the model.\n"
    "Input colorspace: always the Read's colorspace.\n"
    "Custom: the colorspace in 'custom colorspace'.\n"
    "The local model gets 32-bit float in and out; the result goes back through the exact "
    "inverse. Each variation remembers its encoding.")
OUTPUT_TOOLTIP = ("Comp: the fill blended over Source through the expanded, feathered selection.\n"
                  "Layer: just the generated fill in Source's format, premultiplied by the "
                  "expanded, feathered selection (alpha included), ready to Merge over anything. "
                  "Like a Photoshop generative layer with its mask.\n"
                  "Bypass: Source untouched.")
QUALITY_MODES = ["auto", "low", "medium", "high"]

PANEL_EXPR = '__import__("genfill.ui.panel", fromlist=["GenFillPanel"]).GenFillPanel()'

KNOB_CHANGED = """
try:
    import genfill.node
    genfill.node.knob_changed()
except ImportError:
    pass
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _set_frame_hold(node):
    for name in ("first_frame", "firstFrame"):
        if name in node.knobs():
            node[name].setExpression("parent.gf_frame")
            return
    raise RuntimeError("Unknown FrameHold knob layout in this Nuke version")


def _place(node, col, row):
    node.setXYpos(int(col * 140), int(row * 70))


def _make_write(name, channels, raw, col, row, inp, file_type="png", datatype="8 bit"):
    w = nuke.nodes.Write(name=name, file_type=file_type)
    w.setInput(0, inp)
    if "datatype" in w.knobs():
        w["datatype"].setValue(datatype)
    if file_type == "tiff" and "compression" in w.knobs():
        w["compression"].setValue("none")
    w["channels"].setValue(channels)
    if "create_directories" in w.knobs():
        w["create_directories"].setValue(True)
    if raw:
        colorspace.set_raw(w, True)
    w["disable"].setValue(True)   # only enabled while GenFill renders
    w["tile_color"].setValue(0x333333FF)
    _place(w, col, row)
    return w


def backend_labels():
    return [b.label for b in backends.all_backends()]


def backend_for(node):
    label = node["gf_backend"].value()
    for b in backends.all_backends():
        if b.label == label or label in getattr(b, "aliases", ()):
            return b
    return backends.all_backends()[0]


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def _make_roto(src):
    """The built-in selection: a Roto fed by the Source (for its format), alpha only."""
    roto = nuke.nodes.Roto(name=ROTO)
    roto.setInput(0, src)
    roto["output"].setValue("alpha")
    if "replace" in roto.knobs():
        # Shapes replace the alpha instead of merging with the plate's alpha,
        # so a plate with a solid alpha doesn't select the whole frame.
        roto["replace"].setValue(True)
    return roto


def roto_has_shapes(group):
    roto = group.node(ROTO)
    if roto is None or "curves" not in roto.knobs():
        return False
    try:
        return len(list(roto["curves"].rootLayer)) > 0
    except Exception:
        return False


def show_roto(group):
    """Open the built-in Roto's panel so its tools appear in the Viewer."""
    roto = group.node(ROTO)
    if roto is not None:
        nuke.show(roto)


# Nuke only puts the Roto tools (and its curve overlay) in the Viewer while the
# Roto's own control panel is open; a linked 'curves' knob shows the shape list
# but not the tools. So while the GenerativeFill panel is open we open the
# internal Roto's panel as a floating window and hide that window: the tools
# stay in the Viewer and the GenerativeFill panel is the only one you see.
# A floating panel also doesn't count against the Properties bin's panel limit.

def _hide_floating_panels(roto_name):
    try:
        from .ui.qt import QtWidgets
    except Exception:
        return 0
    app = QtWidgets.QApplication.instance()
    if app is None:
        return 0
    hidden = 0
    for w in app.topLevelWidgets():
        try:
            if w.isVisible() and w.objectName() == roto_name:
                w.hide()
                hidden += 1
        except Exception:
            pass
    return hidden


def attach_roto_tools(group):
    """Show the built-in Roto's Viewer tools without showing its panel.

    Returns True when the tools are attached with the panel hidden."""
    roto = group.node(ROTO)
    if roto is None or not nuke.GUI:
        return False
    try:
        roto.showControlPanel(forceFloat=True)
    except TypeError:          # Nuke without forceFloat
        return False
    name = roto.name()
    if _hide_floating_panels(name):
        return True
    try:                       # the window can appear a moment later
        from .ui.qt import QtCore
        QtCore.QTimer.singleShot(0, lambda: _hide_floating_panels(name))
        QtCore.QTimer.singleShot(150, lambda: _hide_floating_panels(name))
    except Exception:
        pass
    return True


def detach_roto_tools(group):
    roto = group.node(ROTO)
    if roto is not None and nuke.GUI:
        try:
            roto.hideControlPanel()
        except Exception:
            pass


def _safe_call(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:   # never break the properties panel
        print("GenFill: %s failed: %s" % (fn.__name__, exc))


def _sync_roto_tools(group):
    if "gf_selection" not in group.knobs():
        return
    if int(group["gf_selection"].getValue()) == SEL_SHAPES:
        _safe_call(attach_roto_tools, group)
    else:
        _safe_call(detach_roto_tools, group)


def _build_internals(group):
    with group:
        src = nuke.nodes.Input(name=SRC_IN)
        _place(src, 0, 0)
        mask = nuke.nodes.Input(name=MASK_IN)
        _place(mask, 3, 0)

        # ---- selection matte -------------------------------------------
        roto = _make_roto(src)
        _place(roto, 2, 0)
        sel = nuke.nodes.Switch(name=SEL_SWITCH)
        sel.setInput(SEL_SHAPES, roto)
        sel.setInput(SEL_MASK, mask)
        sel.setInput(SEL_ALPHA, src)
        sel["which"].setExpression("parent.gf_selection")
        _place(sel, 3, 1)

        grow = nuke.nodes.Dilate(name=GROW)
        grow.setInput(0, sel)
        grow["channels"].setValue("alpha")
        grow["size"].setExpression("parent.gf_expand")
        _place(grow, 3, 2)

        hold_m = nuke.nodes.FrameHold(name=HOLD_MATTE)
        hold_m.setInput(0, grow)
        _set_frame_hold(hold_m)
        _place(hold_m, 3, 3)

        mexpr = nuke.nodes.Expression(name=MASK_EXPR)
        mexpr.setInput(0, hold_m)
        for i, ch in enumerate(("rgba.red", "rgba.green", "rgba.blue", "rgba.alpha")):
            try:
                mexpr["channel%d" % i].setValue(ch)
            except Exception:
                pass  # defaults are already r, g, b, a
        mexpr["expr0"].setValue("clamp(a)")
        mexpr["expr1"].setValue("clamp(a)")
        mexpr["expr2"].setValue("clamp(a)")
        mexpr["expr3"].setValue("1-clamp(a)")
        _place(mexpr, 4, 4)

        # ---- probe (low-res selection, used to find its bounds) ---------
        probe = nuke.nodes.Reformat(name=PROBE_REFORMAT)
        probe.setInput(0, mexpr)
        probe["type"].setValue("to box")
        probe["box_fixed"].setValue(True)
        probe["resize"].setValue("distort")
        probe["filter"].setValue("Impulse")
        _place(probe, 6, 5)
        _make_write(PROBE_WRITE, "rgb", True, 6, 6, probe)

        # ---- crops sent to the model ----------------------------------
        hold_s = nuke.nodes.FrameHold(name=HOLD_SRC)
        hold_s.setInput(0, src)
        _set_frame_hold(hold_s)
        _place(hold_s, 1, 1)

        crop_s = nuke.nodes.Crop(name=CROP_SRC)
        crop_s.setInput(0, hold_s)
        crop_s["reformat"].setValue(True)
        _place(crop_s, 1, 2)
        ref_s = nuke.nodes.Reformat(name=REFORMAT_SRC)
        ref_s.setInput(0, crop_s)
        ref_s["type"].setValue("to box")
        ref_s["box_fixed"].setValue(True)
        ref_s["resize"].setValue("distort")
        _place(ref_s, 1, 3)
        _make_write(WRITE_SRC, "rgb", False, 1, 4, ref_s)

        crop_m = nuke.nodes.Crop(name=CROP_MASK)
        crop_m.setInput(0, mexpr)
        crop_m["reformat"].setValue(True)
        _place(crop_m, 4, 5)
        ref_m = nuke.nodes.Reformat(name=REFORMAT_MASK)
        ref_m.setInput(0, crop_m)
        ref_m["type"].setValue("to box")
        ref_m["box_fixed"].setValue(True)
        ref_m["resize"].setValue("distort")
        _place(ref_m, 4, 6)
        _make_write(WRITE_MASK, "rgb", True, 4, 7, ref_m)
        _make_write(WRITE_MASK_A, "rgba", True, 5, 7, ref_m)

        # ---- result coming back --------------------------------------
        read = nuke.nodes.Read(name=READ_RESULT)
        if "on_error" in read.knobs():
            read["on_error"].setValue("checkerboard")
        _place(read, 0, 6)

        ref_r = nuke.nodes.Reformat(name=REFORMAT_RECV)
        ref_r.setInput(0, read)
        ref_r["type"].setValue("to box")
        ref_r["box_fixed"].setValue(True)
        ref_r["resize"].setValue("distort")
        _place(ref_r, 0, 7)

        place = nuke.nodes.Transform(name=TRANSFORM_RECV)
        place.setInput(0, ref_r)
        place["filter"].setValue("Impulse")
        _place(place, 0, 8)

        feather = nuke.nodes.Blur(name=FEATHER)
        feather.setInput(0, hold_m)
        feather["channels"].setValue("alpha")
        feather["size"].setExpression("parent.gf_feather")
        _place(feather, 2, 8)

        comp = nuke.nodes.Merge2(name=COMP)
        comp.setInput(0, src)       # B
        comp.setInput(1, place)     # A
        comp.setInput(2, feather)   # mask
        comp["operation"].setValue("copy")
        comp["Achannels"].setValue("rgb")
        comp["output"].setValue("rgb")
        comp["maskChannelMask"].setValue("rgba.alpha")
        _place(comp, 1, 10)

        cp = nuke.nodes.Copy(name=COPY_ALPHA)
        cp.setInput(0, place)
        cp.setInput(1, feather)
        cp["from0"].setValue("rgba.alpha")
        cp["to0"].setValue("rgba.alpha")
        _place(cp, 0, 10)
        pm = nuke.nodes.Premult(name=PREMULT)
        pm.setInput(0, cp)
        _place(pm, 0, 11)

        out_sw = nuke.nodes.Switch(name=OUT_SWITCH)
        out_sw.setInput(0, comp)
        out_sw.setInput(1, pm)
        out_sw.setInput(2, src)
        out_sw["which"].setExpression("parent.gf_has_result ? parent.gf_output : 2")
        _place(out_sw, 1, 12)

        out = nuke.nodes.Output()
        out.setInput(0, out_sw)
        _place(out, 1, 13)
    _build_layer(group)
    _build_color_nodes(group)


def _expression(name, exprs, inp, col, row):
    e = nuke.nodes.Expression(name=name)
    e.setInput(0, inp)
    for i, ch in enumerate(("rgba.red", "rgba.green", "rgba.blue", "rgba.alpha")):
        try:
            e["channel%d" % i].setValue(ch)
        except Exception:
            pass
    for i, ex in enumerate(exprs):
        e["expr%d" % i].setValue(ex)
    e["expr3"].setValue("a")
    _place(e, col, row)
    return e


def _build_color_nodes(group):
    """Encode working space -> model space before the Writes, and decode the
    result back after the Read (see colorspace.py). Values are set per
    generation from Python, so the comp renders without the plugin."""
    with group:
        ref_s = group.node(REFORMAT_SRC)
        read = group.node(READ_RESULT)
        ref_r = group.node(REFORMAT_RECV)
        # to model
        tl = _expression(TO_MODEL_LOG, colorspace.ENCODE_EXPRS, ref_s, 2, 3.5)
        to = nuke.nodes.OCIOColorSpace(name=TO_MODEL_OCIO)
        to.setInput(0, ref_s)
        _place(to, 2.8, 3.5)
        tsw = nuke.nodes.Switch(name=TO_MODEL)
        tsw.setInput(0, tl)
        tsw.setInput(1, to)
        tsw.setInput(2, ref_s)
        tsw["which"].setValue(colorspace.SWITCH_INDEX["log"])
        _place(tsw, 1, 4)
        old_png = group.node(WRITE_SRC)
        if old_png is not None:
            old_png.setInput(0, tsw)
            colorspace.set_raw(old_png, True)
        else:
            _make_write(WRITE_SRC, "rgb", True, 1, 5, tsw)
        if group.node(WRITE_SRC_F) is None:
            _make_write(WRITE_SRC_F, "rgb", True, 2, 5, tsw, file_type="tiff",
                        datatype="32 bit float")
        # from model
        fl = _expression(FROM_MODEL_LOG, colorspace.DECODE_EXPRS, read, -1, 6.5)
        fo = nuke.nodes.OCIOColorSpace(name=FROM_MODEL_OCIO)
        fo.setInput(0, read)
        _place(fo, -1.8, 6.5)
        fsw = nuke.nodes.Switch(name=FROM_MODEL)
        fsw.setInput(0, fl)
        fsw.setInput(1, fo)
        fsw.setInput(2, read)
        fsw["which"].setValue(colorspace.SWITCH_INDEX["none"])
        _place(fsw, 0, 7)
        ref_r.setInput(0, fsw)


def _add_color_knob(group, before_transfer=True):
    k = nuke.Enumeration_Knob("gf_color_mode", "color pipeline", colorspace.MODES)
    k.setTooltip(COLOR_TOOLTIP)
    group.addKnob(k)
    return k


def _add_display_knob(group):
    k = nuke.String_Knob("gf_display", "display colorspace", "auto")
    k.setTooltip("Display colorspace for the Display transform mode, e.g. 'Output - Rec.709'. "
                 "'auto' picks Rec.709 (then sRGB) output from your OCIO config.")
    group.addKnob(k)
    return k


def _build_layer(group):
    """Layer output: the premultiplied fill merged over a transparent copy of
    Source, so it has Source's format (the raw patch only has the crop's)."""
    with group:
        src = group.node(SRC_IN)
        pm = group.node(PREMULT)
        out_sw = group.node(OUT_SWITCH)
        blank = nuke.nodes.Remove(name=BLANK)
        blank.setInput(0, src)
        blank["operation"].setValue("remove")
        blank["channels"].setValue("all")
        _place(blank, -1, 10)
        layer = nuke.nodes.Merge2(name=LAYER)
        layer.setInput(0, blank)    # B: empty, Source's format
        layer.setInput(1, pm)       # A: fill premultiplied by feathered selection
        layer["operation"].setValue("over")
        _place(layer, -1, 11)
        out_sw.setInput(1, layer)


def ensure_current(group):
    """Upgrade nodes made by older GenFill versions (called when the panel opens)."""
    changed = False
    if group.node(LAYER) is None and group.node(PREMULT) is not None \
            and group.node(OUT_SWITCH) is not None and group.node(SRC_IN) is not None:
        _build_layer(group)
        changed = True
    if group.node(TO_MODEL) is None and group.node(REFORMAT_SRC) is not None \
            and group.node(READ_RESULT) is not None and group.node(REFORMAT_RECV) is not None:
        _build_color_nodes(group)
        changed = True
    if "gf_color_mode" not in group.knobs():
        _add_color_knob(group)
        changed = True
    elif list(group["gf_color_mode"].values()) != colorspace.MODES:
        group["gf_color_mode"].setValues(colorspace.MODES)
        changed = True
    if "gf_display" not in group.knobs():
        _add_display_knob(group)
        changed = True
    if group.node(ROTO) is None and group.node(SEL_SWITCH) is not None \
            and group.node(SRC_IN) is not None:
        _upgrade_selection(group)
        changed = True
    if "gf_shapes" not in group.knobs() and "gf_selection" in group.knobs():
        _add_shape_knobs(group, upgrade=True)
        changed = True
    if "gf_transfer" in group.knobs():
        group["gf_transfer"].setLabel("custom colorspace")
    knob = group.knobs().get("gf_backend")
    if knob is not None and list(knob.values()) != backend_labels():
        # A backend was renamed or added: keep the node on the same backend.
        current = backend_for(group).label
        knob.setValues(backend_labels())
        knob.setValue(current)
        changed = True
    knob = group.knobs().get("gf_output")
    if knob is not None and list(knob.values()) != OUTPUT_MODES:
        idx = int(knob.getValue())
        knob.setValues(OUTPUT_MODES)
        knob.setValue(idx)
        knob.setTooltip(OUTPUT_TOOLTIP)
        changed = True
    return changed


def _upgrade_selection(group):
    """Pre-0.7 nodes: Switch inputs were (Mask, Source); add the Roto in front."""
    sw = group.node(SEL_SWITCH)
    with group:
        src = group.node(SRC_IN)
        mask = group.node(MASK_IN)
        roto = _make_roto(src)
        roto.setXYpos(src.xpos() + 2 * 110, src.ypos())
    sw.setInput(SEL_SHAPES, roto)
    sw.setInput(SEL_MASK, mask)
    sw.setInput(SEL_ALPHA, src)
    knob = group.knobs().get("gf_selection")
    if knob is not None:
        old = int(knob.getValue())
        knob.setValues(SELECTION_MODES)
        knob.setTooltip(SELECTION_TOOLTIP)
        knob.setValue(SEL_MASK if old == 0 else SEL_ALPHA)


def _add_shape_knobs(group, upgrade=False):
    """Draw Selection button + the built-in Roto's shape list on the main tab."""
    knobs = []
    b = nuke.PyScript_Knob("gf_draw", "\u270f Draw Selection",
                           "import genfill.node; genfill.node.draw_selection(nuke.thisNode())")
    b.setTooltip("Puts the Roto tools in the Viewer so you can draw the area to fill "
                 "(they also appear whenever this panel is open with 'selection from' set to "
                 "Shapes). Sets 'selection from' to Shapes.")
    b.setFlag(nuke.STARTLINE)
    knobs.append(b)
    try:
        link = nuke.Link_Knob("gf_shapes", "shapes")
        link.setLink(ROTO + ".curves")
        knobs.append(link)
    except Exception:
        pass   # older Nuke without linkable curves: the button still works
    if upgrade:
        # Moving existing knobs around would disturb the expressions that use them,
        # so older nodes get the tools on their own tab instead.
        group.addKnob(nuke.Tab_Knob("gf_shapes_tab", "Selection"))
    for k in knobs:
        group.addKnob(k)


def draw_selection(group):
    """Draw Selection button: Shapes mode, Roto tools in the Viewer."""
    if "gf_selection" in group.knobs():
        group["gf_selection"].setValue(SEL_SHAPES)
    if not attach_roto_tools(group):
        show_roto(group)       # fallback: the Roto's own panel


def _add_knobs(group):
    tab = nuke.Tab_Knob("gf_tab", "Generative Fill")
    group.addKnob(tab)

    panel = nuke.PyCustom_Knob("gf_panel", "", PANEL_EXPR)
    panel.setFlag(nuke.STARTLINE)
    group.addKnob(panel)

    group.addKnob(nuke.Text_Knob("gf_div_sel", "Selection"))
    k = nuke.Enumeration_Knob("gf_selection", "selection from", SELECTION_MODES)
    k.setTooltip(SELECTION_TOOLTIP)
    group.addKnob(k)
    _add_shape_knobs(group)
    k = nuke.Double_Knob("gf_expand", "expand")
    k.setRange(0, 50)
    k.setValue(4)
    k.setTooltip("Grow the selection before generating (pixels). Like Select > Modify > Expand.")
    group.addKnob(k)
    k = nuke.Double_Knob("gf_feather", "feather")
    k.setRange(0, 100)
    k.setValue(8)
    k.setTooltip("Soften the edge where the fill is blended back into the plate.")
    group.addKnob(k)
    k = nuke.Enumeration_Knob("gf_output", "output", OUTPUT_MODES)
    k.setTooltip(OUTPUT_TOOLTIP)
    group.addKnob(k)

    # ---- Settings tab ----------------------------------------------------
    group.addKnob(nuke.Tab_Knob("gf_settings_tab", "Settings"))
    k = nuke.Enumeration_Knob("gf_backend", "model", backend_labels())
    k.setTooltip("Which generator to use. API keys are set with the API Keys button, "
                 "never saved in the script.")
    group.addKnob(k)
    k = nuke.String_Knob("gf_model", "model version", "")
    k.setTooltip("Optional model override, e.g. gpt-image-1.5. Empty = backend default.")
    group.addKnob(k)
    k = nuke.Enumeration_Knob("gf_quality", "quality", QUALITY_MODES)
    k.setTooltip("Passed to backends that support it (OpenAI).")
    group.addKnob(k)
    k = nuke.Int_Knob("gf_variations", "variations")
    k.setValue(3)
    k.setTooltip("Variations per Generate click. Photoshop gives 3.")
    group.addKnob(k)
    k = nuke.Int_Knob("gf_seed", "seed")
    k.setValue(0)
    k.setTooltip("0 = new random seed every time (Photoshop behaviour).")
    group.addKnob(k)
    k = nuke.String_Knob("gf_negative", "negative prompt", "")
    k.setTooltip("What to avoid (Stability only).")
    group.addKnob(k)

    group.addKnob(nuke.Text_Knob("gf_div_ctx", "Context"))
    k = nuke.Double_Knob("gf_context", "context")
    k.setRange(0.0, 2.0)
    k.setValue(0.35)
    k.setTooltip("How much surrounding image to send, as a fraction of the selection size. "
                 "More context = better matching, less detail.")
    group.addKnob(k)
    k = nuke.Int_Knob("gf_min_size", "min region")
    k.setValue(512)
    k.setTooltip("Smallest crop sent to the model, in plate pixels.")
    group.addKnob(k)

    group.addKnob(nuke.Text_Knob("gf_div_color", "Color"))
    _add_color_knob(group)
    _add_display_knob(group)
    k = nuke.String_Knob("gf_transfer", "custom colorspace", "matte_paint")
    k.setTooltip("Only used when 'color pipeline' is Custom: the OCIO colorspace the plate is "
                 "converted to for the model (and back from).")
    group.addKnob(k)

    group.addKnob(nuke.Text_Knob("gf_div_files", "Files"))
    k = nuke.File_Knob("gf_cache_dir", "cache folder")
    k.setTooltip("Where generated images are stored. Empty = <script folder>/genfill_cache.")
    group.addKnob(k)
    k = nuke.PyScript_Knob("gf_keys_btn", "API Keys...",
                           "import genfill.ui.settings; genfill.ui.settings.show()")
    k.setFlag(nuke.STARTLINE)
    group.addKnob(k)
    group.addKnob(nuke.PyScript_Knob("gf_open_cache_btn", "Open Cache Folder",
                                     "import genfill.engine; genfill.engine.open_cache(nuke.thisNode())"))
    k = nuke.Int_Knob("gf_frame", "reference frame")
    k.setTooltip("Frame the selected variation was generated on. The fill is held from it.")
    k.setFlag(nuke.READ_ONLY)
    group.addKnob(k)

    # ---- hidden state ----------------------------------------------------
    for knob in (nuke.Boolean_Knob("gf_has_result", "has result"),
                 nuke.String_Knob("gf_prompt", "prompt", ""),
                 nuke.String_Knob("gf_info", "info", ""),
                 nuke.String_Knob("gf_uid", "uid", uuid.uuid4().hex[:12]),
                 nuke.Multiline_Eval_String_Knob("gf_state", "state", "")):
        knob.setFlag(nuke.INVISIBLE)
        group.addKnob(knob)


def create():
    """Menu entry point: create a GenerativeFill node (selection drawn on the node itself)."""
    selected = nuke.selectedNodes()
    upstream = selected[0] if len(selected) == 1 else None
    for n in nuke.allNodes():
        n.setSelected(False)

    group = nuke.nodes.Group()
    group.setName(NODE_CLASS_LABEL, uncollide=True)
    # Knobs first: the internal nodes' expressions reference parent.gf_*,
    # and Nuke reports "Nothing is named parent.gf_frame" if they don't exist yet.
    _add_knobs(group)
    _build_internals(group)
    group["knobChanged"].setValue(KNOB_CHANGED)
    group["tile_color"].setValue(TILE_COLOR)
    group["label"].setValue("[value gf_info]")
    group["help"].setValue("Photoshop-style Generative Fill. Click Draw Selection and draw "
                           "shapes in the Viewer (or use the Mask input / Source alpha), type a "
                           "prompt (or leave it empty to remove), and click Generate.")

    if upstream is not None:
        group.setInput(0, upstream)
        group.setXYpos(upstream.xpos(), upstream.ypos() + 90)
    group["gf_selection"].setValue(SEL_SHAPES)

    group.setSelected(True)
    if nuke.GUI:
        nuke.show(group)
    return group


# --------------------------------------------------------------------------
# callbacks
# --------------------------------------------------------------------------

def knob_changed():
    node = nuke.thisNode()
    knob = nuke.thisKnob()
    name = knob.name()
    if name == "inputChange":
        has_mask = node.input(1) is not None
        sel = int(node["gf_selection"].getValue())
        if has_mask and sel == SEL_SHAPES and not roto_has_shapes(node):
            node["gf_selection"].setValue(SEL_MASK)
        elif not has_mask and sel == SEL_MASK:
            node["gf_selection"].setValue(SEL_SHAPES)
    elif name in ("gf_cache_dir", "gf_transfer", "showPanel"):
        from . import engine
        try:
            engine.refresh_result(node)
        except Exception as exc:  # never break the properties panel
            print("GenFill: could not refresh result: %s" % exc)
        if name == "showPanel":
            _sync_roto_tools(node)
    elif name == "hidePanel":
        _safe_call(detach_roto_tools, node)
    elif name == "gf_selection":
        _sync_roto_tools(node)
