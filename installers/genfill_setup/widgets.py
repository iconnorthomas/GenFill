"""Painted widgets for GenFill Setup: shimmer, toggles, choice cards, stepper, progress.

Everything animated runs off one shared clock (``CLOCK``) so the shimmer on the
logo, the button and the progress bar stay in step, and previews can freeze it.
"""
from __future__ import annotations

import math
import time

from genfill.ui import style
from genfill.ui.panel import sparkle_pixmap
from genfill.ui.qt import QtCore, QtGui, QtWidgets, Qt

ACCENT = QtGui.QColor(style.ACCENT)            # #3b63fb
ACCENT_2 = QtGui.QColor("#7a5cff")             # node-tile purple side of the gradient
TEXT = QtGui.QColor("#f2f2f4")
MUTED = QtGui.QColor("#9b9ba3")
FAINT = QtGui.QColor("#5c5c66")
GOOD = QtGui.QColor("#5fd39a")
BAD = QtGui.QColor(style.ERROR)

SWEEP = 1.3          # seconds a shimmer takes to cross
PERIOD = 4.2         # seconds between shimmers


class _Clock(QtCore.QObject):
    """~60 fps tick shared by every animated widget; ``frozen`` pins the time for previews."""
    tick = QtCore.Signal()

    def __init__(self):
        super(_Clock, self).__init__()
        self.frozen = None
        self._start = time.monotonic()
        self._timer = None

    def now(self):
        return self.frozen if self.frozen is not None else time.monotonic() - self._start

    def ensure_running(self):
        if self._timer is None:
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(16)
            self._timer.timeout.connect(self.tick)
            self._timer.start()


CLOCK = _Clock()


def sweep_phase(offset=0.0):
    """Position of the periodic shimmer band: 0..1 while sweeping, None while idle."""
    t = (CLOCK.now() + offset) % PERIOD
    return t / SWEEP if t < SWEEP else None


def ease(x):
    return 1 - (1 - x) ** 3


def paint_shimmer(painter, rect, phase, strength=0.32, width=0.35):
    """A soft diagonal light band crossing ``rect`` (call with the shape already clipped)."""
    if phase is None:
        return
    w = rect.width() + rect.height()
    x = rect.left() - rect.height() + (w + rect.height()) * phase * (1 + width) - w * width / 2
    grad = QtGui.QLinearGradient(x - w * width / 2, rect.top(), x + w * width / 2, rect.bottom())
    clear = QtGui.QColor(255, 255, 255, 0)
    grad.setColorAt(0.0, clear)
    grad.setColorAt(0.5, QtGui.QColor(255, 255, 255, int(255 * strength)))
    grad.setColorAt(1.0, clear)
    painter.fillRect(rect, grad)


def accent_gradient(rect):
    grad = QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, ACCENT_2)
    grad.setColorAt(1.0, ACCENT)
    return grad


class Animated(object):
    """Mixin: repaint on the shared clock while visible."""

    def _start_anim(self):
        CLOCK.ensure_running()
        CLOCK.tick.connect(self._on_tick)

    def _on_tick(self):
        if self.isVisible():
            self.update()


# --------------------------------------------------------------------------
# logo
# --------------------------------------------------------------------------

def logo_pixmap(size, phase=None):
    """The app icon: white sparkle on a purple-to-blue rounded tile (+ optional shimmer)."""
    dpr = 2
    s = size * dpr
    pm = QtGui.QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    rect = QtCore.QRectF(0, 0, s, s)
    path = QtGui.QPainterPath()
    path.addRoundedRect(rect, s * 0.26, s * 0.26)
    p.fillPath(path, accent_gradient(rect))
    gloss = QtGui.QLinearGradient(0, 0, 0, s)
    gloss.setColorAt(0.0, QtGui.QColor(255, 255, 255, 46))
    gloss.setColorAt(0.5, QtGui.QColor(255, 255, 255, 0))
    p.fillPath(path, gloss)
    inset = int(s * 0.21)
    p.drawPixmap(QtCore.QRect(inset, inset, s - 2 * inset, s - 2 * inset),
                 sparkle_pixmap(size, "#ffffff"))
    if phase is not None:
        p.setClipPath(path)
        paint_shimmer(p, rect, phase, strength=0.45)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


