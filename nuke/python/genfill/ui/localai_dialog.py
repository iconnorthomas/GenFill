"""GenFill > Local AI > Install / Repair... dialog.

Modeless: you can keep working in Nuke while it downloads. Closing the window
doesn't stop the install; reopen it from the menu to check progress.
"""
from __future__ import annotations

import os
import shutil
import threading

from .qt import QtCore, QtGui, QtWidgets, Qt
from . import style
from .panel import sparkle_pixmap

PENDING, RUNNING, DONE, FAILED = u"○", u"◔", u"✓", u"✕"
_dialog = [None]


class _Emitter(QtCore.QObject):
    event = QtCore.Signal(str, object)


class LocalAIDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super(LocalAIDialog, self).__init__(parent)
        from .. import localai
        from ..localai import installer as inst
        self._inst_mod = inst
        self._localai = localai
        self.installer = None
        self.thread = None
        self.setWindowTitle("GenFill Local AI")
        self.setMinimumWidth(580)
        self.setStyleSheet(style.STYLESHEET + """
            QDialog { background: #2b2b2b; }
            QLabel { color: #dcdcdc; }
            QPlainTextEdit { background: #1e1e1e; color: #b8b8b8; border: 1px solid #3c3c3c;
                             font-family: Consolas, monospace; font-size: 11px; }
            QPushButton { background: #3a3a3a; color: #e8e8e8; border: 1px solid #4a4a4a;
                          border-radius: 6px; padding: 6px 14px; }
            QPushButton:hover { background: #454545; }
            QPushButton:disabled { color: #777; }
        """)
        self.emitter = _Emitter()
        self.emitter.event.connect(self._on_event)
        self._build()
        self._refresh_info()

    # -- ui -----------------------------------------------------------------
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        lay.setSpacing(10)
        head = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel()
        icon.setPixmap(sparkle_pixmap(18, "#e6e6e6"))
        head.addWidget(icon)
        title = QtWidgets.QLabel("Local AI")
        title.setObjectName("gfTitle")
        head.addWidget(title)
        head.addStretch(1)
        lay.addLayout(head)

        intro = QtWidgets.QLabel(
            "Free, offline Generative Fill on your own GPU, with no tokens or accounts. Installs "
            "a fill model and LaMa (object removal) into a private folder. The AI runs in a "
            "hidden helper that starts loading when a GenerativeFill node uses it and exits "
            "with Nuke.")
        intro.setWordWrap(True)
        intro.setObjectName("gfMuted")
        lay.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.gpu_label = QtWidgets.QLabel("detecting...")
        form.addRow("GPU", self.gpu_label)
        self.folder_label = QtWidgets.QLabel(self._localai.root())
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Install folder", self.folder_label)
        self.family_combo = QtWidgets.QComboBox()
        self.family_combo.setObjectName("gfModel")
        for fid, fam in self._inst_mod.FAMILIES.items():
            self.family_combo.addItem(fam["label"] + ("  (commercial use OK)" if fam["commercial"]
                                                      else "  (non-commercial)"), fid)
        self.family_combo.setCurrentIndex(max(0, self.family_combo.findData(
            self._localai.family() if self._localai.manifest() else self._inst_mod.FLUX1)))
        self.family_combo.currentIndexChanged.connect(self._family_changed)
        form.addRow("Model", self.family_combo)
        self.size_combo = QtWidgets.QComboBox()
        self.size_combo.setObjectName("gfModel")
        for qid, (label, _gb, _vram) in self._inst_mod.QUANTS.items():
            self.size_combo.addItem(label, qid)
        self.size_combo.currentIndexChanged.connect(self._refresh_space)
        self.size_label = QtWidgets.QLabel("Model size")
        form.addRow(self.size_label, self.size_combo)
        self.t5_combo = QtWidgets.QComboBox()
        self.t5_combo.setObjectName("gfModel")
        for tid, (label, _gb) in getattr(self._inst_mod, "T5_VARIANTS", {}).items():
            self.t5_combo.addItem(label, tid)
        current_t5 = (self._localai.manifest() or {}).get("t5")
        if current_t5 is None and self._localai.manifest():
            current_t5 = "bf16"           # installed before the choice existed
        idx = self.t5_combo.findData(current_t5 or getattr(self._inst_mod, "DEFAULT_T5", None))
        self.t5_combo.setCurrentIndex(max(0, idx))
        self.t5_combo.setToolTip(
            "The text encoder that reads your prompt. 8-bit is half the download and disk "
            "space and reads faster from a hard disk; in side-by-side tests its fills looked "
            "the same as 16-bit. Both use about 9.5 GB of RAM once loaded.")
        self.t5_combo.currentIndexChanged.connect(self._refresh_space)
        self.t5_label = QtWidgets.QLabel("Text encoder")
        form.addRow(self.t5_label, self.t5_combo)
        self.space_label = QtWidgets.QLabel("")
        form.addRow("Disk", self.space_label)
        keep = QtWidgets.QHBoxLayout()
        self.idle_combo = QtWidgets.QComboBox()
        self.idle_combo.setObjectName("gfModel")
        for minutes in self._localai.IDLE_CHOICES:
            self.idle_combo.addItem(_idle_label(minutes), minutes)
        current = self._localai.idle_minutes()
        if self.idle_combo.findData(current) < 0:
            self.idle_combo.addItem(_idle_label(current), current)
        self.idle_combo.setCurrentIndex(self.idle_combo.findData(current))
        self.idle_combo.setToolTip(
            "How long the AI stays loaded on the GPU with nothing to do. Loading it again takes "
            "30-60 s. Applies the next time the helper starts.")
        self.idle_combo.currentIndexChanged.connect(self._idle_changed)
        keep.addWidget(self.idle_combo)
        self.stop_btn = QtWidgets.QPushButton("Stop AI Helper")
        self.stop_btn.setToolTip("Unload the AI now to free GPU memory. It loads again when "
                                 "needed.")
        self.stop_btn.clicked.connect(self._stop_helper)
        keep.addWidget(self.stop_btn)
        keep.addStretch(1)
        form.addRow("Keep loaded", keep)
        lay.addLayout(form)

        self.license_label = QtWidgets.QLabel("")
        self.license_label.setWordWrap(True)
        self.license_label.setOpenExternalLinks(True)
        self.license_label.setObjectName("gfMuted")
        lay.addWidget(self.license_label)

        self.step_labels = []
        steps_box = QtWidgets.QFrame()
        steps_box.setStyleSheet("QFrame { background: #232323; border-radius: 8px; }")
        sl = QtWidgets.QVBoxLayout(steps_box)
        sl.setContentsMargins(12, 10, 12, 10)
        for _key, label in self._inst_mod.Installer.steps_for(self._family()):
            row = QtWidgets.QLabel("%s  %s" % (PENDING, label))
            row.setStyleSheet("color: #9a9a9a; background: transparent;")
            sl.addWidget(row)
            self.step_labels.append(row)
        lay.addWidget(steps_box)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setObjectName("gfProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("gfMuted")
        lay.addWidget(self.status)

        self.details_btn = QtWidgets.QToolButton()
        self.details_btn.setObjectName("gfIcon")
        self.details_btn.setText(u"▸ Details")
        self.details_btn.setCheckable(True)
        self.details_btn.toggled.connect(self._toggle_details)
        lay.addWidget(self.details_btn)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        self.log_view.setMinimumHeight(160)
        self.log_view.setVisible(False)
        lay.addWidget(self.log_view)

        btns = QtWidgets.QHBoxLayout()
        self.folder_btn = QtWidgets.QPushButton("Open Folder")
        self.folder_btn.clicked.connect(self._open_folder)
        btns.addWidget(self.folder_btn)
        btns.addStretch(1)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.setEnabled(False)
        btns.addWidget(self.cancel_btn)
        self.install_btn = QtWidgets.QPushButton("Install")
        self.install_btn.setObjectName("gfGenerate")
        self.install_btn.clicked.connect(self._start)
        btns.addWidget(self.install_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.hide)
        btns.addWidget(close)
        lay.addLayout(btns)
        self._family_changed()

    def _family(self):
        return self.family_combo.currentData() or self._inst_mod.FLUX1

    def _family_changed(self, *args):
        fam = self._inst_mod.FAMILIES[self._family()]
        flux1 = self._family() == self._inst_mod.FLUX1
        self.size_combo.setVisible(flux1)
        self.size_label.setVisible(flux1)
        self.t5_combo.setVisible(flux1 and self.t5_combo.count() > 0)
        self.t5_label.setVisible(flux1 and self.t5_combo.count() > 0)
        self.license_label.setText('%s License: <a href="%s" style="color:#8fa6ff">%s</a>.'
                                   % (fam["summary"], fam["license_url"], fam["license"]))
        if self.thread is None or not self.thread.is_alive():
            for i, (_key, label) in enumerate(self._inst_mod.Installer.steps_for(self._family())):
                self.step_labels[i].setText("%s  %s" % (PENDING, label))
            self.install_btn.setText(self._install_text())
        self._refresh_space()

    def _install_text(self):
        if not self._localai.is_installed():
            return "Install"
        if self._localai.family() != self._family():
            return "Switch to %s" % self._inst_mod.FAMILIES[self._family()]["label"]
        return "Repair / Reinstall"

    def _toggle_details(self, on):
        self.log_view.setVisible(on)
        self.details_btn.setText((u"▾" if on else u"▸") + " Details")
        self.adjustSize()

    def _refresh_info(self):
        if self.thread is not None and self.thread.is_alive():
            return
        installed = self._localai.is_installed()
        self.install_btn.setText(self._install_text())
        m = self._localai.manifest() or {}
        if installed:
            verified = "verified" if m.get("verified") else "not verified yet"
            fam = self._inst_mod.FAMILIES.get(self._localai.family(), {})
            what = fam.get("label", "?") + (", %s" % m["quant"] if m.get("quant") else "")
            self.status.setText("Installed: %s (%s). Repair re-checks everything and fixes "
                                "missing files; pick the other model to switch." % (what, verified))

        def detect():
            name, vram = self._inst_mod.detect_gpu()
            self.emitter.event.emit("gpu", (name, vram))
        threading.Thread(target=detect, daemon=True).start()

    def _set_gpu(self, name, vram):
        if name:
            self.gpu_label.setText("%s, %.0f GB" % (name, vram))
        else:
            self.gpu_label.setText("No NVIDIA GPU detected. Local AI needs an NVIDIA card.")
        m = self._localai.manifest() or {}
        q = m.get("quant") or self._inst_mod.recommended_quant(vram)
        idx = self.size_combo.findData(q)
        if idx >= 0:
            self.size_combo.setCurrentIndex(idx)
        rec = self._inst_mod.recommended_quant(vram)
        i = self.size_combo.findData(rec)
        if i >= 0 and "(recommended)" not in self.size_combo.itemText(i):
            self.size_combo.setItemText(i, self.size_combo.itemText(i) + "  (recommended)")
        self._refresh_space()

    def _refresh_space(self, *args):
        q = self.size_combo.currentData()
        base = self._localai.root()
        probe = base
        while probe and not os.path.exists(probe):
            probe = os.path.dirname(probe)
        free = shutil.disk_usage(probe or os.path.expanduser("~")).free / 1e9
        self.space_label.setText("about %.0f GB needed, %.0f GB free"
                                 % (self._inst_mod.estimate_gb(q, self._family(), **self._t5_kw()),
                                    free))

    def _t5_kw(self):
        t5 = self.t5_combo.currentData() if self.t5_combo.count() else None
        return {"t5": t5} if t5 else {}

    # -- actions ---------------------------------------------------------------
    def _start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        steps = self._inst_mod.Installer.steps_for(self._family())
        for i, row in enumerate(self.step_labels):
            row.setText("%s  %s" % (PENDING, steps[i][1]))
            row.setStyleSheet("color: #9a9a9a; background: transparent;")
        self.log_view.clear()
        self.bar.setValue(0)
        self.status.setObjectName("gfMuted")
        self.status.setStyleSheet("")
        from ..localai import client
        client.stop_helper()   # files may be replaced
        self.installer = self._inst_mod.Installer(
            base=self._localai.root(), quant=self.size_combo.currentData(),
            family=self._family(), on_event=lambda kind, **kw: self.emitter.event.emit(kind, kw),
            **self._t5_kw())
        self.thread = threading.Thread(target=self.installer.run, name="GenFill-install",
                                       daemon=True)
        self.install_btn.setEnabled(False)
        self.size_combo.setEnabled(False)
        self.t5_combo.setEnabled(False)
        self.family_combo.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.thread.start()

    def _idle_changed(self, *args):
        minutes = self.idle_combo.currentData()
        if minutes is not None and minutes != self._localai.idle_minutes():
            self._localai.set_idle_minutes(minutes)

    def _stop_helper(self):
        from ..localai import client
        client.stop_helper()
        self.status.setText("AI helper stopped; GPU memory is free.")

    def _cancel(self):
        if self.installer:
            self.installer.cancel()
            self.status.setText("Cancelling...")

    def _open_folder(self):
        path = self._localai.root()
        if not os.path.isdir(path):
            path = os.path.dirname(path)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def closeEvent(self, event):
        event.ignore()
        self.hide()   # keep installing in the background

    # -- events from the installer thread ---------------------------------------
    def _on_event(self, kind, data):
        if kind == "gpu":
            self._set_gpu(*data)
        elif kind == "step":
            i, state, label = data["index"], data["state"], data["label"]
            glyph = {"running": RUNNING, "done": DONE, "failed": FAILED}.get(state, PENDING)
            color = {"running": "#ffffff", "done": "#7fd17f", "failed": style.ERROR}.get(state, "#9a9a9a")
            self.step_labels[i].setText("%s  %s" % (glyph, label))
            self.step_labels[i].setStyleSheet("color: %s; background: transparent;" % color)
            if state == "running":
                steps = len(self.step_labels)
                self._step_base = i / float(steps)
                self._step_span = 1.0 / steps
                self.bar.setValue(int(1000 * self._step_base))
        elif kind == "progress":
            if data.get("text"):
                self.status.setText(data["text"])
            frac = data.get("fraction")
            if frac is not None and hasattr(self, "_step_base"):
                self.bar.setValue(int(1000 * (self._step_base + self._step_span * frac)))
        elif kind == "log":
            self.log_view.appendPlainText(data.get("line", ""))
        elif kind == "finished":
            ok = data.get("ok")
            self.status.setText(data.get("message", ""))
            self.status.setStyleSheet("color: %s;" % ("#7fd17f" if ok else style.ERROR))
            if ok:
                self.bar.setValue(1000)
            elif not self.log_view.isVisible():
                self.details_btn.setChecked(True)
            self.install_btn.setEnabled(True)
            self.size_combo.setEnabled(True)
            self.t5_combo.setEnabled(True)
            self.family_combo.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.install_btn.setText(self._install_text())
            if not self.isVisible():
                self.show()
                self.raise_()


def _idle_label(minutes):
    if not minutes:
        return "Until Nuke closes"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "%d hour%s idle" % (hours, "" if hours == 1 else "s")
    return "%d minutes idle" % minutes


def show():
    """Open (or bring back) the Local AI dialog."""
    if _dialog[0] is None:
        _dialog[0] = LocalAIDialog(QtWidgets.QApplication.activeWindow())
    else:
        _dialog[0]._refresh_info()
    _dialog[0].show()
    _dialog[0].raise_()
    _dialog[0].activateWindow()
    return _dialog[0]
