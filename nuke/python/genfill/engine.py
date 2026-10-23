"""Generate pipeline and variation management for a GenerativeFill node.

Flow of one Generate click (mirrors Photoshop):

1. Main thread: render a low-res probe of the selection and find its bounds.
2. Grow the bounds into a context region matched to the model's aspect.
3. Render the crop (8-bit, project colourspace) and the masks to the cache.
4. Background thread: call the backend for N variations, save them as PNGs.
5. Main thread: append a generation to the node's history and select its
   first variation (Read path, crop size and position are set on the
   internal nodes, so the result renders without this plugin).
"""
from __future__ import annotations

import base64
import datetime
import os
import shutil
import subprocess
import sys
import threading
import uuid

import nuke

from . import backends, colorspace, config, pngio, region, state as gfstate
from . import node as gfnode
from . import log as gflog
from .backends import BackendError, Cancelled, GenerationRequest

PROBE_LONG_SIDE = 192

_jobs = {}            # uid -> status dict
_jobs_lock = threading.Lock()


class UserError(Exception):
    """Problem the artist can fix; shown as-is."""


QUIET = False   # set by the smoke test: report errors in status, no popups


def _notify(message):
    print("GenFill: %s" % message)
    if nuke.GUI and not QUIET:
        nuke.message(message)


# --------------------------------------------------------------------------
# status (polled by the panel)
# --------------------------------------------------------------------------

def _uid(node):
    return node["gf_uid"].value()


# TCL, so Nuke Indie doesn't count a node retrieval per node in the script.
# Top level only: copies inside other Groups aren't seen there.
_UIDS_TCL = ("in root {set _gf {}; foreach _n [nodes] {if {[exists $_n.gf_uid]} "
             "{lappend _gf $_n [value $_n.gf_uid]}}; set _gf}")


def _genfill_uids():
    """[(full name, uid)] of every GenerativeFill node in the script."""
    if gfnode.limited_python():
        try:
            words = gfnode._tcl_list(nuke.tcl(_UIDS_TCL))
        except Exception:
            gflog.exception("could not list GenFill nodes via TCL; skipping the copy check")
            return []
        return list(zip(words[0::2], words[1::2]))
    try:
        nodes = nuke.allNodes("Group", recurseGroups=True)
    except TypeError:
        nodes = nuke.allNodes()
    return [(n.fullName(), _uid(n)) for n in nodes if "gf_uid" in n.knobs()]


def ensure_unique_uid(node):
    """Give a copy-pasted node its own uid (and so its own cache folder and job
    status). Its earlier generations still resolve through their ``abs_dir``.
    Returns True if the uid was changed."""
    uid = _uid(node)
    name = node.fullName()
    for other_name, other_uid in _genfill_uids():
        if other_name != name and other_uid == uid:
            new = uuid.uuid4().hex[:12]
            node["gf_uid"].setValue(new)
            gflog.info("%s shared uid %s with %s (copy/paste); now %s", name, uid, other_name,
                       new)
            return True
    return False


def _claim_uid(node):
    """ensure_unique_uid(), except while this node's own job is running under its uid."""
    current = status(node)
    if not (current.get("running") and current.get("owner") == node.fullName()):
        ensure_unique_uid(node)


def status(node):
    with _jobs_lock:
        return dict(_jobs.get(_uid(node), {"running": False, "message": "", "progress": 0.0,
                                            "error": ""}))


def _set_status(uid, **kw):
    with _jobs_lock:
        st = _jobs.setdefault(uid, {"running": False, "message": "", "progress": 0.0,
                                    "error": ""})
        st.update(kw)


def is_running(node):
    return status(node).get("running", False)


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def cache_root(node):
    override = ""
    knob = node["gf_cache_dir"]
    try:
        override = knob.evaluate() or ""
    except Exception:
        override = knob.value() or ""
    override = override.strip()
    if override:
        return override
    script = nuke.root().name()
    if script and script != "Root":
        return os.path.join(os.path.dirname(script), "genfill_cache")
    return config.default_cache_root()


def cache_dir(node):
    return os.path.join(cache_root(node), _uid(node))