class Logo(Animated, QtWidgets.QWidget):
    def __init__(self, size=44, parent=None):
        super(Logo, self).__init__(parent)
        self._size = size
        self.setFixedSize(size + 16, size + 16)
        self._start_anim()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        # soft glow under the tile
        c = QtCore.QPointF(self.width() / 2, self.height() / 2 + 3)
        glow = QtGui.QRadialGradient(c, self._size * 0.75)
        glow.setColorAt(0.0, QtGui.QColor(91, 92, 255, 90))
        glow.setColorAt(1.0, QtGui.QColor(91, 92, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, self._size * 0.75, self._size * 0.75)
        p.drawPixmap(8, 8, logo_pixmap(self._size, sweep_phase()))


# --------------------------------------------------------------------------
# buttons
# --------------------------------------------------------------------------

class PrimaryButton(Animated, QtWidgets.QPushButton):
    """Pill button with the accent gradient and a periodic shimmer."""

    def __init__(self, text="", parent=None):
        super(PrimaryButton, self).__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(36)
        self.setMinimumWidth(112)
        self._hover = 0.0
        self._start_anim()

    def sizeHint(self):
        fm = self.fontMetrics()
        return QtCore.QSize(fm.horizontalAdvance(self.text()) + 48, 36)

    def enterEvent(self, e):
        self._hover = 1.0
        super(PrimaryButton, self).enterEvent(e)

    def leaveEvent(self, e):
        self._hover = 0.0
        super(PrimaryButton, self).leaveEvent(e)

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QtGui.QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        if self.isEnabled():
            p.fillPath(path, accent_gradient(r))
            if self._hover or self.isDown():
                p.fillPath(path, QtGui.QColor(255, 255, 255, 34 if self._hover else 0))
            if self.isDown():
                p.fillPath(path, QtGui.QColor(0, 0, 0, 40))
            p.setClipPath(path)
            paint_shimmer(p, r, sweep_phase(0.35), strength=0.30)
            p.setClipping(False)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 40), 1))
            p.drawPath(path)
            p.setPen(QtGui.QColor("#ffffff"))
        else:
            p.fillPath(path, QtGui.QColor(255, 255, 255, 18))
            p.setPen(QtGui.QColor(255, 255, 255, 80))
        f = self.font()
        f.setWeight(QtGui.QFont.DemiBold)
        p.setFont(f)
        p.drawText(r, Qt.AlignCenter, self.text())


class GhostButton(QtWidgets.QPushButton):
    def __init__(self, text="", parent=None):
        super(GhostButton, self).__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(36)
        self.setObjectName("gsGhost")


