"""A tiny stand-in for the ``nuke`` module, enough to exercise GenFill's
node-building and generate pipeline outside Nuke.

It is *not* a Nuke emulator: Write nodes "render" synthetic PNGs (a square
selection for the probe, flat images for crops) so the Python logic around
them can be tested end to end.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

# Keep test runs out of the artist's real log file.
import tempfile as _tempfile  # noqa: E402
os.environ.setdefault("GENFILL_LOG", os.path.join(_tempfile.gettempdir(), "genfill_test.log"))

from genfill import pngio  # noqa: E402

STARTLINE = 1
INVISIBLE = 2
READ_ONLY = 4
GUI = False


class Knob(object):
    def __init__(self, name, label="", value=None, values=None):
        self._name = name
        self._value = value
        self._values = values or []
        self.expression = None
        self.flags = 0

    def name(self):
        return self._name

    def value(self):
        if self._values and isinstance(self._value, int):
            return self._values[self._value]
        return self._value if self._value is not None else ""

    def getValue(self):
        if self._values:
            v = self._value if self._value is not None else 0
            return v if isinstance(v, int) else self._values.index(v)
        return self._value if self._value is not None else 0

    def evaluate(self):
        return self.value()

    def setValue(self, v):
        if self._values and isinstance(v, str) and v in self._values:
            v = self._values.index(v)
        self._value = v

    def values(self):
        return list(self._values)

    def setValues(self, values):
        self._values = list(values)

    def setExpression(self, e):
        # Like Nuke, complain if a parent.<knob> reference doesn't exist yet.
        import re
        for ref in re.findall(r"parent\.(\w+)", e):
            group = _context[-1] if _context else None
            if group is None or ref not in group._knobs:
                EXPRESSION_ERRORS.append("Nothing is named parent.%s (%s)" % (ref, self._name))
        self.expression = e

    def setFlag(self, f):
        self.flags |= f

    def setTooltip(self, t):
        pass

    def setLabel(self, t):
        self.label = t

    def setRange(self, a, b):
        pass


class CurvesKnob(Knob):
    """Roto 'curves': rootLayer is iterable over the shapes."""
    def __init__(self, name):
        Knob.__init__(self, name)
        self.rootLayer = []


def add_shape(roto, name="Bezier1"):
    roto["curves"].rootLayer.append(name)


class LinkKnob(Knob):
    def setLink(self, target):
        self.link = target


def Link_Knob(name, label=""):
    return LinkKnob(name, label)


def _knob_factory(cls_name):
    def make(name, label="", *args):
        if cls_name == "Enumeration_Knob":
            return Knob(name, label, 0, list(args[0]))
        if args:
            default = args[0]
        elif cls_name in ("Int_Knob", "Double_Knob", "Boolean_Knob"):
            default = 0
        else:
            default = None
        return Knob(name, label, default)
    return make


for _k in ("Tab_Knob", "PyCustom_Knob", "Text_Knob", "Enumeration_Knob", "Double_Knob",
           "Int_Knob", "String_Knob", "File_Knob", "PyScript_Knob", "Boolean_Knob",
           "Multiline_Eval_String_Knob"):
    globals()[_k] = _knob_factory(_k)


class Format(object):
    def __init__(self, w, h):
        self._w, self._h = w, h

    def width(self):
        return self._w

    def height(self):
        return self._h


_context = []
_root_nodes = []
_counter = [0]
EXECUTED = []
EXPRESSION_ERRORS = []
SELECTION_BOX = [0.4, 0.3, 0.6, 0.6]   # normalised x0, y0(top), x1, y1 for the probe


class Node(object):
    def __init__(self, cls, **kw):
        _counter[0] += 1
        self._cls = cls
        self._knobs = {}
        self._inputs = {}
        self._children = []
        self._name = kw.pop("name", "%s%d" % (cls, _counter[0]))
        self._xy = (0, 0)
        self._format = Format(1920, 1080)
        if cls == "Roto":
            self._knobs["curves"] = CurvesKnob("curves")
        for k, v in kw.items():
            self[k].setValue(v)
        (_context[-1]._children if _context else _root_nodes).append(self)

    # knobs
    def __getitem__(self, name):
        if name not in self._knobs:
            self._knobs[name] = Knob(name)
        return self._knobs[name]

    def knobs(self):
        common = {"datatype", "create_directories", "raw", "on_error", "replace",
                  "first_frame", "channel0", "channel1", "channel2", "channel3"}
        d = dict((k, None) for k in common)
        d.update(self._knobs)
        return d

    def addKnob(self, k):
        self._knobs[k.name()] = k

    # graph
    def setInput(self, i, n):
        self._inputs[i] = n

    def input(self, i):
        return self._inputs.get(i)

    def name(self):
        return self._name

    def setName(self, n, uncollide=False):
        self._name = n + "1" if uncollide else n

    def Class(self):
        return self._cls

    def format(self):
        return self._format

    def setXYpos(self, x, y):
        self._xy = (x, y)

    def xpos(self):
        return self._xy[0]

    def ypos(self):
        return self._xy[1]

    def setSelected(self, s):
        pass

    def nodes(self):
        return list(self._children)

    def fullName(self):
        return self._name

    def node(self, name):
        for c in self._children:
            if c._name == name:
                return c
        return None

    def __enter__(self):
        _context.append(self)
        return self

    def __exit__(self, *a):
        _context.pop()


class _Nodes(object):
    def __getattr__(self, cls):
        return lambda **kw: Node(cls, **kw)


nodes = _Nodes()


class _Root(Node):
    def __init__(self):
        self._cls = "Root"
        self._knobs = {}
        self._inputs = {}
        self._children = []
        self._name = "Root"
        self.script_path = ""

    def name(self):
        return self.script_path or "Root"


_root = _Root()
_root["proxy"].setValue(False)


def root():
    return _root


def frame():
    return 1001


env = {"indie": False, "nc": False}
SELECTED = []          # top-level nodes the fake treats as selected
RETRIEVED = [0]        # node objects handed out by the retrieval calls (Indie allows 10)


def _retrieve(nodes):
    RETRIEVED[0] += len(nodes)
    return nodes


def selectedNodes():
    return _retrieve(list(SELECTED))


def selectedNode():
    if not SELECTED:
        raise ValueError("no node selected")
    return _retrieve([SELECTED[-1]])[0]


def selectAll():
    SELECTED[:] = list(_root_nodes)


def invertSelection():
    SELECTED[:] = [n for n in _root_nodes if n not in SELECTED]


def tcl(script):
    """Only the TCL GenFill sends; anything else raises like an unknown command."""
    if script == "selected_nodes":
        return " ".join(n._name for n in SELECTED)
    if "gf_uid" in script and "foreach" in script:
        words = []
        for n in _root_nodes:
            if "gf_uid" in n._knobs:
                words += [n._name, n["gf_uid"].value() or "{}"]
        return " ".join(words)
    raise RuntimeError("fake tcl: unknown command %r" % script)


def allNodes(filter=None, group=None, recurseGroups=False):
    found = list(_root_nodes)
    if recurseGroups:
        for top in _root_nodes:
            found.extend(_all(top))
    return _retrieve([n for n in found if filter is None or n.Class() == filter])


def show(n):
    pass


NUKE_VERSION_STRING = "fake"


def _all(container):
    out = []
    for c in container._children:
        out.append(c)
        out.extend(_all(c))
    return out


def delete(n):
    holders = [_root] + _all(_root)
    for top in _root_nodes:
        holders.append(top)
        holders.extend(_all(top))
    for holder in holders:
        if n in holder._children:
            holder._children.remove(n)
    if n in _root_nodes:
        _root_nodes.remove(n)


def toNode(name):
    for n in _root_nodes:
        if n._name == name:
            return _retrieve([n])[0]
    return None


def message(m):
    print("nuke.message:", m)


def _box_size(write):
    """Find the upstream Reformat box size for a Write."""
    up = write.input(0)
    for _ in range(6):          # look through Switch / colour nodes to the Reformat
        if up is None:
            break
        try:
            return int(up["box_width"].value()), int(up["box_height"].value())
        except (TypeError, ValueError):
            up = up.input(int(up["which"].value() or 0)) if up.Class() == "Switch" else up.input(0)
    return 64, 36   # rendering a node without a box (e.g. the whole Group)


def execute(write, first, last, incr=1):
    path = write["file"].value()
    assert not write["disable"].value(), "Write executed while disabled"
    w, h = _box_size(write)
    name = write.name()
    if name == "gf_probe_write":
        img = pngio.Image(w, h, 3)
        x0, y0, x1, y1 = SELECTION_BOX
        for y in range(int(y0 * h), int(y1 * h)):
            for x in range(int(x0 * w), int(x1 * w)):
                img.rows[y][x * 3:x * 3 + 3] = b"\xff\xff\xff"
    elif name == "gf_write_mask_alpha":
        img = pngio.Image(w, h, 4)
        for y in range(h):
            for x in range(w):
                inside = w // 4 <= x < 3 * w // 4 and h // 4 <= y < 3 * h // 4
                img.rows[y][x * 4:x * 4 + 4] = bytes([255, 255, 255, 0 if inside else 255])
    elif name == "gf_write_mask":
        img = pngio.Image(w, h, 3)
        for y in range(h // 4, 3 * h // 4):
            for x in range(w // 4, 3 * w // 4):
                img.rows[y][x * 3:x * 3 + 3] = b"\xff\xff\xff"
    else:
        img = pngio.Image(w, h, 3)
        for y in range(h):
            for x in range(w):
                img.rows[y][x * 3:x * 3 + 3] = bytes([(x * 255) // w, 90, (y * 255) // h])
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    pngio.write(path, img)
    EXECUTED.append((name, path, first))


def executeInMainThread(fn, args=()):
    fn(*args)


class ProgressTask(object):
    def __init__(self, title):
        pass

    def setMessage(self, m):
        pass

    def setProgress(self, p):
        pass

    def isCancelled(self):
        return False


def install():
    mod = types.ModuleType("nuke")
    for k, v in globals().items():
        if not k.startswith("__"):
            setattr(mod, k, v)
    sys.modules["nuke"] = mod
    return mod