def resolve(node, gen, rel_path):
    """Absolute path of a variation, tolerant of moved/unsaved scripts."""
    candidates = [os.path.join(cache_dir(node), rel_path)]
    if gen.get("abs_dir"):
        candidates.append(os.path.join(gen["abs_dir"], os.path.basename(rel_path)))
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


def _fwd(path):
    return path.replace("\\", "/")


# --------------------------------------------------------------------------
# state helpers
# --------------------------------------------------------------------------

STATE_PREFIX = "b64:"


def decode_state(text):
    """Knob text -> state. Stored base64-encoded: the knob is a TCL-evaluated
    string knob and raw JSON brackets would be treated as TCL commands."""
    text = (text or "").strip()
    if text.startswith(STATE_PREFIX):
        try:
            text = base64.b64decode(text[len(STATE_PREFIX):].encode("ascii")).decode("utf-8")
        except Exception:
            gflog.exception("Could not decode stored GenFill state; starting empty")
            text = ""
    return gfstate.loads(text)


def encode_state(st):
    return STATE_PREFIX + base64.b64encode(gfstate.dumps(st).encode("utf-8")).decode("ascii")


def get_state(node):
    return decode_state(node["gf_state"].value())


def _save_state(node, st):
    node["gf_state"].setValue(encode_state(st))


def _internal(node, name):
    n = node.node(name)
    if n is None:
        raise UserError("GenerativeFill internals are missing node %r. Recreate the node." % name)
    return n


def refresh_result(node):
    """Point the internal Read/Reformat/Transform at the selected variation."""
    try:
        if gfnode.ensure_current(node):
            gflog.info("upgraded %s to the current GenFill node layout", node.fullName())
    except Exception:
        gflog.exception("could not upgrade %s", node.fullName())
    st = get_state(node)
    sel = gfstate.selected(st)
    if sel is None:
        node["gf_has_result"].setValue(False)
        node["gf_info"].setValue("")
        return
    gi, vi = sel
    gen = st["generations"][gi]
    path = resolve(node, gen, gen["files"][vi])
    read = _internal(node, gfnode.READ_RESULT)
    if _fwd(read["file"].value()) != _fwd(path):
        read["file"].setValue(_fwd(path))
    _configure_decode(node, gen)
    x, y, w, h = gen["region"]
    ref = _internal(node, gfnode.REFORMAT_RECV)
    ref["box_width"].setValue(w)
    ref["box_height"].setValue(h)
    _internal(node, gfnode.TRANSFORM_RECV)["translate"].setValue([x, y])
    node["gf_frame"].setValue(int(gen["frame"]))
    node["gf_has_result"].setValue(True)
    order = gfstate.variations(st)
    idx = order.index((gi, vi)) + 1
    prompt = gen.get("prompt") or "(remove)"
    for bad, good in (("[", "("), ("]", ")"), ("{", "("), ("}", ")"), ("$", ""), ("\\", "/")):
        prompt = prompt.replace(bad, good)
    if len(prompt) > 28:
        prompt = prompt[:27] + u"…"
    node["gf_info"].setValue("%s  (%d/%d)" % (prompt, idx, len(order)))


def _configure_decode(node, gen):
    """Read the result raw and decode it with the transfer it was made with."""
    read = _internal(node, gfnode.READ_RESULT)
    transfer = gen.get("transfer")
    nodes = [node.node(n) for n in (gfnode.FROM_MODEL, gfnode.FROM_MODEL_LOG, gfnode.FROM_MODEL_OCIO)]
    if transfer and all(n is not None for n in nodes):
        colorspace.set_raw(read, True)
        colorspace.configure(nodes[0], nodes[1], nodes[2], transfer, "from")
    else:
        # Made by GenFill <= 0.5: 8-bit file in the default (or typed) colorspace.
        colorspace.set_raw(read, False)
        colorspace.apply_to_read(read, node["gf_transfer"].value() if "gf_transfer" in node.knobs()
                                 else colorspace.DEFAULT)
        if nodes[0] is not None:
            nodes[0]["which"].setValue(colorspace.SWITCH_INDEX["none"])