class Combo(QtWidgets.QComboBox):
    """Styled combo box with a painted chevron (stylesheets drop the native arrow)."""

    def paintEvent(self, event):
        super(Combo, self).paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pen = QtGui.QPen(MUTED if self.isEnabled() else FAINT, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        cx, cy = self.width() - 16, self.height() / 2
        path = QtGui.QPainterPath(QtCore.QPointF(cx - 4, cy - 2))
        path.lineTo(cx, cy + 2)
        path.lineTo(cx + 4, cy - 2)
        p.drawPath(path)


class LinkButton(QtWidgets.QPushButton):
    def __init__(self, text="", parent=None):
        super(LinkButton, self).__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("gsLink")
        self.setFlat(True)


# --------------------------------------------------------------------------
# toggle switch
# --------------------------------------------------------------------------

class Toggle(QtWidgets.QAbstractButton):
    """iOS-style switch with an animated knob, plus a title and an optional description."""

    def __init__(self, title, description="", parent=None):
        super(Toggle, self).__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._title = title
        self._desc = description
        self._pos = 0.0
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)
        self.toggled.connect(self._animate)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def setTitle(self, title):
        self._title = title
        self.updateGeometry()
        self.update()

    def _set_pos(self, v):
        self._pos = float(v)
        self.update()

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _text_rect(self, width):
        return QtCore.QRect(52, 0, max(40, width - 52), 10000)

    def _heights(self, width):
        fm = QtGui.QFontMetrics(self._title_font())
        h = fm.height()
        if self._desc:
            fm2 = QtGui.QFontMetrics(self.font())
            h += 3 + fm2.boundingRect(self._text_rect(width), Qt.TextWordWrap, self._desc).height()
        return max(24, h)

    def _title_font(self):
        f = QtGui.QFont(self.font())
        f.setWeight(QtGui.QFont.DemiBold)
        return f

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._heights(width) + 4

    def sizeHint(self):
        return QtCore.QSize(360, self.heightForWidth(360))

    def minimumSizeHint(self):
        return QtCore.QSize(200, 28)

    def resizeEvent(self, e):
        self.setMinimumHeight(self.heightForWidth(self.width()))
        super(Toggle, self).resizeEvent(e)

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        enabled = self.isEnabled()
        track = QtCore.QRectF(2, 3, 38, 22)
        off = QtGui.QColor(255, 255, 255, 30)
        if self._pos > 0:
            p.setPen(Qt.NoPen)
            p.setBrush(off)
            p.drawRoundedRect(track, 11, 11)
            p.setOpacity(self._pos if enabled else self._pos * 0.4)
            p.setBrush(accent_gradient(track))
            p.drawRoundedRect(track, 11, 11)
            p.setOpacity(1.0)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(off)
            p.drawRoundedRect(track, 11, 11)
        knob_x = track.left() + 3 + (track.width() - 22) * self._pos
        p.setBrush(QtGui.QColor(0, 0, 0, 60))
        p.drawEllipse(QtCore.QRectF(knob_x, track.top() + 4, 16, 16))
        p.setBrush(QtGui.QColor("#ffffff") if enabled else QtGui.QColor("#8a8a8a"))
        p.drawEllipse(QtCore.QRectF(knob_x, track.top() + 3, 16, 16))

        p.setPen(TEXT if enabled else FAINT)
        p.setFont(self._title_font())
        fm = QtGui.QFontMetrics(self._title_font())
        p.drawText(QtCore.QRect(52, 0, self.width() - 52, fm.height() + 2),
                   Qt.AlignLeft | Qt.AlignVCenter, self._title)
        if self._desc:
            p.setFont(self.font())
            p.setPen(MUTED if enabled else FAINT)
            r = QtCore.QRect(52, fm.height() + 3, self.width() - 52, self.height())
            p.drawText(r, Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, self._desc)


# --------------------------------------------------------------------------
# choice card
# --------------------------------------------------------------------------

