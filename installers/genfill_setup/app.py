"""GenFill Setup: a graphical installer for Windows, macOS and Linux.

Installs (or updates / removes) the GenFill plugin bundled inside this app, and
on Windows can also set up Local AI by running GenFill's own Local AI installer.
The colours and the sparkle come from the plugin itself (genfill.ui); the
painted, animated pieces live in widgets.py.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

from . import core

core.use_bundled_genfill()

from genfill.ui import style  # noqa: E402
from genfill.ui.qt import QtCore, QtGui, QtWidgets, Qt  # noqa: E402

from . import widgets as w  # noqa: E402
from .widgets import logo_pixmap  # noqa: E402,F401  (build.py renders the icon from it)

LOG_PATH = os.path.join(tempfile.gettempdir(), "GenFill-Setup.log")
WINDOWS = sys.platform.startswith("win")
FLUX_LICENSE = "https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev/blob/main/LICENSE.md"
LINK = "#9db0ff"

STYLESHEET = """
QWidget {{ color: #e6e6ea; }}
QWidget#gsRoot, QDialog#gsRoot {{ background: #1b1b1f; }}
QLabel {{ background: transparent; }}
QLabel#gsEyebrow {{ color: #8f9cff; font-size: 11px; font-weight: 600; letter-spacing: 1.2px; }}
QLabel#gsTitle {{ color: #f4f4f6; font-size: 25px; font-weight: 600; }}
QLabel#gsSubtitle {{ color: #a3a3ad; font-size: 13px; }}
QLabel#gsAppName {{ color: #f4f4f6; font-size: 16px; font-weight: 600; }}
QLabel#gsMuted {{ color: #9b9ba3; }}
QLabel#gsFaint {{ color: #6c6c76; font-size: 11px; }}
QLabel#gsSection {{ color: #8b8b95; font-size: 11px; font-weight: 600; letter-spacing: 0.8px; }}
QLabel#gsError {{ color: {error}; }}
QLabel#gsWarn {{ color: #f3c46b; }}
QLabel#gsChip {{ background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
                 border-radius: 11px; padding: 3px 10px; color: #d6d6dc; font-size: 11px; }}
QLabel#gsChipAccent {{ background: rgba(91,99,255,0.16); border: 1px solid rgba(122,140,255,0.35);
                       border-radius: 11px; padding: 3px 10px; color: #c9d1ff; font-size: 11px; }}
QFrame#gsCard {{ background: rgba(255,255,255,0.035); border: 1px solid rgba(255,255,255,0.07);
                 border-radius: 14px; }}
QFrame#gsDivider {{ background: rgba(255,255,255,0.07); max-height: 1px; min-height: 1px; }}
QPushButton#gsGhost {{ background: transparent; color: #d8d8de; border: 1px solid rgba(255,255,255,0.14);
                       border-radius: 18px; padding: 0 18px; font-size: 12px; }}
QPushButton#gsGhost:hover {{ background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.22); }}
QPushButton#gsGhost:disabled {{ color: #66666e; border-color: rgba(255,255,255,0.06); }}
QPushButton#gsLink {{ background: transparent; border: none; color: {link}; padding: 2px 0;
                      text-align: left; font-size: 12px; }}
QPushButton#gsLink:hover {{ color: #c3cdff; }}
QPushButton#gsLaunch {{ background: rgba(255,255,255,0.05); color: #ececf0;
                        border: 1px solid rgba(255,255,255,0.10); border-radius: 12px;
                        padding: 10px 16px; font-size: 12px; font-weight: 600; text-align: left; }}
QPushButton#gsLaunch:hover {{ background: rgba(91,99,255,0.14); border-color: rgba(122,140,255,0.45); }}
QLineEdit {{ background: rgba(0,0,0,0.28); color: #ececf0; border: 1px solid rgba(255,255,255,0.10);
            border-radius: 9px; padding: 0 10px; min-height: 34px; selection-background-color: {accent}; }}