def resolve_transfer(node):
    """Work out how this generation is encoded for the model (see colorspace.py)."""
    knobs = node.knobs()
    mode = int(node["gf_color_mode"].getValue()) if "gf_color_mode" in knobs else colorspace.MODE_AUTO
    read = colorspace.find_upstream_read(node.input(0))
    rcs, rext = colorspace.read_info(read)
    display = node["gf_display"].value().strip() if "gf_display" in knobs else "auto"
    if not display or display.lower() == "auto":
        ocio = node.node(gfnode.TO_MODEL_OCIO)
        names = list(ocio["out_colorspace"].values()) if ocio is not None else []
        display = colorspace.pick_display_colorspace(names)
    custom = node["gf_transfer"].value() if "gf_transfer" in knobs else ""
    transfer = colorspace.resolve(mode, rcs, rext, custom, colorspace.working_space(), display)
    gflog.info("color: plate Read %s (colorspace %r, %s) -> %s",
               read.name() if read is not None else "none", rcs, rext or "?", transfer["label"])
    return transfer


def select(node, gi, vi):
    st = gfstate.select(get_state(node), gi, vi)
    _save_state(node, st)
    refresh_result(node)


def step(node, delta):
    st = gfstate.step(get_state(node), delta)
    _save_state(node, st)
    refresh_result(node)


def delete_variation(node, gi, vi, delete_file=True):
    _claim_uid(node)
    st = get_state(node)
    gen = st["generations"][gi]
    path = resolve(node, gen, gen["files"][vi])
    st, _removed = gfstate.remove_variation(st, gi, vi)
    _save_state(node, st)
    refresh_result(node)
    own_dir = os.path.normcase(os.path.abspath(cache_dir(node))) + os.sep
    if delete_file and not os.path.normcase(os.path.abspath(path)).startswith(own_dir):
        # Inherited from the node this one was copied from: that node still uses it.
        gflog.info("not deleting %s: it belongs to another node's cache", path)
        delete_file = False
    if delete_file:
        for p in (path, os.path.splitext(path)[0] + "_preview.png"):
            try:
                os.remove(p)
            except OSError:
                pass


def _thumb_path(path):
    """Float results have an 8-bit preview next to them for the panel."""
    preview = os.path.splitext(path)[0] + "_preview.png"
    return preview if os.path.exists(preview) else path


def thumbnails(node):
    """Display list for the panel: newest generation first."""
    st = get_state(node)
    sel = gfstate.selected(st)
    out = []
    for gi, vi in gfstate.variations(st):
        gen = st["generations"][gi]
        out.append({
            "gi": gi, "vi": vi,
            "path": _thumb_path(resolve(node, gen, gen["files"][vi])),
            "prompt": gen.get("prompt", ""),
            "backend": gen.get("backend", ""),
            "seed": gen.get("seed", 0),
            "frame": gen.get("frame", 0),
            "selected": sel == (gi, vi),
        })
    return out


def open_cache(node):
    path = cache_dir(node)
    if not os.path.isdir(path):
        path = cache_root(node)
    if not os.path.isdir(path):
        nuke.message("Nothing generated yet.\nCache folder will be:\n%s" % cache_dir(node))
        return
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: pylint
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------------------
# rendering helpers (main thread)
# --------------------------------------------------------------------------

def _execute(node, write_name, path, frame):
    w = _internal(node, write_name)
    gflog.debug("render %s frame %s -> %s", write_name, frame, path)
    w["file"].setValue(_fwd(path))
    w["disable"].setValue(False)
    try:
        nuke.execute(w, frame, frame, 1)
    finally:
        w["disable"].setValue(True)
    if not os.path.exists(path):
        raise UserError("Nuke did not write %s" % path)