class ChoiceCard(QtWidgets.QAbstractButton):
    """A large selectable card with an icon, a title and a description (radio-style)."""

    def __init__(self, title, description, icon="sparkle", parent=None, badge=None,
                 badge_good=True):
        super(ChoiceCard, self).__init__(parent)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.PointingHandCursor)
        self._title, self._desc, self._icon = title, description, icon
        self._badge, self._badge_good = badge, badge_good
        self._hover = False
        self.setMinimumHeight(84 if icon else 74)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super(ChoiceCard, self).enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super(ChoiceCard, self).leaveEvent(e)

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on = self.isChecked()
        bg = QtGui.QColor(255, 255, 255, 14 if on else (10 if self._hover else 6))
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 14, 14)
        if on:
            glow = QtGui.QLinearGradient(r.topLeft(), r.bottomRight())
            glow.setColorAt(0.0, QtGui.QColor(122, 92, 255, 34))
            glow.setColorAt(1.0, QtGui.QColor(59, 99, 251, 10))
            p.setBrush(glow)
            p.drawRoundedRect(r, 14, 14)
            pen = QtGui.QPen(QtGui.QBrush(accent_gradient(r)), 1.5)
        else:
            pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 30 if self._hover else 16), 1)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 14, 14)

        # icon chip (none on compact cards)
        chip = QtCore.QRectF(r.left() + 18, r.center().y() - 20, 40, 40)
        p.setPen(Qt.NoPen)
        if not self._icon:
            chip = QtCore.QRectF(r.left() + 2, r.center().y(), 0, 0)
        elif self._icon == "sparkle":
            p.setBrush(accent_gradient(chip) if on else QtGui.QBrush(QtGui.QColor(255, 255, 255, 18)))
            p.drawRoundedRect(chip, 11, 11)
            p.drawPixmap(chip.adjusted(10, 10, -10, -10).toRect(),
                         sparkle_pixmap(20, "#ffffff" if on else "#b9b9c2"))
        else:
            p.setBrush(QtGui.QColor(255, 107, 94, 50) if on else QtGui.QColor(255, 255, 255, 18))
            p.drawRoundedRect(chip, 11, 11)
            pen = QtGui.QPen(QtGui.QColor("#ff8a7e") if on else QtGui.QColor("#b9b9c2"), 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            c = chip.center()
            p.drawLine(QtCore.QPointF(c.x() - 7, c.y()), QtCore.QPointF(c.x() + 7, c.y()))

        # radio dot
        dot = QtCore.QRectF(r.right() - 36, r.center().y() - 9, 18, 18)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 60) if not on else ACCENT, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(dot)
        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(accent_gradient(dot))
            p.drawEllipse(dot.adjusted(4, 4, -4, -4))

        text_left = chip.right() + 16
        text_w = dot.left() - 14 - text_left
        tf = QtGui.QFont(self.font())
        tf.setPointSizeF(self.font().pointSizeF() + 1.5)
        tf.setWeight(QtGui.QFont.DemiBold)
        fm = QtGui.QFontMetrics(tf)
        df = QtGui.QFontMetrics(self.font())
        desc_h = df.boundingRect(QtCore.QRect(0, 0, int(text_w), 1000), Qt.TextWordWrap,
                                 self._desc).height()
        top = r.center().y() - (fm.height() + 4 + desc_h) / 2
        p.setFont(tf)
        p.setPen(TEXT)
        p.drawText(QtCore.QRectF(text_left, top, text_w, fm.height()), Qt.AlignLeft, self._title)
        if self._badge:
            bf = QtGui.QFont(self.font())
            bf.setPointSizeF(max(7.0, self.font().pointSizeF() - 2))
            bf.setWeight(QtGui.QFont.DemiBold)
            bm = QtGui.QFontMetrics(bf)
            bw = bm.horizontalAdvance(self._badge) + 14
            bx = text_left + fm.horizontalAdvance(self._title) + 10
            badge = QtCore.QRectF(bx, top + (fm.height() - 18) / 2, bw, 18)
            col = GOOD if self._badge_good else QtGui.QColor("#f3c46b")
            fill = QtGui.QColor(col)
            fill.setAlpha(36)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(badge, 9, 9)
            p.setFont(bf)
            p.setPen(col)
            p.drawText(badge, Qt.AlignCenter, self._badge)
        p.setFont(self.font())
        p.setPen(MUTED)
        p.drawText(QtCore.QRectF(text_left, top + fm.height() + 4, text_w, desc_h + 4),
                   Qt.TextWordWrap | Qt.AlignLeft, self._desc)


# --------------------------------------------------------------------------
# sidebar stepper
# --------------------------------------------------------------------------