QLineEdit:focus {{ border-color: rgba(122,140,255,0.8); }}
QLineEdit:disabled {{ color: #6c6c76; }}
QComboBox {{ background: rgba(0,0,0,0.28); color: #ececf0; border: 1px solid rgba(255,255,255,0.10);
            border-radius: 9px; padding: 0 30px 0 10px; min-height: 34px; }}
QComboBox:hover {{ border-color: rgba(255,255,255,0.2); }}
QComboBox:disabled {{ color: #6c6c76; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{ background: #232328; color: #ececf0; border: 1px solid #3a3a42;
                               selection-background-color: {accent}; outline: none; padding: 4px; }}
QPlainTextEdit {{ background: rgba(0,0,0,0.35); color: #b4b4bc; border: 1px solid rgba(255,255,255,0.07);
                 border-radius: 10px; padding: 6px; font-family: Consolas, Menlo, monospace;
                 font-size: 11px; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.14); border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    height: 0; background: none; }}
QToolTip {{ background: #232328; color: #ececf0; border: 1px solid #3a3a42; padding: 4px 8px; }}
""".format(error=style.ERROR, accent=style.ACCENT, link=LINK)


def app_icon():
    icon = QtGui.QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(size))
    return icon


def apply_app_font(app):
    font = QtGui.QFont(app.font())
    if WINDOWS:
        families = ["Segoe UI Variable Text", "Segoe UI"]
    elif sys.platform == "darwin":
        families = [".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue"]
    else:
        families = ["Inter", "Cantarell", "Noto Sans", "DejaVu Sans"]
    if hasattr(font, "setFamilies"):
        font.setFamilies(families)
    else:
        font.setFamily(families[0])
    font.setPointSizeF(10 if WINDOWS else 12.5 if sys.platform == "darwin" else 10)
    font.setHintingPreference(QtGui.QFont.PreferNoHinting)
    app.setFont(font)


# --------------------------------------------------------------------------
# background work
# --------------------------------------------------------------------------

class Worker(QtCore.QThread):
    """Runs a job on a thread. ``job(emit)`` calls emit(kind, **kw) like the Local AI installer."""
    event = QtCore.Signal(str, object)

    def __init__(self, job, parent=None):
        super(Worker, self).__init__(parent)
        self.job = job

    def run(self):
        try:
            ok, message = self.job(self._emit)
        except Exception as exc:
            self._emit("log", line=traceback.format_exc())
            ok, message = False, str(exc) if isinstance(exc, core.SetupError) else \
                "%s: %s" % (type(exc).__name__, exc)
        self._emit("finished", ok=ok, message=message)

    def _emit(self, kind, **kw):
        self.event.emit(kind, kw)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def label(text, name=None, wrap=True, selectable=False):
    lab = QtWidgets.QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    lab.setTextFormat(Qt.RichText)
    lab.setOpenExternalLinks(True)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    return lab


def link(url, text):
    return '<a href="%s" style="color:%s; text-decoration:none">%s</a>' % (url, LINK, text)


def divider():
    line = QtWidgets.QFrame()
    line.setObjectName("gsDivider")
    return line


class FolderRow(QtWidgets.QWidget):
    def __init__(self, path, parent=None):
        super(FolderRow, self).__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.edit = QtWidgets.QLineEdit(path)
        lay.addWidget(self.edit, 1)
        self.browse = w.GhostButton("Browse")
        self.browse.setFixedHeight(36)
        self.browse.clicked.connect(self._browse)
        lay.addWidget(self.browse)

    def _browse(self):
        start = os.path.dirname(self.path())
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        if chosen:
            name = os.path.basename(self.path())
            # Keep a GenFill-named leaf so the folder can be safely replaced later.
            self.edit.setText(chosen if "genfill" in os.path.basename(chosen).lower()
                              else os.path.join(chosen, name))

    def path(self):
        return os.path.abspath(os.path.expanduser(self.edit.text().strip()))


class Page(QtWidgets.QWidget):
    """A page: eyebrow, title and subtitle, then a scrollable body."""

    def __init__(self, eyebrow, title, subtitle="", parent=None):
        super(Page, self).__init__(parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(40, 34, 40, 0)
        outer.setSpacing(0)
        self.eyebrow = label(eyebrow, "gsEyebrow", wrap=False)
        self.title = label(title, "gsTitle")
        self.subtitle = label(subtitle, "gsSubtitle")
        outer.addWidget(self.eyebrow)
        outer.addSpacing(6)
        outer.addWidget(self.title)
        outer.addSpacing(6)
        outer.addWidget(self.subtitle)
        self.subtitle.setVisible(bool(subtitle))
        outer.addSpacing(22)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        body = QtWidgets.QWidget()
        self.body = QtWidgets.QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 6, 16)
        self.body.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    def set_header(self, eyebrow=None, title=None, subtitle=None):
        if eyebrow is not None:
            self.eyebrow.setText(eyebrow)
        if title is not None:
            self.title.setText(title)
        if subtitle is not None:
            self.subtitle.setText(subtitle)
            self.subtitle.setVisible(bool(subtitle))


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class SetupWindow(QtWidgets.QWidget):
    STAGES = ["Welcome", "Options", "Install", "Done"]

    def __init__(self):
        super(SetupWindow, self).__init__()
        self.setObjectName("gsRoot")
        self.setStyleSheet(STYLESHEET)
        self.setWindowTitle("GenFill Setup")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(880, 600)
        self.resize(940, 640)
        self.source = core.bundled_source()
        self.version = (core.read_build_info(self.source) or {}).get("version") \
            or core.read_version(self.source)
        self.installed = core.installed_version()
        self.nukes = core.find_nuke_installs()
        self.nuke_running = core.nuke_is_running()
        self.worker = None
        self.installer = None
        self.mode = "install"
        self._log_fh = None
        self._build()
        self._go(0)

    # -- layout -------------------------------------------------------------
    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        content = w.GlowBackground()
        cl = QtWidgets.QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.pages = w.FadeStack()
        for build in (self._page_welcome, self._page_options, self._page_progress, self._page_done):
            self.pages.addWidget(build())
        cl.addWidget(self.pages, 1)

        footer = QtWidgets.QWidget()
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(40, 14, 40, 22)
        fl.setSpacing(10)
        self.footer_hint = label("", "gsFaint", wrap=False)
        fl.addWidget(self.footer_hint)
        fl.addStretch(1)
        self.back_btn = w.GhostButton("Back")
        self.back_btn.clicked.connect(lambda: self._go(self.pages.currentIndex() - 1))
        fl.addWidget(self.back_btn)
        self.cancel_btn = w.GhostButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        fl.addWidget(self.cancel_btn)
        self.next_btn = w.PrimaryButton("Continue")
        self.next_btn.clicked.connect(self._next)
        fl.addWidget(self.next_btn)
        cl.addWidget(footer)
        root.addWidget(content, 1)

    def _build_sidebar(self):
        side = w.Sidebar()
        side.setFixedWidth(232)
        lay = QtWidgets.QVBoxLayout(side)
        lay.setContentsMargins(22, 26, 22, 22)
        lay.setSpacing(0)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(w.Logo(40))
        names = QtWidgets.QVBoxLayout()
        names.setSpacing(1)
        names.addStretch(1)
        names.addWidget(label("GenFill", "gsAppName", wrap=False))
        names.addWidget(label("for Nuke &middot; %s" % self.version, "gsFaint", wrap=False))
        names.addStretch(1)
        head.addLayout(names)
        head.addStretch(1)
        lay.addLayout(head)
        lay.addSpacing(40)
        lay.addWidget(label("SETUP", "gsSection", wrap=False))
        lay.addSpacing(12)
        self.stepper = w.Stepper(self.STAGES)
        lay.addWidget(self.stepper)
        lay.addStretch(1)
        about = w.LinkButton("About && credits")
        about.clicked.connect(self._about)
        lay.addWidget(about, 0, Qt.AlignLeft)
        lay.addSpacing(2)
        lay.addWidget(label(link("https://github.com/iconnorthomas/GenFill",
                                 "github.com/iconnorthomas/GenFill"), "gsFaint", wrap=False))
        return side

    def _page_welcome(self):
        page = Page("WELCOME", "Generative Fill, right inside Nuke.",
                    "Draw a selection on a GenerativeFill node, describe what you want (or leave "
                    "it empty to remove), then pick from the variations. Use a cloud model with "
                    "your own key, or run it free and offline on your NVIDIA GPU.")
        b = page.body
        self.rb_install = w.ChoiceCard(
            "Update to %s" % self.version if self.installed else "Install GenFill %s" % self.version,
            ("You have %s. Your settings, API keys and Local AI are kept." % self.installed)
            if self.installed else "Adds GenFill to every Nuke version that uses your .nuke folder.",
            "sparkle")
        self.rb_uninstall = w.ChoiceCard("Remove GenFill",
                                         "Take GenFill out of Nuke. You can keep your settings.",
                                         "remove")
        self.rb_install.setChecked(True)
        b.addWidget(self.rb_install)
        if self.installed:
            b.addWidget(self.rb_uninstall)
        else:
            self.rb_uninstall.setVisible(False)

        card = w.Card(spacing=8)
        card.lay.addWidget(label("NUKE ON THIS COMPUTER", "gsSection", wrap=False))
        chips = QtWidgets.QHBoxLayout()
        chips.setSpacing(6)
        if self.nukes:
            for i, n in enumerate(self.nukes[:6]):
                chips.addWidget(w.Chip(n.name, accent=(i == 0)))
        else:
            chips.addWidget(w.Chip("None found in the usual places"))
        chips.addStretch(1)
        card.lay.addLayout(chips)
        card.lay.addWidget(label("GenFill installs into your <b>.nuke</b> folder, so it loads in "
                                 "every version, including ones installed somewhere else.",
                                 "gsMuted"))
        b.addWidget(card)
        if self.nuke_running:
            b.addWidget(label("Nuke is open. Close it first, or restart it afterwards to load "
                              "the new GenFill.", "gsWarn"))
        b.addStretch(1)
        return page

    def _page_options(self):
        page = self.options_page = Page("OPTIONS", "")
        b = page.body

        # install options
        self.install_opts = QtWidgets.QWidget()
        il = QtWidgets.QVBoxLayout(self.install_opts)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(14)
        card = w.Card()
        card.lay.addWidget(label("PLUGIN FOLDER", "gsSection", wrap=False))
        self.target_row = FolderRow(core.default_target())
        card.lay.addWidget(self.target_row)
        card.lay.addWidget(label("Nuke loads GenFill from here through a marked block in "
                                 "<b>.nuke/init.py</b>. Your init.py is backed up first.",
                                 "gsFaint"))
        il.addWidget(card)

        card = w.Card(spacing=12)
        self.ai_check = w.Toggle(
            "Set up Local AI",
            "Generative Fill and LaMa removal on your own NVIDIA GPU: free, offline, no "
            "accounts. Downloads resume if interrupted.")
        card.lay.addWidget(self.ai_check)
        self.ai_box = QtWidgets.QWidget()
        form = QtWidgets.QGridLayout(self.ai_box)
        form.setContentsMargins(52, 4, 0, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setColumnStretch(1, 1)
        self.family_cards = {}
        cards = QtWidgets.QVBoxLayout()
        cards.setSpacing(8)
        for fid, title, desc, badge, good in self._families():
            c = w.ChoiceCard(title, desc, icon=None, badge=badge, badge_good=good)
            c.toggled.connect(self._family_changed)
            self.family_cards[fid] = c
            cards.addWidget(c)
        form.addLayout(cards, 0, 0, 1, 2)
        self.gpu_label = label("", "gsMuted")
        self.quant_combo = w.Combo()
        self.size_name = label("Model size", "gsMuted", wrap=False)
        self.ai_row = FolderRow("")
        form.addWidget(label("GPU", "gsMuted", wrap=False), 1, 0, Qt.AlignLeft | Qt.AlignVCenter)
        form.addWidget(self.gpu_label, 1, 1)
        form.addWidget(self.size_name, 2, 0, Qt.AlignLeft | Qt.AlignVCenter)
        form.addWidget(self.quant_combo, 2, 1)
        form.addWidget(label("Folder", "gsMuted", wrap=False), 3, 0, Qt.AlignLeft | Qt.AlignVCenter)
        form.addWidget(self.ai_row, 3, 1)
        self.license_check = w.Toggle("I accept the FLUX.1 [dev] Non-Commercial License")
        form.addWidget(self.license_check, 4, 0, 1, 2)
        self.license_link = label("", "gsFaint")
        form.addWidget(self.license_link, 5, 0, 1, 2)
        card.lay.addWidget(self.ai_box)
        if WINDOWS:
            self._init_local_ai()
        else:
            self.ai_check.setEnabled(False)
            self.ai_box.setVisible(False)
            card.lay.addWidget(label("Automatic Local AI setup is Windows-only for now. Cloud "
                                     "models work on every platform.", "gsFaint"))
        self.ai_check.toggled.connect(self._ai_toggled)
        self.license_check.toggled.connect(self._refresh_buttons)
        self._ai_toggled(False)
        il.addWidget(card)
        b.addWidget(self.install_opts)

        # uninstall options
        self.uninstall_opts = QtWidgets.QWidget()
        ul = QtWidgets.QVBoxLayout(self.uninstall_opts)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.setSpacing(14)
        card = w.Card(spacing=14)
        card.lay.addWidget(label("GenFill will be removed from <b>%s</b> and from your "
                                 "<b>init.py</b>. Images you generated stay in your shots' "
                                 "cache folders." % core.default_target(), "gsMuted"))
        card.lay.addWidget(divider())
        from genfill import localai
        self.rm_ai_check = w.Toggle("Also delete Local AI",
                                    "Models and the private Python in %s." % localai.root())
        self.rm_ai_check.setVisible(os.path.isdir(localai.root()))
        self.rm_cfg_check = w.Toggle("Also delete settings and API keys", core.config_dir())
        card.lay.addWidget(self.rm_ai_check)
        card.lay.addWidget(self.rm_cfg_check)
        ul.addWidget(card)
        b.addWidget(self.uninstall_opts)
        b.addStretch(1)
        return page

    def _families(self):
        """(id, title, description, badge, badge is good) for each Local AI model offered."""
        from genfill.localai import installer as inst
        fams = getattr(inst, "FAMILIES", None)
        if not fams:                                   # a plugin from before 0.8
            return [("flux1-fill", "FLUX.1 Fill [dev]", "Best fill quality. About 25 GB.",
                     "NON-COMMERCIAL", False)]
        out = []
        for fid in (inst.KLEIN, inst.FLUX1):
            f = fams[fid]
            size = "About %.0f GB." % (inst.estimate_gb("Q4_K_S", fid) - 7)
            out.append((fid, f["label"], "%s %s" % (f["summary"], size),
                        "COMMERCIAL USE OK" if f["commercial"] else "NON-COMMERCIAL",
                        f["commercial"]))
        return out

    def family(self):
        for fid, card in self.family_cards.items():
            if card.isChecked():
                return fid
        return "flux1-fill"

    def _family_changed(self, *args):
        from genfill.localai import installer as inst
        fid = self.family()
        flux1 = fid == "flux1-fill"
        self.size_name.setVisible(flux1)
        self.quant_combo.setVisible(flux1)
        self.license_check.setVisible(flux1)
        fams = getattr(inst, "FAMILIES", {})
        f = fams.get(fid, {"license": "FLUX.1 [dev] Non-Commercial License",
                           "license_url": FLUX_LICENSE})
        if flux1:
            self.license_link.setText(link(f["license_url"], "Read the license"))
        else:
            self.license_link.setText("Licensed under the %s, so you can use it in commercial "
                                      "work. %s" % (f["license"], link(f["license_url"],
                                                                       "Read the license")))
        self._refresh_buttons()

    def _init_local_ai(self):
        from genfill import localai
        from genfill.localai import installer as inst
        self.ai_row.edit.setText(localai.root())
        current = localai.family() if localai.manifest() and hasattr(localai, "family") else None
        default = current or ("flux2-klein" if "flux2-klein" in self.family_cards else "flux1-fill")
        self.family_cards[default].setChecked(True)
        name, vram = inst.detect_gpu()
        self.gpu_label.setText("%s &middot; %.0f GB" % (name, vram) if name else
                               "No NVIDIA GPU detected. Local AI needs one.")
        for qid, (text, _gb, _vram) in inst.QUANTS.items():
            self.quant_combo.addItem(text, qid)
        self.quant_combo.setCurrentIndex(self.quant_combo.findData(inst.recommended_quant(vram)))

    def _ai_toggled(self, on):
        self.ai_box.setEnabled(on)
        self.ai_box.setVisible(on and WINDOWS)
        self._refresh_buttons()

    def _page_progress(self):
        page = self.progress_page = Page("INSTALL", "")
        b = page.body
        card = w.Card(padding=(18, 12, 18, 12), spacing=2)
        self.steps_layout = card.lay
        self.step_rows = []
        b.addWidget(card)
        b.addSpacing(4)
        self.bar = w.ShimmerBar()
        b.addWidget(self.bar)
        status_row = QtWidgets.QHBoxLayout()
        self.status = label("", "gsMuted", wrap=False)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        status_row.addWidget(self.status, 1)
        self.details_btn = w.LinkButton("Show details")
        self.details_btn.setCheckable(True)
        self.details_btn.toggled.connect(self._toggle_details)
        status_row.addWidget(self.details_btn)
        b.addLayout(status_row)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setMinimumHeight(170)
        self.log_view.setVisible(False)
        b.addWidget(self.log_view)
        b.addStretch(1)
        return page

    def _page_done(self):
        page = self.done_page = Page("DONE", "")
        page.title.setVisible(False)
        b = page.body
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(18)
        self.mark = w.ResultMark()
        head.addWidget(self.mark, 0, Qt.AlignTop)
        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(6)
        self.done_title = label("", "gsTitle")
        self.done_text = label("", "gsSubtitle")
        texts.addWidget(self.done_title)
        texts.addWidget(self.done_text)
        head.addLayout(texts, 1)
        b.addLayout(head)
        b.addSpacing(6)

        self.launch_card = w.Card(spacing=10)
        self.launch_card.lay.addWidget(label("START NUKE", "gsSection", wrap=False))
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(8)
        for i, n in enumerate(self.nukes[:4]):
            btn = QtWidgets.QPushButton(u"%s   →" % n.name)
            btn.setObjectName("gsLaunch")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, n=n: self._launch(n))
            grid.addWidget(btn, i // 2, i % 2)
        self.launch_card.lay.addLayout(grid)
        b.addWidget(self.launch_card)

        self.tip_card = w.Card(spacing=6)
        self.tip_card.lay.addWidget(label("NEXT", "gsSection", wrap=False))
        self.tip_card.lay.addWidget(label(
            "Find <b>GenFill</b> in the Nodes toolbar, or press Tab and type "
            "<b>Generative Fill</b>. To confirm the build, use GenFill &rsaquo; Developer "
            "&rsaquo; Run Smoke Test.", "gsMuted"))
        b.addWidget(self.tip_card)
        log_btn = w.LinkButton("Open the setup log")
        log_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(LOG_PATH)))
        b.addWidget(log_btn, 0, Qt.AlignLeft)
        b.addStretch(1)
        return page

    # -- navigation ---------------------------------------------------------
    def _go(self, index):
        index = max(0, min(index, self.pages.count() - 1))
        if index == 1:
            uninstall = self.mode == "uninstall"
            self.options_page.set_header(
                "OPTIONS",
                "Remove GenFill" if uninstall else ("Update options" if self.installed
                                                    else "Install options"),
                "Choose what else to clean up." if uninstall else
                "Where GenFill goes, and whether to add the free Local AI.")
            self.install_opts.setVisible(not uninstall)
            self.uninstall_opts.setVisible(uninstall)
        self.pages.setCurrentIndex(index)
        self.stepper.set_current(index, failed=getattr(self, "_failed", False) and index == 3)
        self._refresh_buttons()

    def _refresh_buttons(self):
        if not hasattr(self, "next_btn"):       # still building the pages
            return
        index = self.pages.currentIndex()
        busy = self.worker is not None and self.worker.isRunning()
        self.back_btn.setVisible(index == 1)
        self.cancel_btn.setVisible(index in (0, 1) or (busy and self.installer is not None))
        self.next_btn.setVisible(index != 2)
        hint = ""
        if index == 0:
            self.next_btn.setText("Continue")
            self.next_btn.setEnabled(True)
            hint = "Takes a few seconds. Nothing changes until you confirm."
        elif index == 1:
            if self.mode == "uninstall":
                self.next_btn.setText("Remove")
                self.next_btn.setEnabled(True)
            else:
                self.next_btn.setText("Update" if self.installed else "Install")
                need_license = (self.ai_check.isChecked() and self.family() == "flux1-fill"
                                and not self.license_check.isChecked())
                self.next_btn.setEnabled(not need_license)
                if need_license:
                    hint = "Accept the FLUX.1 [dev] license to set up Local AI."
        elif index == 2:
            hint = "You can keep working in Nuke while Local AI downloads." \
                if self.installer is not None else ""
        elif index == 3:
            self.next_btn.setText("Close")
            self.next_btn.setEnabled(True)
        self.footer_hint.setText(hint)
        self.next_btn.updateGeometry()

    def _next(self):
        index = self.pages.currentIndex()
        if index == 0:
            self.mode = "uninstall" if self.rb_uninstall.isChecked() else "install"
            self._go(1)
        elif index == 1:
            self._start()
        else:
            self.close()

    # -- running ------------------------------------------------------------
    def _set_steps(self, labels):
        for row in self.step_rows:
            row.deleteLater()
        self.step_rows = []
        for text in labels:
            row = w.TaskRow(text)
            self.steps_layout.addWidget(row)
            self.step_rows.append(row)

    def _step(self, i, state):
        if 0 <= i < len(self.step_rows):
            self.step_rows[i].set_state(state)

    def _start(self):
        try:
            if self.mode == "install":
                job, labels, title = self._install_job()
            else:
                job, labels, title = self._uninstall_job()
        except core.SetupError as exc:
            QtWidgets.QMessageBox.warning(self, "GenFill Setup", str(exc))
            return
        self._log_fh = open(LOG_PATH, "a", encoding="utf-8")
        self._log("==== GenFill Setup %s, %s, %s" % (self.version, self.mode,
                                                     time.strftime("%Y-%m-%d %H:%M")))
        self.progress_page.set_header("REMOVING" if self.mode == "uninstall" else "INSTALLING",
                                      title, "")
        self._set_steps(labels)
        self.bar.set_value(None)
        self.status.setText("Starting...")
        self._failed = False
        self.worker = Worker(job, self)
        self.worker.event.connect(self._on_event)
        self._go(2)
        self.worker.start()
        self._refresh_buttons()

    def _install_job(self):
        target = self.target_row.path()
        core.assert_safe_target(target, self.source)
        labels = ["Copy GenFill %s into %s" % (self.version, target), "Tell Nuke to load it"]
        with_ai = WINDOWS and self.ai_check.isChecked()
        if with_ai:
            from genfill.localai import installer as inst
            ai_root = self.ai_row.path()
            family = self.family()
            steps = inst.Installer.steps_for(family) if hasattr(inst.Installer, "steps_for") \
                else inst.Installer.STEPS
            labels += [text for _key, text in steps]
            quant = self.quant_combo.currentData()
            extra = {"family": family} if hasattr(inst, "FAMILIES") else {}
        steps = float(len(labels))

        def job(emit):
            emit("step", index=0, state="running")
            core.install_plugin(target, self.source, log=lambda l: emit("log", line=l))
            emit("step", index=0, state="done")
            emit("progress", fraction=1 / steps, text="Copied GenFill to %s" % target)
            emit("step", index=1, state="running")
            res = core.set_init_block(target)
            emit("log", line="updated %s (backup: %s)" % (res["init_py"], res["backup"]))
            for line in res["disabled"]:
                emit("log", line="disabled an older GenFill line: %s" % line)
            emit("step", index=1, state="done")
            emit("progress", fraction=2 / steps, text="Nuke will load GenFill from %s" % target)
            if not with_ai:
                return True, "GenFill %s is installed." % self.version
            emit("progress", fraction=None, text="Setting up Local AI...")

            final = {"message": ""}

            def on_ai(kind, **kw):
                if kind == "step":
                    emit("step", index=kw["index"] + 2, state=kw["state"])
                elif kind == "finished":
                    final["message"] = kw.get("message", "")
                else:
                    emit(kind, **kw)
            self.installer = inst.Installer(base=ai_root, quant=quant, on_event=on_ai, **extra)
            ok = self.installer.run()
            msg = final["message"]
            if ok:
                return True, "GenFill %s and Local AI are installed." % self.version
            return False, ("GenFill %s is installed, but Local AI setup stopped. Run Setup again "
                           "to resume; finished downloads are kept. %s" % (self.version, msg))
        return job, labels, "Updating GenFill" if self.installed else "Installing GenFill"

    def _uninstall_job(self):
        target = core.default_target()
        rm_ai = self.rm_ai_check.isChecked() and self.rm_ai_check.isVisible()
        rm_cfg = self.rm_cfg_check.isChecked()
        ai_root = None
        if rm_ai:
            from genfill import localai
            ai_root = localai.root()
        labels = ["Remove GenFill from Nuke"]
        if rm_ai:
            labels.append("Delete Local AI in %s" % ai_root)
        if rm_cfg:
            labels.append("Delete settings and API keys")

        def job(emit):
            log = lambda l: emit("log", line=l)  # noqa: E731
            emit("step", index=0, state="running")
            core.uninstall(target, log=log)
            emit("step", index=0, state="done")
            i = 1
            if rm_ai:
                emit("step", index=i, state="running")
                from genfill.localai.client import kill_stale_helpers
                kill_stale_helpers(ai_root)
                core.uninstall(target, remove_ai_root=ai_root, log=log)
                emit("step", index=i, state="done")
                i += 1
            if rm_cfg:
                emit("step", index=i, state="running")
                core.uninstall(target, remove_config=True, log=log)
                emit("step", index=i, state="done")
            return True, "GenFill was removed. Restart Nuke if it's open."
        return job, labels, "Removing GenFill"

    def _on_event(self, kind, kw):
        if kind == "step":
            self._step(kw["index"], kw["state"])
            if kw["state"] == "running" and kw["index"] < len(self.step_rows):
                self.status.setText(self.step_rows[kw["index"]].text)
        elif kind == "progress":
            self.bar.set_value(kw.get("fraction"))
            self.status.setText(kw.get("text", ""))
        elif kind == "log":
            self._log(kw["line"])
        elif kind == "finished":
            self._finished(kw["ok"], kw["message"])
        if kind in ("step", "finished"):
            self._refresh_buttons()

    def _log(self, line):
        line = line.rstrip()
        self.log_view.appendPlainText(line)
        if self._log_fh:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()

    def _finished(self, ok, message):
        self._log("==== %s: %s" % ("OK" if ok else "FAILED", message))
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        for row in self.step_rows:
            if row.state == "running":
                row.set_state("done" if ok else "failed")
        self.bar.set_value(1.0 if ok else self.bar.value)
        self.installer = None
        self._failed = not ok
        installed = ok and self.mode == "install"
        if installed:
            self.done_title.setText("GenFill is ready")
        elif ok:
            self.done_title.setText("GenFill was removed")
        else:
            self.done_title.setText("Setup didn't finish")
            message += " The details are in the setup log."
        self.done_text.setText(message)
        self.done_page.set_header("ALL DONE" if ok else "SOMETHING WENT WRONG")
        self.launch_card.setVisible(installed and bool(self.nukes))
        self.tip_card.setVisible(installed)
        self._go(3)
        self.mark.play(ok)

    def _toggle_details(self, on):
        self.details_btn.setText("Hide details" if on else "Show details")
        self.log_view.setVisible(on)

    def _cancel(self):
        if self.installer is not None:
            self.status.setText("Cancelling...")
            self.installer.cancel()
            return
        if self.worker is None or not self.worker.isRunning():
            self.close()

    def _launch(self, install):
        try:
            core.launch_nuke(install)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "GenFill Setup", "Couldn't start %s: %s"
                                          % (install.name, exc))

    def _about(self):
        AboutDialog(self.version, self).exec()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            if self.installer is None:
                event.ignore()          # plugin copy takes a second; let it finish
                return
            answer = QtWidgets.QMessageBox.question(
                self, "GenFill Setup", "Stop the Local AI setup? Run Setup again later to resume.")
            if answer != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            self.installer.cancel()
            self.worker.wait(15000)
        event.accept()


class AboutDialog(QtWidgets.QDialog):
    CREDITS = [
        ("FLUX.1 Fill [dev]", "Black Forest Labs", "FLUX.1 [dev] Non-Commercial",
         "https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev"),
        ("FLUX.1 Fill GGUF", "YarvixPA", "FLUX.1 [dev] Non-Commercial",
         "https://huggingface.co/YarvixPA/FLUX.1-Fill-dev-GGUF"),
        ("LaMa inpainting", "Samsung AI Center", "Apache 2.0", "https://github.com/advimman/lama"),
        ("T5 v1.1 XXL encoder", "Google, bf16 by city96", "Apache 2.0",
         "https://huggingface.co/city96/t5-v1_1-xxl-encoder-bf16"),
        ("CLIP ViT-L/14", "OpenAI", "MIT", "https://huggingface.co/openai/clip-vit-large-patch14"),
        ("PyTorch, diffusers, transformers", "PyTorch Foundation, Hugging Face",
         "BSD-3 / Apache 2.0", "https://github.com/huggingface/diffusers"),
        ("uv", "Astral", "Apache 2.0 / MIT", "https://github.com/astral-sh/uv"),
        ("Qt for Python (PySide6)", "The Qt Company", "LGPL v3",
         "https://code.qt.io/cgit/pyside/pyside-setup.git/"),
    ]

    def __init__(self, version, parent=None):
        super(AboutDialog, self).__init__(parent)
        self.setObjectName("gsRoot")
        self.setStyleSheet(STYLESHEET)
        self.setWindowTitle("About GenFill Setup")
        self.setMinimumWidth(720)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(30, 26, 30, 22)
        lay.setSpacing(14)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(w.Logo(52))
        names = QtWidgets.QVBoxLayout()
        names.setSpacing(2)
        names.addStretch(1)
        names.addWidget(label("GenFill for Nuke", "gsAppName", wrap=False))
        names.addWidget(label("Version %s &middot; by %s" % (
            version, link("https://github.com/iconnorthomas", "Connor Thomas")), "gsMuted",
            wrap=False))
        names.addStretch(1)
        head.addLayout(names, 1)
        lay.addLayout(head)
        lay.addWidget(label("Thanks to %s, whose Windows setup app inspired this one."
                            % link("https://github.com/AlesUshakou", "Ale&#353; Ushakou"),
                            "gsMuted"))
        card = w.Card(spacing=0)
        card.lay.addWidget(label("THIRD-PARTY MODELS AND LIBRARIES", "gsSection", wrap=False))
        card.lay.addSpacing(8)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(7)
        for r, (name, who, lic, url) in enumerate(self.CREDITS):
            grid.addWidget(label('<a href="%s" style="color:#ececf0; text-decoration:none">%s</a>'
                                 % (url, name), wrap=False), r, 0)
            grid.addWidget(label(who, "gsMuted", wrap=False), r, 1)
            grid.addWidget(label(lic, "gsFaint", wrap=False), r, 2)
        grid.setColumnStretch(1, 1)
        card.lay.addLayout(grid)
        lay.addWidget(card)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(label(link("https://github.com/iconnorthomas/GenFill",
                                 "github.com/iconnorthomas/GenFill"), wrap=False))
        row.addStretch(1)
        close = w.PrimaryButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)


def self_test(out_path):
    """Check a built app without clicking through it: ``--self-test result.json``."""
    import json
    result = {"ok": False, "source": core.bundled_source(), "checks": {}}
    checks = result["checks"]
    try:
        src = core.bundled_source()
        checks["plugin_files"] = all(os.path.exists(os.path.join(src, *p)) for p in (
            ("init.py",), ("menu.py",), ("icons", "GenFill.png"),
            ("python", "genfill", "localai", "worker.py")))
        checks["build_info"] = core.read_build_info(src)
        import genfill
        checks["genfill_from_files"] = os.path.isfile(genfill.__file__)
        import ssl
        import urllib.request  # noqa: F401
        checks["https"] = bool(ssl.OPENSSL_VERSION)
        from genfill.localai import installer as inst
        checks["local_ai_steps"] = len(inst.Installer.STEPS)
        checks["nuke_installs"] = [n.name for n in core.find_nuke_installs()]
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        apply_app_font(app)
        window = SetupWindow()
        checks["window"] = window.windowTitle()
        window.close()
        del app
        result["ok"] = bool(checks["plugin_files"] and checks["genfill_from_files"]
                            and checks["https"])
    except Exception:
        result["error"] = traceback.format_exc()
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2)
    return 0 if result["ok"] else 1


def main(argv=None):
    argv = list(argv or sys.argv)
    if "--self-test" in argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        return self_test(argv[argv.index("--self-test") + 1])
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv)
    app.setApplicationName("GenFill Setup")
    app.setWindowIcon(app_icon())
    apply_app_font(app)
    window = SetupWindow()
    window.show()
    return app.exec()
