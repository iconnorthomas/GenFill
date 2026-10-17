"""GenFill Setup: a graphical installer for Windows, macOS and Linux.

Installs (or updates / removes) the GenFill plugin bundled inside this app, and
on Windows can also set up Local AI by running GenFill's own Local AI installer.
The look comes from the plugin itself (genfill.ui.style and the sparkle icon).
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
from genfill.ui.panel import sparkle_pixmap  # noqa: E402
from genfill.ui.qt import QtCore, QtGui, QtWidgets, Qt  # noqa: E402

PENDING, RUNNING, DONE, FAILED, SKIPPED = u"○", u"◔", u"✓", u"✕", u"–"
STEP_COLORS = {"pending": "#7a7a7a", "running": "#ffffff", "done": "#7fd17f",
               "failed": style.ERROR, "skipped": "#5f5f5f"}
STEP_GLYPHS = {"pending": PENDING, "running": RUNNING, "done": DONE, "failed": FAILED,
               "skipped": SKIPPED}
TILE_TOP, TILE_BOTTOM = "#6a5cff", "#3b63fb"          # node tile purple into the accent blue
LOG_PATH = os.path.join(tempfile.gettempdir(), "GenFill-Setup.log")
WINDOWS = sys.platform.startswith("win")
FLUX_LICENSE = "https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev/blob/main/LICENSE.md"

STYLESHEET = style.STYLESHEET + """
QWidget#gsRoot {{ background: {bg}; font-family: {font}; }}
QWidget#gsRoot QLabel {{ color: #dcdcdc; font-size: 12px; }}
QFrame#gsHeader {{ background: {bar}; border-bottom: 1px solid #383838; }}
QWidget#gsRoot QLabel#gsAppName {{ color: #f4f4f4; font-size: 17px; font-weight: 600; }}
QWidget#gsRoot QLabel#gsHeadline {{ color: #f4f4f4; font-size: 20px; font-weight: 600; }}
QWidget#gsRoot QLabel#gsCrumb {{ color: #6f6f6f; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }}
QWidget#gsRoot QLabel#gsCrumb[current="true"] {{ color: #f0f0f0; }}
QFrame#gsCard {{ background: #232323; border: 1px solid #383838; border-radius: 10px; }}
QFrame#gsCard QLabel {{ background: transparent; }}
QWidget#gsRoot QLabel#gfSection {{ color: {muted}; font-size: 11px; font-weight: 600; }}
QWidget#gsRoot QLabel#gfMuted {{ color: {muted}; font-size: 12px; }}
QWidget#gsRoot QLabel#gfError {{ color: {error}; font-size: 12px; }}
QFrame#gsFooter {{ background: {bg}; border-top: 1px solid #383838; }}
QPushButton {{ background: #3a3a3a; color: #e8e8e8; border: 1px solid #4a4a4a;
              border-radius: 15px; padding: 6px 18px; font-size: 12px; min-height: 18px; }}