class Stepper(QtWidgets.QWidget):
    """Vertical list of the setup stages with painted progress dots and a connecting line."""
    ROW = 46

    def __init__(self, labels, parent=None):
        super(Stepper, self).__init__(parent)
        self.labels = list(labels)
        self.current = 0
        self.failed = False
        self.setFixedHeight(self.ROW * len(self.labels))

    def set_current(self, index, failed=False):
        self.current = index
        self.failed = failed
        self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        x = 14
        for i, label in enumerate(self.labels):
            cy = i * self.ROW + self.ROW / 2
            if i < len(self.labels) - 1:
                done = i < self.current
                p.setPen(QtGui.QPen(QtGui.QColor(91, 99, 255, 150) if done
                                    else QtGui.QColor(255, 255, 255, 26), 2))
                p.drawLine(QtCore.QPointF(x, cy + 11), QtCore.QPointF(x, cy + self.ROW - 11))
            dot = QtCore.QRectF(x - 9, cy - 9, 18, 18)
            if i < self.current or (i == self.current and i == len(self.labels) - 1
                                    and not self.failed):
                p.setPen(Qt.NoPen)
                p.setBrush(accent_gradient(dot))
                p.drawEllipse(dot)
                pen = QtGui.QPen(QtGui.QColor("#ffffff"), 1.8)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                path = QtGui.QPainterPath(QtCore.QPointF(x - 4, cy + 0.5))
                path.lineTo(x - 1, cy + 3.5)
                path.lineTo(x + 4.5, cy - 3)
                p.drawPath(path)
                color = MUTED
            elif i == self.current:
                ring = BAD if self.failed else ACCENT
                halo = QtGui.QColor(ring)
                halo.setAlpha(50)
                p.setPen(Qt.NoPen)
                p.setBrush(halo)
                p.drawEllipse(dot.adjusted(-4, -4, 4, 4))
                p.setPen(QtGui.QPen(ring, 2))
                p.setBrush(QtGui.QColor("#16161a"))
                p.drawEllipse(dot)
                p.setPen(Qt.NoPen)
                p.setBrush(ring)
                p.drawEllipse(dot.adjusted(5, 5, -5, -5))
                color = TEXT
            else:
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 50), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(dot)
                color = FAINT
            f = QtGui.QFont(self.font())
            f.setWeight(QtGui.QFont.DemiBold if i == self.current else QtGui.QFont.Normal)
            p.setFont(f)
            p.setPen(color)
            p.drawText(QtCore.QRectF(x + 22, cy - 12, self.width() - x - 22, 24),
                       Qt.AlignLeft | Qt.AlignVCenter, label)


# --------------------------------------------------------------------------
# task list (progress page)
# --------------------------------------------------------------------------

