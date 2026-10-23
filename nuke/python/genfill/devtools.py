"""Developer helpers: reload a freshly installed build, and a smoke test that
exercises the real node inside Nuke and writes a report.

The smoke test writes to ``<repo>/test_output/`` (or ``~/.nuke/genfill/
test_output`` if the repo isn't known):

    report.json        Nuke/Qt versions, every step's pass/fail + traceback,
                       knob inventories of the internal nodes (to catch API
                       differences between Nuke versions), final knob values
    comp.png           the node's Comp output (8-bit, default colourspace)
    fill.png           the Fill-only output
    source.png, mask.png, mask_alpha.png, var_01.png   what went to / came from the model
    panel.png          screenshot of the Photoshop-style panel (GUI only)

It uses the Offline Preview backend, so it needs no network or API key. It
never touches your own nodes: everything is built inside a Group named
``GenFill_SmokeTest`` (replaced on every run).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback

import nuke

import genfill

CONTAINER = "GenFill_SmokeTest"
PLATE_FORMAT = "HD_1080"
MASK_AREA = [760, 380, 1160, 700]      # x, y, r, t in 1920x1080

# Knobs whose existence the node build depends on, per internal node class.
EXPECTED_KNOBS = {
    "FrameHold": ["first_frame", "firstFrame"],
    "Dilate": ["size", "channels"],
    "Expression": ["expr0", "expr3", "channel0", "channel3"],
    "Reformat": ["type", "box_width", "box_height", "box_fixed", "resize", "filter"],
    "Crop": ["box", "reformat"],
    "Write": ["file", "file_type", "datatype", "channels", "colorspace", "raw",
              "create_directories", "disable", "compression"],
    "Read": ["file", "colorspace", "on_error", "raw"],
    "Merge2": ["operation", "Achannels", "output", "maskChannelMask"],
    "Copy": ["from0", "to0"],
    "Transform": ["translate", "filter"],
    "Switch": ["which"],
    "Roto": ["output", "replace"],
    "OCIOColorSpace": ["in_colorspace", "out_colorspace"],
    "Remove": ["operation", "channels"],
}


# --------------------------------------------------------------------------
# reload
# --------------------------------------------------------------------------

def reload_genfill(verbose=True):
    """Drop every genfill module so the next import loads the files on disk.

    Open GenerativeFill panels pick up the new code when they're reopened.
    """
    # Stop the Local AI helper first: after the reload nothing would own it.
    try:
        from genfill.localai import client as _lc
        _lc.stop_helper()
    except Exception:
        pass
    names = [n for n in list(sys.modules) if n == "genfill" or n.startswith("genfill.")]
    for n in names:
        del sys.modules[n]
    import genfill as fresh  # noqa: F401  (re-import the package)
    label = fresh.build_label()
    if verbose:
        print("GenFill reloaded: %s from %s" % (label, fresh.build_info()["location"]))
        if nuke.GUI:
            nuke.message("Reloaded %s.\n\nClose and reopen any GenerativeFill panels." % label)
    return label


def show_build_info():
    b = genfill.build_info()
    lines = ["%s" % genfill.build_label(), "",
             "Loaded from: %s" % b.get("location", ""),
             "Install mode: %s" % b.get("mode", ""),
             "Source repo: %s" % (b.get("source") or "-")]
    nuke.message("\n".join(lines))


# --------------------------------------------------------------------------
# smoke test
# --------------------------------------------------------------------------

def default_output_dir():
    repo = genfill.repo_root()
    if repo:
        return os.path.join(repo, "test_output")
    from . import config
    return os.path.join(config.CONFIG_DIR, "test_output")


class _Report(object):
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.data = {
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "build": genfill.build_info(),
            "build_label": genfill.build_label(),
            "nuke_version": getattr(nuke, "NUKE_VERSION_STRING", "?"),
            "gui": bool(nuke.GUI),
            "python": sys.version,
            "platform": sys.platform,
            "steps": [],
            "knob_inventory": {},
            "values": {},
        }

    def step(self, name, fn, *args, **kw):
        t0 = time.time()
        entry = {"step": name}
        try:
            result = fn(*args, **kw)
            entry["ok"] = True
            if result is not None:
                entry["result"] = result
            return result
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            entry["traceback"] = traceback.format_exc()
            return None
        finally:
            entry["seconds"] = round(time.time() - t0, 3)
            self.data["steps"].append(entry)
            print("GenFill smoke test: %-34s %s" % (name, "ok" if entry.get("ok") else
                                                     "FAILED  " + entry.get("error", "")))

    @property
    def passed(self):
        return all(s.get("ok") for s in self.data["steps"])

    def save(self):
        self.data["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.data["passed"] = self.passed
        with open(os.path.join(self.out_dir, "report.json"), "w") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True, default=str)


def _knob_inventory(group):
    """Which of the knobs we rely on exist on each internal node class."""
    inv = {}
    seen = set()
    nodes = list(group.nodes()) if hasattr(group, "nodes") else []
    for n in nodes:
        cls = n.Class()
        if cls in seen or cls not in EXPECTED_KNOBS:
            continue
        seen.add(cls)
        knobs = n.knobs()
        inv[cls] = dict((k, k in knobs) for k in EXPECTED_KNOBS[cls])
    return inv


def _values(group, gfnode):
    def v(node_name, knob):
        n = group.node(node_name)
        if n is None or knob not in n.knobs():
            return None
        k = n[knob]
        try:
            return k.value()
        except Exception:
            return str(k)
    out = {
        "read_file": v(gfnode.READ_RESULT, "file"),
        "read_colorspace": v(gfnode.READ_RESULT, "colorspace"),
        "write_src_colorspace": v(gfnode.WRITE_SRC, "colorspace"),
        "write_src_datatype": v(gfnode.WRITE_SRC, "datatype"),
        "crop_src_box": v(gfnode.CROP_SRC, "box"),
        "reformat_src": [v(gfnode.REFORMAT_SRC, "box_width"), v(gfnode.REFORMAT_SRC, "box_height")],
        "reformat_result": [v(gfnode.REFORMAT_RECV, "box_width"),
                            v(gfnode.REFORMAT_RECV, "box_height")],
        "place_translate": v(gfnode.TRANSFORM_RECV, "translate"),
        "out_switch_which": v(gfnode.OUT_SWITCH, "which"),
        "mask_expr_channels": [v(gfnode.MASK_EXPR, "channel%d" % i) for i in range(4)],
        "gf_info": group["gf_info"].value(),
        "gf_frame": group["gf_frame"].value(),
        "gf_has_result": group["gf_has_result"].value(),
        "writes_disabled": dict((w, v(w, "disable")) for w in gfnode.WRITE_NODES),
    }
    try:
        cs = group.node(gfnode.WRITE_SRC)["colorspace"].values()
        out["write_colorspace_menu_first_entries"] = list(cs)[:12]
    except Exception:
        pass
    return out


def _render(container, src_node, path, frame):
    with container:
        w = nuke.nodes.Write(file_type="png")
        w.setInput(0, src_node)
        w["file"].setValue(path.replace("\\", "/"))
        if "datatype" in w.knobs():
            w["datatype"].setValue("8 bit")
        try:
            nuke.execute(w, frame, frame, 1)
        finally:
            nuke.delete(w)
    if not os.path.exists(path):
        raise RuntimeError("render did not produce %s" % path)
    return os.path.basename(path)


def _panel_screenshot(group, path):
    from .ui.panel import GenFillPanel, NukeController
    from .ui.qt import QtWidgets
    w = GenFillPanel(NukeController(group))
    w.resize(470, 10)
    w.adjustSize()
    w.resize(470, max(w.sizeHint().height(), 300))
    QtWidgets.QApplication.processEvents()
    pm = w.grab()
    ok = pm.save(path, "PNG")
    w._timer.stop()
    w.deleteLater()
    if not ok:
        raise RuntimeError("could not save panel screenshot")
    return {"size": [pm.width(), pm.height()]}


def run_smoke_test(output_dir=None, show_result=True):
    """Build a test setup inside a Group, generate offline, render, report."""
    from . import engine, node as gfnode, state as gfstate

    out = output_dir or default_output_dir()
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    rep = _Report(out)
    rep.data["output_dir"] = out
    engine.QUIET = True
    frame = int(nuke.frame())
    ctx = {}
    limited = gfnode.limited_python()
    rep.data["limited_python"] = limited

    def build_scene():
        old = nuke.toNode(CONTAINER)
        if old is not None:
            nuke.delete(old)
        gfnode.deselect_all()
        c = nuke.nodes.Group(name=CONTAINER)
        c["tile_color"].setValue(0x884444FF)
        c["label"].setValue("delete me any time")
        ctx["container"] = c
        with c:
            plate = nuke.nodes.CheckerBoard2(format=PLATE_FORMAT)
            ctx["plate"] = plate
            rect = nuke.nodes.Rectangle(format=PLATE_FORMAT)
            rect["output"].setValue("alpha")
            rect["area"].setValue(MASK_AREA)
            ctx["rect"] = rect
        return {"format": PLATE_FORMAT}

    def create_node():
        c = ctx["container"]
        with c:
            ctx["plate"].setSelected(True)
            g = gfnode.create()
            g.setInput(1, ctx["rect"])
            roto = g.node(gfnode.ROTO)
            if roto is not None:
                ctx["roto_replace_knob"] = "replace" in roto.knobs()
            ctx["shape_link"] = "gf_shapes" in g.knobs()
            g.setInput(0, ctx["plate"])
            g["gf_selection"].setValue(gfnode.SEL_MASK)
            label = [l for l in gfnode.backend_labels() if "Offline" in l][0]
            g["gf_backend"].setValue(label)
        ctx["g"] = g
        if limited:
            # Listing ~35 internal nodes would go over Indie's 10 node retrievals.
            rep.data["knob_inventory"] = "skipped (Nuke Indie / Non-commercial)"
            return {"node": g.fullName()}
        rep.data["knob_inventory"] = _knob_inventory(g)
        return {"node": g.fullName(), "internal_nodes": len(g.nodes())}

    def generate():
        g = ctx["g"]
        ok = engine.generate(g, "smoke test", blocking=True)
        st = engine.get_state(g)
        if not ok or not st["generations"]:
            raise RuntimeError("generate failed: %s" % engine.status(g).get("error"))
        gen = st["generations"][-1]
        ctx["gen"] = gen
        return {"region": gen["region"], "model_size": gen["model_size"],
                "files": gen["files"], "cache": engine.cache_dir(g)}

    def copy_model_files():
        g, gen = ctx["g"], ctx["gen"]
        job = os.path.dirname(engine.resolve(g, gen, gen["files"][0]))
        copied = []
        for name in ("source.png", "mask.png", "mask_alpha.png", "probe.png", "var_01.png"):
            src = os.path.join(job, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(out, name))
                copied.append(name)
        return copied

    def render_comp():
        g = ctx["g"]
        g["gf_output"].setValue(0)
        return _render(ctx["container"], g, os.path.join(out, "comp.png"), frame)

    def render_fill():
        g = ctx["g"]
        g["gf_output"].setValue(1)
        try:
            return _render(ctx["container"], g, os.path.join(out, "fill.png"), frame)
        finally:
            g["gf_output"].setValue(0)

    def variations():
        g = ctx["g"]
        engine.step(g, 1)
        info = g["gf_info"].value()
        engine.step(g, -1)
        return {"after_step": info, "count": len(engine.thumbnails(g))}

    def write_bypass_check():
        # The internal Writes must stay disabled so Render All never runs them.
        g = ctx["g"]
        enabled = [w for w in gfnode.WRITE_NODES if not g.node(w)["disable"].value()]
        if enabled:
            raise RuntimeError("internal Writes left enabled: %s" % enabled)
        return "all disabled"

    try:
        rep.step("build test scene", build_scene)
        if "container" in ctx:
            rep.step("create GenerativeFill node", create_node)
        if "g" in ctx:
            rep.step("generate (offline, blocking)", generate)
            rep.data["values"] = _values(ctx["g"], gfnode)
        if "gen" in ctx:
            rep.step("copy model inputs/outputs", copy_model_files)
            rep.step("render comp", render_comp)
            rep.step("render fill only", render_fill)
            rep.step("variation navigation", variations)
            rep.step("internal writes disabled", write_bypass_check)
        if "g" in ctx and nuke.GUI:
            rep.step("panel screenshot", _panel_screenshot, ctx["g"],
                     os.path.join(out, "panel.png"))
        rep.data["roto_has_replace_knob"] = ctx.get("roto_replace_knob")
        rep.data["shape_link_knob"] = ctx.get("shape_link")
    finally:
        engine.QUIET = False
        rep.save()

    summary = "%s\n\n%d/%d steps passed.\nReport: %s" % (
        genfill.build_label(), sum(1 for s in rep.data["steps"] if s.get("ok")),
        len(rep.data["steps"]), os.path.join(out, "report.json"))
    print(summary)
    if show_result and nuke.GUI:
        nuke.message(("Smoke test PASSED\n\n" if rep.passed else "Smoke test FAILED\n\n") + summary)
    return rep.passed