QPushButton:hover {{ background: #454545; }}
QPushButton:disabled {{ color: #6a6a6a; border-color: #3c3c3c; }}
QPushButton#gfGenerate {{ padding: 6px 20px; }}
QPushButton#gsLink {{ background: transparent; border: none; color: #8fa6ff; padding: 4px 2px; }}
QPushButton#gsLink:hover {{ color: #b3c2ff; }}
QLineEdit {{ background: #1f1f1f; color: {text}; border: 1px solid #444; border-radius: 6px;
            padding: 5px 8px; selection-background-color: {accent}; }}
QLineEdit:focus {{ border-color: {accent}; }}
QCheckBox, QRadioButton {{ color: #e2e2e2; font-size: 12px; spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator {{ border: 1px solid #5a5a5a; border-radius: 4px; background: #1f1f1f; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QRadioButton::indicator {{ border: 1px solid #5a5a5a; border-radius: 8px; background: #1f1f1f; }}
QRadioButton::indicator:checked {{ background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
    fx:0.5, fy:0.5, stop:0 {accent}, stop:0.45 {accent}, stop:0.55 #1f1f1f, stop:1 #1f1f1f);
    border-color: {accent}; }}
QPlainTextEdit {{ background: #1b1b1b; color: #b8b8b8; border: 1px solid #383838;
                 border-radius: 6px; font-family: Consolas, Menlo, monospace; font-size: 11px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #4a4a4a; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
""".format(bg=style.BG, bar=style.BAR, font=style.FONT_STACK, text=style.FIELD_TEXT,
           accent=style.ACCENT, muted=style.MUTED, error=style.ERROR)


# --------------------------------------------------------------------------
# artwork
# --------------------------------------------------------------------------

def logo_pixmap(size):
    """GenFill's app icon: the white sparkle on a purple-to-blue rounded tile."""
    dpr = 2
    pm = QtGui.QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    s = size * dpr
    grad = QtGui.QLinearGradient(0, 0, s, s)
    grad.setColorAt(0.0, QtGui.QColor(TILE_TOP))
    grad.setColorAt(1.0, QtGui.QColor(TILE_BOTTOM))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QtCore.QRectF(0, 0, s, s), s * 0.22, s * 0.22)
    inset = int(s * 0.2)
    p.drawPixmap(QtCore.QRect(inset, inset, s - 2 * inset, s - 2 * inset),
                 sparkle_pixmap(size, "#ffffff"))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def app_icon():
    icon = QtGui.QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(size))
    return icon


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
# widgets
# --------------------------------------------------------------------------

def _label(text, name=None, wrap=True):
    lab = QtWidgets.QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    lab.setTextFormat(Qt.RichText)
    lab.setOpenExternalLinks(True)
    return lab


def _card():
    card = QtWidgets.QFrame()
    card.setObjectName("gsCard")
    lay = QtWidgets.QVBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    return card, lay


class FolderRow(QtWidgets.QWidget):
    def __init__(self, path, parent=None):
        super(FolderRow, self).__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QtWidgets.QLineEdit(path)
        lay.addWidget(self.edit, 1)
        btn = QtWidgets.QPushButton("Browse...")
        btn.clicked.connect(self._browse)
        lay.addWidget(btn)

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


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class SetupWindow(QtWidgets.QWidget):
    CRUMBS = ["WELCOME", "OPTIONS", "INSTALL", "DONE"]

    def __init__(self):
        super(SetupWindow, self).__init__()
        self.setObjectName("gsRoot")
        self.setStyleSheet(STYLESHEET)
        self.setWindowTitle("GenFill Setup")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(720, 560)
        self.resize(760, 600)
        self.source = core.bundled_source()
        self.version = (core.read_build_info(self.source) or {}).get("version") \
            or core.read_version(self.source)
        self.installed = core.installed_version()
        self.nukes = core.find_nuke_installs()
        self.worker = None
        self.installer = None
        self.mode = "install"
        self._log_fh = None
        self._build()
        self._go(0)

    # -- layout -------------------------------------------------------------
    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QtWidgets.QFrame()
        header.setObjectName("gsHeader")
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(22, 16, 22, 16)
        logo = QtWidgets.QLabel()
        logo.setPixmap(logo_pixmap(40))
        hl.addWidget(logo)
        names = QtWidgets.QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(_label("GenFill for Nuke", "gsAppName", wrap=False))
        names.addWidget(_label("Setup &middot; version %s" % self.version, "gfMuted", wrap=False))
        hl.addLayout(names)
        hl.addStretch(1)
        self.crumbs = []
        for i, text in enumerate(self.CRUMBS):
            if i:
                hl.addWidget(_label(u"›", "gsCrumb", wrap=False))
            lab = _label(text, "gsCrumb", wrap=False)
            self.crumbs.append(lab)
            hl.addWidget(lab)
        root.addWidget(header)

        self.pages = QtWidgets.QStackedWidget()
        for build in (self._page_welcome, self._page_options, self._page_progress, self._page_done):
            page = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(page)
            lay.setContentsMargins(28, 24, 28, 18)
            lay.setSpacing(12)
            build(lay)
            self.pages.addWidget(page)
        root.addWidget(self.pages, 1)

        footer = QtWidgets.QFrame()
        footer.setObjectName("gsFooter")
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(22, 12, 22, 12)
        about = QtWidgets.QPushButton("About && credits")
        about.setObjectName("gsLink")
        about.clicked.connect(self._about)
        fl.addWidget(about)
        fl.addStretch(1)
        self.back_btn = QtWidgets.QPushButton("Back")
        self.back_btn.clicked.connect(lambda: self._go(self.pages.currentIndex() - 1))
        fl.addWidget(self.back_btn)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        fl.addWidget(self.cancel_btn)
        self.next_btn = QtWidgets.QPushButton("Next")
        self.next_btn.setObjectName("gfGenerate")
        self.next_btn.clicked.connect(self._next)
        fl.addWidget(self.next_btn)
        root.addWidget(footer)

    def _page_welcome(self, lay):
        lay.addWidget(_label("Photoshop-style Generative Fill, inside Nuke.", "gsHeadline"))
        lay.addWidget(_label(
            "Draw a selection on a GenerativeFill node, type what you want (or nothing, to "
            "remove), and pick from the variations. Use a cloud model with your own API key, or "
            "run it free and offline on your NVIDIA GPU with Local AI.", "gfMuted"))

        card, cl = _card()
        cl.addWidget(_label("NUKE ON THIS COMPUTER", "gfSection"))
        if self.nukes:
            names = ", ".join(n.name for n in self.nukes)
            cl.addWidget(_label("Found %s. GenFill loads in every version that uses your "
                                "<b>.nuke</b> folder." % names))
        else:
            cl.addWidget(_label("No Nuke found in the usual places. That's fine if it lives "
                                "somewhere else: GenFill still installs into your <b>.nuke</b> "
                                "folder, which every Nuke version reads."))
        cl.addSpacing(4)
        cl.addWidget(_label("GENFILL", "gfSection"))
        if self.installed:
            cl.addWidget(_label("GenFill %s is installed in %s." % (self.installed,
                                                                  core.default_target())))
        else:
            cl.addWidget(_label("Not installed yet."))
        lay.addWidget(card)

        self.rb_install = QtWidgets.QRadioButton(
            "Update GenFill to %s" % self.version if self.installed else
            "Install GenFill %s" % self.version)
        self.rb_uninstall = QtWidgets.QRadioButton("Remove GenFill from Nuke")
        self.rb_install.setChecked(True)
        if self.installed:
            lay.addWidget(self.rb_install)
            lay.addWidget(self.rb_uninstall)
        if core.nuke_is_running():
            lay.addWidget(_label("Nuke is open. Close it before continuing, or restart it "
                                 "afterwards to load the new GenFill.", "gfError"))
        lay.addStretch(1)

    def _page_options(self, lay):
        self.options_title = _label("", "gsHeadline")
        lay.addWidget(self.options_title)

        # install options
        self.install_opts = QtWidgets.QWidget()
        il = QtWidgets.QVBoxLayout(self.install_opts)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(10)
        card, cl = _card()
        cl.addWidget(_label("PLUGIN FOLDER", "gfSection"))
        self.target_row = FolderRow(core.default_target())
        cl.addWidget(self.target_row)
        cl.addWidget(_label("Nuke loads GenFill from here through a marked block in your "
                            "<b>.nuke/init.py</b>, which is backed up first.", "gfMuted"))
        il.addWidget(card)

        card, cl = _card()
        cl.addWidget(_label("LOCAL AI (OPTIONAL)", "gfSection"))
        self.ai_check = QtWidgets.QCheckBox(
            "Also set up Local AI: FLUX.1 Fill and LaMa on your own GPU (about 25 GB)")
        cl.addWidget(self.ai_check)
        self.ai_box = QtWidgets.QWidget()
        al = QtWidgets.QFormLayout(self.ai_box)
        al.setContentsMargins(24, 2, 0, 0)
        al.setLabelAlignment(Qt.AlignLeft)
        self.gpu_label = _label("", "gfMuted")
        al.addRow(_label("GPU", wrap=False), self.gpu_label)
        self.quant_combo = QtWidgets.QComboBox()
        self.quant_combo.setObjectName("gfModel")
        al.addRow(_label("Model size", wrap=False), self.quant_combo)
        self.ai_row = FolderRow("")
        al.addRow(_label("Folder", wrap=False), self.ai_row)
        self.license_check = QtWidgets.QCheckBox("I accept the FLUX.1 [dev] Non-Commercial License")
        al.addRow("", self.license_check)
        al.addRow("", _label('<a href="%s" style="color:#8fa6ff">Read the license</a>'
                             % FLUX_LICENSE, wrap=False))
        cl.addWidget(self.ai_box)
        if WINDOWS:
            self._init_local_ai()
        else:
            self.ai_check.setEnabled(False)
            self.ai_box.setVisible(False)
            cl.addWidget(_label("The automatic Local AI setup is Windows-only for now. Cloud "
                                "models work on every platform.", "gfMuted"))
        self.ai_check.toggled.connect(self.ai_box.setEnabled)
        self.ai_check.toggled.connect(self._refresh_buttons)
        self.license_check.toggled.connect(self._refresh_buttons)
        self.ai_box.setEnabled(False)
        il.addWidget(card)
        lay.addWidget(self.install_opts)

        # uninstall options
        self.uninstall_opts = QtWidgets.QWidget()
        ul = QtWidgets.QVBoxLayout(self.uninstall_opts)
        ul.setContentsMargins(0, 0, 0, 0)
        card, cl = _card()
        cl.addWidget(_label("GenFill will be removed from %s and from your <b>init.py</b>. "
                            "Images you generated stay in your shots' cache folders."
                            % core.default_target()))
        self.rm_ai_check = QtWidgets.QCheckBox("Also delete Local AI (models and private Python)")
        self.rm_cfg_check = QtWidgets.QCheckBox("Also delete settings and API keys (%s)"
                                                % core.config_dir())
        from genfill import localai
        self.rm_ai_check.setVisible(os.path.isdir(localai.root()))
        cl.addWidget(self.rm_ai_check)
        cl.addWidget(self.rm_cfg_check)
        ul.addWidget(card)
        lay.addWidget(self.uninstall_opts)
        lay.addStretch(1)

    def _init_local_ai(self):
        from genfill import localai
        from genfill.localai import installer as inst
        self.ai_row.edit.setText(localai.root())
        name, vram = inst.detect_gpu()
        self.gpu_label.setText("%s, %.0f GB" % (name, vram) if name else
                               "No NVIDIA GPU detected. Local AI needs one.")
        for qid, (label, _gb, _vram) in inst.QUANTS.items():
            self.quant_combo.addItem(label, qid)
        self.quant_combo.setCurrentIndex(self.quant_combo.findData(inst.recommended_quant(vram)))

    def _page_progress(self, lay):
        self.progress_title = _label("", "gsHeadline")
        lay.addWidget(self.progress_title)
        card, cl = _card()
        self.steps_layout = cl
        self.step_rows = []
        lay.addWidget(card)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setObjectName("gfProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.status = _label("", "gfMuted")
        lay.addWidget(self.status)
        self.details_btn = QtWidgets.QToolButton()
        self.details_btn.setObjectName("gfIcon")
        self.details_btn.setText(u"▸ Details")
        self.details_btn.setCheckable(True)
        self.details_btn.toggled.connect(self._toggle_details)
        lay.addWidget(self.details_btn, 0, Qt.AlignLeft)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setVisible(False)
        lay.addWidget(self.log_view, 1)
        lay.addStretch(1)

    def _page_done(self, lay):
        row = QtWidgets.QHBoxLayout()
        self.done_glyph = _label("", wrap=False)
        row.addWidget(self.done_glyph, 0, Qt.AlignTop)
        self.done_title = _label("", "gsHeadline")
        row.addWidget(self.done_title, 1)
        lay.addLayout(row)
        self.done_text = _label("", "gfMuted")
        lay.addWidget(self.done_text)
        self.launch_card, cl = _card()
        cl.addWidget(_label("START NUKE", "gfSection"))
        launch_row = QtWidgets.QHBoxLayout()
        for n in self.nukes[:4]:
            btn = QtWidgets.QPushButton(n.name)
            btn.clicked.connect(lambda _=False, n=n: self._launch(n))
            launch_row.addWidget(btn)
        launch_row.addStretch(1)
        cl.addLayout(launch_row)
        lay.addWidget(self.launch_card)
        log_btn = QtWidgets.QPushButton("Open the setup log")
        log_btn.setObjectName("gsLink")
        log_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(LOG_PATH)))
        lay.addWidget(log_btn, 0, Qt.AlignLeft)
        lay.addStretch(1)

    # -- navigation ---------------------------------------------------------
    def _go(self, index):
        index = max(0, min(index, self.pages.count() - 1))
        self.pages.setCurrentIndex(index)
        for i, lab in enumerate(self.crumbs):
            lab.setProperty("current", "true" if i == index else "false")
            lab.style().unpolish(lab)
            lab.style().polish(lab)
        if index == 1:
            uninstall = self.mode == "uninstall"
            self.options_title.setText("Remove GenFill" if uninstall else
                                       ("Update options" if self.installed else "Install options"))
            self.install_opts.setVisible(not uninstall)
            self.uninstall_opts.setVisible(uninstall)
        self._refresh_buttons()

    def _refresh_buttons(self):
        index = self.pages.currentIndex()
        busy = self.worker is not None and self.worker.isRunning()
        self.back_btn.setVisible(index in (1,))
        self.cancel_btn.setVisible(index in (0, 1) or busy)
        self.cancel_btn.setEnabled(not busy or self.installer is not None)
        self.next_btn.setVisible(index != 2)
        if index == 0:
            self.next_btn.setText("Next")
            self.next_btn.setEnabled(True)
        elif index == 1:
            if self.mode == "uninstall":
                self.next_btn.setText("Remove")
                self.next_btn.setEnabled(True)
            else:
                self.next_btn.setText("Update" if self.installed else "Install")
                need_license = self.ai_check.isChecked() and not self.license_check.isChecked()
                self.next_btn.setEnabled(not need_license)
        elif index == 3:
            self.next_btn.setText("Close")
            self.next_btn.setEnabled(True)

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
            row = _label("", wrap=False)
            row.setProperty("text_", text)
            self.steps_layout.addWidget(row)
            self.step_rows.append(row)
            self._step(len(self.step_rows) - 1, "pending")

    def _step(self, i, state):
        if 0 <= i < len(self.step_rows):
            row = self.step_rows[i]
            row.setText(u"%s&nbsp;&nbsp;%s" % (STEP_GLYPHS[state], row.property("text_")))
            row.setStyleSheet("color: %s; font-size: 12px;" % STEP_COLORS[state])

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
        self.progress_title.setText(title)
        self._set_steps(labels)
        self.bar.setValue(0)
        self.status.setText("")
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
            labels += [label for _key, label in inst.Installer.STEPS]
            quant = self.quant_combo.currentData()

        def job(emit):
            emit("step", index=0, state="running")
            core.install_plugin(target, self.source, log=lambda l: emit("log", line=l))
            emit("step", index=0, state="done")
            emit("step", index=1, state="running")
            res = core.set_init_block(target)
            emit("log", line="updated %s (backup: %s)" % (res["init_py"], res["backup"]))
            for line in res["disabled"]:
                emit("log", line="disabled an older GenFill line: %s" % line)
            emit("step", index=1, state="done")
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
            self.installer = inst.Installer(base=ai_root, quant=quant, on_event=on_ai)
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
            if kw["state"] == "running":
                self.status.setText(self.step_rows[kw["index"]].property("text_")
                                    if kw["index"] < len(self.step_rows) else "")
        elif kind == "progress":
            frac = kw.get("fraction")
            if frac is None:
                self.bar.setRange(0, 0)
            else:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(frac * 1000))
            self.status.setText(kw.get("text", ""))
        elif kind == "log":
            self._log(kw["line"])
        elif kind == "finished":
            self._finished(kw["ok"], kw["message"])

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
        for i, row in enumerate(self.step_rows):
            if not ok and row.text().startswith(RUNNING):
                self._step(i, "failed")
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000 if ok else self.bar.value())
        self.installer = None
        color = "#7fd17f" if ok else style.ERROR
        self.done_glyph.setText('<span style="color:%s; font-size:26px">%s</span>'
                                % (color, DONE if ok else FAILED))
        if ok and self.mode == "install":
            self.done_title.setText("GenFill is ready")
            self.done_text.setText(
                message + " Start Nuke and find <b>GenFill</b> in the Nodes toolbar, or press "
                "Tab and type &ldquo;Generative Fill&rdquo;. To check the build: GenFill &rsaquo; "
                "Developer &rsaquo; Run Smoke Test.")
        elif ok:
            self.done_title.setText("GenFill was removed")
            self.done_text.setText(message)
        else:
            self.done_title.setText("Setup didn't finish")
            self.done_text.setText(message + " The details are in the setup log.")
        self.launch_card.setVisible(ok and self.mode == "install" and bool(self.nukes))
        self._go(3)

    def _toggle_details(self, on):
        self.details_btn.setText((u"▾" if on else u"▸") + " Details")
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
        ("FLUX.1 Fill [dev]", "Black Forest Labs", "FLUX.1 [dev] Non-Commercial License",
         "https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev"),
        ("FLUX.1 Fill GGUF", "YarvixPA", "FLUX.1 [dev] Non-Commercial License",
         "https://huggingface.co/YarvixPA/FLUX.1-Fill-dev-GGUF"),
        ("LaMa inpainting", "Samsung AI Center", "Apache 2.0", "https://github.com/advimman/lama"),
        ("T5 v1.1 XXL encoder", "Google; bf16 by city96", "Apache 2.0",
         "https://huggingface.co/city96/t5-v1_1-xxl-encoder-bf16"),
        ("CLIP ViT-L/14", "OpenAI", "MIT", "https://huggingface.co/openai/clip-vit-large-patch14"),
        ("PyTorch, diffusers, transformers", "PyTorch Foundation; Hugging Face",
         "BSD-3-Clause; Apache 2.0", "https://github.com/huggingface/diffusers"),
        ("uv", "Astral", "Apache 2.0 / MIT", "https://github.com/astral-sh/uv"),
        ("Qt for Python (PySide6)", "The Qt Company", "LGPL v3",
         "https://code.qt.io/cgit/pyside/pyside-setup.git/"),
    ]

    def __init__(self, version, parent=None):
        super(AboutDialog, self).__init__(parent)
        self.setObjectName("gsRoot")
        self.setStyleSheet(STYLESHEET)
        self.setWindowTitle("About GenFill Setup")
        self.setMinimumWidth(560)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        head = QtWidgets.QHBoxLayout()
        logo = QtWidgets.QLabel()
        logo.setPixmap(logo_pixmap(48))
        head.addWidget(logo)
        head.addWidget(_label("<b>GenFill for Nuke</b> %s<br>by "
                              '<a href="https://github.com/iconnorthomas" style="color:#8fa6ff">'
                              "Connor Thomas</a>" % version), 1)
        lay.addLayout(head)
        lay.addWidget(_label('Thanks to <a href="https://github.com/AlesUshakou" '
                             'style="color:#8fa6ff">Ale&#353; Ushakou</a>, whose Windows setup '
                             "app inspired this one.", "gfMuted"))
        card, cl = _card()
        cl.addWidget(_label("THIRD-PARTY MODELS AND LIBRARIES", "gfSection"))
        for name, who, lic, url in self.CREDITS:
            cl.addWidget(_label('<a href="%s" style="color:#dcdcdc">%s</a> &middot; %s '
                                '&middot; <span style="color:#9a9a9a">%s</span>'
                                % (url, name, who, lic)))
        lay.addWidget(card)
        lay.addWidget(_label('<a href="https://github.com/iconnorthomas/GenFill" '
                             'style="color:#8fa6ff">github.com/iconnorthomas/GenFill</a>',
                             wrap=False))
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        lay.addWidget(close, 0, Qt.AlignRight)


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
    window = SetupWindow()
    window.show()
    return app.exec()