class TaskRow(Animated, QtWidgets.QWidget):
    """One install step: painted state icon (spinner while running) and its label."""

    def __init__(self, text, parent=None):
        super(TaskRow, self).__init__(parent)
        self.text = text
        self.state = "pending"
        self.setFixedHeight(30)
        self._start_anim()

    def set_state(self, state):
        self.state = state
        self.update()

    def _on_tick(self):
        if self.state == "running" and self.isVisible():
            self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        c = QtCore.QPointF(11, self.height() / 2)
        r = QtCore.QRectF(c.x() - 8, c.y() - 8, 16, 16)
        st = self.state
        if st == "running":
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 30), 2))
            p.drawEllipse(r)
            pen = QtGui.QPen(QtGui.QBrush(accent_gradient(r)), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            start = int(-(CLOCK.now() * 360 * 1.1) % 360 * 16)
            p.drawArc(r, start, 100 * 16)
            color = TEXT
        elif st == "done":
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(95, 211, 154, 40))
            p.drawEllipse(r)
            pen = QtGui.QPen(GOOD, 1.8)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            path = QtGui.QPainterPath(QtCore.QPointF(c.x() - 3.5, c.y() + 0.5))
            path.lineTo(c.x() - 1, c.y() + 3)
            path.lineTo(c.x() + 4, c.y() - 3)
            p.drawPath(path)
            color = QtGui.QColor("#c9c9d0")
        elif st == "failed":
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(255, 107, 94, 45))
            p.drawEllipse(r)
            pen = QtGui.QPen(BAD, 1.8)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(c.x() - 3, c.y() - 3), QtCore.QPointF(c.x() + 3, c.y() + 3))
            p.drawLine(QtCore.QPointF(c.x() + 3, c.y() - 3), QtCore.QPointF(c.x() - 3, c.y() + 3))
            color = BAD
        elif st == "skipped":
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 30), 1.5, Qt.DotLine))
            p.drawEllipse(r)
            color = FAINT
        else:
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 40), 1.5))
            p.drawEllipse(r.adjusted(2, 2, -2, -2))
            color = FAINT
        f = QtGui.QFont(self.font())
        f.setWeight(QtGui.QFont.DemiBold if st == "running" else QtGui.QFont.Normal)
        p.setFont(f)
        p.setPen(color)
        p.drawText(QtCore.QRectF(32, 0, self.width() - 32, self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(self.text, Qt.ElideMiddle, self.width() - 34))


class ShimmerBar(Animated, QtWidgets.QWidget):
    """Rounded progress bar; the fill carries a moving sheen, and with no value it sweeps."""

    def __init__(self, parent=None):
        super(ShimmerBar, self).__init__(parent)
        self.value = 0.0
        self.busy = False
        self.setFixedHeight(8)
        self._shown = 0.0
        self._start_anim()

    def set_value(self, value):
        self.busy = value is None
        if value is not None:
            self.value = max(0.0, min(1.0, value))

    def _on_tick(self):
        if self.isVisible():
            self._shown += (self.value - self._shown) * 0.18
            self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect())
        rad = r.height() / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(255, 255, 255, 20))
        p.drawRoundedRect(r, rad, rad)
        t = CLOCK.now()
        if self.busy:
            phase = (t % 1.6) / 1.6
            seg = r.width() * 0.3
            x = -seg + (r.width() + seg) * ease(phase)
            fill = QtCore.QRectF(max(r.left(), x), r.top(), min(seg, r.right() - x), r.height())
            if fill.width() > 0:
                p.setBrush(accent_gradient(r))
                p.drawRoundedRect(fill, rad, rad)
            return
        w = max(r.height(), r.width() * (self._shown if self.isVisible() else self.value))
        if self.value <= 0:
            return
        fill = QtCore.QRectF(r.left(), r.top(), w, r.height())
        path = QtGui.QPainterPath()
        path.addRoundedRect(fill, rad, rad)
        p.fillPath(path, accent_gradient(r))
        p.setClipPath(path)
        paint_shimmer(p, QtCore.QRectF(fill.left(), fill.top(), fill.width(), fill.height()),
                      (t % 1.8) / 1.8, strength=0.45, width=0.25)


# --------------------------------------------------------------------------
# done mark
# --------------------------------------------------------------------------

class ResultMark(QtWidgets.QWidget):
    """Big circle with a check (or cross) that draws itself in."""

    def __init__(self, parent=None):
        super(ResultMark, self).__init__(parent)
        self.setFixedSize(76, 76)
        self.ok = True
        self.progress = 1.0
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(700)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set)

    def _set(self, v):
        self.progress = float(v)
        self.update()

    def play(self, ok):
        self.ok = ok
        self._anim.stop()
        self._anim.start()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(8, 8, 60, 60)
        color = GOOD if self.ok else BAD
        k = min(1.0, self.progress / 0.6)
        halo = QtGui.QColor(color)
        halo.setAlpha(int(40 * k))
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(r.adjusted(-7, -7, 7, 7))
        ring = QtGui.QPen(color, 3)
        ring.setCapStyle(Qt.RoundCap)
        p.setPen(ring)
        p.drawArc(r, 90 * 16, int(-360 * 16 * k))
        m = max(0.0, (self.progress - 0.45) / 0.55)
        if m <= 0:
            return
        pen = QtGui.QPen(color, 3.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        c = r.center()
        if self.ok:
            pts = [QtCore.QPointF(c.x() - 12, c.y() + 1), QtCore.QPointF(c.x() - 3, c.y() + 10),
                   QtCore.QPointF(c.x() + 13, c.y() - 9)]
            path = QtGui.QPainterPath(pts[0])
            path.lineTo(pts[1])
            path.lineTo(pts[2])
            length = path.length()
            stroke = QtGui.QPainterPath(pts[0])
            n = 30
            for i in range(1, n + 1):
                stroke.lineTo(path.pointAtPercent(path.percentAtLength(length * m * i / n)))
            p.drawPath(stroke)
        else:
            d = 10 * m
            p.drawLine(QtCore.QPointF(c.x() - d, c.y() - d), QtCore.QPointF(c.x() + d, c.y() + d))
            p.drawLine(QtCore.QPointF(c.x() + d, c.y() - d), QtCore.QPointF(c.x() - d, c.y() + d))


# --------------------------------------------------------------------------
# backgrounds
# --------------------------------------------------------------------------

class GlowBackground(Animated, QtWidgets.QWidget):
    """Content-area background: near-black with a slow, faint accent glow drifting top right."""

    def __init__(self, parent=None):
        super(GlowBackground, self).__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._start_anim()
        self._last = 0.0

    def _on_tick(self):
        now = CLOCK.now()
        if self.isVisible() and now - self._last > 0.05:      # 20 fps is plenty for a glow
            self._last = now
            self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect())
        p.fillRect(r, QtGui.QColor("#1b1b1f"))
        t = CLOCK.now()
        cx = r.right() - r.width() * (0.16 + 0.04 * math.sin(t * 0.25))
        cy = r.top() + r.height() * (0.02 + 0.03 * math.cos(t * 0.21))
        rad = max(r.width(), r.height()) * 0.62
        g = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), rad)
        g.setColorAt(0.0, QtGui.QColor(98, 90, 255, 46))
        g.setColorAt(0.45, QtGui.QColor(59, 99, 251, 14))
        g.setColorAt(1.0, QtGui.QColor(59, 99, 251, 0))
        p.fillRect(r, g)


