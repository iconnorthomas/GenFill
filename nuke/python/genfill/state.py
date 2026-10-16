"""Generation history ("variations") stored as JSON on the node.

Everything the node needs to show and re-select a variation lives in a hidden
knob on the Group, so it is saved with the .nk script. Image files live in
the node's cache folder; paths in the state are relative to that folder so a
project can be moved as a whole.

State layout::

    {
      "version": 1,
      "generations": [
        {
          "id": "g001_20261012-201500",
          "prompt": "a red vintage car",
          "backend": "stability", "model": "",
          "seed": 1234, "frame": 1001,
          "format": [1920, 1080],
          "region": [x, y, w, h],        # Nuke pixels, bottom-left origin
          "model_size": [w, h],
          "files": ["g001_.../var_01.png", ...],
          "created": "2026-10-12T20:15:00"
        }
      ],
      "selected": [generation_index, variation_index]   # or null
    }
"""
from __future__ import annotations

import copy
import json

VERSION = 1


def empty():
    return {"version": VERSION, "generations": [], "selected": None}


def loads(text):
    if not text or not text.strip():
        return empty()
    try:
        data = json.loads(text)
    except ValueError:
        return empty()
    if not isinstance(data, dict):
        return empty()
    data.setdefault("version", VERSION)
    gens = data.get("generations")
    data["generations"] = [g for g in gens if isinstance(g, dict)] if isinstance(gens, list) else []
    sel = data.get("selected")
    if not (isinstance(sel, list) and len(sel) == 2 and all(isinstance(i, int) for i in sel)):
        data["selected"] = None
    return data


def dumps(state):
    return json.dumps(state, indent=None, separators=(",", ":"), sort_keys=True)


def add_generation(state, generation, select_first=True):
    state = copy.deepcopy(state)
    state["generations"].append(generation)
    if select_first and generation.get("files"):
        state["selected"] = [len(state["generations"]) - 1, 0]
    return state


def variations(state, newest_first=True):
    """Flat list of ``(gen_index, var_index)`` in display order."""
    out = []
    gens = list(enumerate(state["generations"]))
    if newest_first:
        gens.reverse()
    for gi, gen in gens:
        for vi in range(len(gen.get("files", []))):
            out.append((gi, vi))
    return out


def selected(state):
    sel = state.get("selected")
    if not sel:
        return None
    gi, vi = sel
    gens = state["generations"]
    if 0 <= gi < len(gens) and 0 <= vi < len(gens[gi].get("files", [])):
        return gi, vi
    return None


def select(state, gi, vi):
    state = copy.deepcopy(state)
    state["selected"] = [gi, vi]
    if selected(state) is None:
        raise IndexError("No variation %r/%r" % (gi, vi))
    return state


def step(state, delta):
    """Move the selection ``delta`` places through the display order."""
    order = variations(state)
    if not order:
        return state
    cur = selected(state)
    idx = order.index(tuple(cur)) if cur else 0
    gi, vi = order[(idx + delta) % len(order)]
    return select(state, gi, vi)


def remove_variation(state, gi, vi):
    """Remove one variation; drops the generation if it becomes empty.

    Returns ``(new_state, removed_relative_path)``.
    """
    state = copy.deepcopy(state)
    gens = state["generations"]
    removed = gens[gi]["files"].pop(vi)
    dropped_generation = not gens[gi]["files"]
    if dropped_generation:
        gens.pop(gi)
    sel = state.get("selected")
    if sel:
        sgi, svi = sel
        if sgi == gi and svi == vi:
            sel = None
        elif sgi == gi and svi > vi:
            sel = [sgi, svi - 1]
        elif sgi > gi and dropped_generation:
            sel = [sgi - 1, svi]
        state["selected"] = sel
    if state["selected"] is None or selected(state) is None:
        order = variations(state)
        state["selected"] = list(order[0]) if order else None
    return state, removed


def next_generation_number(state):
    """One more than the highest ``gNNN`` id so far (never reuses a deleted one's number)."""
    highest = 0
    for gen in state["generations"]:
        head = str(gen.get("id", "")).split("_", 1)[0]
        if head[:1] == "g" and head[1:].isdigit():
            highest = max(highest, int(head[1:]))
    return max(highest, len(state["generations"])) + 1