def _prepare(node, backend, frame, job_dir):
    """Render probe + crops. Returns (region, model_w, model_h, files dict)."""
    src_input = node.input(0)
    if src_input is None:
        raise UserError("Connect an image to the Source input.")
    gfnode.ensure_current(node)
    sel = int(node["gf_selection"].getValue())
    if sel == gfnode.SEL_MASK and node.input(1) is None:
        raise UserError("Connect a Roto (or any matte) to the Mask input, "
                        "or set 'selection from' to Shapes.")
    if sel == gfnode.SEL_SHAPES and not gfnode.roto_has_shapes(node):
        raise UserError("Nothing is selected yet.\nClick 'Draw Selection' and draw a shape "
                        "in the Viewer around what you want to fill.")
    fmt = src_input.format()
    fw, fh = fmt.width(), fmt.height()

    # 1. probe
    if fw >= fh:
        pw, ph = PROBE_LONG_SIDE, max(1, int(round(PROBE_LONG_SIDE * fh / float(fw))))
    else:
        pw, ph = max(1, int(round(PROBE_LONG_SIDE * fw / float(fh)))), PROBE_LONG_SIDE
    probe = _internal(node, gfnode.PROBE_REFORMAT)
    probe["box_width"].setValue(pw)
    probe["box_height"].setValue(ph)
    probe_path = os.path.join(job_dir, "probe.png")
    _execute(node, gfnode.PROBE_WRITE, probe_path, frame)
    bbox = region.mask_bbox(pngio.read(probe_path))
    gflog.info("selection probe %dx%d of %dx%d plate: bbox %s", pw, ph, fw, fh, bbox)
    if bbox is None:
        raise UserError("The selection is empty on frame %d.\nCheck the shapes (are they "
                        "animated or switched off?), the Mask input or the Source alpha." % frame)

    # 2. region
    box = region.probe_bbox_to_format(bbox, pw, ph, fw, fh)
    context = node["gf_context"].value()
    min_size = int(node["gf_min_size"].value())
    model = node["gf_model"].value().strip()
    rough = region.expand_region(box, fw, fh, context=context, min_size=min_size)
    mw, mh = backend.model_size(rough.w, rough.h, model)
    reg = region.expand_region(box, fw, fh, context=context, min_size=min_size,
                               aspect=float(mw) / mh)

    gflog.info("region x=%d y=%d w=%d h=%d -> model size %dx%d (%s)", reg.x, reg.y, reg.w, reg.h,
               mw, mh, backend.id)

    # 3. crops
    for crop_name, ref_name in ((gfnode.CROP_SRC, gfnode.REFORMAT_SRC),
                                (gfnode.CROP_MASK, gfnode.REFORMAT_MASK)):
        _internal(node, crop_name)["box"].setValue([reg.x, reg.y, reg.r, reg.t])
        ref = _internal(node, ref_name)
        ref["box_width"].setValue(mw)
        ref["box_height"].setValue(mh)
    transfer = resolve_transfer(node)
    colorspace.configure(_internal(node, gfnode.TO_MODEL), _internal(node, gfnode.TO_MODEL_LOG),
                         _internal(node, gfnode.TO_MODEL_OCIO), transfer, "to")
    float_io = getattr(backend, "float_io", False)
    files = {
        "image": os.path.join(job_dir, "source.tif" if float_io else "source.png"),
        "mask": os.path.join(job_dir, "mask.png"),
        "mask_alpha": os.path.join(job_dir, "mask_alpha.png"),
        "transfer": transfer,
    }
    _execute(node, gfnode.WRITE_SRC_F if float_io else gfnode.WRITE_SRC, files["image"], frame)
    _execute(node, gfnode.WRITE_MASK, files["mask"], frame)
    _execute(node, gfnode.WRITE_MASK_A, files["mask_alpha"], frame)
    return reg, mw, mh, (fw, fh), files


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------