class Sidebar(QtWidgets.QFrame):
    def __init__(self, parent=None):
        super(Sidebar, self).__init__(parent)
        self.setObjectName("gsSidebar")
        self.setAttribute(Qt.WA_StyledBackground, True)

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        r = QtCore.QRectF(self.rect())
        g = QtGui.QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, QtGui.QColor("#141417"))
        g.setColorAt(1.0, QtGui.QColor("#111113"))
        p.fillRect(r, g)
        p.setPen(QtGui.QColor(255, 255, 255, 14))
        p.drawLine(QtCore.QPointF(r.right() - 0.5, r.top()), QtCore.QPointF(r.right() - 0.5, r.bottom()))


class Card(QtWidgets.QFrame):
    """Soft translucent panel."""

    def __init__(self, parent=None, padding=(18, 16, 18, 16), spacing=10):
        super(Card, self).__init__(parent)
        self.setObjectName("gsCard")
        self.lay = QtWidgets.QVBoxLayout(self)
        self.lay.setContentsMargins(*padding)
        self.lay.setSpacing(spacing)


class Chip(QtWidgets.QLabel):
    def __init__(self, text, accent=False, parent=None):
        super(Chip, self).__init__(text, parent)
        self.setObjectName("gsChipAccent" if accent else "gsChip")


# --------------------------------------------------------------------------
# page transition
# --------------------------------------------------------------------------

class FadeStack(QtWidgets.QStackedWidget):
    """QStackedWidget that fades and slides the new page in."""

    def __init__(self, parent=None):
        super(FadeStack, self).__init__(parent)
        self._anims = []

    def setCurrentIndex(self, index):
        if index == self.currentIndex() or CLOCK.frozen is not None:
            super(FadeStack, self).setCurrentIndex(index)
            return
        forward = index > self.currentIndex()
        super(FadeStack, self).setCurrentIndex(index)
        page = self.currentWidget()
        effect = QtWidgets.QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        fade = QtCore.QPropertyAnimation(effect, b"opacity", self)
        fade.setDuration(260)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        end = page.pos()
        slide = QtCore.QPropertyAnimation(page, b"pos", self)
        slide.setDuration(320)
        slide.setStartValue(end + QtCore.QPoint(18 if forward else -18, 0))
        slide.setEndValue(end)
        slide.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        group = QtCore.QParallelAnimationGroup(self)
        group.addAnimation(fade)
        group.addAnimation(slide)
        group.finished.connect(lambda: page.setGraphicsEffect(None))
        group.start(QtCore.QAbstractAnimation.DeleteWhenStopped)