def generate(node, prompt=None, blocking=False):
    """Start a generation. Returns False if it could not start.

    ``blocking=True`` runs the backend call on the calling thread and applies
    the result before returning (used by the smoke test and terminal mode).
    In that case the return value is True only if variations were added.
    """
    gflog.info("Generate clicked on %s (blocking=%s)", node.fullName(), blocking)
    _claim_uid(node)
    if is_running(node):
        gflog.warning("A generation is already running on %s; ignoring click", node.fullName())
        _set_status(_uid(node), message="Already generating. Click Cancel to stop it.")
        return False
    if prompt is not None:
        node["gf_prompt"].setValue(prompt)
    prompt = node["gf_prompt"].value()
    backend = gfnode.backend_for(node)
    gflog.info("backend=%s model=%r prompt=%r frame=%s", backend.id, node["gf_model"].value(),
               prompt, nuke.frame())
    if getattr(backend, "is_installed", None) is not None and not backend.is_installed():
        msg = "Local AI isn't installed yet. Click 'Local AI...' in the node's panel to install it."
        gflog.warning(msg)
        _set_status(_uid(node), running=False, message="", error=msg)
        if nuke.GUI and not QUIET and nuke.ask(
                "Local AI isn't installed yet.\n\nOpen the installer now? It downloads about 25 GB "
                "in the background while you keep working."):
            from .ui import localai_dialog
            localai_dialog.show()
        return False
    api_key = ""
    if backend.needs_key:
        api_key = config.get_api_key(backend.id, backend.env_var)
        if not api_key:
            gflog.info("No API key for %s; showing the API Keys dialog", backend.id)
            from .ui import settings
            settings.show(focus=backend.id)
            api_key = config.get_api_key(backend.id, backend.env_var)
            if not api_key:
                msg = ("No API key set for %s. Add one with API Keys..., or switch the model "
                       "to Offline Preview to test without a key." % backend.label)
                gflog.warning(msg)
                _set_status(_uid(node), running=False, message="", error=msg)
                return False

    uid = _uid(node)
    st = get_state(node)
    gflog.info("node uid=%s has %d earlier generation(s) (state knob: %d chars)", uid,
               len(st["generations"]), len(node["gf_state"].value() or ""))
    frame = int(nuke.frame())
    prev_frame = int(node["gf_frame"].value())
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    gen_id = "g%03d_%s" % (gfstate.next_generation_number(st), stamp)
    job_dir = os.path.join(cache_dir(node), gen_id)

    root = nuke.root()
    proxy = root["proxy"].value()
    _set_status(uid, running=True, owner=node.fullName(), message="Preparing selection...",
                progress=0.0, error="")
    made_dir = False
    try:
        if not os.path.isdir(job_dir):
            os.makedirs(job_dir)
            made_dir = True
        if proxy:
            root["proxy"].setValue(False)
        node["gf_frame"].setValue(frame)
        reg, mw, mh, fmt, files = _prepare(node, backend, frame, job_dir)
    except UserError as exc:
        gflog.warning("Cannot generate: %s", exc)
        node["gf_frame"].setValue(prev_frame)
        if made_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        _set_status(uid, running=False, message="", error=str(exc))
        _notify(str(exc))
        return False
    except Exception as exc:  # unexpected: show and log
        gflog.exception("Preparing the selection failed")
        node["gf_frame"].setValue(prev_frame)
        if made_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        _set_status(uid, running=False, message="", error="Could not prepare: %s" % exc)
        _notify("GenFill could not prepare the selection:\n%s" % exc)
        return False
    finally:
        if proxy:
            root["proxy"].setValue(True)

    seed = int(node["gf_seed"].value())
    count = max(1, min(10, int(node["gf_variations"].value())))
    request = GenerationRequest(
        image_path=files["image"], mask_path=files["mask"],
        mask_alpha_path=files["mask_alpha"], prompt=prompt, width=mw, height=mh,
        seed=seed, count=count, negative_prompt=node["gf_negative"].value(),
        model=node["gf_model"].value().strip(), quality=node["gf_quality"].value())
    request.transfer = files["transfer"]
    meta = {
        "id": gen_id,
        "prompt": prompt,
        "backend": backend.id,
        "model": request.model,
        "seed": seed,
        "frame": frame,
        "prev_frame": prev_frame,
        "format": list(fmt),
        "region": [reg.x, reg.y, reg.w, reg.h],
        "model_size": [mw, mh],
        "abs_dir": job_dir,
        "transfer": files["transfer"],
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    gflog.info("starting %s: %d variation(s) at %dx%d, job %s", backend.id, count, mw, mh, job_dir)
    if blocking:
        _worker(node, uid, backend, request, api_key, meta, job_dir,
                dispatch=lambda fn, args: fn(*args), show_progress=False)
        return not status(node).get("error")
    t = threading.Thread(target=_worker, args=(node, uid, backend, request, api_key, meta,
                                               job_dir), name="GenFill-%s" % uid)
    t.daemon = True
    t.start()
    return True


def cancel(node):
    _set_status(_uid(node), cancel_requested=True)


class _NoProgress(object):
    def setMessage(self, m):
        pass

    def setProgress(self, p):
        pass

    def isCancelled(self):
        return False


def _main_thread_dispatch(fn, args):
    nuke.executeInMainThread(fn, args=args)


def _worker(node, uid, backend, request, api_key, meta, job_dir,
            dispatch=_main_thread_dispatch, show_progress=True):
    dispatched = [False]

    def send(fn, args):
        dispatched[0] = True
        dispatch(fn, args)

    task = None
    try:
        try:
            task = nuke.ProgressTask("Generative Fill") if show_progress else _NoProgress()
        except Exception:
            gflog.exception("Could not create a progress window; continuing without one")
            task = _NoProgress()
        task.setMessage("Sending to %s..." % backend.label)

        def progress(frac, msg):
            task.setProgress(int(5 + 90 * frac))
            task.setMessage(msg)
            _set_status(uid, progress=frac, message=msg)
            gflog.debug("progress %.0f%% %s", frac * 100, msg)

        def cancelled():
            with _jobs_lock:
                requested = _jobs.get(uid, {}).get("cancel_requested", False)
            return requested or task.isCancelled()

        _set_status(uid, message="Generating...", progress=0.05, cancel_requested=False)
        images = backend.generate(request, api_key, progress=progress, cancelled=cancelled)
        gflog.info("%s returned %d image(s)", backend.id, len(images))
        files = []
        ext = getattr(backend, "result_ext", ".png")
        for i, item in enumerate(images):
            data, preview = item if isinstance(item, tuple) else (item, None)
            name = "var_%02d%s" % (i + 1, ext)
            with open(os.path.join(job_dir, name), "wb") as fh:
                fh.write(data)
            if preview:
                with open(os.path.join(job_dir, "var_%02d_preview.png" % (i + 1)), "wb") as fh:
                    fh.write(preview)
            files.append("%s/%s" % (meta["id"], name))
        meta["files"] = files
        send(_finish, (node, uid, meta))
    except Cancelled:
        gflog.info("Generation cancelled")
        send(_fail, (node, uid, meta, "Cancelled.", False))
    except BackendError as exc:
        gflog.error("Backend error: %s", exc)
        send(_fail, (node, uid, meta, str(exc), show_progress))
    except Exception as exc:
        gflog.exception("Unexpected error while generating")
        send(_fail, (node, uid, meta, "Unexpected error: %s" % exc, show_progress))
    finally:
        task = None
        if not dispatched[0]:
            # Never leave the node stuck in "running".
            _set_status(uid, running=False, error="Generation stopped unexpectedly (see log).")


def _node_alive(node):
    try:
        node.name()
        return True
    except ValueError:
        return False


def _finish(node, uid, meta):
    try:
        if not _node_alive(node):
            gflog.warning("Node was deleted before the result arrived")
            return
        meta = dict(meta)
        meta.pop("prev_frame", None)
        st = gfstate.add_generation(get_state(node), meta, select_first=True)
        _save_state(node, st)
        readback = get_state(node)
        if len(readback["generations"]) != len(st["generations"]):
            gflog.warning("State did not stick: wrote %d generation(s), read back %d",
                          len(st["generations"]), len(readback["generations"]))
        refresh_result(node)
        gflog.info("Added generation %s with %d variation(s); selected the first",
                   meta["id"], len(meta.get("files", [])))
        _set_status(uid, running=False, progress=1.0, message="", error="")
    except Exception as exc:
        gflog.exception("Applying the generated result failed")
        _set_status(uid, running=False, progress=0.0, message="",
                    error="Generated, but applying the result failed: %s (see log)" % exc)
        _notify("Generative Fill: the images were generated but could not be applied:\n%s\n\n"
                "Log: %s" % (exc, gflog.log_path()))


def _fail(node, uid, meta, message, show):
    _set_status(uid, running=False, progress=0.0, message="", error=message)
    if not _node_alive(node):
        return
    try:
        node["gf_frame"].setValue(int(meta.get("prev_frame", meta["frame"])))
        refresh_result(node)
    except Exception:
        gflog.exception("Restoring the node after a failed generation also failed")
    if show:
        _notify("Generative Fill failed:\n\n%s" % message)
    else:
        print("GenFill: %s" % message)
